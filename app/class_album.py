"""Read-only class album selection, scoped to one account session.

Only the website's verified ALBUM_FEED response is accepted. Tokens retain
validated image hashes, never browser-supplied download URLs or user objects.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
import warnings

from PIL import Image, ImageOps
import requests

from config import YSK_IMAGE_HOST, YSK_OSS_ENDPOINT


CHINA_TZ = timezone(timedelta(hours=8))
MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
TOKEN_TTL = 30 * 60
MAX_TOKENS = 12
MAX_TOKEN_ASSETS = 2000
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


class AlbumError(ValueError):
    pass


class AlbumTokenError(AlbumError):
    pass


def image_hash(value):
    """Website media names are UUIDs; paths, query strings and URLs are rejected."""
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise AlbumError("相册图片标识无效，请重新读取相册")
    return value


def date_bounds(from_date, to_date):
    """Inclusive teacher-selected calendar dates, interpreted in China time."""
    def parse(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise AlbumError("请选择有效的相册起止日期")
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=CHINA_TZ)
        except ValueError as exc:
            raise AlbumError("请选择有效的相册起止日期") from exc
    start, end = parse(from_date), parse(to_date)
    if end < start:
        raise AlbumError("结束日期不能早于开始日期")
    if (end - start).days > 366:
        raise AlbumError("一次最多读取一年内的班级相册，请缩小日期范围")
    return int(start.timestamp() * 1000), int((end + timedelta(days=1)).timestamp() * 1000) - 1


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number > 0 and str(value) == str(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _timestamp(value, date_only=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    try:
        dt = datetime.fromtimestamp(value / 1000, CHINA_TZ)
        return dt.strftime("%Y-%m-%d") if date_only else dt.isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ""


def parse_page(data, class_id):
    """Return a small field allowlist; never expose nested author/user metadata."""
    cid = _positive_id(class_id)
    if cid is None:
        raise AlbumError("班级标识无效")
    if (not isinstance(data, dict) or not isinstance(data.get("classDataVos"), list)
            or not isinstance(data.get("hasMore"), bool)):
        raise AlbumError("班级相册返回格式已变化，请重新读取或反馈给管理员")
    albums, seen_albums = [], set()
    for row in data["classDataVos"]:
        if (not isinstance(row, dict) or row.get("entityType") != "ALBUM_FEED"
                or row.get("deleted") or _positive_id(row.get("classId")) != cid):
            continue
        feed = row.get("albumFeedVo")
        if not isinstance(feed, dict) or _positive_id(feed.get("classId")) != cid:
            continue
        aid = _positive_id(feed.get("id"))
        if aid is None or aid in seen_albums:
            continue
        seen_albums.add(aid)
        photos, seen_photos, video_ids, video_hashes = [], set(), set(), set()
        media = feed.get("mediaFileVos") or []
        if not isinstance(media, list):
            media = []
        if len(media) > MAX_TOKEN_ASSETS:
            raise AlbumError("单条相册的素材太多，请缩小日期范围或反馈给管理员")
        # Python's stable sort preserves the original order when orders is absent.
        def order(entry):
            value = entry.get("orders") if isinstance(entry, dict) else None
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        for item in sorted(media, key=order):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("fileType") or "").upper()
            if kind == "VIDEO":
                video_ids.add(str(item.get("id") or item.get("hashName") or len(video_ids)))
                if isinstance(item.get("hashName"), str):
                    video_hashes.add(item["hashName"])
                continue
            if kind != "IMAGE":
                continue
            try:
                h = image_hash(item.get("hashName"))
            except AlbumError:
                continue
            if h in seen_photos:
                continue
            seen_photos.add(h)
            photos.append({"id": h, "width": _positive_id(item.get("imgWidth")),
                           "height": _positive_id(item.get("imgHeight"))})
        # images is the website's secondary image list. It can contain photos
        # omitted from mediaFileVos but must not duplicate that list.
        images = feed.get("images") or []
        if isinstance(images, list) and len(images) > MAX_TOKEN_ASSETS:
            raise AlbumError("单条相册的照片太多，请缩小日期范围或反馈给管理员")
        for value in images if isinstance(images, list) else []:
            try:
                h = image_hash(value)
            except AlbumError:
                continue
            if h not in seen_photos and h not in video_hashes:
                seen_photos.add(h)
                photos.append({"id": h, "width": None, "height": None})
        albums.append({"id": aid, "title": _text(feed.get("tag"), 200) or "班级相册",
                       "description": _text(feed.get("descs"), 4000),
                       "author": _text(feed.get("authorName"), 128),
                       "record_date": _timestamp(feed.get("recordDate") or row.get("recordDate"), True),
                       "published_at": _timestamp(feed.get("createDate")),
                       "photos": photos, "video_count": len(video_ids)})
    count = data.get("albumFeedCount")
    total = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else len(albums)
    return {"has_more": data["hasMore"], "total": total, "albums": albums}


class AlbumStore:
    """Bounded in-memory selection data; old account sessions cannot resolve it."""
    def __init__(self, ttl=TOKEN_TTL, max_tokens=MAX_TOKENS,
                 max_assets=MAX_TOKEN_ASSETS, clock=time.monotonic):
        self.ttl, self.max_tokens, self.max_assets = ttl, max_tokens, max_assets
        self.clock = clock
        self._lock = threading.RLock()
        self._tokens = OrderedDict()

    def clear(self):
        with self._lock:
            self._tokens.clear()

    def _expire(self):
        now = self.clock()
        for token in [t for t, state in self._tokens.items() if now >= state["expires"]]:
            del self._tokens[token]

    def _state(self, token, uid, cid, generation):
        self._expire()
        state = self._tokens.get(token) if isinstance(token, str) else None
        if state is None or state["owner"] != (str(uid), int(cid), int(generation)):
            raise AlbumTokenError("相册页面已过期或登录状态已变化，请重新打开相册")
        return state

    def store_page(self, parsed, uid, cid, generation, page, from_date, to_date, token=None):
        date_bounds(from_date, to_date)
        if not uid or _positive_id(cid) is None or isinstance(page, bool) or not isinstance(page, int) or page < 0:
            raise AlbumError("班级相册请求无效")
        assets = {}
        for album in parsed["albums"]:
            for photo in album["photos"]:
                h = image_hash(photo["id"])
                assets[h] = {"hash": h, "width": photo.get("width"), "height": photo.get("height")}
        with self._lock:
            self._expire()
            if token:
                state = self._state(token, uid, cid, generation)
                if state["range"] != (from_date, to_date):
                    raise AlbumTokenError("相册日期范围已变化，请重新打开相册")
            else:
                if page != 0:
                    raise AlbumTokenError("请从相册第一页开始读取")
                token = secrets.token_urlsafe(24)
                state = {"owner": (str(uid), int(cid), int(generation)),
                         "range": (from_date, to_date), "expires": self.clock() + self.ttl,
                         "assets": {}}
            if len(state["assets"].keys() | assets.keys()) > self.max_assets:
                raise AlbumError("这段日期的照片太多，请缩小日期范围后重新读取")
            state["assets"].update(assets)
            self._tokens[token] = state
            while len(self._tokens) > self.max_tokens:
                self._tokens.popitem(last=False)
            return {"token": token, "page": page, **deepcopy(parsed)}

    def resolve(self, token, asset_id, uid, cid, generation):
        h = image_hash(asset_id)
        with self._lock:
            state = self._state(token, uid, cid, generation)
            asset = state["assets"].get(h)
            if asset is None:
                raise AlbumTokenError("这张照片不在当前相册选择中，请重新读取相册")
            return dict(asset)


STORE = AlbumStore()


def _jpeg(data, preview):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > MAX_IMAGE_PIXELS or im.width <= 0 or im.height <= 0:
                    raise AlbumError("相册图片尺寸过大，暂时无法读取")
                im.load()
                image = ImageOps.exif_transpose(im).convert("RGB")
                side = 500 if preview else 1600
                image.thumbnail((side, side))
                out = io.BytesIO()
                image.save(out, "JPEG", quality=80 if preview else 90)
                return out.getvalue()
    except AlbumError:
        raise
    except Exception as exc:
        raise AlbumError("相册图片损坏或格式不受支持，请换一张照片") from exc


def _read_photo(get, url, kwargs, preview):
    """One complete attempt: partial response bytes never survive a retry."""
    with get(url, **kwargs) as response:
        if 300 <= response.status_code < 400:
            raise AlbumError("相册图片地址发生跳转，请重新读取相册")
        response.raise_for_status()
        media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if not media_type.startswith("image/"):
            raise AlbumError("相册图片返回了非图片内容，请重新登录或换一张照片")
        length = response.headers.get("Content-Length")
        if length:
            try:
                declared_size = int(length)
            except (ValueError, TypeError) as exc:
                raise AlbumError("相册图片返回格式异常") from exc
            if declared_size < 0:
                raise AlbumError("相册图片返回格式异常")
            if declared_size > MAX_DOWNLOAD_BYTES:
                raise AlbumError("相册图片文件过大，暂时无法读取")
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise AlbumError("相册图片文件过大，暂时无法读取")
            chunks.append(chunk)
    return _jpeg(b"".join(chunks), preview)


def download_photo(client, asset, preview=False):
    """Try the fixed CDN, then its configured official origin on network failure."""
    h = image_hash(asset.get("hash") if isinstance(asset, dict) else asset)
    for base, expected in ((YSK_IMAGE_HOST, "image.youshikoudai.com"),
                           (YSK_OSS_ENDPOINT, "1bplus-product-01.oss-cn-hangzhou.aliyuncs.com")):
        try:
            host = urlsplit(base)
            valid = (host.scheme == "https" and host.hostname == expected
                     and not host.username and not host.password and host.port in (None, 443)
                     and host.path in ("", "/") and not host.query and not host.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise AlbumError("相册图片服务地址配置无效")
    routes = ["cdn_direct"] if getattr(client, "_direct", False) else ["cdn"]
    if routes == ["cdn"] and client._proxy_configured():
        routes.append("cdn_direct")
    routes.append("origin")
    saved = getattr(client, "_album_route", None)
    if saved in routes:
        routes = routes[routes.index(saved):]
    query = "image/quality,Q_75/resize,w_500,h_400" if preview else "image/resize,w_1600"
    for route in routes:
        base = YSK_OSS_ENDPOINT if route == "origin" else YSK_IMAGE_HOST
        url = base.rstrip("/") + "/" + h + "?x-oss-process=" + query
        kwargs = {"stream": True, "timeout": (4, 45), "allow_redirects": False}
        if route != "cdn":
            kwargs["proxies"] = client._DIRECT
        try:
            if route == "origin":
                # No website cookies, private headers, proxy or netrc credentials.
                with requests.Session() as public:
                    public.trust_env = False
                    kwargs["headers"] = {"Accept": "image/*"}
                    result = _read_photo(public.get, url, kwargs, preview)
            else:
                result = _read_photo(client.s.get, url, kwargs, preview)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError):
            if route == routes[-1]:
                raise
            continue
        client._album_route = route
        return result
