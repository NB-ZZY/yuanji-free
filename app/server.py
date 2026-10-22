"""本地 HTTP 服务（FastAPI）。前端页面 static/index.html 通过 /api/* 与之通信。"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import brief as brief_mod
import db
import face_client
import pipeline
import roster
import session_state
import class_album
import access_guard
from secrets import token_urlsafe
from config import APP_TITLE, MOCK, STATIC_DIR, VERSION, YSK_BASE
from knowledge import (class_type_to_level, domains, indicator_lines, load_indicators,
                       NURSERY_LEVELS, modules_for_level)
from ysk_client import NotLoggedIn, YskClient

log = logging.getLogger("server")
app = FastAPI(title=APP_TITLE)


@app.exception_handler(HTTPException)
async def _http_error(request, exc):
    return JSONResponse({'error': str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)


from knowledge_api import router as knowledge_router
app.include_router(knowledge_router)
from resources_api import router as resources_router
app.include_router(resources_router)
db.init()

_ysk = YskClient(db.get_setting("ysk_cookies"))
_jobs: dict[str, dict] = {}     # 长任务进度
_login_attempt = None
_resource_importing = False
if db.get_setting('current_uid'):
    import style
    style.migrate_legacy(db.get_setting('current_uid'))


def _job(name: str):
    state = {"done": 0, "total": 0, "status": "running", "result": None, "error": None,
             "_owner_uid": str(db.get_setting('current_uid') or '')}
    _jobs[name] = state
    def prog(d, t):
        state.update(done=d, total=t)
    return prog


def _run_job(name: str, fn):
    with session_state.lock:
        if _jobs.get(name, {}).get('status') == 'running':
            raise HTTPException(409, '这项任务正在处理，请等待完成')
        session_state.retain()
        prog = _job(name)
        state = _jobs[name]
    def _t():
        try:
            state["result"] = fn(prog)
            state["status"] = "done"
        except Exception as e:
            log.exception("job %s", name)
            state.update(status="error", error=str(e))
        finally:
            session_state.release()
    try:
        threading.Thread(target=_t, daemon=True).start()
    except BaseException:
        state.update(status='error', error='任务未能启动，请重试')
        session_state.release()
        raise


def _err(e: Exception, code: int = 400):
    return JSONResponse({"error": str(e)}, status_code=code)


# ---------- 基本 ----------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status():
    with session_state.lock:
        client, epoch = _ysk, session_state.generation
    user = None
    try:
        if MOCK or db.get_setting('ysk_cookies'):
            user = client.check_login()
    except NotLoggedIn:
        pass
    except Exception as exc:
        log.warning("check_login: %s", type(exc).__name__)
    with session_state.lock:
        if client is not _ysk or epoch != session_state.generation:
            return _err(ValueError('登录状态已变化，请刷新页面'), 409)
        if user and user.get('id'):
            previous = db.get_setting('current_uid')
            if str(previous or '') != str(user['id']):
                db.set_setting('current_class', None)
                db.set_setting('current_uid', user['id'])
        current = db.get_setting('current_class') if user else None
        cls = db.one('SELECT * FROM classes WHERE class_id=?', (current,)) if current else None
        import config
        return {'version': VERSION, 'edition': 'community', 'mock': MOCK, 'logged_in': bool(user),
                'session_generation': epoch,
                'user': {'id': user['id'], 'name': user.get('userName')} if user else None,
                'current_class': cls, 'consent': db.get_setting('consent', False),
                'ai_configured': bool(config.ai_conf().get('key')), 'face_enabled': config.face_enabled()}


# ---------- 本机 AI 设置 ----------


@app.get('/api/community/settings')
def community_settings_get():
    import config
    return config.public_settings()


@app.post('/api/community/settings')
def community_settings_save(body: dict):
    import config
    try:
        with session_state.lock:
            _require_idle()
            return config.save_settings(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class AiModeBody(BaseModel):
    mode: str


@app.get("/api/ai/mode")
def ai_mode_get():
    """老师现在用哪一档，以及两档各是什么。"""
    from config import ai_modes, ai_conf
    ms = ai_modes()
    cur = ai_conf()
    return {"mode": cur.get("mode"), "default": ms.get("default"),
            "fast": {"model": ms["fast"].get("model"),
                     "thinking": bool(ms["fast"].get("thinking"))},
            "deep": {"model": ms["deep"].get("model"),
                     "thinking": bool(ms["deep"].get("thinking"))}}


@app.post("/api/ai/mode")
def ai_mode_set(body: AiModeBody):
    if body.mode not in ("fast", "deep"):
        return JSONResponse({"error": "只能是 fast 或 deep"}, status_code=400)
    with session_state.lock:
        _require_idle()
        db.set_setting("ai_mode", body.mode)
    return {"ok": True, "mode": body.mode}


class AiEffortBody(BaseModel):
    effort: str


@app.get('/api/ai/effort')
def ai_effort_get():
    import config
    result = config.effort_options()
    selected = db.get_setting('ai_effort_default', '')
    result['selected'] = selected if selected in result['levels'] else result['default']
    return result


def _item_effort_options(item):
    import config
    result = config.effort_options(config.ai_conf(item.get('ai_speed') or ''))
    supported = result['levels'] or ['default']
    selected = (db.get_setting('ai_effort_item_' + str(item['id']), '')
                or db.get_setting('ai_effort_default', ''))
    result['selected'] = selected if selected in supported else result['default']
    return result


@app.post('/api/ai/effort')
def ai_effort_set(body: AiEffortBody):
    with session_state.lock:
        _require_idle()
        current = ai_effort_get()
        if body.effort not in (current['levels'] or ['default']):
            raise HTTPException(400, '当前模型不支持这个思考档位')
        db.set_setting('ai_effort_default', body.effort)
    return {**current, 'selected': body.effort}


@app.post('/api/item/{iid}/effort')
def item_effort_set(iid: int, body: AiEffortBody):
    item = db.one('SELECT * FROM items WHERE id=?', (iid,))
    if not item:
        raise HTTPException(404)
    if item.get('ai_status') == 'writing':
        raise HTTPException(409, '正在生成的记录不能更换思考档位')
    current = _item_effort_options(item)
    if body.effort not in (current['levels'] or ['default']):
        raise HTTPException(400, '当前模型不支持这个思考档位')
    db.set_setting('ai_effort_item_' + str(iid), body.effort)
    return {'ok': True, 'effort': body.effort, 'options': {**current, 'selected': body.effort}}


@app.get('/api/item/{iid}/ai-progress')
def item_ai_progress(iid: int):
    if not db.one('SELECT 1 FROM items WHERE id=?', (iid,)):
        raise HTTPException(404)
    import ai_progress
    return ai_progress.snapshot(iid) or {}


# Local settings remain available before a third-party platform login.


@app.middleware("http")
async def _gate(request, call_next):
    """Keep browser-origin and account-generation boundaries without license gates."""
    global _resource_importing
    from urllib.parse import urlsplit
    host = request.url.hostname
    if host not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
        return JSONResponse({'error': '仅允许从本机访问。'}, status_code=403)
    origin = request.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        if parsed.netloc != request.url.netloc or parsed.scheme != request.url.scheme:
            return JSONResponse({'error': '不允许其他网站访问本机程序。'}, status_code=403)
    path = request.url.path
    # Exact local setup routes: adding a similarly named API cannot expose account data.
    free = path in ('/api/status', '/api/selfcheck', '/api/consent', '/api/community/settings',
                    '/api/login/begin', '/api/login/cancel', '/api/login/cookies', '/api/logout',
                    '/api/ai/mode', '/api/ai/effort', '/api/face/test',
                    '/api/resources', '/api/resources/import')
    # Removed private endpoints must stay absent, including before platform login.
    removed = path.startswith(('/api/license', '/api/client-policy', '/api/update', '/api/dev/', '/api/monthly'))
    if removed:
        return JSONResponse({'error': '接口不存在。'}, status_code=404)
    needs_account = path.startswith('/api/') and not free
    importing = path == '/api/resources/import' and request.method == 'POST'
    local_work = request.method == 'POST' and path in ('/api/face/test', '/api/resources/import')
    tracked = needs_account or local_work
    with session_state.lock:
        expected = request.headers.get('X-Kidobs-Session')
        if expected is not None and expected != str(session_state.generation):
            return JSONResponse({'error': '登录状态已变化，请刷新页面'}, status_code=409)
        if _resource_importing and path.startswith('/api/') and (tracked or request.method != 'GET'):
            return JSONResponse({'error': '正在导入资料，请稍后再试。'}, status_code=409)
        if local_work and session_state.busy():
            return JSONResponse({'error': '正在处理任务，请完成后再测试服务或导入资料。'}, status_code=409)
        if needs_account and not (db.get_setting('current_uid') and db.get_setting('ysk_cookies')) and not MOCK:
            return JSONResponse({'error': '请先登录幼师口袋。'}, status_code=401)
        if tracked:
            session_state.retain()
        if importing:
            _resource_importing = True
    try:
        if needs_account:
            body = None
            if request.method in ('POST', 'PATCH', 'PUT') and 'application/json' in request.headers.get('content-type', ''):
                try:
                    body = await request.json()
                except ValueError:
                    pass  # Let the route's normal request validation report invalid JSON.
            try:
                await run_in_threadpool(access_guard.authorize, path, dict(request.query_params), body, _ysk)
            except HTTPException as exc:
                return JSONResponse({'error': str(exc.detail)}, status_code=exc.status_code)
        return await call_next(request)
    finally:
        if tracked:
            with session_state.lock:
                if importing:
                    _resource_importing = False
                session_state.release()


@app.post("/api/consent")
def consent():
    db.set_setting("consent", True)
    return {"ok": True}


@app.get("/api/job/{name}")
def job(name: str):
    state = _jobs.get(name)
    if not state:
        return {"status": "none"}
    if state.get('_owner_uid') != str(db.get_setting('current_uid') or ''):
        raise HTTPException(404, '任务不存在。')
    return {key: value for key, value in state.items() if not key.startswith('_')}


# ---------- 登录 ----------
class CookieBody(BaseModel):
    cookies: dict
    attempt: str = ''


class LoginAttemptBody(BaseModel):
    attempt: str = ''


def _require_idle():
    if session_state.busy():
        raise HTTPException(409, '正在生成、保存、同步或发布，请等待任务完成后再退出或切换账号')


@app.post('/api/login/begin')
def login_begin():
    global _login_attempt
    with session_state.lock:
        _require_idle()
        _login_attempt = token_urlsafe(24)
        return {'attempt': _login_attempt, 'session_generation': session_state.generation}


@app.post('/api/login/cancel')
def login_cancel(body: LoginAttemptBody):
    global _login_attempt
    with session_state.lock:
        if body.attempt and body.attempt == _login_attempt:
            _login_attempt = None
    return {'ok': True}


def _check_attempt(attempt):
    if not attempt or attempt != _login_attempt:
        raise HTTPException(409, '这次登录已取消，请重新打开登录窗口')


def _store_session(cookies, uid, clear_class):
    fields = {'ysk_cookies': cookies, 'current_uid': uid}
    if clear_class:
        fields['current_class'] = None
    with db.conn() as c:
        for key, value in fields.items():
            c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                      (key, json.dumps(value, ensure_ascii=False)))


@app.post("/api/login/cookies")
def login_cookies(body: CookieBody):
    """Validate a separate client; commit only the still-current login attempt."""
    global _ysk, _login_attempt
    with session_state.lock:
        _check_attempt(body.attempt)
        _require_idle()
        epoch = session_state.generation
    candidate = YskClient(body.cookies)
    try:
        u = candidate.check_login()
    except NotLoggedIn as e:
        return _err(e, 401)
    with session_state.lock:
        _check_attempt(body.attempt)
        if epoch != session_state.generation:
            raise HTTPException(409, '登录状态已变化，请重新打开登录窗口')
        _require_idle()
        changed = str(db.get_setting('current_uid') or '') != str(u['id'])
        _store_session(body.cookies, u['id'], clear_class=changed)
        _ysk = candidate
        _login_attempt = None
        session_state.generation += 1
        _jobs.clear()
        class_album.STORE.clear()
        epoch = session_state.generation
    return {'ok': True, 'user': {'id': u['id'], 'name': u.get('userName')}, 'session_generation': epoch}


@app.get("/api/selfcheck")
def selfcheck():
    """Read-only local diagnostics; never contact a publisher or print credentials."""
    import config
    import plat
    import importlib.util
    rows = [{'名目': '本地数据目录', '好': config.DATA_DIR.is_dir(), '说明': str(config.DATA_DIR), '毫秒': 0},
            {'名目': 'AI 配置', '好': bool(config.ai_conf().get('key')), '说明': '已设置' if config.ai_conf().get('key') else '请在设置中填写自己的 API', '毫秒': 0},
            {'名目': '桌面窗口依赖', '好': importlib.util.find_spec('webview') is not None, '说明': 'pywebview', '毫秒': 0},
            {'名目': '登录凭证', '好': bool(db.get_setting('ysk_cookies')), '说明': '已保存' if db.get_setting('ysk_cookies') else '尚未登录', '毫秒': 0}]
    return {'rows': rows, 'version': VERSION, 'plat': plat.name_cn(), 'edition': 'community'}


@app.post("/api/logout")
def logout(request: Request = None):
    """Leave stored class records intact; revoke only the current account session."""
    global _ysk, _login_attempt
    with session_state.lock:
        expected = request.headers.get('X-Kidobs-Session') if request is not None else None
        if expected is not None and expected != str(session_state.generation):
            raise HTTPException(409, '登录状态已变化，请刷新页面')
        _require_idle()
        _store_session(None, None, clear_class=True)
        _ysk = YskClient({})
        _login_attempt = None
        session_state.generation += 1
        _jobs.clear()
        class_album.STORE.clear()
        return {'ok': True, 'session_generation': session_state.generation}


# ---------- 班级 / 花名册 ----------
@app.get("/api/classes")
def classes():
    try:
        _ysk.check_login()
        lst = _ysk.my_classes()
    except NotLoggedIn as e:
        return _err(e, 401)
    except Exception as e:
        return _err(e, 502)
    for c in lst:
        c["level"] = class_type_to_level(c.get("type"), c["name"])
    return lst


@app.post("/api/class/{class_id}/sync")
def sync_class(class_id: int):
    """从幼师口袋同步班级名单到本地（照片保留）。"""
    try:
        _ysk.check_login()
        cl = next((c for c in _ysk.my_classes() if c["classId"] == class_id), None)
        if not cl:
            return _err(ValueError('当前账号不属于这个班级，请重新选择班级'), 403)
        info = _ysk.class_info(class_id)
        kid = (cl or {}).get("kid") or _ysk.user.get("kid")
    except NotLoggedIn as e:
        return _err(e, 401)
    except Exception as e:
        return _err(e, 502)
    class_type = info.get("type") or cl.get("type") or ""
    level = class_type_to_level(class_type, info.get("name", ""))
    db.upsert_class(class_id, kid, info.get("name", ""), class_type, level)
    for s in info.get("students", []):
        db.upsert_student(s)
    db.set_setting("current_class", class_id)
    # 头像顺手存一份（可作为第一张人脸样本）。
    # 注意：幼师口袋里很多孩子用的是同一张默认灰头像，这种要跳过——
    # 做法是先全部下载到内存，按原始字节哈希分组，出现 3 次以上的一律当默认头像丢掉。
    def _avatars(prog):
        import hashlib
        sts = db.students_of(class_id)
        blobs, counts = {}, {}
        for i, s in enumerate(sts):
            if s["photo_count"] == 0 and s["avatar"] and not MOCK:
                try:
                    data = _ysk.download(_ysk.avatar_url(s["avatar"], 600))
                    h = hashlib.sha1(data).hexdigest()
                    blobs[s["student_id"]] = (h, data)
                    counts[h] = counts.get(h, 0) + 1
                except Exception as e:
                    log.warning("头像下载失败 %s: %s", s["name"], e)
            prog(i + 1, len(sts) + 1)
        saved = skipped = 0
        for sid, (h, data) in blobs.items():
            if counts[h] >= 3:          # 多人共用 = 默认头像
                skipped += 1
                continue
            try:
                roster.save_student_photo(sid, data)
                saved += 1
            except Exception as e:
                log.warning("头像保存失败 %s: %s", sid, e)
        prog(len(sts) + 1, len(sts) + 1)
        log.info("头像入库：保存 %s 张，跳过默认头像 %s 张", saved, skipped)
        return {"saved": saved, "skipped_default": skipped}
    _run_job(f"avatars_{class_id}", _avatars)
    # 顺手问一下云端：这个班是不是已经有同事入过库了。失败不影响同步名单。
    cloud = {}
    try:
        import config
        if config.face_enabled():
            cloud = roster.refresh_cloud_state(class_id)
    except Exception as e:
        log.warning("读云端入库状态失败（不影响使用）：%s", e)
    return {"ok": True, "class": db.one("SELECT * FROM classes WHERE class_id=?", (class_id,)),
            "students": len(info.get("students", [])), "cloud": cloud}


@app.post("/api/class/{class_id}/cloud_refresh")
def cloud_refresh(class_id: int):
    """手动去云端刷一次入库状态（同班另一位老师刚入完库时用得上）。"""
    try:
        return roster.refresh_cloud_state(class_id)
    except Exception as e:
        return _err(e, 502)


@app.post("/api/class/{class_id}/select")
def select_class(class_id: int):
    try:
        _ysk.check_login()
        if not any(str(c['classId']) == str(class_id) for c in _ysk.my_classes()):
            return _err(ValueError('当前账号不属于这个班级，请重新选择班级'), 403)
        if not db.one('SELECT class_id FROM classes WHERE class_id=?', (class_id,)):
            return _err(ValueError('请先同步这个班级'), 400)
    except NotLoggedIn as e:
        return _err(e, 401)
    db.set_setting("current_class", class_id)
    return {"ok": True}


@app.get("/api/class/{class_id}/students")
def students(class_id: int):
    return db.students_of(class_id)


@app.get("/api/student/{student_id}/photos")
def student_photos(student_id: int):
    return db.rows("SELECT id,path,enrolled,error,created_at FROM photos WHERE student_id=? ORDER BY id", (student_id,))


@app.get("/api/student/{student_id}/cover")
def student_cover(student_id: int):
    ph = db.one("SELECT path FROM photos WHERE student_id=? ORDER BY enrolled DESC, id DESC LIMIT 1", (student_id,))
    if not ph:
        raise HTTPException(404)
    return FileResponse(ph["path"])


@app.get("/api/photo/{photo_id}")
def photo(photo_id: int):
    ph = db.one("SELECT path FROM photos WHERE id=?", (photo_id,))
    if not ph:
        raise HTTPException(404)
    return FileResponse(ph["path"])


@app.get("/api/file")
def file(path: str):
    """只预览当前账号有权使用的已登记照片。"""
    return FileResponse(access_guard.media_path(path, _ysk))


@app.post("/api/student/{student_id}/photos")
async def add_student_photos(student_id: int, files: list[UploadFile] = File(...)):
    out, errs = [], []
    for f in files:
        try:
            out.append(roster.save_student_photo(student_id, await f.read())["id"])
        except Exception as e:
            errs.append(f"{f.filename}: {e}")
    return {"added": out, "errors": errs}


@app.delete("/api/photo/{photo_id}")
def del_photo(photo_id: int):
    roster.delete_photo(photo_id)
    return {"ok": True}


@app.get("/api/class/{class_id}/photos")
def class_photos(class_id: int):
    """整班底照，供「复核底照」页用。"""
    return roster.class_photos(class_id)


def _album_context(class_id: int, verify: bool = True):
    """Use only the active teacher's selected class, never a supplied media URL."""
    with session_state.lock:
        client, generation = _ysk, session_state.generation
        uid = db.get_setting('current_uid')
        current = db.get_setting('current_class')
    if not uid or not client.uid:
        raise HTTPException(401, '请先登录幼师口袋')
    if str(current or '') != str(class_id):
        raise HTTPException(409, '班级已变化，请重新打开当前班级的相册')
    if str(client.uid) != str(uid):
        raise HTTPException(409, '账号已变化，请刷新页面')
    if verify:
        try:
            user = client.check_login()
            if str(user.get('id') or '') != str(uid):
                raise HTTPException(409, '账号已变化，请刷新页面')
            if not any(str(c['classId']) == str(class_id) for c in client.my_classes()):
                raise HTTPException(403, '当前账号没有这个班级的相册权限')
        except NotLoggedIn as e:
            raise HTTPException(401, str(e)) from e
    _assert_album_context(client, uid, class_id, generation)
    return client, uid, generation


