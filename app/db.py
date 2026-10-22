"""本地 SQLite：花名册、照片、人脸入库状态、观察批次、发布记录、设置。"""
import json
import sqlite3
import threading
import time
import shutil
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterable, Optional

from config import DB_PATH

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS classes (
    class_id INTEGER PRIMARY KEY,
    kid INTEGER, name TEXT, class_type TEXT, level INTEGER,
    face_group_id TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS students (
    student_id INTEGER PRIMARY KEY,
    class_id INTEGER, child_id INTEGER, name TEXT,
    avatar TEXT, grow_avatar TEXT,
    face_person_id TEXT, face_count INTEGER DEFAULT 0,
    nickname TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER, path TEXT, sha1 TEXT UNIQUE,
    face_id TEXT, enrolled INTEGER DEFAULT 0, quality REAL, grade TEXT,
    error TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER, path TEXT, sha1 TEXT UNIQUE,
    score REAL, grade TEXT, src TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER, note TEXT, status TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER, path TEXT, sha1 TEXT,
    student_id INTEGER, student_name TEXT, face_score REAL, face_status TEXT,
    teacher_note TEXT,
    indicator_id INTEGER, indicator_name TEXT, level INTEGER, behavior_line TEXT,
    behavior_desc TEXT, text TEXT, ai_status TEXT, ai_error TEXT,
    indicators TEXT, modules TEXT, sync_parent INTEGER DEFAULT 1, record_date TEXT, medal_ids TEXT,
    table_json TEXT,
    box TEXT, paths TEXT, group_key TEXT, face_idx INTEGER DEFAULT 0, student_ids TEXT,
    boxes TEXT, pending_faces TEXT, stu_indicators TEXT,
    approved INTEGER DEFAULT 0,
    publish_status TEXT, record_id INTEGER, record_type TEXT, medal_id_remote INTEGER, publish_error TEXT,
    created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_items_batch ON items(batch_id);
CREATE TABLE IF NOT EXISTS local_trash (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER, batch_id INTEGER, kind TEXT,
    payload TEXT NOT NULL, deleted_at REAL NOT NULL, restored_at REAL
);
"""


def init():
    preserve_before_upgrade()
    with conn() as c:
        c.executescript(SCHEMA)
        # 老版本数据库的补列（新装的库里 SCHEMA 已经有了，这里会报错忽略）
        for ddl in ("ALTER TABLE photos ADD COLUMN grade TEXT",
                    "ALTER TABLE students ADD COLUMN nickname TEXT",
                    "ALTER TABLE items ADD COLUMN indicators TEXT",
                    "ALTER TABLE items ADD COLUMN modules TEXT",
                    "ALTER TABLE items ADD COLUMN sync_parent INTEGER DEFAULT 1",
                    "ALTER TABLE items ADD COLUMN record_date TEXT",
                    "ALTER TABLE batches ADD COLUMN record_date TEXT",
                    "ALTER TABLE items ADD COLUMN medal_ids TEXT",
                    "ALTER TABLE items ADD COLUMN table_json TEXT",
                    "ALTER TABLE items ADD COLUMN record_type TEXT",
                    "ALTER TABLE batches ADD COLUMN mode TEXT",
                    "ALTER TABLE items ADD COLUMN box TEXT",
                    "ALTER TABLE items ADD COLUMN paths TEXT",
                    "ALTER TABLE items ADD COLUMN group_key TEXT",
                    "ALTER TABLE items ADD COLUMN face_idx INTEGER DEFAULT 0",
                    "ALTER TABLE items ADD COLUMN ai_speed TEXT",
                    "ALTER TABLE batches ADD COLUMN length TEXT",
                    "ALTER TABLE items ADD COLUMN student_ids TEXT",
                    "ALTER TABLE items ADD COLUMN boxes TEXT",
                    "ALTER TABLE items ADD COLUMN pending_faces TEXT",
                    "ALTER TABLE items ADD COLUMN stu_indicators TEXT",
                    "ALTER TABLE students ADD COLUMN cloud_faces INTEGER DEFAULT 0",
                    "ALTER TABLE students ADD COLUMN cloud_at REAL",
                    "ALTER TABLE batches ADD COLUMN brief TEXT"):
            try:
                c.execute(ddl)
            except Exception:
                pass
        # 旧版未识别 NURSERY 时误归中班；仅修正班级元数据，不改历史正文与发布状态。
        c.execute("UPDATE classes SET level=128 WHERE UPPER(class_type)='NURSERY' AND (level IS NULL OR level<>128)")


def preserve_before_upgrade():
    """首次升级前保留原始字节（含空闲页），不能用 SQLite backup/VACUUM 替代。"""
    path = Path(DB_PATH)
    dest = path.parent / "recovery-before-1.4.4"
    if not path.exists() or (dest / "complete.json").exists():
        return
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        source = Path(str(path) + suffix)
        if source.exists():
            # 重试时也不覆盖首次保存的原始副本。
            target = dest / source.name
            if not target.exists():
                pending = dest / (source.name + ".copying")
                shutil.copy2(source, pending)
                pending.replace(target)
            saved.append(target.name)
    (dest / "complete.json").write_text(json.dumps({"files": saved, "saved_at": time.time(),
        "note": "升级前原始文件副本，仅用于恢复排查，不保证含有旧版已删除记录。"}, ensure_ascii=False), encoding="utf-8")


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def rows(sql: str, args: Iterable = ()) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, tuple(args)).fetchall()]


def one(sql: str, args: Iterable = ()) -> Optional[dict]:
    r = rows(sql, args)
    return r[0] if r else None


def exec(sql: str, args: Iterable = ()) -> int:
    with conn() as c:
        cur = c.execute(sql, tuple(args))
        return cur.lastrowid


def get_setting(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any):
    exec("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


def upsert_class(class_id: int, kid: int, name: str, class_type: str, level: int):
    exec(
        """INSERT INTO classes(class_id,kid,name,class_type,level,updated_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(class_id) DO UPDATE SET kid=excluded.kid,name=excluded.name,
           class_type=excluded.class_type,level=excluded.level,updated_at=excluded.updated_at""",
        (class_id, kid, name, class_type, level, time.time()),
    )


def upsert_student(s: dict):
    exec(
        """INSERT INTO students(student_id,class_id,child_id,name,avatar,grow_avatar,updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(student_id) DO UPDATE SET class_id=excluded.class_id,child_id=excluded.child_id,
           name=excluded.name,avatar=excluded.avatar,grow_avatar=excluded.grow_avatar,updated_at=excluded.updated_at""",
        (s["id"], s["classId"], s.get("childId"), s["name"], s.get("avatar"), s.get("growAvatar"), time.time()),
    )


def set_nickname(student_id: int, nickname: str):
    exec("UPDATE students SET nickname=? WHERE student_id=?", ((nickname or "").strip() or None, student_id))


def students_of(class_id: int) -> list[dict]:
    return rows(
        """SELECT s.*, (SELECT COUNT(*) FROM photos p WHERE p.student_id=s.student_id) AS photo_count,
                  (SELECT COUNT(*) FROM photos p WHERE p.student_id=s.student_id AND p.enrolled=1) AS enrolled_count
           FROM students s WHERE class_id=? ORDER BY name""",
        (class_id,),
    )


def update_item(item_id: int, **fields):
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    exec(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))
