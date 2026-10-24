"""Authorize local classroom objects against the current platform account."""
import json
import os
from pathlib import Path
import re
import threading
import time

from fastapi import HTTPException
import db
import session_state
from ysk_client import NotLoggedIn

_lock = threading.RLock()
_cache = None


def authorized_classes(client):
    """Short-lived, session-bound permission snapshot; never reuse another account's list."""
    global _cache
    identity = (client, session_state.generation, str(db.get_setting('current_uid') or ''))
    with _lock:
        if _cache and _cache[0] == identity and time.monotonic() < _cache[1]:
            return _cache[2]
        try:
            classes = client.my_classes()
        except NotLoggedIn as exc:
            raise HTTPException(401, '幼师口袋登录已失效，请重新登录。') from exc
        except Exception as exc:
            raise HTTPException(502, '暂时无法核对班级权限，请稍后重试。') from exc
        allowed = frozenset(int(c['classId']) for c in classes if c.get('classId'))
        _cache = (identity, time.monotonic() + 60, allowed)
        return allowed


def _identity(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r'[0-9]+', str(value)) or int(value) < 1:
        raise HTTPException(400, '资料编号格式不正确。')
    return int(value)


def _list(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 1000:
        raise HTTPException(400, '请选择有效的资料列表。')
    return value


def require_class(class_id, client):
    class_id = _identity(class_id)
    if class_id not in authorized_classes(client):
        raise HTTPException(403, '当前账号无权访问这份班级资料。')
    return class_id


_OBJECT_SQL = {
    'batch': 'SELECT class_id FROM batches WHERE id=?',
    'item': 'SELECT b.class_id FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?',
    'student': 'SELECT class_id FROM students WHERE student_id=?',
    'photo': 'SELECT s.class_id FROM photos p JOIN students s ON s.student_id=p.student_id WHERE p.id=?',
    'candidate': 'SELECT s.class_id FROM candidates p JOIN students s ON s.student_id=p.student_id WHERE p.id=?',
    'trash': 'SELECT class_id FROM local_trash WHERE id=?',
}


def object_class(kind, identity):
    row = db.one(_OBJECT_SQL[kind], (_identity(identity),))
    if not row:
        raise HTTPException(404, '资料不存在。')
    return row['class_id']


def require_student(student_id, class_id, client):
    actual = require_class(object_class('student', student_id), client)
    if class_id is not None and actual != class_id:
        raise HTTPException(403, '请选择这份记录所属班级的幼儿。')


def pending_path(path):
    import roster
    if not isinstance(path, (str, Path)):
        raise HTTPException(400, '照片路径格式不正确。')
    target = Path(path).resolve()
    if target.parent != roster.pending_root().resolve() or target.suffix.lower() != '.jpg':
        raise HTTPException(403, '无权访问这张待指认照片。')
    if not target.is_file():
        raise HTTPException(404, '待指认照片不存在。')
    return target


def media_path(path, client):
    """Never expose config, database, or arbitrary data-directory files via image preview."""
    import roster
    target = Path(path).resolve()
    if target.parent == roster.pending_root().resolve():
        return pending_path(target)
    if target.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.heic'):
        raise HTTPException(403, '仅能预览当前账号的已登记照片。')
    wanted = os.path.normcase(str(target))
    allowed = authorized_classes(client)
    rows = db.rows('SELECT p.path,s.class_id FROM photos p JOIN students s ON s.student_id=p.student_id')
    rows += db.rows('SELECT p.path,s.class_id FROM candidates p JOIN students s ON s.student_id=p.student_id')
    for row in rows:
        if row['class_id'] in allowed and os.path.normcase(str(Path(row['path']).resolve())) == wanted:
            return target
    for row in db.rows('SELECT i.path,i.paths,b.class_id FROM items i JOIN batches b ON b.id=i.batch_id'):
        if row['class_id'] not in allowed:
            continue
        paths = [row['path']]
        try:
            extra = json.loads(row['paths'] or '[]')
            if isinstance(extra, list):
                paths += [p for p in extra if isinstance(p, str)]
        except (TypeError, ValueError):
            pass
        if any(p and os.path.normcase(str(Path(p).resolve())) == wanted for p in paths):
            return target
    raise HTTPException(403, '当前账号无权预览这张照片。')


def authorize(path, query, body, client):
    """Validate IDs before handlers perform any mutation, including JSON secondary IDs."""
    # Match every path spelling before validating it: FastAPI also accepts +3,
    # 3.0 and whitespace as integers, which must not skip this authorization.
    match = re.match(r'^/api/(class|student|photo|candidate|batch|item|trash)/([^/]+)(?:/|$)', path)
    kind, identity, class_id = None, None, None
    if match:
        kind, identity = match[1], _identity(match[2])
        class_id = require_class(identity if kind == 'class' else object_class(kind, identity), client)
    if path in ('/api/batches', '/api/trash') and query.get('class_id') is not None:
        require_class(query['class_id'], client)
    if path == '/api/file' and query.get('path'):
        media_path(query['path'], client)
    if path == '/api/pending' and query.get('temp'):
        pending_path(query['temp'])
    if not isinstance(body, dict):
        return
    if path == '/api/brief/parse':
        require_class(body.get('class_id') or db.get_setting('current_class'), client)
    if path == '/api/assign':
        require_student(body.get('student_id'), None, client)
        pending_path(body.get('temp') or '')
    if kind in ('item', 'photo'):
        if body.get('student_id') is not None:
            require_student(body['student_id'], class_id, client)
        for student_id in _list(body.get('student_ids')):
            require_student(student_id, class_id, client)
    if kind == 'class' and path.endswith('/nicknames/bulk'):
        for child in _list(body.get('items')):
            if isinstance(child, dict) and child.get('student_id'):
                require_student(child['student_id'], class_id, client)
    if kind == 'batch' and path.endswith('/bulk'):
        if body.get('action') == 'students_add':
            value = body.get('value')
            for student_id in _list(value if isinstance(value, list) else [value]):
                require_student(student_id, class_id, client)
        elif body.get('action') == 'copy_indicators':
            source_class = require_class(object_class('item', body.get('value')), client)
            if source_class != class_id:
                raise HTTPException(403, '只能复制本班记录的指标。')
    # IDs in bulk and link payloads must all belong to the route's batch.
    targets = body.get('item_ids') if kind == 'batch' else body.get('targets') if kind == 'item' else None
    if targets:
        batch_id = identity if kind == 'batch' else db.one('SELECT batch_id FROM items WHERE id=?', (identity,))['batch_id']
        for item_id in _list(targets):
            row = db.one('SELECT batch_id FROM items WHERE id=?', (_identity(item_id),))
            if not row or row['batch_id'] != batch_id:
                raise HTTPException(403, '只能操作这份批次内的记录。')