def _assert_album_context(client, uid, class_id, generation):
    # Class selection can change while a read-only image download is running.
    with session_state.lock:
        if (client is not _ysk or generation != session_state.generation or
                str(uid) != str(db.get_setting('current_uid') or '') or
                str(class_id) != str(db.get_setting('current_class') or '')):
            raise HTTPException(409, '账号或班级已变化，请重新打开相册')


@app.get('/api/class/{class_id}/albums')
def class_albums(class_id: int, from_date: str, to_date: str,
                 page: int = 0, token: Optional[str] = None):
    """Read the website's ALBUM_FEED records; no album edits or publication."""
    try:
        if page < 0 or page > 1000:
            raise ValueError('相册页码不正确')
        first, last = class_album.date_bounds(from_date, to_date)
        client, uid, generation = _album_context(class_id)
        raw = client.class_albums(class_id, first, last, page)
        parsed = class_album.parse_page(raw, class_id)
        _assert_album_context(client, uid, class_id, generation)
        return class_album.STORE.store_page(parsed, uid, class_id, generation,
                                            page, from_date, to_date, token=token)
    except HTTPException:
        raise
    except NotLoggedIn as e:
        return _err(e, 401)
    except (ValueError, KeyError) as e:
        return _err(e, 400)
    except Exception:
        log.exception('读取班级相册失败')
        return _err(ValueError('暂时无法读取班级相册，请稍后重试'), 502)


