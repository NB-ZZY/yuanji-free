"""幼师口袋（youshikoudai.com）PC 端接口客户端。

依据 docs/youshikoudai_api.md 的抓包结果实现：
  登录状态检测 → 班级/花名册 → 指标库 → 打指标 → OSS 传图 → 发布记录 → 撤销
登录：由内嵌浏览器窗口扫码/输密码登录后，取出 Cookie 交给本客户端。
"""
import base64
from copy import deepcopy
import hashlib
import hmac
import io
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from email.utils import formatdate
from typing import Optional

import requests
from PIL import Image

from config import (MOCK, YSK_BASE, YSK_IMAGE_HOST, YSK_OSS_BUCKET, YSK_OSS_ENDPOINT)
from knowledge import grade_score

log = logging.getLogger("ysk")


class NotLoggedIn(Exception):
    pass


class TablePublishUncertain(RuntimeError):
    """发布请求已发出但结果不明；必须保留关联指标，并在网页核对后再处理。"""


class YskClient:
    def __init__(self, cookies: Optional[dict] = None):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": YSK_BASE + "/react/pc/home",
            "Origin": YSK_BASE,
        })
        if cookies:
            self.set_cookies(cookies)
        self.uid: Optional[int] = None
        self.user: Optional[dict] = None
        self._direct = False
        self._sts: Optional[dict] = None

    # ---------- 会话 ----------
    def set_cookies(self, cookies: dict):
        self.s.cookies.clear()
        self.uid = None
        self.user = None
        self._sts = None
        for k, v in cookies.items():
            self.s.cookies.set(k, v, domain="www.youshikoudai.com")

    def _q(self, **extra) -> dict:
        q = {"logPlatform": "NA", "appVersion": "0.0.0", "uid": self.uid or ""}
        q.update(extra)
        return q

    # ---------- 发请求（会绕开坏掉的系统代理） ----------
    # 坑记：Mac 上 requests 会自动读系统里的代理设置，内嵌的登录窗口却走苹果自己那一套。
    # 老师装过加速器、后来关了但配置还留着的时候，就会出现"网页打得开、登录也成功，
    # 可程序一问幼师口袋就失败"——表现正是登录窗口不关、程序一直说没登录。
    # 所以：第一次失败且系统里确实挂着代理，就绕过代理直连再试一次，成了就记住。
    # requests 在移除 http/https 代理后仍会回退 ALL_PROXY，也必须禁用。
    _DIRECT = {"http": None, "https": None, "all": None}

    @staticmethod
    def _proxy_configured() -> bool:
        try:
            import urllib.request
            return bool(urllib.request.getproxies())
        except Exception:
            return False

    def _send(self, method: str, path: str, **kw):
        if getattr(self, "_direct", False):
            kw.setdefault("proxies", self._DIRECT)
        try:
            return self.s.request(method, YSK_BASE + path, **kw)
        except requests.exceptions.RequestException as e:
            # 表格创建不是幂等请求：断线可能发生在服务端成功之后，不能自动重发。
            if method.upper() == "POST" and (path == "/api/growRecordTable/add" or
                    path.startswith("/api/v2/growStudentMonthReport/")):
                raise
            if getattr(self, "_direct", False) or not self._proxy_configured():
                raise
            log.warning("走系统代理连不上幼师口袋（%s），绕过代理直连再试一次", e)
            kw["proxies"] = self._DIRECT
            r = self.s.request(method, YSK_BASE + path, **kw)
            self._direct = True
            log.info("直连成功，这次会话之后都不走代理了")
            return r

    def _get(self, path: str, **params):
        return self._unwrap(self._send("GET", path, params=self._q(**params), timeout=30))

    def _post(self, path: str, body, **params):
        return self._unwrap(self._send("POST", path, params=self._q(**params), json=body, timeout=60))

    def _delete(self, path: str, **params):
        return self._unwrap(self._send("DELETE", path, params=self._q(**params), timeout=30))

    @staticmethod
    def _unwrap(r: requests.Response):
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已失效，请重新登录幼师口袋")
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "json" not in ct:
            txt = r.text.strip()
            if txt.startswith("<"):
                raise NotLoggedIn("登录已失效，请重新登录幼师口袋")
            try:
                return json.loads(txt)
            except Exception:
                return txt
        data = r.json()
        if isinstance(data, dict) and "code" in data and "data" in data:
            if data["code"] != "OK":
                raise RuntimeError(data.get("msg") or f"接口返回 {data['code']}")
            return data["data"]
        return data

    def check_login(self) -> dict:
        """返回用户信息；未登录抛 NotLoggedIn。"""
        if MOCK:
            self.uid, self.user = 900001, {"id": 900001, "name": "示例教师"}
            return self.user
        if not self.s.cookies:
            self.uid, self.user, self._sts = None, None, None
            raise NotLoggedIn("请先登录幼师口袋")
        try:
            u = self._get("/api/user/getMyUserInfo")
        except NotLoggedIn:
            self.uid, self.user, self._sts = None, None, None
            raise
        except Exception as e:
            raise NotLoggedIn(f"无法确认登录状态：{e}")
        if not isinstance(u, dict) or not u.get("id"):
            self.uid, self.user, self._sts = None, None, None
            raise NotLoggedIn("未登录")
        self.uid, self.user = u["id"], u
        return u

    # ---------- 班级 / 花名册 ----------
    def my_classes(self) -> list[dict]:
        """返回 [{classId, name, kid, type}]。"""
        if MOCK:
            return [{"classId": 900101, "name": "示例园大班", "kid": 900201, "type": "SENIOR"}]
        page = self._get("/api/miniGrowMainPage/getMainPage", isAdmin="false", classId="")
        kg = (page.get("kindergartenVo") or {}) if isinstance(page, dict) else {}
        out = []
        for c in (page.get("classVos") or []):
            out.append({"classId": c["id"], "name": c["name"], "kid": c.get("kid") or kg.get("id"),
                        "type": c.get("type"), "studentCount": c.get("studentCount"),
                        "kindergarten": kg.get("name")})
        return out

    def class_info(self, class_id: int) -> dict:
        """{id,name,type,students:[{id,childId,name,avatar,growAvatar,classId}]}"""
        if MOCK:
            names = ["示例幼儿甲", "示例幼儿乙", "示例幼儿丙"]
            return {"id": class_id, "name": "示例园大班", "type": "SENIOR",
                    "students": [{"id": 900301 + i, "childId": 900401 + i, "name": n, "avatar": None, "growAvatar": None, "classId": class_id} for i, n in enumerate(names)]}
        info = self._get("/api/miniprogClass/v3/loadClassBasicInfo", classId=class_id)
        for s in info.get("students", []):
            s.setdefault("classId", class_id)
        return info

    def avatar_url(self, hash_: str, size: int = 400) -> str:
        return f"{YSK_IMAGE_HOST}/{hash_}?x-oss-process=image/resize,w_{size}"

    def image_url(self, hash_: str, size: int = 900) -> str:
        return f"{YSK_IMAGE_HOST}/{hash_}?x-oss-process=image/resize,w_{size}"

    def class_records(self, class_id: int, page: int = 1) -> dict:
        """按班级分页拉取历史观察记录（随手记）。"""
        if MOCK:
            return {}
        return self._get("/api/v2/hybridGrowthRecord/loadGrowRecordByClass", classId=class_id, page=page)

    def class_albums(self, class_id: int, from_ms: int, to_ms: int, page: int = 0) -> dict:
        """Website-verified read-only PC album feed; page numbering starts at zero."""
        if MOCK:
            return {"hasMore": False, "albumFeedCount": 0, "classDataVos": []}
        return self._post("/api/hybridClassGrowthRecord/getPcClassMainPage", {
            "entityTypes": ["ALBUM_FEED"], "fromDate": from_ms, "toDate": to_ms,
            "classIds": [class_id], "realms": None, "page": page,
            "inKindergarten": True, "inFamily": False,
        })

    def album_photo(self, hash_name: str, preview: bool = False) -> bytes:
        """Fetch only a validated album image, with size and format checks."""
        from class_album import download_photo
        return download_photo(self, hash_name, preview=preview)

    def download(self, url: str) -> bytes:
        r = self.s.get(url, timeout=60)
        r.raise_for_status()
        return r.content

    # ---------- 指标库（用于校验/刷新站内 ID） ----------
    def medal_library(self, class_id: int, system_id: int = 9) -> dict[int, dict]:
        """按体系返回站内指标；核心发展 9、托育 35，不能仅按名称混配。"""
        if system_id not in (9, 35):
            raise ValueError("不支持的评价体系")
        if MOCK:
            return {}
        from_date = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lib = self._get("/api/v2/growMedal/getMedalLibraryWithFinishStudentIds", classId=class_id, fromDate=from_date)
        out = {}
        def walk(o):
            if isinstance(o, dict):
                m = o.get("growMedalVo")
                if isinstance(m, dict) and m.get("systemId") == system_id:
                    out[m["id"]] = {
                        "systemId": system_id,
                        "name": m["name"], "realm": m.get("realm"), "module": m.get("module"),
                        "grades": [{"id": g["id"], "gradeId": g["gradeId"], "gradeName": g["gradeName"], "desc": g["gradeDesc"]} for g in (m.get("gradeDescVos") or [])],
                    }
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(lib)
        return out

    # ---------- 发布链路 ----------
    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"

    @staticmethod
    def _iso_date(d: str = None) -> str:
        """'YYYY-MM-DD' → 站内要的 ISO 时间串；不给就用当下。
           补上当前时刻，这样同一天发的多条记录在时间轴上还是按顺序排。"""
        if not d:
            return YskClient._iso_now()
        try:
            y, m, dd = (int(x) for x in d.split("-"))
        except Exception:
            return YskClient._iso_now()
        now = datetime.now()
        local = datetime(y, m, dd, now.hour, now.minute, now.second)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def add_student_medal(self, class_id: int, kid: int, student_id: int, medal_id: int, level: int,
                          grade_id: int, behavior_desc, record_date: str = None) -> int:
        """记录表现行为，返回 studentMedalId。

        behavior_desc：一句话，也可以是一串 —— 同一条指标下老师勾了好几条表现行为时，
        它们一起放进 dataVos，站上就是这条指标下打了好几个勾。
        每一项可以是字符串（用默认档位），也可以是 {"grade_id":.., "desc":".."}。
        """
        if MOCK:
            return int(time.time() * 1000) % 100000000
        raw = behavior_desc if isinstance(behavior_desc, (list, tuple)) else [behavior_desc]
        vos, seen = [], set()
        for x in raw:
            if isinstance(x, dict):
                gid, desc = int(x.get("grade_id") or grade_id), str(x.get("desc") or "")
            else:
                gid, desc = grade_id, str(x or "")
            if not desc or (gid, desc) in seen:
                continue
            seen.add((gid, desc))
            vos.append({"gradeId": gid, "gradeDesc": desc})
        if not vos and level not in (2, 4):
            vos = [{"gradeId": grade_id, "gradeDesc": ""}]
        body = {
            "classId": class_id, "kid": kid, "medalId": medal_id, "recordDate": self._iso_date(record_date),
            "studentMedalPerformanceStatusVos": [{
                "studentId": student_id, "grade": grade_score(level), "dataVos": vos,
            }],
        }
        data = self._post("/api/v3/growMedal/addStudentMedal", body)
        if isinstance(data, list) and data:
            return data[0]["id"]
        raise RuntimeError(f"打指标失败：{data}")

    def _sts_token(self) -> dict:
        if self._sts:
            exp = datetime.strptime(self._sts["expiration"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if exp.timestamp() - time.time() > 60:
                return self._sts
        self._sts = self._get("/api/sts/getToken")
        return self._sts

    def _oss_put(self, key: str, data: bytes, content_type: str):
        """阿里云 OSS 签名 V1 直传（避免依赖 oss2）。"""
        tok = self._sts_token()
        date = formatdate(usegmt=True)
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        oss_headers = {"x-oss-security-token": tok["securityToken"]}
        canon_headers = "".join(f"{k}:{v}\n" for k, v in sorted(oss_headers.items()))
        resource = f"/{YSK_OSS_BUCKET}/{key}"
        string_to_sign = f"PUT\n{md5}\n{content_type}\n{date}\n{canon_headers}{resource}"
        sig = base64.b64encode(hmac.new(tok["accessKeySecret"].encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()
        headers = {
            "Content-Type": content_type, "Content-MD5": md5, "Date": date,
            "Authorization": f"OSS {tok['accessKeyId']}:{sig}", **oss_headers,
        }
        r = requests.put(f"{YSK_OSS_ENDPOINT}/{key}", data=data, headers=headers, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"OSS 上传失败 {r.status_code}: {r.text[:300]}")

    def upload_image(self, path: str, max_side: int = 1600) -> dict:
        """压缩→OSS→登记；返回 fileHashWithEntityVo。"""
        im = Image.open(path)
        im = im.convert("RGB")
        try:
            from PIL import ImageOps
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        data = buf.getvalue()
        key = f"{uuid.uuid4()}.jpg"
        vo = {
            "fileName": key, "hashName": key, "usages": "TEMP", "fileType": "IMAGE",
            "imgWidth": im.size[0], "imgHeight": im.size[1],
            "fileSize": round(len(data) / 1024 / 1024, 4),
        }
        if MOCK:
            vo["id"] = int(time.time() * 1000) % 1000000000
            return vo
        self._oss_put(key, data, "image/jpeg")
        fid = self._post("/api/file/addFileVoOnly", vo)
        vo["id"] = int(fid)
        return vo

    def add_record(self, class_id: int, kid: int, student, content: str, files: list,
                   student_medal_ids=None, modules: list = None, sync_with_parent: bool = True,
                   record_date: str = None, medal_map: dict = None) -> int:
        """发布随手记（观察记录），返回记录 id。

        student：一个孩子（dict），也可以是一串孩子（list）—— 一条记录可以同时评好几个孩子。
        student_medal_ids：可以是一个 id，也可以是多个（一条记录可以挂多条指标）。
        medal_map：{student_id: [打好的指标id…]}，多个孩子时每人挂自己的那几条。
        modules：关联领域，形如 [{"realm":"社会领域","module":"社会适应"}]。
        sync_with_parent：是否同步给家长。
        record_date：'YYYY-MM-DD'，不给就用当下。
        """
        if MOCK:
            return int(time.time() * 1000) % 100000000
        if student_medal_ids is None:
            ids = []
        elif isinstance(student_medal_ids, (list, tuple)):
            ids = [i for i in student_medal_ids if i]
        else:
            ids = [student_medal_ids]
        studs = student if isinstance(student, (list, tuple)) else [student]
        studs = [x for x in studs if x]
        if medal_map is None:
            medal_map = {studs[0]["student_id"]: ids} if studs else {}
        mvos, with_medal = [], []
        for st in studs:
            mine = [i for i in (medal_map.get(st["student_id"]) or []) if i]
            for i in mine:
                mvos.append({"studentId": st["student_id"], "studentMedalId": i})
            if mine:
                with_medal.append({"id": st["student_id"], "classId": class_id,
                                   "childId": st.get("child_id"), "name": st["name"],
                                   "avatar": st.get("avatar")})
        body = {
            "kid": kid, "classId": class_id, "studentIds": [st["student_id"] for st in studs],
            "moduleVos": [{"systemId": m.get("systemId", 9), "realm": m["realm"], "module": m["module"]}
                          for m in (modules or []) if m.get("realm") and m.get("module")],
            "content": content,
            "fileHashWithEntityVos": files, "audioFileHashWithEntityVos": [],
            "recordDate": self._iso_date(record_date), "type": "SIMPLE",
            "syncWithParent": bool(sync_with_parent),
            "studentMedalInfoVos": mvos,
            "studentVosWithMedal": with_medal,
        }
        rid = self._post("/api/v3/growRecord/add", body)
        return int(rid)

    def get_table_field_options(self, field_type: str) -> list:
        """读取官网观察场景/关联活动的分组选项，不接受任意字段或本地编造的选项。"""
        if field_type not in ("SCENE_MULTI_SELECT", "ACTIVITY_MULTI_SELECT"):
            raise ValueError("不支持的观察记录表选项类型")
        if MOCK:
            return []
        groups = self._get("/api/growRecordTableSystemField/findDataByFieldType", fieldType=field_type)
        if not isinstance(groups, list) or not groups:
            raise ValueError("未获取到官网观察记录表选项，请稍后重试")
        result, seen = [], set()
        for group in groups:
            if (not isinstance(group, dict) or not isinstance(group.get("label"), str)
                    or not group["label"].strip() or not isinstance(group.get("options"), list)):
                raise ValueError("官网观察记录表选项格式异常")
            options = []
            for option in group["options"]:
                if (not isinstance(option, dict) or option.get("groupLabel") != group["label"]
                        or not isinstance(option.get("value"), str) or not option["value"].strip()
                        or option.get("customized", False) is not False):
                    raise ValueError("官网观察记录表选项格式异常")
                identity = (option["groupLabel"], option["value"])
                if identity in seen:
                    raise ValueError("官网观察记录表选项重复")
                seen.add(identity)
                options.append({"groupLabel": option["groupLabel"], "value": option["value"],
                                "customized": False, "systemId": option.get("systemId")})
            result.append({"label": group["label"], "options": options})
        if not seen:
            raise ValueError("未获取到官网观察记录表选项，请稍后重试")
        return result

    def prepare_table_record(self, class_id: int, kid: int, students: list, table: dict,
                             modules: list = None, sync_with_parent: bool = True,
                             record_date: str = None) -> dict:
        """在上传照片和新增指标前，校验已启用的段落并读取动态字段；这里只发 GET。"""
        import record_table
        table = record_table.normalize(table)
        story = table.get("观察内容")
        if not isinstance(story, str) or not story.strip():
            raise ValueError("观察内容为空，请填写后发布")
        texts = [("观察内容", story)]
        if "分析" in table:
            texts.append(("分析", table["分析"]))
        if "家庭指导" in table:
            texts.append(("家庭指导", table["家庭指导"]))
        option_fields = {"观察场景": ("SCENE_MULTI_SELECT", "scene"),
                         "关联活动": ("ACTIVITY_MULTI_SELECT", "activity")}
        if "行为跟进" in table:
            follow = table["行为跟进"]
            if not follow or any(not x.strip() for x in follow):
                raise ValueError("「行为跟进」为空，请填写或删除这一段后发布")
            suggestion = ''.join(follow)
            texts.append(("行为跟进", suggestion))
        extras = table.get("自定义段落") or []
        for extra in extras:
            texts.append((extra["title"], extra["content"]))
        for label, text in texts:
            if not text.strip():
                raise ValueError(f"「{label}」为空，请填写或删除这一段后发布")
            # 网站按 JavaScript string.length 限长；emoji 等字符会占两个 UTF-16 单元。
            if len(text.encode("utf-16-le")) // 2 > 3000:
                raise ValueError(f"「{label}」超过幼师口袋的 3000 字符限制，请缩短后发布；原文已保留")
        if not isinstance(students, list) or not students or any(
                not isinstance(s, dict) or not isinstance(s.get("student_id"), int)
                or s["student_id"] <= 0 for s in students):
            raise ValueError("观察记录表缺少有效的观察对象")
        if record_date:
            try:
                datetime.strptime(record_date, "%Y-%m-%d")
            except (TypeError, ValueError):
                raise ValueError("观察记录表的记录日期无效") from None
        body = {"id": None, "republishStudentMonthReportId": None,
                "kid": kid, "classId": class_id, "type": "TABLE", "content": "观察记录表",
                "fieldVos": [],
                "syncWithParent": bool(sync_with_parent)}
        if MOCK:
            return body
        teacher = (self.user or {}).get("realName") or (self.user or {}).get("userName")
        if not isinstance(teacher, str) or not teacher.strip():
            raise ValueError("没有获取到当前登录老师的姓名，请重新确认登录状态")
        official = self._get("/api/growRecordTableTemplate/findOfficial")
        matches = [t for t in official if isinstance(t, dict)
                   and t.get("name") == "自定义观察记录表" and t.get("type") == "OFFICIAL"] \
            if isinstance(official, list) else []
        if len(matches) != 1 or not matches[0].get("id"):
            raise ValueError("无法唯一确定幼师口袋的自定义观察记录表模板，请在网页核对模板")
        template = self._get(f"/api/growRecordTableTemplate/find/{int(matches[0]['id'])}")
        fields = template.get("fieldVos") if isinstance(template, dict) else None
        if not isinstance(fields, list) or not fields:
            raise ValueError("幼师口袋的观察记录表模板缺少字段，尚未发布")
        selected = self._table_modules(modules or [])
        values = {
            ("DATE", None): {"recordDate": self._iso_date(record_date)},
            ("INPUT", "teacherName"): {"content": teacher},
            ("STUDENT", "student"): {"studentIds": list(dict.fromkeys(s["student_id"] for s in students))},
            ("OBSERVATION_RECORD", "content"): {"observationRecordItemVos": [{
                "content": story,
                "fileHashWithEntityVos": []}]},
            ("REALM_MULTI_SELECT", "realm"): {"selectedOptions": selected},
        }
        if table.get("行为表现", True):
            values[("STUDENT_MEDAL", "studentMedal")] = {"studentMedalIds": []}
        if "分析" in table:
            values[("IMAGE_TEXT", "analysis")] = {"content": table["分析"]}
        if "行为跟进" in table:
            values[("SUGGESTION", "suggestion")] = {"suggestionDataVo": {
                "content": suggestion,
                "fileHashWithEntityVos": [], "fileVos": [], "subDataVos": []}}
        if "家庭指导" in table:
            values[("FAMILY_GUIDE", "familyGuide")] = {"familyGuideDataVo": {
                "content": table["家庭指导"].strip(),
                "fileHashWithEntityVos": [], "fileVos": [], "subDataVos": []}}
        for label, key in option_fields.items():
            if label not in table:
                continue
            groups = self.get_table_field_options(key[0])
            choices = {(o["groupLabel"], o["value"]): o for g in groups for o in g["options"]}
            chosen = []
            for option in table[label]:
                identity = (option["groupLabel"], option["value"])
                if identity not in choices:
                    raise ValueError(f"{label}「{option['groupLabel']}·{option['value']}」已不在官网选项中，请重新选择")
                if choices[identity] not in chosen:
                    chosen.append(choices[identity])
            values[key] = {"selectedOptions": chosen}
        seen = set()
        for original in fields:
            if not isinstance(original, dict) or not original.get("id"):
                raise ValueError("观察记录表模板字段格式异常，尚未发布")
            field = deepcopy(original)
            field["templateFieldId"] = field.pop("id")
            key = (field.get("type"), field.get("esProperty"))
            if key in values:
                if key in seen:
                    raise ValueError("观察记录表模板存在重复字段，尚未发布")
                seen.add(key)
                if field.get("required") and key == ("REALM_MULTI_SELECT", "realm") and not selected:
                    raise ValueError("观察记录表模板要求关联领域，请先选择领域")
                body["fieldVos"].append(field)
            elif field.get("required"):
                raise ValueError(f"观察记录表有未填写的必填字段「{field.get('name') or field.get('type')}」，请在网页完善")
        additional = {("STUDENT_MEDAL", "studentMedal"), ("IMAGE_TEXT", "analysis"),
                      ("SUGGESTION", "suggestion"), ("FAMILY_GUIDE", "familyGuide"),
                      *option_fields.values()}
        if set(values) - seen - additional:
            raise ValueError("自定义观察记录表模板的基础字段已改变，尚未发布")
        if set(values) - seen:
            catalog = self._get("/api/growRecordTableSystemField/findAll")
            if not isinstance(catalog, list):
                raise ValueError("幼师口袋的观察记录表系统字段格式异常，尚未发布")
            for key in values:
                if key in seen:
                    continue
                found = [f for f in catalog if isinstance(f, dict)
                         and (f.get("type"), f.get("esProperty")) == key]
                if len(found) != 1 or not found[0].get("id"):
                    raise ValueError("无法唯一确定观察记录表系统字段，尚未发布")
                field = deepcopy(found[0])
                field["systemFieldId"] = field.pop("id")
                body["fieldVos"].append(field)
        official_names = {"scene": "观察场景", "activity": "关联活动", "studentMedal": "行为表现",
                          "analysis": "观察分析", "suggestion": "回应策略", "familyGuide": "家庭指导"}
        for field in body["fieldVos"]:
            key = (field.get("type"), field.get("esProperty"))
            field.update(fileHashWithEntityVos=[], selectedOptions=[], studentIds=[])
            field.update(deepcopy(values[key]))
            if key in option_fields.values() and field.get("required") and not field["selectedOptions"]:
                raise ValueError(f"观察记录表要求填写「{field['name']}」，请先选择选项")
            if field.get("esProperty") in official_names:
                field["name"] = official_names[field["esProperty"]]
            field["showForParent"] = field.get("esProperty") not in ("analysis", "suggestion")
        for extra in extras:
            body["fieldVos"].append({"name": extra["title"], "type": "IMAGE_TEXT", "required": False,
                                     "showForParent": True, "content": extra["content"],
                                     "fileHashWithEntityVos": [], "audioHashNames": [],
                                     "audioFileHashWithEntityVos": []})
        names = [f.get("name") for f in body["fieldVos"]]
        if len(names) != len(set(names)):
            raise ValueError("观察记录表中有重复的段落标题，请改名后发布")
        return body

    def _table_modules(self, modules: list) -> list:
        """把本地的领域/子领域与当前站内体系核对，不能把未知领域静默丢掉。"""
        if not isinstance(modules, list):
            raise ValueError("关联领域格式不正确")
        if not modules:
            return []
        systems = self._get("/api/evaluationSystem/findRealmDatas")
        choices = []
        for system in systems if isinstance(systems, list) else []:
            for realm in system.get("growRealmModuleVos") or []:
                system_id = realm.get("systemId", system.get("id"))
                if system_id not in (9, 35):
                    continue
                for module in realm.get("modules") or []:
                    choices.append({"groupLabel": realm.get("realm"), "value": module,
                                    "systemId": system_id, "customized": False})
        selected = []
        for module in modules:
            if not isinstance(module, dict) or not module.get("realm") or not module.get("module"):
                raise ValueError("关联领域格式不正确")
            found = [x for x in choices if x["systemId"] == module.get("systemId", 9)
                     and x["groupLabel"] == module["realm"] and x["value"] == module["module"]]
            if not found:
                raise ValueError(f"关联领域「{module['realm']}·{module['module']}」不在当前幼师口袋指标体系中")
            if found[0] not in selected:
                selected.append(found[0])
        return selected

    def add_table_record(self, prepared: dict, files: list, medal_map: dict) -> int:
        """补入本次上传图片/新增指标后发布；不修改已校验模板和调用方的数据。"""
        if MOCK:
            return int(time.time() * 1000) % 100000000
        if not isinstance(prepared, dict) or prepared.get("type") != "TABLE":
            raise ValueError("观察记录表尚未通过发布校验")
        body = deepcopy(prepared)
        fields = body.get("fieldVos")
        if not isinstance(fields, list) or not all(isinstance(f, dict) for f in fields):
            raise ValueError("观察记录表字段损坏，尚未发布")
        observation = [f for f in fields if f.get("type") == "OBSERVATION_RECORD"]
        student_fields = [f for f in fields if f.get("type") == "STUDENT"]
        medal_fields = [f for f in fields if f.get("type") == "STUDENT_MEDAL"]
        if len(observation) != 1 or len(student_fields) != 1 or len(medal_fields) > 1:
            raise ValueError("观察记录表的实录、观察对象或行为表现字段异常")
        entries = observation[0].get("observationRecordItemVos")
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise ValueError("观察记录表实录格式异常")
        if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
            raise ValueError("观察记录表照片格式异常")
        if not isinstance(medal_map, dict):
            raise ValueError("观察记录表行为表现格式异常")
        if not medal_fields and medal_map:
            raise ValueError("已关闭行为表现，不能附带学生指标")
        student_ids = student_fields[0].get("studentIds") or []
        medals = []
        for sid, ids in medal_map.items():
            if sid not in student_ids or not isinstance(ids, (list, tuple)) or any(
                    type(i) is not int or i <= 0 for i in ids):
                raise ValueError("观察记录表行为表现与观察对象不匹配")
            for medal_id in ids:
                if medal_id not in medals:
                    medals.append(medal_id)
        if medal_fields and medal_fields[0].get("required") and not medals:
            raise ValueError("观察记录表模板要求行为表现，请先勾选指标")
        entries[0]["fileHashWithEntityVos"] = deepcopy(files)
        if medal_fields:
            medal_fields[0]["studentMedalIds"] = medals
        # 表格服务由这些远端学生徽章 ID 恢复归属，不需要随手记的 studentMedalInfoVos。
        uncertain = "观察记录表可能已发布，请先在幼师口袋网页核对，暂勿重复发布"
        try:
            rid = self._post("/api/growRecordTable/add", body)
        except requests.exceptions.RequestException as e:
            raise TablePublishUncertain(uncertain) from e
        if isinstance(rid, dict) and "code" in rid and rid["code"] != "OK":
            raise RuntimeError(rid.get("msg") or f"接口返回 {rid['code']}")
        if isinstance(rid, bool) or not isinstance(rid, (int, str)):
            raise TablePublishUncertain(uncertain)
        try:
            rid = int(rid)
        except ValueError:
            raise TablePublishUncertain(uncertain) from None
        if rid <= 0:
            raise TablePublishUncertain(uncertain)
        return rid

    def delete_record(self, record_id: int):
        if MOCK:
            return
        self._delete("/api/growRecord/delete", id=record_id)

    def delete_table_record(self, record_id: int):
        """表格有独立撤回接口，由服务器同时撤回关联的学生指标。"""
        if MOCK:
            return
        self._post("/api/growRecordTable/delete", {}, id=record_id,
                   deleteRelatedStudentMedal="true")

    def delete_student_medal(self, student_medal_id: int):
        if MOCK:
            return
        self._delete("/api/growMedal/deleteStudentMedal", ids=student_medal_id)

    def count_this_year(self, kid: int, class_id: int) -> dict:
        year = datetime.now().year if datetime.now().month >= 9 else datetime.now().year - 1
        return self._get("/api/hybridClassGrowthRecord/countInKindergarten", kid=kid, classId=class_id, schoolYear=year)