@app.get('/api/class/{class_id}/album-photo/{asset_id}')
def class_album_photo(class_id: int, asset_id: str, token: str, preview: bool = False):
    try:
        client, uid, generation = _album_context(class_id, verify=not preview)
        asset = class_album.STORE.resolve(token, asset_id, uid, class_id, generation)
        data = class_album.download_photo(client, asset, preview=preview)
        _assert_album_context(client, uid, class_id, generation)
        return Response(data, media_type='image/jpeg', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': 'inline; filename="album-' + asset_id + '.jpg"'})
    except HTTPException:
        raise
    except NotLoggedIn as e:
        return _err(e, 401)
    except (ValueError, KeyError) as e:
        return _err(e, 400)
    except Exception:
        log.exception('读取班级相册照片失败')
        return _err(ValueError('照片读取失败，请重新选择或稍后重试'), 502)


class ReassignBody(BaseModel):
    student_id: int


@app.post("/api/photo/{photo_id}/reassign")
def reassign_photo(photo_id: int, body: ReassignBody):
    try:
        return roster.reassign_photo(photo_id, body.student_id)
    except Exception as e:
        return _err(e)


class PathsBody(BaseModel):
    paths: list[str]


@app.post("/api/class/{class_id}/import/excel")
def import_excel(class_id: int, body: PathsBody):
    try:
        return roster.import_excel(body.paths[0], class_id)
    except Exception as e:
        return _err(e)


@app.post("/api/class/{class_id}/import/files")
def import_files(class_id: int, body: PathsBody):
    _run_job(f"import_{class_id}", lambda prog: roster.import_folder_or_files(body.paths, class_id))
    return {"job": f"import_{class_id}"}


@app.post("/api/class/{class_id}/import/upload")
async def import_upload(class_id: int, files: list[UploadFile] = File(...)):
    """浏览器拖拽/多选上传的一堆照片：按文件名/子文件夹名/人脸自动归档。"""
    blobs = []
    for f in files:
        rel = Path(f.filename or "")
        folder = rel.parent.name if len(rel.parts) > 1 else ""
        blobs.append((rel.name, folder, await f.read()))
    _run_job(f"import_{class_id}", lambda prog: roster.import_blobs(blobs, class_id))
    return {"job": f"import_{class_id}"}


@app.post("/api/class/{class_id}/import/excel_upload")
async def import_excel_upload(class_id: int, file: UploadFile = File(...)):
    from config import DATA_DIR
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='roster-', dir=DATA_DIR) as temporary:
        p = Path(temporary) / Path(file.filename or 'roster.xlsx').name
        p.write_bytes(await file.read())
        try:
            return roster.import_excel(str(p), class_id)
        except Exception as e:
            return _err(e)


@app.get("/api/pending")
def pending_photos():
    d = roster.pending_root()
    if not d.exists():
        return []
    return [{"temp": str(p)} for p in sorted(d.glob("*.jpg"))]


@app.delete("/api/pending")
def drop_pending(temp: str):
    access_guard.pending_path(temp).unlink()
    return {"ok": True}


class AssignBody(BaseModel):
    temp: str
    student_id: int


@app.post("/api/assign")
def assign(body: AssignBody):
    return {"photo_id": roster.assign_pending(str(access_guard.pending_path(body.temp)), body.student_id)["id"]}


@app.post("/api/class/{class_id}/harvest")
def harvest(class_id: int, want: int = 3, reset: bool = False):
    """从幼师口袋这个班的历史观察记录里自动抓取每个孩子的底照。"""
    try:
        _ysk.check_login()
    except NotLoggedIn as e:
        return _err(e, 401)
    _run_job(f"harvest_{class_id}",
             lambda prog: roster.harvest_from_ysk(class_id, _ysk, want=want, reset=reset, progress=prog))
    return {"job": f"harvest_{class_id}"}


@app.post("/api/class/{class_id}/regrade")
def regrade(class_id: int):
    """给还没评过级的底照补一次正脸判定。"""
    _run_job(f"regrade_{class_id}", lambda prog: roster.regrade_photos(class_id, progress=prog))
    return {"job": f"regrade_{class_id}"}


@app.post("/api/class/{class_id}/harvest_group")
def harvest_group(class_id: int):
    """第二轮：还缺正脸的孩子，去合影里抠脸找（认得出的直接入库，认不出的交给老师指认）。"""
    try:
        _ysk.check_login()
    except NotLoggedIn as e:
        return _err(e, 401)
    _run_job(f"harvestg_{class_id}",
             lambda prog: roster.harvest_from_group_photos(class_id, _ysk, progress=prog))
    return {"job": f"harvestg_{class_id}"}


@app.get("/api/class/{class_id}/candidates")
def class_candidates(class_id: int):
    return roster.class_candidates(class_id)


@app.get("/api/candidate/{cand_id}")
def candidate_img(cand_id: int):
    c = db.one("SELECT path FROM candidates WHERE id=?", (cand_id,))
    if not c:
        raise HTTPException(404)
    return FileResponse(c["path"])


@app.post("/api/candidate/{cand_id}/confirm")
def candidate_confirm(cand_id: int):
    try:
        return roster.confirm_candidate(cand_id)
    except Exception as e:
        return _err(e, 400)


@app.delete("/api/candidate/{cand_id}")
def candidate_drop(cand_id: int):
    roster.drop_candidate(cand_id)
    return {"ok": True}


@app.post("/api/class/{class_id}/enroll")
def enroll(class_id: int):
    _run_job(f"enroll_{class_id}", lambda prog: roster.enroll_pending(class_id, prog))
    return {"job": f"enroll_{class_id}"}


@app.post("/api/face/test")
def face_test():
    try:
        return face_client.test_connection()
    except Exception as e:
        return _err(e, 502)


# ---------- 知识库 ----------
@app.get("/api/indicators")
def indicators(level: int = 5):
    if level not in (1, 3, 5, 128):
        raise HTTPException(400, "不支持的班级年龄档")
    return [{"id": i["id"], "name": i["name"], "domain": i["domain"], "subdomain": i["subdomain"], "goal": i["goal"],
             "lines": indicator_lines(i, level),
             # 三个档位都给前端：同一条指标下想勾几条勾几条，档位不一样也行
             "all_lines": {str(lv): indicator_lines(i, lv) for lv in ((128,) if level in NURSERY_LEVELS else (1, 3, 5))},
             "systemId": i.get("systemId", 9),
             "cover": i.get("cover")} for i in load_indicators(level)]


@app.get("/api/realms")
def realms(level: int = 5):
    """关联领域可选项：领域 → 模块（和幼师页面上的一致，直接由指标库推出来）。"""
    out = {}
    if level not in (1, 3, 5, 128):
        raise HTTPException(400, "不支持的班级年龄档")
    for i in modules_for_level(level):
        out.setdefault(i["realm"], [])
        if i["module"] not in out[i["realm"]]:
            out[i["realm"]].append(i["module"])
    return [{"realm": k, "modules": v} for k, v in out.items()]


# ---------- 小名 ----------
@app.post("/api/class/{class_id}/nicknames")
def mine_nicknames(class_id: int):
    """从幼师口袋的历史记录里挖每个孩子的小名。"""
    import nickname
    try:
        _ysk.check_login()
    except NotLoggedIn as e:
        return _err(e, 401)
    _run_job(f"nick_{class_id}", lambda prog: nickname.mine(class_id, _ysk, progress=prog))
    return {"job": f"nick_{class_id}"}


class NickBody(BaseModel):
    nickname: str = ""


@app.get("/api/class/{class_id}/nicknames")
def list_nicknames(class_id: int):
    import nickname
    return [{"student_id": s["student_id"], "name": s["name"], "nickname": s.get("nickname") or "",
             "suggest": nickname.suggest(s["name"])} for s in db.students_of(class_id)]


class NickBulk(BaseModel):
    items: list = []       # [{student_id, nickname}]


@app.post("/api/class/{class_id}/nicknames/bulk")
def set_nicknames(class_id: int, body: NickBulk):
    n = 0
    for one in body.items or []:
        if one.get("student_id"):
            db.set_nickname(int(one["student_id"]), str(one.get("nickname") or ""))
            n += 1
    return {"saved": n}


@app.post("/api/student/{student_id}/nickname")
def set_nickname(student_id: int, body: NickBody):
    db.set_nickname(student_id, body.nickname)
    return db.one("SELECT student_id,name,nickname FROM students WHERE student_id=?", (student_id,))


# ---------- 观察批次 ----------
@app.post("/api/batch")
async def new_batch(class_id: int = Form(...), note: str = Form(""), record_date: str = Form(""),
                    mode: str = Form("simple"), length: str = Form("normal"),
                    brief: str = Form(""),
                    files: list[UploadFile] = File(...)):
    await run_in_threadpool(access_guard.require_class, class_id, _ysk)
    data = [(f.filename, await f.read()) for f in files]
    parsed = None
    if brief.strip():
        # 老师在上传前已经点过「让 AI 读一读」，结果原样带上来，不再花第二次钱
        try:
            parsed = json.loads(brief)
        except Exception:
            parsed = None
    bid = pipeline.create_batch(class_id, data, note, record_date, mode, length, parsed)
    return {"batch_id": bid}


class BriefBody(BaseModel):
    class_id: Optional[int] = None
    text: str = ""


@app.post("/api/brief/parse")
def brief_parse(body: BriefBody):
    """老师说的一段话 → 谁做了什么 + 指标。只解析，不改任何记录。"""
    cid = body.class_id or _current_class_id()
    if not cid:
        raise HTTPException(400, "请先选择班级")
    lvl = _level_of_class(cid)
    try:
        return brief_mod.parse(body.text, cid, lvl)
    except Exception as e:
        raise HTTPException(500, f"没读懂这段话：{e}")


@app.post("/api/batch/{bid}/brief")
def batch_brief(bid: int, body: BriefBody):
    """在批次里补一段说明：解析完直接分到这一批的每条记录上。"""
    b = db.one("SELECT * FROM batches WHERE id=?", (bid,))
    if not b:
        raise HTTPException(404, "批次不在了")
    lvl = _level_of_class(b["class_id"])
    parsed = brief_mod.parse(body.text, b["class_id"], lvl)
    if not parsed.get("ok"):
        return parsed
    brief_mod.save(bid, parsed)
    db.exec("UPDATE batches SET note=? WHERE id=?", (body.text.strip(), bid))
    r = brief_mod.apply_to_batch(bid, parsed, lvl)
    return {**parsed, "applied": r}


def _level_of_class(class_id: int) -> int:
    c = db.one("SELECT level FROM classes WHERE class_id=?", (class_id,))
    return c["level"] if c else 5


def _current_class_id():
    return db.get_setting('current_class')


@app.get("/api/batches")
def batches(class_id: int):
    return db.rows("""SELECT b.*, (SELECT COUNT(*) FROM items i WHERE i.batch_id=b.id) n,
                      (SELECT COUNT(*) FROM items i WHERE i.batch_id=b.id AND i.publish_status='done') published
                      FROM batches b WHERE class_id=? ORDER BY id DESC LIMIT 50""", (class_id,))


@app.get("/api/batch/{bid}")
def batch(bid: int):
    b = db.one("SELECT * FROM batches WHERE id=?", (bid,))
    if not b:
        raise HTTPException(404)
    b["items"] = db.rows("SELECT * FROM items WHERE batch_id=? ORDER BY id", (bid,))
    import ai_progress
    for item in b['items']:
        if b.get('mode') == 'table':
            import record_table
            try:
                if isinstance(item.get('text'), str):
                    item['text'] = record_table.clean_analysis(item['text'])
                table = json.loads(item.get('table_json') or '{}')
                if isinstance(table, dict):
                    item['table_json'] = json.dumps(record_table.without_analysis_notice(table), ensure_ascii=False)
            except (TypeError, ValueError):
                pass  # Keep malformed legacy content available for manual repair.
        item['ai_progress'] = ai_progress.snapshot(item['id'])
        item['ai_effort'] = db.get_setting('ai_effort_item_' + str(item['id']), '')
        item['ai_effort_options'] = _item_effort_options(item)
    return b


@app.delete("/api/batch/{bid}")
def del_batch(bid: int):
    """仅移入本机回收站，绝不撤回网站记录。"""
    try:
        return pipeline.clear_batch(bid)
    except Exception as e:
        return _err(e, 400)


@app.get("/api/trash")
def trash_list(class_id: int):
    return pipeline.list_trash(class_id)


@app.post("/api/trash/{tid}/restore")
def trash_restore(tid: int):
    try:
        return pipeline.restore_trash(tid)
    except Exception as e:
        return _err(e, 400)


@app.post("/api/item/{iid}/rescan")
def item_rescan(iid: int):
    try:
        return pipeline.rescan(iid)
    except Exception as e:
        return _err(e, 400)


@app.post("/api/batch/{bid}/rescan")
def batch_rescan(bid: int):
    try:
        return {"started": pipeline.rescan_batch(bid)}
    except Exception as e:
        return _err(e, 400)


class LengthBody(BaseModel):
    length: str = "normal"


class StudentsBody(BaseModel):
    student_ids: list = []


@app.post("/api/item/{iid}/students")
def item_students(iid: int, body: StudentsBody):
    """一条记录评几个孩子（发成一条记录、挂多个孩子，每人各自打指标）。"""
    if not db.one("SELECT 1 FROM items WHERE id=?", (iid,)):
        raise HTTPException(404)
    try:
        return pipeline.set_students(iid, body.student_ids)
    except Exception as e:
        return _err(e, 400)


class BulkBody(BaseModel):
    effort: str = ""
    item_ids: list = []
    action: str
    value: object = None
    speed: str = ""          # 批量生成时用哪一档


@app.post("/api/batch/{bid}/bulk")
def batch_bulk(bid: int, body: BulkBody):
    """选中的这些条，统一改：加/删指标、加/删关联领域、改日期、同步家长、批量生成……"""
    try:
        if body.action == "generate" and body.speed in ("fast", "deep"):
            db.set_setting("ai_mode", body.speed)
        return pipeline.bulk(bid, body.item_ids, body.action, body.value, speed=body.speed, effort=body.effort)
    except Exception as e:
        return _err(e, 400)


@app.post("/api/batch/{bid}/length")
def batch_length(bid: int, body: LengthBody):
    if not db.one("SELECT 1 FROM batches WHERE id=?", (bid,)):
        raise HTTPException(404)
    return pipeline.set_batch_length(bid, body.length)


class MergeBody(BaseModel):
    item_ids: list = []


@app.post("/api/batch/{bid}/merge")
def batch_merge(bid: int, body: MergeBody):
    """把选中的几条并成一条（一次活动拍了好几张，认脸自己合不了的交给老师点）。"""
    if not db.one("SELECT 1 FROM batches WHERE id=?", (bid,)):
        raise HTTPException(404)
    try:
        return pipeline.merge_items(bid, body.item_ids)
    except Exception as e:
        return _err(e, 400)


@app.delete("/api/item/{iid}")
def del_item(iid: int):
    """删掉一条记录（合影里多认出来的那条、或者老师不想要的那条）。"""
    try:
        return pipeline.delete_item(iid)
    except Exception as e:
        return _err(e, 400)


@app.post("/api/item/{iid}/photos")
async def item_add_photos(iid: int, files: list[UploadFile] = File(...)):
    """给这条记录追加照片：同一个孩子的另一张，或者他的作品、画、搭出来的东西。"""
    try:
        data = [(f.filename, await f.read()) for f in files]
        return pipeline.add_photos(iid, data)
    except Exception as e:
        return _err(e, 400)


@app.delete("/api/item/{iid}/photo")
def item_del_photo(iid: int, idx: int):
    try:
        return pipeline.del_photo(iid, idx)
    except Exception as e:
        return _err(e, 400)


class LinkBody(BaseModel):
    idx: int = 0
    targets: list = []
    with_students: bool = False


@app.post("/api/item/{iid}/photo/link")
def item_link_photo(iid: int, body: LinkBody):
    """把这条记录的某张照片，同时挂到这一批里别的几条记录上（不复制文件）。"""
    try:
        return pipeline.link_photo(iid, body.idx, body.targets, body.with_students)
    except Exception as e:
        return _err(e, 400)


@app.post("/api/item/{iid}/split")
def item_split(iid: int):
    """认出几个孩子就拆成几条，一人一条，照片共用。"""
    try:
        return pipeline.split_item(iid)
    except Exception as e:
        return _err(e, 400)


@app.get("/api/item/{iid}/photos")
def item_photos(iid: int):
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    return {"paths": pipeline.item_paths(it)}


class StuIndBody(BaseModel):
    student_id: int
    picks: object = None          # None = 跟整条一样


@app.post("/api/item/{iid}/student_indicators")
def item_stu_indicators(iid: int, body: StuIndBody):
    """给这条记录里的某一个孩子单独设指标（一张合影里几个孩子各评各的）。"""
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    try:
        return pipeline.set_student_indicators(iid, body.student_id,
                                               body.picks if body.picks is not None else None,
                                               _level_of_item(it))
    except Exception as e:
        return _err(e, 400)


class FaceBody(BaseModel):
    face_idx: int
    student_id: Optional[int] = None


@app.post("/api/item/{iid}/face_assign")
def item_face_assign(iid: int, body: FaceBody):
    """待指认的那张脸，老师点了名字（或者点了"不是本班的"）。"""
    try:
        if body.student_id:
            return pipeline.assign_face(iid, body.face_idx, body.student_id)
        return pipeline.drop_face(iid, body.face_idx)
    except Exception as e:
        return _err(e, 400)


@app.get("/api/item/{iid}/face")
def item_face_crop(iid: int, idx: int = 0, kind: str = "stu"):
    """把某一张脸裁出来给老师看。kind=stu 按孩子顺序，kind=pending 按待指认的框。"""
    import io as _io
    import json as _j
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    box = None
    if kind == "pending":
        for f in pipeline.pending_faces(it):
            if int(f.get("idx", -1)) == idx:
                box = f.get("box")
                break
    else:
        bs = pipeline.item_boxes(it)
        if 0 <= idx < len(bs):
            box = bs[idx]
    if not box:
        try:
            box = _j.loads(it.get("box") or "null")
        except Exception:
            box = None
    if not box or not box.get("w"):
        return FileResponse(it["path"])
    from PIL import Image
    from fastapi.responses import Response
    im = Image.open(it["path"]).convert("RGB")
    W, H = im.size
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    mx, my = int(w * 0.7), int(h * 0.7)
    im = im.crop((max(0, x - mx), max(0, y - my), min(W, x + w + mx), min(H, y + h + my)))
    if max(im.size) > 360:
        im.thumbnail((360, 360))
    buf = _io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg")


# ---------- 风格参考文件 ----------
@app.get("/api/style/samples")
def style_samples():
    import style
    return [{"name": x.get("name"), "n": len(x.get("chunks") or []), "chars": x.get("chars", 0)}
            for x in style.extra()]


@app.post("/api/style/samples")
async def style_samples_add(files: list[UploadFile] = File(...)):
    """老师上传自己写过的东西（txt / md / docx），当作风格参考。"""
    import style
    ok, errs = [], []
    for f in files:
        try:
            txt = style.extract_text(f.filename, await f.read())
            ok.append(style.add_extra(f.filename, txt))
        except Exception as e:
            errs.append(f"{f.filename}：{e}")
    return {"added": ok, "errors": errs}


@app.delete("/api/style/samples")
def style_samples_del(name: str = ""):
    import style
    if name:
        style.drop_extra(name)
    else:
        style.clear_extra()
    return {"ok": True}


@app.get("/api/style")
def style_get():
    import style
    return style.profile()


class StyleBody(BaseModel):
    on: Optional[bool] = None
    card: Optional[dict] = None


@app.post("/api/style")
def style_set(body: StyleBody):
    import style
    p = style.profile()
    if body.on is not None:
        p["on"] = bool(body.on)
    if body.card is not None:
        p["card"] = body.card
    style.save_profile(p)
    return p


@app.post("/api/class/{class_id}/style/learn")
def style_learn(class_id: int, use_ysk: bool = True):
    """让智谱模型读一遍这位老师过去写的记录（外加她自己传的参考文本），总结出她的口吻。"""
    import style
    uid = None
    if use_ysk:
        try:
            uid = (_ysk.check_login() or {}).get("id")
        except NotLoggedIn as e:
            if not style.extra():
                return _err(e, 401)
            use_ysk = False               # 没登录但传了文件，就只学文件
    _run_job(f"style_{class_id}",
             lambda prog: style.learn(class_id, _ysk if use_ysk else None, uid, progress=prog, use_ysk=use_ysk))
    return {"job": f"style_{class_id}"}


@app.delete("/api/batch/{bid}/_old")
def del_batch_old(bid: int):
    return del_batch(bid)


class ItemUpdate(BaseModel):
    text: Optional[str] = None
    behavior_desc: Optional[str] = None
    teacher_note: Optional[str] = None
    approved: Optional[bool] = None
    student_id: Optional[int] = None
    indicator_id: Optional[int] = None
    behavior_line: Optional[str] = None


def _level_of_item(it: dict) -> int:
    b = db.one("SELECT class_id FROM batches WHERE id=?", (it["batch_id"],))
    c = db.one("SELECT level FROM classes WHERE class_id=?", (b["class_id"],))
    return c["level"] if c else 5


@app.patch("/api/item/{iid}")
def update_item(iid: int, body: ItemUpdate):
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    if it.get("publish_status") in ("publishing", "uncertain"):
        raise HTTPException(409, "发布结果尚未确认，暂时不能修改或审核")
    level = _level_of_item(it)
    fields = {k: v for k, v in body.model_dump().items() if v is not None and k not in ("student_id", "indicator_id", "behavior_line")}
    if "text" in fields:
        fields["approved"] = 0       # 改过正文必须重新审核；指标保持原样。
    elif "approved" in fields:
        fields["approved"] = int(fields["approved"])
    if fields:
        db.update_item(iid, **fields)
    if body.student_id and body.student_id != it["student_id"]:
        pipeline.set_student(iid, body.student_id, level)
    if body.indicator_id and (body.indicator_id != it["indicator_id"] or body.behavior_line):
        pipeline.set_indicator(iid, body.indicator_id, level, body.behavior_line)
    return db.one("SELECT * FROM items WHERE id=?", (iid,))


class IndicatorsBody(BaseModel):
    picks: list = []


@app.post("/api/item/{iid}/indicators")
def item_indicators(iid: int, body: IndicatorsBody):
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    return pipeline.set_indicators(iid, body.picks, _level_of_item(it))


class ModulesBody(BaseModel):
    modules: list = []


@app.post("/api/item/{iid}/modules")
def item_modules(iid: int, body: ModulesBody):
    if not db.one("SELECT 1 FROM items WHERE id=?", (iid,)):
        raise HTTPException(404)
    return pipeline.set_modules(iid, body.modules)


class OptionsBody(BaseModel):
    sync_parent: Optional[bool] = None
    record_date: Optional[str] = None


@app.post("/api/item/{iid}/options")
def item_options(iid: int, body: OptionsBody):
    if not db.one("SELECT 1 FROM items WHERE id=?", (iid,)):
        raise HTTPException(404)
    return pipeline.set_options(iid, body.sync_parent, body.record_date)


@app.post("/api/item/{iid}/generate")
def item_generate(iid: int, speed: str = "", effort: str = ""):
    """speed: fast=写得快 / deep=想得细 / 空=沿用这条上次的、再不然就是默认。"""
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    if speed in ("fast", "deep"):
        db.set_setting("ai_mode", speed)        # 记住老师最近一次的选择，下一条默认跟着
    try:
        pipeline.generate(iid, _level_of_item(it), speed=speed, effort=effort)
    except Exception as e:
        return _err(e, 400)
    return {"ok": True}


@app.post("/api/batch/{bid}/generate")
def batch_generate(bid: int, only_missing: bool = True, speed: str = "", effort: str = ""):
    b = db.one("SELECT * FROM batches WHERE id=?", (bid,))
    if not b:
        raise HTTPException(404)
    if speed in ("fast", "deep"):
        db.set_setting("ai_mode", speed)
    c = db.one("SELECT level FROM classes WHERE class_id=?", (b["class_id"],))
    try:
        n = pipeline.generate_batch(bid, c["level"] if c else 5, only_missing, speed=speed, effort=effort)
    except ValueError as e:
        return _err(e, 400)
    return {"started": n}


@app.get("/api/table/field-options")
def table_field_options(field_type: str):
    if field_type not in ("SCENE_MULTI_SELECT", "ACTIVITY_MULTI_SELECT"):
        raise HTTPException(400, "不支持的观察记录表选项类型")
    try:
        _ysk.check_login()
        return {"groups": _ysk.get_table_field_options(field_type)}
    except NotLoggedIn as e:
        return _err(e, 401)
    except Exception as e:
        return _err(e, 502)


class TableBody(BaseModel):
    table: dict


@app.post("/api/item/{iid}/table")
def item_table(iid: int, body: TableBody):
    import json as _j
    import record_table
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    if it.get("publish_status") in ("done", "publishing", "uncertain"):
        raise HTTPException(409, "已发布、正在发布或结果待核对的表格不能修改")
    try:
        t = record_table.normalize(body.table)
    except ValueError as e:
        raise HTTPException(400, str(e))
    raw_saved = {}
    try:
        raw_saved = _j.loads(it.get("table_json") or "{}")
        saved = record_table.normalize(raw_saved)
    except (TypeError, ValueError):
        saved = None
    if isinstance(raw_saved, dict) and record_table.without_analysis_notice(raw_saved).get('情境补写') is True:
        t['情境补写'] = True
    if saved == t and _j.loads(it.get("table_json") or "{}") == t:
        return it
    db.update_item(iid, table_json=_j.dumps(t, ensure_ascii=False), text=record_table.to_text(t),
                   approved=it.get("approved", 0) if saved == t else 0)
    return db.one("SELECT * FROM items WHERE id=?", (iid,))


class RewriteBody(BaseModel):
    effort: str = ""
    key: str
    want: str = ""


@app.post("/api/item/{iid}/table/rewrite")
def item_table_rewrite(iid: int, body: RewriteBody):
    """只重写记录表里仍保留的某一段，包括自定义段落。

       want 是老师顺手写的一句要求，可以不写。这里是同步的 ——
       老师点了就等这一段回来，比整份重跑快得多。"""
    if not db.one("SELECT 1 FROM items WHERE id=?", (iid,)):
        raise HTTPException(404)
    try:
        t = pipeline.rewrite_table_field(iid, body.key, body.want or "", effort=body.effort)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"table": t, "item": db.one("SELECT * FROM items WHERE id=?", (iid,))}


@app.get("/api/batch/{bid}/export")
def export_batch(bid: int, ids: str = ""):
    """导成 Word，直接存到老师找得到的文件夹里，把文件名和位置告诉前台。

       坑记：这是装在电脑上的程序，不是网页。以前让浏览器「下载」，
       在 pywebview 里点了经常什么都不发生，老师就以为按钮是坏的。"""
    import export_doc
    import plat
    only = [x for x in (ids or "").replace("，", ",").split(",") if x.strip().isdigit()]
    try:
        out = export_doc.batch_to_docx(bid, only_ids=only or None)
    except Exception as e:
        return _err(e, 400)
    d = Path(out).parent
    return {"ok": True, "path": str(out), "name": Path(out).name,
            "dir": str(d), "dir_cn": _folder_cn(d)}


def _folder_cn(d: Path) -> str:
    """说人话的位置：「桌面 → 幼儿观察助手·导出」。"""
    import plat
    name = d.name
    par = d.parent
    home = Path.home()
    tag = ""
    try:
        if par.name in ("Desktop", "桌面"):
            tag = "桌面"
        elif par.name in ("Documents", "文档"):
            tag = "文档"
        elif par == home:
            tag = "个人文件夹"
    except Exception:
        pass
    return f"{tag} → {name}" if tag else str(d)


class OpenBody(BaseModel):
    path: str = ""            # 空的就只开文件夹


@app.post("/api/open-export")
def open_export(body: OpenBody):
    """帮老师把导出文件夹打开。只认导出目录里的东西，别的一律不开。"""
    import plat
    root = plat.export_root().resolve()
    target = root
    if body.path:
        try:
            cand = Path(body.path).resolve()
            cand.relative_to(root)
            if cand.exists():
                target = cand
        except Exception:
            target = root
    try:
        if target.is_file():
            plat.reveal_path(target)
        else:
            plat.open_path(target)
    except Exception as e:
        return _err(e, 400)
    return {"ok": True, "dir": str(root), "dir_cn": _folder_cn(root)}


@app.get("/api/export-dir")
def export_dir():
    import plat
    d = plat.export_root()
    return {"dir": str(d), "dir_cn": _folder_cn(d)}


class RegenBody(BaseModel):
    effort: str = ""
    indicator_id: Optional[int] = None
    note: Optional[str] = None
    text_only: bool = False
    speed: str = ""            # 换个档重写这一条


@app.post("/api/item/{iid}/regenerate")
def regen(iid: int, body: RegenBody):
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    if body.speed in ("fast", "deep"):
        db.set_setting("ai_mode", body.speed)
    pipeline.regenerate(iid, _level_of_item(it), body.indicator_id, body.note,
                        body.text_only, speed=body.speed, effort=body.effort)
    return {"ok": True}


@app.get("/api/item/{iid}/image")
def item_image(iid: int, idx: int = 0):
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404)
    ps = pipeline.item_paths(it)
    if not ps:
        raise HTTPException(404)
    return FileResponse(ps[idx] if 0 <= idx < len(ps) else ps[0])


class PublishBody(BaseModel):
    item_ids: list[int]


@app.post("/api/batch/{bid}/publish")
def publish(bid: int, body: PublishBody):
    try:
        _ysk.check_login()
        _require_batch_class(bid)
    except NotLoggedIn as e:
        return _err(e, 401)
    _run_job(f"publish_{bid}", lambda prog: pipeline.publish(bid, body.item_ids, _ysk, prog))
    return {"job": f"publish_{bid}"}


@app.post("/api/item/{iid}/unpublish")
def unpublish(iid: int):
    try:
        _ysk.check_login()
        it = db.one('SELECT batch_id FROM items WHERE id=?', (iid,))
        if not it:
            raise HTTPException(404, '记录不存在')
        _require_batch_class(it['batch_id'])
        pipeline.unpublish(iid, _ysk)
    except NotLoggedIn as e:
        return _err(e, 401)
    except Exception as e:
        return _err(e, 502)
    return {"ok": True}


def _require_batch_class(bid):
    batch = db.one('SELECT class_id FROM batches WHERE id=?', (bid,))
    if not batch:
        raise HTTPException(404, '批次不存在')
    cid = batch['class_id']
    if str(db.get_setting('current_class') or '') != str(cid):
        raise HTTPException(409, '请先选择这份记录所属的班级，再核对发布或撤回')
    if not any(str(c['classId']) == str(cid) for c in _ysk.my_classes()):
        raise HTTPException(403, '当前账号不属于这份记录的班级')


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
