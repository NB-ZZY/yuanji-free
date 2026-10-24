"""观察批次流水线：照片 → 认脸 → AI 写作 → 老师审核 → 一键发布。

所有步骤都在后台线程里跑，前端轮询 /api/batch/{id} 看进度。
"""
import hashlib
import json
import logging
import threading
import time
from session_state import AccountExecutor as ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import db
import face_client
import ai_writer
import record_table
import ai_progress
from config import UPLOAD_DIR
from datetime import date
from knowledge import (GRADE_ID, LEVEL_GRADE_ID, NURSERY_LEVELS, indicator_by_id,
                       indicator_lines, load_indicators, requested_indicators, modules_for_level)
from roster import normalize_image
from ysk_client import YskClient, TablePublishUncertain

log = logging.getLogger("pipeline")
MAX_FACES_PER_PHOTO = 6      # 一张合影最多拆出 6 个孩子
MAX_PHOTOS_PER_ITEM = 6      # 一条记录最多挂 6 张照片
# AI 写作。原来是 3，二十几条要排七八轮；GLM-5.3 强制思考，每条本身就慢，
# 排队的时间比写的时间还长。开到 6，撞上限流有退避兜着（见 ai_writer._chat_full）。
_pool = ThreadPoolExecutor(max_workers=6)
_face_pool = ThreadPoolExecutor(max_workers=4)  # 认脸：单独一个池，不会被写作占满
_aux_pool = ThreadPoolExecutor(max_workers=8)   # 写作过程里派出去的小活（比如一边写实录一边挑指标）

_face_inflight = {}
_face_lock = threading.RLock()


def _queue_recognition(it: dict, class_id: int, students: dict, initial: bool = False):
    """Share one queued/running recognition per item, including initial batches."""
    iid = it['id']
    with _face_lock:
        existing = _face_inflight.get(iid)
        if existing is not None:
            return existing, False
        it = db.one('SELECT * FROM items WHERE id=?', (iid,))
        if not it:
            raise ValueError('这张照片不在了')
        # A manual scan may finish before its delayed initial coordinator runs.
        if initial and it.get('face_status') not in (None, 'pending'):
            return None, False
        previous = {key: it.get(key) for key in ('face_status', 'ai_error')}
        db.update_item(iid, face_status='pending', ai_error=None)
        try:
            future = _face_pool.submit(_recognize, it, class_id, students)
        except BaseException:
            db.update_item(iid, **previous)
            raise
        _face_inflight[iid] = future

        def finished(done):
            # Futures also invoke callbacks on failure or cancellation. Do not use
            # persisted pending as a lock: a restarted process must allow retries.
            with _face_lock:
                if _face_inflight.get(iid) is done:
                    del _face_inflight[iid]

        future.add_done_callback(finished)
        return future, True


def create_batch(class_id: int, files: list[tuple[str, bytes]], note: str = "", record_date: str = "",
                 mode: str = "simple", length: str = "normal", brief: dict = None) -> int:
    """files: [(原文件名, 字节)]。保存照片、建 items，并在后台认脸。
       注意：认完脸**不会**自动写文字 —— 由老师在页面上点「AI 生成」才写。"""
    record_date = record_date or date.today().strftime("%Y-%m-%d")
    import lengths
    bid = db.exec("INSERT INTO batches(class_id,note,status,created_at,record_date,mode,length) VALUES(?,?,?,?,?,?,?)",
                  (class_id, note, "processing", time.time(), record_date, mode, lengths.norm(length)))
    d = UPLOAD_DIR / str(bid)
    d.mkdir(parents=True, exist_ok=True)
    for fname, data in files:
        nd = normalize_image(data, max_side=1600)
        h = hashlib.sha1(nd).hexdigest()
        p = d / f"{h[:12]}.jpg"
        p.write_bytes(nd)
        db.exec("""INSERT INTO items(batch_id,path,sha1,face_status,ai_status,teacher_note,
                       sync_parent,record_date,group_key,face_idx,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bid, str(p), h, "pending", "idle", note or "", 1, record_date, h, 0,
                 time.time(), time.time()))
    if brief and brief.get("ok"):
        # 老师那段话已经在上传前解析好了，先存着；认完脸再按孩子分下去
        import brief as brief_mod
        brief_mod.save(bid, brief)
    _aux_pool.submit(_process_batch, bid)
    return bid


def _process_batch(bid: int):
    try:
        items = db.rows("SELECT * FROM items WHERE batch_id=?", (bid,))
        batch = db.one("SELECT * FROM batches WHERE id=?", (bid,))
        cls = db.one("SELECT * FROM classes WHERE class_id=?", (batch["class_id"],))
        level = cls["level"] if cls else 5
        students = {s["student_id"]: s for s in db.students_of(batch["class_id"])}
        # 并行认脸：一张一张排队太慢，一张网络不好会拖住后面所有的
        futs = []
        for it in items:
            future, _ = _queue_recognition(it, batch["class_id"], students, initial=True)
            if future is not None:
                futs.append(future)
        for f in futs:
            try:
                f.result(timeout=120)
            except Exception as e:
                log.warning("认脸线程异常：%s", e)
        # 兜底：还卡在"识别中"的，直接标成失败，别让老师干等
        db.exec("""UPDATE items SET face_status='error', ai_error=COALESCE(ai_error,'识别超时，请点这张照片的「重新识别」')
                   WHERE batch_id=? AND (face_status IS NULL OR face_status='pending')""", (bid,))
        _apply_brief(bid, level)
        db.exec("UPDATE batches SET status='done' WHERE id=?", (bid,))
    except Exception as e:
        log.exception("批次处理失败")
        db.exec("UPDATE batches SET status=? WHERE id=?", (f"error: {e}", bid))


def _apply_brief(bid: int, level: int):
    """认完脸，才知道每张照片里是谁 —— 这时候再把老师那段话按孩子分下去。"""
    try:
        import brief as brief_mod
        parsed = brief_mod.load(bid)
        if parsed.get("ok"):
            r = brief_mod.apply_to_batch(bid, parsed, level)
            log.info("说明已分到 %d 条记录；没对上的场景 %d 个", r["touched"], len(r["left"]))
    except Exception as e:
        log.warning("分说明失败：%s", e)


def _recognize(it: dict, class_id: int, students: dict):
    """认脸。**一张照片就是一条记录** —— 照片里认出几个孩子，就全挂在这一条上，
       不再拆成好几条。认不出来的脸单独列在「待指认」里，等老师点一下是谁。"""
    try:
        if not face_client.enabled():
            db.update_item(it['id'], face_status='manual', face_score=0, pending_faces=None, ai_error=None)
            return
        data = Path(it["path"]).read_bytes()
        hits = face_client.search(class_id, data, max_faces=MAX_FACES_PER_PHOTO)
        if not hits:
            # 没脸 —— 多半是作品照（画、搭的东西、手工）。留着让老师指给某个孩子。
            db.update_item(it["id"], face_status="noface", face_score=0, pending_faces=None)
            return
        if hits[0].get("mock"):
            sid = next(iter(students)) if students else None
            db.update_item(it["id"], student_id=sid, student_ids=json.dumps([sid] if sid else []),
                           student_name=students[sid]["name"] if sid else None,
                           face_score=99, face_status="matched")
            return
        got, boxes, pending, taken = [], [], [], set()
        for n, hit in enumerate(hits):
            box = _norm_box(hit.get("face_rect"))
            sid = hit["student_id"] if (hit["matched"] and hit["student_id"] in students) else None
            if sid in taken:              # 同一个孩子被认成两张脸，第二张交给老师定夺
                sid = None
            if sid:
                taken.add(sid)
                got.append(sid)
                boxes.append(box)
            else:
                cand = hit.get("candidate_id")
                pending.append({"idx": n, "box": box, "score": hit["score"],
                                "candidate_id": cand if cand in students else None,
                                "candidate_name": students[cand]["name"] if cand in students else ""})
        names = "、".join(students[x]["name"] for x in got)
        db.update_item(it["id"],
                       student_ids=json.dumps(got), student_id=(got[0] if got else None),
                       student_name=names or None,
                       boxes=json.dumps(boxes, ensure_ascii=False),
                       box=json.dumps(boxes[0], ensure_ascii=False) if boxes else None,
                       pending_faces=json.dumps(pending, ensure_ascii=False) if pending else None,
                       face_score=hits[0]["score"],
                       face_status=("matched" if got and not pending else
                                    ("part" if got else "unknown")))
    except Exception as e:
        db.update_item(it["id"], face_status="error", ai_error=str(e))


# ---------- 一条记录挂的照片 ----------
def item_paths(it: dict) -> list:
    """这条记录用到的所有照片：第一张是主图，后面是老师自己加的
       （他的另一张、他做出来的作品）。文件没了的跳过，不让整条记录崩掉。"""
    out = []
    main = (it.get("path") or "").strip()
    if main:
        out.append(main)
    try:
        extra = json.loads(it.get("paths") or "[]")
    except Exception:
        extra = []
    if isinstance(extra, list):
        for x in extra:
            x = str(x or "").strip()
            if x and x not in out:
                out.append(x)
    alive = [x for x in out if Path(x).exists()]
    return alive or out[:1]


def add_photos(iid: int, files: list) -> dict:
    """给这条记录再加几张照片。AI 会把它们当成一件事一起看，合起来写一条。"""
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise RuntimeError("这条记录不在了")
    have = item_paths(it)
    room = MAX_PHOTOS_PER_ITEM - len(have)
    if room <= 0:
        raise RuntimeError(f"一条记录最多挂 {MAX_PHOTOS_PER_ITEM} 张照片，先去掉几张再加")
    d = UPLOAD_DIR / str(it["batch_id"])
    d.mkdir(parents=True, exist_ok=True)
    try:
        extra = json.loads(it.get("paths") or "[]")
        if not isinstance(extra, list):
            extra = []
    except Exception:
        extra = []
    added = 0
    for _fname, data in files[:room]:
        nd = normalize_image(data, max_side=1600)
        h = hashlib.sha1(nd).hexdigest()
        fp = d / f"{h[:12]}.jpg"
        if not fp.exists():
            fp.write_bytes(nd)
        sp = str(fp)
        if sp in have or sp in extra:      # 同一张传了两遍，不重复挂
            continue
        extra.append(sp)
        added += 1
    db.update_item(iid, paths=json.dumps(extra, ensure_ascii=False), updated_at=time.time())
    return {"ok": True, "added": added, "count": 1 + len(extra)}


def del_photo(iid: int, idx: int) -> dict:
    """去掉这条记录的第 idx 张照片（0 是主图，主图不能去掉 —— 整条删就是了）。"""
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise RuntimeError("这条记录不在了")
    if idx <= 0:
        raise RuntimeError("第一张是这条记录的主图，去不掉；不要这条的话点「删掉」")
    try:
        extra = json.loads(it.get("paths") or "[]")
        if not isinstance(extra, list):
            extra = []
    except Exception:
        extra = []
    k = idx - 1
    if not (0 <= k < len(extra)):
        raise RuntimeError("没有这张照片")
    gone = extra.pop(k)
    db.update_item(iid, paths=json.dumps(extra, ensure_ascii=False), updated_at=time.time())
    _drop_file_if_unused(gone)
    return {"ok": True, "count": 1 + len(extra)}


def _drop_file_if_unused(path: str):
    """照片文件只在没有任何一条记录还用着它的时候才真删 ——
       同一张图可能被别的记录挂着，删早了那边就白框了。"""
    path = str(path or "").strip()
    if not path:
        return
    like = f'%{json.dumps(path, ensure_ascii=False)[1:-1]}%'
    used = db.one("SELECT 1 AS x FROM items WHERE path=? OR paths LIKE ? LIMIT 1", (path, like))
    if used:
        return
    # 回收站中的照片也仍然被引用，移除其他记录的同一张照片不能毁掉备份。
    for entry in db.rows("SELECT payload FROM local_trash"):
        if any(path in item_paths(it) for it in json.loads(entry["payload"])["items"]):
            return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def delete_item(iid: int, ysk=None) -> dict:
    """仅移入本机回收站；保留 ysk 参数兼容旧调用，但绝不调用网站。"""
    return _move_to_trash(item_id=iid)


def merge_items(batch_id: int, item_ids: list) -> dict:
    """把选中的几条并成一条。

    为什么需要：认脸只能把「同一张照片里的人」合起来，但一次活动往往是
    好几张照片讲同一件事 —— 他动手的过程、半成品、最后的作品。作品照上没有脸，
    程序自己没法知道它们是一回事，只能交给老师点。

    规则：
      · 以老师选的第一条为主，其余几条的照片按顺序接在后面；
      · 孩子取并集（谁都不丢，重复的去掉）；
      · 指标、关联领域、老师说明也合并；
      · 并完把原文清成待写 —— 照片都变了，原来那段话已经不算数了。
    """
    ids = []
    for x in (item_ids or []):
        x = int(x)
        if x not in ids:
            ids.append(x)
    if len(ids) < 2:
        raise ValueError("要合并至少选两条")
    rows = {r["id"]: r for r in db.rows(
        f"SELECT * FROM items WHERE batch_id=? AND id IN ({','.join('?' * len(ids))})",
        (batch_id, *ids))}
    miss = [i for i in ids if i not in rows]
    if miss:
        raise ValueError("有几条不在这一批里，刷新一下再试")
    done = [i for i in ids if rows[i].get("publish_status") in ("done", "publishing", "uncertain")]
    if done:
        raise ValueError("已经发布的记录不能合并，先撤回发布")

    host_id = ids[0]
    host = rows[host_id]
    paths, kids, inds, mods, notes = [], [], [], [], []
    names = {}
    for i in ids:
        it = rows[i]
        for pth in item_paths(it):
            if pth not in paths:
                paths.append(pth)
        for st in (item_students(it) or []):        # 返回的是整行，取 id
            sid = int(st["student_id"])
            if sid not in kids:
                kids.append(sid)
                names[sid] = st.get("name") or ""
        for ind in (_load_indicators(it, it.get("level") or 5) or []):
            if ind not in inds:
                inds.append(ind)
        for m in (_load_modules(it) or []):
            if m not in mods:
                mods.append(m)
        nt = (it.get("teacher_note") or "").strip()
        if nt and nt not in notes:
            notes.append(nt)

    if len(paths) > MAX_PHOTOS_PER_ITEM:
        raise ValueError(f"合起来有 {len(paths)} 张照片，一条最多放 {MAX_PHOTOS_PER_ITEM} 张，少选几条")

    # 人脸框只对主图那张有意义，后面并进来的孩子没有框 —— 补 None，头像就退回整张图
    boxes = (item_boxes(host) or [])[:len(item_students(host) or [])]
    boxes = list(boxes) + [None] * max(0, len(kids) - len(boxes))

    db.update_item(host_id,
                   path=paths[0],
                   paths=json.dumps(paths[1:], ensure_ascii=False),
                   student_ids=json.dumps(kids, ensure_ascii=False),
                   student_id=(kids[0] if kids else None),
                   student_name=("、".join(names.get(k, "") for k in kids).strip("、") or None),
                   boxes=json.dumps(boxes, ensure_ascii=False),
                   indicators=json.dumps(inds, ensure_ascii=False) if inds else None,
                   modules=json.dumps(mods, ensure_ascii=False) if mods else None,
                   teacher_note="；".join(notes),
                   text=None, table_json=None, ai_status="idle", ai_error=None,
                   approved=0,
                   face_status=("matched" if kids else "noface"))
    gone = [i for i in ids if i != host_id]
    db.exec(f"DELETE FROM items WHERE id IN ({','.join('?' * len(gone))})", tuple(gone))
    return {"ok": True, "item_id": host_id, "merged": len(gone),
            "photos": len(paths), "students": len(kids)}


def link_photo(iid: int, idx: int, target_ids: list, with_students: bool = False) -> dict:
    """把这条记录的第 idx 张照片，**同时**挂到别的几条记录上。

    为什么需要：合并是把几条并成一条，但有时候正相反 —— 一张合影里三个孩子，
    每人要各写各的一条（各挑各的指标、各写各的评价），照片却是同一张。
    这里不复制文件，只是让几条记录指向同一张图；哪一条删掉了，只要还有别人用着，
    文件就不会被清掉（见 _drop_file_if_unused）。
    """
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise ValueError("这条记录不在了")
    paths = item_paths(it)
    if not (0 <= idx < len(paths)):
        raise ValueError("没有这张照片")
    src = paths[idx]

    ids = []
    for x in (target_ids or []):
        try:
            x = int(x)
        except Exception:
            continue
        if x != iid and x not in ids:
            ids.append(x)
    if not ids:
        raise ValueError("选一条要挂过去的记录")

    rows = {r["id"]: r for r in db.rows(
        f"SELECT * FROM items WHERE batch_id=? AND id IN ({','.join('?' * len(ids))})",
        (it["batch_id"], *ids))}
    miss = [i for i in ids if i not in rows]
    if miss:
        raise ValueError("有几条不在这一批里，刷新一下再试")
    done = [i for i in ids if rows[i].get("publish_status") == "done"]
    if done:
        raise ValueError("已经发布的记录不能改照片，先撤回发布")

    kids_here = [int(x["student_id"]) for x in (item_students(it) or [])]
    linked, full, already = [], [], []
    for i in ids:
        tgt = rows[i]
        have = item_paths(tgt)
        if src in have:
            already.append(i)
            continue
        if len(have) >= MAX_PHOTOS_PER_ITEM:
            full.append(i)
            continue
        try:
            extra = json.loads(tgt.get("paths") or "[]")
            if not isinstance(extra, list):
                extra = []
        except Exception:
            extra = []
        extra.append(src)
        fields = {"paths": json.dumps(extra, ensure_ascii=False), "updated_at": time.time()}
        if with_students and kids_here:
            # 顺带把这张照片里认出的孩子也加到那一条上（老师勾了才做）
            cur = [int(x["student_id"]) for x in (item_students(tgt) or [])]
            nms = {int(x["student_id"]): x.get("name") or "" for x in (item_students(tgt) or [])}
            for st in (item_students(it) or []):
                sid = int(st["student_id"])
                if sid not in cur and len(cur) < MAX_STUDENTS:
                    cur.append(sid)
                    nms[sid] = st.get("name") or ""
            fields.update(student_ids=json.dumps(cur, ensure_ascii=False),
                          student_id=(cur[0] if cur else None),
                          student_name=("、".join(nms.get(k, "") for k in cur).strip("、") or None))
        db.update_item(i, **fields)
        linked.append(i)
    return {"ok": True, "linked": linked, "full": full, "already": already, "path": src}


def split_item(iid: int) -> dict:
    """这条记录认出了几个孩子，就拆成几条，一人一条，照片几条共用。

    合影按「一张照片一条记录」挂在一起是默认做法，省事；但要给每个孩子
    各挑各的指标、各写各的评价时，就得拆开 —— 拆完照片还是那一张，不复制文件。
    """
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise ValueError("这条记录不在了")
    if it.get("publish_status") == "done":
        raise ValueError("已经发布的记录不能拆，先撤回发布")
    studs = item_students(it) or []
    if len(studs) < 2:
        raise ValueError("这条记录上只有一个孩子，不用拆")

    paths = item_paths(it)
    main, extra = paths[0], paths[1:]
    boxes = item_boxes(it) or []
    per = stu_indicators(it)
    base_inds = it.get("indicators")
    made = []
    for n, st in enumerate(studs):
        sid = int(st["student_id"])
        box = boxes[n] if n < len(boxes) else None
        inds = per.get(sid)
        inds_json = json.dumps(inds, ensure_ascii=False) if inds else base_inds
        if n == 0:
            db.update_item(iid, student_ids=json.dumps([sid]), student_id=sid,
                           student_name=st.get("name"),
                           boxes=json.dumps([box], ensure_ascii=False),
                           box=json.dumps(box, ensure_ascii=False) if box else None,
                           indicators=inds_json, stu_indicators=None,
                           text=None, table_json=None, ai_status="idle", ai_error=None,
                           approved=0, updated_at=time.time())
            made.append(iid)
            continue
        nid = db.exec("""INSERT INTO items(batch_id,path,sha1,student_id,student_name,face_score,face_status,
                             teacher_note,indicators,modules,sync_parent,record_date,
                             paths,group_key,face_idx,student_ids,boxes,box,approved,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (it["batch_id"], main, it.get("sha1"), sid, st.get("name"),
                       it.get("face_score"), "manual", it.get("teacher_note") or "",
                       inds_json, it.get("modules"), it.get("sync_parent") if it.get("sync_parent") is not None else 1,
                       it.get("record_date"),
                       json.dumps(extra, ensure_ascii=False), it.get("group_key"), n,
                       json.dumps([sid]), json.dumps([box], ensure_ascii=False),
                       json.dumps(box, ensure_ascii=False) if box else None,
                       0, time.time(), time.time()))
        made.append(nid)
    return {"ok": True, "items": made, "count": len(made)}


def set_batch_length(bid: int, length: str) -> dict:
    """改这一批的篇幅档位（简要／普通／详细）。改完点重新生成才会按新篇幅写。"""
    import lengths
    v = lengths.norm(length)
    db.exec("UPDATE batches SET length=? WHERE id=?", (v, bid))
    return {"ok": True, "length": v, "cn": lengths.cn(v)}


def pending_faces(it: dict) -> list:
    try:
        v = json.loads(it.get("pending_faces") or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def item_boxes(it: dict) -> list:
    try:
        v = json.loads(it.get("boxes") or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def assign_face(item_id: int, face_idx: int, student_id: int) -> dict:
    """老师给「待指认」的某张脸点了名字：把这个孩子加进这条记录。"""
    # 两张待确认脸可能接连点选；校验和保存使用同一次事务，避免覆盖已选孩子。
    with db.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError("这条记录不在了")
        it = dict(row)
        if it.get("publish_status") in ("done", "publishing", "withdrawing", "uncertain"):
            raise ValueError("这条记录已发布或正在发布，不能更改人物")
        batch = c.execute("SELECT class_id FROM batches WHERE id=?", (it["batch_id"],)).fetchone()
        st = c.execute("SELECT * FROM students WHERE student_id=?", (student_id,)).fetchone()
        if not st or not batch or st["class_id"] != batch["class_id"]:
            raise ValueError("请从当前班级名单中选择孩子")
        pend = pending_faces(it)
        face = next((f for f in pend if int(f.get("idx", -1)) == int(face_idx)), None)
        if face is None:
            raise ValueError("这张脸已处理或重新识别，请刷新后再选")
        students = item_students(it)
        ids = [s["student_id"] for s in students]
        if student_id in ids:
            raise ValueError("这条记录已有这个孩子，请为这张脸改选或点「不要」")
        if len(ids) >= MAX_STUDENTS:
            raise ValueError(f"一条记录最多评 {MAX_STUDENTS} 个孩子")
        boxes = item_boxes(it)
        # 兼容旧版仅保存主脸框的记录，保持人名与裁剪框的下标一致。
        if not boxes and ids:
            try:
                boxes = [json.loads(it.get("box") or "{}")]
            except (TypeError, ValueError):
                boxes = [{}]
        boxes = (boxes + [{} for _ in ids])[:len(ids)]
        ids.append(student_id)
        boxes.append(face.get("box") or {})
        pend = [f for f in pend if int(f.get("idx", -1)) != int(face_idx)]
        names = "、".join(s["name"] for s in [*students, st])
        c.execute("""UPDATE items SET student_ids=?,student_id=?,student_name=?,boxes=?,box=?,
                     pending_faces=?,face_status=?,approved=0,updated_at=? WHERE id=?""",
                  (json.dumps(ids), ids[0], names, json.dumps(boxes, ensure_ascii=False),
                   json.dumps(boxes[0], ensure_ascii=False),
                   json.dumps(pend, ensure_ascii=False) if pend else None,
                   "matched" if not pend else "part", time.time(), item_id))
        return dict(c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())


def drop_face(item_id: int, face_idx: int) -> dict:
    """这张脸不是本班的孩子（或者老师不想要），从待指认里划掉。"""
    it = db.one("SELECT * FROM items WHERE id=?", (item_id,))
    if not it:
        raise ValueError("这条记录不在了")
    pend = [f for f in pending_faces(it) if int(f.get("idx", -1)) != int(face_idx)]
    db.update_item(item_id, pending_faces=json.dumps(pend, ensure_ascii=False) if pend else None,
                   face_status="matched" if (not pend and it.get("student_id")) else it.get("face_status"))
    return db.one("SELECT * FROM items WHERE id=?", (item_id,))


def _norm_box(rect) -> dict:
    r = rect or {}
    return {"x": r.get("X", 0), "y": r.get("Y", 0), "w": r.get("Width", 0), "h": r.get("Height", 0)}


def rescan(item_id: int) -> dict:
    """老师点某一张的「重新识别」。"""
    it = db.one("SELECT * FROM items WHERE id=?", (item_id,))
    if not it:
        raise ValueError("这张照片不在了")
    b = db.one("SELECT class_id FROM batches WHERE id=?", (it["batch_id"],))
    students = {s["student_id"]: s for s in db.students_of(b["class_id"])}
    _, started = _queue_recognition(it, b["class_id"], students)
    return {"ok": True, "started": started}


def rescan_batch(batch_id: int) -> int:
    """整批里没认出来的重新认一遍。"""
    rows = db.rows("""SELECT * FROM items WHERE batch_id=?
                      AND (student_id IS NULL OR face_status IN ('error','noface','pending'))""", (batch_id,))
    b = db.one("SELECT class_id FROM batches WHERE id=?", (batch_id,))
    students = {s["student_id"]: s for s in db.students_of(b["class_id"])}
    return sum(_queue_recognition(it, b["class_id"], students)[1] for it in rows)


def clear_batch(batch_id: int, ysk=None) -> dict:
    """整批移入本机回收站。删除本地批次不等于撤回网站记录。"""
    return _move_to_trash(batch_id=batch_id)


def _move_to_trash(batch_id=None, item_id=None) -> dict:
    # 与发布的占用事务使用同一 DB 锁；先存副本再删原行，任一步失败全部回滚。
    with db.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if item_id is not None:
            row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise ValueError("这条记录不在了")
            items = [dict(row)]
            batch_id = row["batch_id"]
        else:
            items = [dict(r) for r in c.execute("SELECT * FROM items WHERE batch_id=?", (batch_id,))]
        batch = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ValueError("这个批次不在了")
        if any(it.get("publish_status") in ("publishing", "withdrawing", "uncertain") for it in items):
            raise ValueError("有记录正在发布、撤回或结果尚未确认，请先核对，暂时不能删除")
        if batch["status"] == "processing" or any(it.get("ai_status") == "writing" or it.get("face_status") == "pending" for it in items):
            raise ValueError("正在识别或生成，请完成后再删除")
        payload = json.dumps({"batch": dict(batch), "items": items}, ensure_ascii=False)
        tid = c.execute("INSERT INTO local_trash(class_id,batch_id,kind,payload,deleted_at) VALUES(?,?,?,?,?)",
                        (batch["class_id"], batch_id, "item" if item_id is not None else "batch", payload, time.time())).lastrowid
        if item_id is not None:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))
        else:
            c.execute("DELETE FROM items WHERE batch_id=?", (batch_id,))
            c.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    # 照片留在原处；恢复时无需搬运，且不会破坏合影/跨记录共享的文件。
    return {"ok": True, "deleted": len(items), "unpublished": 0, "trash_id": tid}


def list_trash(class_id: int) -> list:
    entries = db.rows("SELECT * FROM local_trash WHERE class_id=? AND restored_at IS NULL ORDER BY id DESC", (class_id,))
    for entry in entries:
        data = json.loads(entry.pop("payload"))
        entry.update(count=len(data["items"]), note=data["batch"].get("note") or "",
                     names="、".join(dict.fromkeys(it.get("student_name") or "未分配" for it in data["items"])))
    return entries


def restore_trash(trash_id: int) -> dict:
    """原样恢复本地数据及发布标记；绝不重新发布或重复打指标。"""
    with db.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM local_trash WHERE id=?", (trash_id,)).fetchone()
        if not row:
            raise ValueError("未找到可恢复的副本")
        if row["restored_at"] is not None:
            return {"ok": True, "batch_id": row["batch_id"], "already_restored": True}
        data = json.loads(row["payload"])
        batch, items = data["batch"], data["items"]
        def insert(table, values):
            c.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        existing = c.execute("SELECT * FROM batches WHERE id=?", (batch["id"],)).fetchone()
        if existing and (existing["class_id"] != batch["class_id"] or existing["created_at"] != batch["created_at"]):
            raise ValueError("批次编号冲突，已保留副本，请联系管理员处理")
        if any(c.execute("SELECT 1 FROM items WHERE id=?", (it["id"],)).fetchone() for it in items):
            raise ValueError("记录编号冲突，已保留副本，未覆盖任何现有记录")
        if not existing:
            insert("batches", batch)
        for it in items:
            insert("items", it)
        c.execute("UPDATE local_trash SET restored_at=? WHERE id=?", (time.time(), trash_id))
    missing = sum(not Path(p).is_file() for it in items for p in item_paths(it))
    return {"ok": True, "batch_id": batch["id"], "restored": len(items), "missing_photos": missing}


def peer_context(it: dict, limit: int = 4) -> tuple:
    """同批成稿都参与句式自查，未填说明或说明不同也不能套同一段话。"""
    rows = db.rows("SELECT id, text, ai_status FROM items WHERE batch_id=? ORDER BY id",
                   (it["batch_id"],))
    idx = next((i for i, r in enumerate(rows) if r["id"] == it["id"]), 0)
    peers = [(r.get("text") or "").strip() for r in rows
             if r["id"] != it["id"] and r.get("ai_status") != "error"
             and (r.get("text") or "").strip()]
    return peers[-limit:], idx


def _tier_of(it: dict) -> str:
    """这条记录该按哪个篇幅写 —— 跟着它所在批次的档位走（简要／普通／详细）。"""
    import lengths
    try:
        b = db.one("SELECT length FROM batches WHERE id=?", (it.get("batch_id"),)) or {}
    except Exception:
        b = {}
    return lengths.norm(b.get("length") or lengths.DEFAULT)


def _call_names(it: dict) -> tuple:
    """(主角怎么称呼, 这条记录里所有孩子的称呼)。有小名就用小名。"""
    studs = item_students(it)
    calls = [(s.get("nickname") or "").strip() or s["name"] for s in studs]
    return (calls[0] if calls else (it.get("student_name") or "小朋友")), calls


def _writing_picks(it: dict, level: int, forced_indicator_id=None):
    """保留完整已选指标；老师说明中的明确要求增补到这一组，AI 不得替换它。"""
    picks = _load_indicators(it, level) if it.get("indicators") is not None or it.get("indicator_id") else None
    intent = requested_indicators(it.get("teacher_note") or "", level)
    requested = intent["picks"]
    excluded = set(intent["excluded"])
    if forced_indicator_id and not any(p["indicator_id"] == forced_indicator_id for p in (picks or [])):
        requested = _clean_picks([{"indicator_id": forced_indicator_id}], level) + requested
    if requested or (excluded and picks is not None):
        picks = [p for p in (picks or []) if p["indicator_id"] not in excluded]
        for p in requested:
            if p["indicator_id"] not in excluded and not any(x["indicator_id"] == p["indicator_id"] for x in picks):
                picks.append(p)
    if picks is not None and len(picks) > MAX_INDICATORS:
        raise ValueError(f"明确要求与已选指标合计超过 {MAX_INDICATORS} 条，请先调整指标")
    return picks


def _save_simple_result(it: dict, res: dict, level: int, picks, names) -> str:
    """在同一数据库锁中检查最新成稿再保存，防止并发生成同时漏过重复自查。"""
    with db.conn() as c:
        row = c.execute("SELECT * FROM items WHERE id=?", (it["id"],)).fetchone()
        if row is None:
            raise ValueError("这条记录已删除")
        current = dict(row)
        if current.get("publish_status") in ("done", "publishing", "uncertain"):
            raise ValueError("已发布或正在发布的记录不能重写")
        # 生成期间老师改了内容，就保留老师的新内容，不用旧请求覆盖它。
        for key in ("text", "teacher_note", "indicators", "indicator_id", "behavior_line",
                    "behavior_desc", "path", "paths", "modules", "stu_indicators",
                    "student_id", "student_ids"):
            if current.get(key) != it.get(key):
                raise ValueError("生成期间文字、说明或指标已修改，已保留你的修改，请重新生成")
        peers, _ = peer_context(it, limit=20)
        issue = ai_writer.repetition_issue(res["text"], peers, names)
        if issue:
            return issue
        selected = res["indicators"] if picks is None else picks
        first = selected[0] if selected else {}
        mods = _load_modules(it) if it.get("modules") is not None else ai_writer._modules_of(selected)
        old_ids = {p["indicator_id"] for p in _load_indicators(it, level)}
        for p in selected:
            m = ai_writer._modules_of([p])[0]
            if p["indicator_id"] not in old_ids and m not in mods:
                mods.append(m)
        fields = dict(indicator_id=first.get("indicator_id"), indicator_name=first.get("indicator_name"),
                      behavior_line=first.get("behavior_line"), level=level,
                      behavior_desc=res["behavior"], text=res["text"],
                      indicators=json.dumps(selected, ensure_ascii=False),
                      modules=json.dumps(mods, ensure_ascii=False),
                      approved=0, ai_status="done", ai_error=None, updated_at=time.time())
        c.execute("UPDATE items SET " + ", ".join(k + "=?" for k in fields) + " WHERE id=?",
                  (*fields.values(), it["id"]))
    return ""


def _write(it: dict, level: int, forced_indicator_id: Optional[int] = None, reroll: bool = False,
           text_only: bool = False):
    db.update_item(it["id"], ai_status="writing", ai_error=None, approved=0)
    try:
        nick, calls = _call_names(it)
        picks = _writing_picks(it, level, forced_indicator_id)
        previous = (it.get("text") or "").strip()
        for attempt in range(2):
            peers, vidx = peer_context(it, limit=20)
            if previous:
                peers.append(previous)
            if text_only:
                text = ai_writer.rewrite_text(it["student_name"] or "小朋友", level,
                    it.get("indicator_name") or "", it.get("behavior_line") or "",
                    it.get("behavior_desc") or "", it.get("teacher_note") or "",
                    nickname=nick, tier=_tier_of(it), picks=picks, children=calls,
                    peers=peers, previous_text=previous)
                res = {"text": text, "behavior": it.get("behavior_desc") or "", "indicators": picks or []}
            else:
                res = ai_writer.write_observation(item_paths(it), it["student_name"] or "小朋友", level,
                    it.get("teacher_note") or "", nickname=nick, tier=_tier_of(it), children=calls,
                    peers=peers, variant=vidx + (11 if reroll or previous else 0) + attempt * 11,
                    picks=picks)
                # 只排除某一项不等于清空全部；仍允许 AI 自动选其它有依据的指标。
                if picks is None:
                    excluded = set(requested_indicators(it.get("teacher_note") or "", level)["excluded"])
                    res["indicators"] = [p for p in res["indicators"] if p["indicator_id"] not in excluded]
            issue = _save_simple_result(it, res, level, picks, calls)
            if not issue:
                return
            log.info("第 %s 条并发成稿重复，重新组织一次：%s", it["id"], issue)
        raise ValueError("重复自查未通过，请补充观察细节后再生成；原文已保留")
    except Exception as e:
        with db.conn() as c:
            c.execute("UPDATE items SET ai_status='error', ai_error=?, updated_at=? "
                      "WHERE id=? AND ai_status='writing'", (str(e), time.time(), it["id"]))


# ---------- 老师在审核页的操作 ----------
MAX_INDICATORS = 8          # 一条记录最多挂几条指标（老师可以自由增删）
MAX_STUDENTS = 8            # 一条记录最多评几个孩子


def item_students(it: dict) -> list:
    """这条记录评的是哪几个孩子（第一个是主角，显示用）。"""
    ids = []
    if it.get("student_ids"):
        try:
            ids = [int(x) for x in json.loads(it["student_ids"]) if x]
        except Exception:
            ids = []
    if not ids and it.get("student_id"):
        ids = [int(it["student_id"])]
    out = []
    for sid in ids:
        st = db.one("SELECT * FROM students WHERE student_id=?", (sid,))
        if st and not any(o["student_id"] == st["student_id"] for o in out):
            out.append(st)
    return out


def _apply_students(item_id: int, studs: list, manual: bool = True):
    ids = [s["student_id"] for s in studs]
    names = "、".join(s["name"] for s in studs)
    f = {"student_ids": json.dumps(ids), "student_id": ids[0] if ids else None,
         "student_name": names or None}
    if manual:
        f.update(face_status="manual" if ids else "unknown", face_score=None)
    db.update_item(item_id, **f)


def set_student(item_id: int, student_id: int, level: int):
    """老师在下拉框里直接指定主角（会把这条记录的孩子换成这一个）。"""
    st = db.one("SELECT * FROM students WHERE student_id=?", (student_id,))
    if not st:
        raise ValueError("名单里没有这个孩子")
    batch = db.one('SELECT b.class_id FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?', (item_id,))
    if not batch or st['class_id'] != batch['class_id']:
        raise ValueError('请选择这份记录所属班级的幼儿')
    _apply_students(item_id, [st])


def set_students(item_id: int, student_ids: list) -> dict:
    """一条记录评几个孩子。发布时会发成一条记录、挂多个孩子，每人各自打指标。"""
    batch = db.one('SELECT b.class_id FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?', (item_id,))
    if not batch:
        raise ValueError('这条记录不在了')
    studs, seen = [], set()
    for sid in (student_ids or [])[:MAX_STUDENTS]:
        try:
            sid = int(sid)
        except Exception:
            continue
        if sid in seen:
            continue
        st = db.one("SELECT * FROM students WHERE student_id=?", (sid,))
        if st:
            if st['class_id'] != batch['class_id']:
                raise ValueError('请选择这份记录所属班级的幼儿')
            seen.add(sid)
            studs.append(st)
    _apply_students(item_id, studs)
    return db.one("SELECT * FROM items WHERE id=?", (item_id,))


def _pick_for_table(it: dict, paths: list, level: int) -> list:
    """只返回候选指标；辅助请求可能晚于正文失败结束，不能自行修改记录。"""
    return ai_writer.pick_indicators(paths, it["student_name"] or "小朋友",
                                     level, it.get("teacher_note") or "")


def _write_table(it: dict, level: int, reroll: bool = False):
    """写一份观察记录表。分两步，而且**挑指标和写实录是并排跑的**。

       为什么这么排：挑指标要看图、写实录也要看图，但写实录不需要知道挑了哪条指标；
       真正等着指标的是第二步（分析要按领域调参考资料）。以前三件事排成一条队，
       老师就得挨个等。现在前两件并排跑，一条记录少等一整轮。
    """
    db.update_item(it["id"], ai_status="writing", ai_error=None, approved=0)
    try:
        previous = record_table.normalize(json.loads(it["table_json"])) if it.get("table_json") else None
        nick, calls = _call_names(it)
        show_behaviors = (previous or {}).get("行为表现", True)
        picks = _load_indicators(it, level) if show_behaviors else []
        paths = item_paths(it)
        fut = None if picks or not show_behaviors else _aux_pool.submit(ai_progress.wrap(_pick_for_table, it, paths, level))
        generated_picks = None

        peers, vidx = peer_context(it)
        if reroll:
            vidx += 11
        tier = _tier_of(it)
        name = it["student_name"] or "小朋友"
        note = record_table.writing_note(previous or {}, it.get("teacher_note") or "")

        ai_progress.stage('整理观察实录', 0, 3)
        story = record_table.write_story(paths, name, level, note, picks, nick, tier=tier,
                                         children=calls, peers=peers, variant=vidx)
        if previous and previous.get('情境补写'):
            story['情境补写'] = True
        if fut is not None:
            try:
                picks = fut.result(timeout=150) or []
                generated_picks = picks
            except Exception as e:
                log.warning("记录表挑指标失败，分析先照写：%s", e)
                picks = []
        # 这条记录里有几个孩子就把几个孩子都带上 ——
        # 以前这里只传了主角一个人，分析就只写第一个孩子，另一个在实录里有、分析里没有。
        ai_progress.stage('循证分析与回应', 1, 3)
        ana = record_table.write_analysis(story, name, level, note, picks, nick, tier=tier,
                                          children=calls) if previous is None or any(
                                              k in previous for k in ("分析", "行为跟进")) else {}
        t = {**story, **ana}
        if previous is not None:
            # 整份重写沿用老师选择的结构，自定义段落的原文交给单段重写处理。
            t = {**previous, **story, **{k: v for k, v in ana.items() if k in previous}}
            for evidence_key in ('分析依据', '依据对应实录', '依据对应分析'):
                if '分析' in previous and evidence_key in ana:
                    t[evidence_key] = ana[evidence_key]
                elif '分析' not in previous:
                    t.pop(evidence_key, None)
        if "家庭指导" in t:
            t.update(record_table.rewrite_field(t, "家庭指导", paths, name, level, note, picks, nick,
                                               tier=tier, children=calls, want="根据这次实录给家长具体、简洁的建议。"))
        ai_progress.stage('核对并保存成稿', 2, 3)
        t = record_table.normalize(t)
        _save_table_result(it, t, level, generated_picks=generated_picks)
    except Exception as e:
        db.update_item(it["id"], ai_status="error", ai_error=str(e))


def _save_table_result(snapshot: dict, table: dict, level: int, generated_picks=None):
    """核对开始写作时的稿件后再保存；老师在等待期间的修改始终优先。"""
    with db.conn() as c:
        row = c.execute("SELECT * FROM items WHERE id=?", (snapshot["id"],)).fetchone()
        if row is None:
            raise ValueError("这条记录已删除")
        current = dict(row)
        if current.get("publish_status") in ("done", "publishing", "uncertain"):
            raise ValueError("这条记录已经发布或正在发布，已保留现有内容")
        if any(current.get(k) != snapshot.get(k) for k in (
                "table_json", "text", "teacher_note", "student_id", "student_ids", "path", "paths",
                "indicators", "indicator_id", "indicator_name", "behavior_line", "modules", "stu_indicators")):
            raise ValueError("生成期间内容、指标或观察对象已修改，已保留你的修改，请重新生成")
        fields = dict(table_json=json.dumps(table, ensure_ascii=False), text=record_table.to_text(table),
                      ai_status="done", ai_error=None, level=level, approved=0, updated_at=time.time())
        if generated_picks is not None:
            first = generated_picks[0] if generated_picks else {}
            fields.update(indicators=json.dumps(generated_picks, ensure_ascii=False),
                          indicator_id=first.get("indicator_id"),
                          indicator_name=first.get("indicator_name", ""),
                          behavior_line=first.get("behavior_line", ""))
            # 老师已经明确选择的领域（包括清空）原样保留。
            if snapshot.get("modules") is None:
                fields["modules"] = json.dumps(ai_writer._modules_of(generated_picks), ensure_ascii=False)
        c.execute("UPDATE items SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?",
                  (*fields.values(), snapshot["id"]))


def _prepare_writing_task(it, effort='', speed=''):
    import config
    conf = config.ai_conf(speed or it.get('ai_speed') or '')
    options = config.effort_options(conf)
    supported = options['levels'] or ['default']
    if effort and effort not in supported:
        raise ValueError('当前模型不支持这个思考档位，请重新选择')
    selected = effort or db.get_setting('ai_effort_item_' + str(it['id']), '') or db.get_setting('ai_effort_default', '')
    if selected not in supported:
        selected = options['default']
    conf['observation_effort'] = selected
    db.set_setting('ai_effort_item_' + str(it['id']), selected)
    ai_progress.prepare(it['id'], selected, conf)


def _validate_batch_effort(rows, effort, speed=''):
    """批次模型可能不同；先检查，避免部分任务启动后才静默跳过不支持的记录。"""
    if not effort:
        return
    import config
    incompatible = []
    for it in rows:
        if (not it.get('student_id') or it.get('ai_status') == 'writing'
                or it.get('publish_status') in ('done', 'publishing', 'uncertain')):
            continue
        options = config.effort_options(config.ai_conf(speed or it.get('ai_speed') or ''))
        if effort not in (options['levels'] or ['default']):
            incompatible.append(f"#{it['id']}（{options['model']}）")
    if incompatible:
        raise ValueError('这些记录的模型不支持所选思考档位：' + '、'.join(incompatible)
                         + '。请按单条可用档位生成，或改选各模型都支持的档位。')


def _execute_writing_task(item_id, fn):
    import config
    old_speed = config.cur_speed()
    try:
        with ai_progress.scope(item_id):
            return fn()
    finally:
        config.set_speed(old_speed)
        current = db.one('SELECT ai_status FROM items WHERE id=?', (item_id,)) or {}
        ai_progress.finish(item_id, current.get('ai_status') == 'done')


def _start_generation(item_id: int, speed: str = "", note: Optional[str] = None, effort: str = "") -> dict:
    """检查和占用同一把锁，连点或批量与单条同时启动时也只排队一次。"""
    with db.conn():
        it = db.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not it or not it["student_id"]:
            raise ValueError("请先指认这张照片是哪个孩子")
        if it.get("ai_status") == "writing":
            raise ValueError("这条记录正在生成，请等待完成")
        if it.get("publish_status") in ("done", "publishing", "uncertain"):
            raise ValueError("已发布或正在发布的记录不能重写")
        _prepare_writing_task(it, effort, speed)
        fields = dict(ai_status="writing", ai_error=None, approved=0)
        if speed in ("fast", "deep"):
            fields["ai_speed"] = speed
        if note is not None:
            fields["teacher_note"] = note
        db.update_item(item_id, **fields)
        return it


def generate(item_id: int, level: int, mode: str = "", speed: str = "", effort: str = ""):
    """老师点「AI 生成」。speed 是这一条要「写得快」还是「想得细」。"""
    it = _start_generation(item_id, speed, effort=effort)
    mode = mode or (db.one("SELECT mode FROM batches WHERE id=?", (it["batch_id"],)) or {}).get("mode") or "simple"
    _pool.submit(_guarded_write, (_write_table if mode == "table" else _write),
                 item_id, level, speed=speed)


def generate_batch(batch_id: int, level: int, only_missing: bool = True,
                   speed: str = "", effort: str = "") -> int:
    """整批生成。only_missing=True 时跳过已经写好的，避免把老师改过的文字覆盖掉。"""
    q = """SELECT * FROM items WHERE batch_id=? AND student_id IS NOT NULL
           AND COALESCE(ai_status, '') != 'writing'
           AND COALESCE(publish_status, '') NOT IN ('done', 'publishing', 'uncertain')"""
    if only_missing:
        q += " AND (text IS NULL OR text='')"
    rows = db.rows(q, (batch_id,))
    _validate_batch_effort(rows, effort, speed)
    mode = (db.one("SELECT mode FROM batches WHERE id=?", (batch_id,)) or {}).get("mode") or "simple"
    fn = _write_table if mode == "table" else _write
    scheduled = []
    for it in rows:
        try:
            _start_generation(it["id"], speed, effort=effort)
            scheduled.append(it)
        except ValueError:
            continue  # 查询后可能已由另一个入口开始写或发布。
    # 并发跑。以前这里是串行的 —— 为了让后写的看见前面写了什么好错开花样，
    # 结果二十几条要跑五六分钟。现在每条的写法在动笔前就派好了（见 ai_writer.style_combo），
    # 彼此不用看见对方，就可以放开跑；写完还有一道自检兜底。
    for it in scheduled:
        # 每条带上它自己的档：整批指定了就用整批的，没指定就用这条原来记着的
        _pool.submit(_guarded_write, fn, it["id"], level,
                     speed=speed or (it["ai_speed"] if "ai_speed" in it.keys() else "") or "")
    return len(scheduled)


def _guarded_write(fn, item_id: int, level: int, speed: str = ""):
    return _execute_writing_task(item_id, lambda: _guarded_write_inner(fn, item_id, level, speed))


def _guarded_write_inner(fn, item_id: int, level: int, speed: str = ""):
    """写一条，然后跟同批比一眼；撞车了就换套写法重写一次（只重写一次，不烧 token）。"""
    import config as _cfg
    _cfg.set_speed(speed)          # 这条老师选的是快还是细，整条写作过程都按它来
    try:
        fn(db.one("SELECT * FROM items WHERE id=?", (item_id,)), level)
    except Exception as e:
        log.warning("生成第 %s 条失败：%s", item_id, e)
        db.update_item(item_id, ai_status='error', ai_error=str(e))
        return
    if fn is _write:  # 随手记已在保存前完成自查，不先落库再重写。
        return
    try:
        it = db.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not it or it.get("ai_status") != "done" or not (it.get("text") or "").strip():
            return
        if not _too_alike(it):
            return
        log.info("第 %s 条跟同批撞车了，换套写法重写一次", item_id)
        db.update_item(item_id, ai_status="writing")
        fn(db.one("SELECT * FROM items WHERE id=?", (item_id,)), level, reroll=True)
    except Exception as e:
        log.warning("第 %s 条重写失败（保留原文）：%s", item_id, e)
        db.update_item(item_id, ai_status="done")


def _too_alike(it: dict) -> bool:
    """同批记录的重复句式自查，复用随手记的有序文本比较。"""
    mine = (it.get("text") or "").strip()
    if len(mine) < 20:
        return False
    peers, _ = peer_context(it, limit=8)
    if not peers:
        return False
    return bool(ai_writer.repetition_issue(mine, peers))


# ---------- 批量改 ----------
def bulk(batch_id: int, item_ids: list, action: str, value=None, speed: str = "", effort: str = "") -> dict:
    """老师一次选好几条，统一改。
       add_indicator / del_indicator / set_indicators / add_module / del_module /
       date / sync / students_add / approve / generate / delete
    """
    level = None
    b = db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        raise ValueError("这个批次不在了")
    c = db.one("SELECT level FROM classes WHERE class_id=?", (b["class_id"],))
    level = c["level"] if c else 5
    ids = [int(i) for i in (item_ids or [])]
    rows = db.rows(f"SELECT * FROM items WHERE batch_id=? AND id IN ({','.join('?' * len(ids))})",
                   (batch_id, *ids)) if ids else []
    rows = [r for r in rows if r.get("publish_status") != "done"]
    if action == 'generate':
        _validate_batch_effort(rows, effort, speed)
    n = 0
    for it in rows:
        try:
            if action == "add_indicator":
                picks = [{**p, "behavior_lines": pick_lines(p, level)}
                         for p in _load_indicators(it, level)]
                if any(p["indicator_id"] == int(value) for p in picks):
                    continue
                picks.append({"indicator_id": int(value)})
                set_indicators(it["id"], picks, level)
            elif action == "del_indicator":
                picks = [{**p, "behavior_lines": pick_lines(p, level)}
                         for p in _load_indicators(it, level) if p["indicator_id"] != int(value)]
                set_indicators(it["id"], picks, level)
            elif action == "copy_indicators":
                src = db.one("SELECT * FROM items WHERE id=?", (int(value),))
                if not src or src["id"] == it["id"]:
                    continue
                picks = [{**p, "behavior_lines": pick_lines(p, level)}
                         for p in _load_indicators(src, level)]
                set_indicators(it["id"], picks, level)
                db.update_item(it["id"], modules=src.get("modules"), stu_indicators=None)
            elif action == "clear_indicators":
                set_indicators(it["id"], [], level)
            elif action == "add_module":
                mods = _load_modules(it)
                one = {"realm": value.get("realm"), "module": value.get("module")}
                if one in mods:
                    continue
                mods.append(one)
                set_modules(it["id"], mods)
            elif action == "del_module":
                mods = [m for m in _load_modules(it)
                        if not (m.get("realm") == value.get("realm") and m.get("module") == value.get("module"))]
                set_modules(it["id"], mods)
            elif action == "date":
                set_options(it["id"], record_date=str(value))
            elif action == "sync":
                set_options(it["id"], sync_parent=bool(value))
            elif action == "students_add":
                cur = [s["student_id"] for s in item_students(it)]
                for sid in (value if isinstance(value, list) else [value]):
                    if int(sid) not in cur:
                        cur.append(int(sid))
                set_students(it["id"], cur)
            elif action == "approve":
                db.update_item(it["id"], approved=1 if value else 0)
            elif action == "note":
                db.update_item(it["id"], teacher_note=str(value or ""))
            elif action == "generate":
                if it.get("student_id"):
                    generate(it["id"], level, speed=speed, effort=effort)
            elif action == "delete":
                delete_item(it["id"])
            else:
                raise ValueError(f"不认识的操作：{action}")
            n += 1
        except Exception as e:
            log.warning("批量 %s 第 %s 条失败：%s", action, it["id"], e)
    return {"changed": n, "total": len(rows)}


def stu_indicators(it: dict) -> dict:
    """{孩子id: 他自己的那组指标}。没单独设过的孩子不在这里，发布时用记录级的那组。"""
    try:
        v = json.loads(it.get("stu_indicators") or "{}")
        return {int(k): val for k, val in v.items() if isinstance(val, list)}
    except Exception:
        return {}


def picks_for(it: dict, student_id: int, level: int) -> list:
    """这个孩子这一条记录该挂哪些指标：他自己设过就用他的，没设过就用整条的。"""
    mine = stu_indicators(it).get(int(student_id))
    if mine is not None:
        return mine
    return _load_indicators(it, level)


def set_student_indicators(item_id: int, student_id: int, picks, level: int) -> dict:
    """给这条记录里的某一个孩子单独设指标。picks 传 None 表示"跟整条一样"。"""
    it = db.one("SELECT * FROM items WHERE id=?", (item_id,))
    if not it:
        raise ValueError("这条记录不在了")
    cur = stu_indicators(it)
    if picks is None:
        cur.pop(int(student_id), None)
    else:
        cur[int(student_id)] = _clean_picks(picks, level)
    db.update_item(item_id, stu_indicators=json.dumps({str(k): v for k, v in cur.items()}, ensure_ascii=False))
    return db.one("SELECT * FROM items WHERE id=?", (item_id,))


def _intermediate_grade(pick: dict, level: int):
    """常规体系二、四档只有能力项和评分，不包含某个主档的原句。"""
    value = pick.get("grade")
    if value is None:
        return None
    if level not in (1, 3, 5) or value not in (2, 4, "2", "4"):
        raise ValueError("只有小中大班可选择表现行为二、四；托育体系请勾选24—36个月原句")
    if pick.get("behavior_lines") or pick.get("behavior_line"):
        raise ValueError("表现行为二、四不涉及具体指标原句，请清空原句后重新保存")
    return int(value)


def _clean_picks(picks, level: int) -> list:
    """把一组指标整理成标准结构（校验指标存在、表现行为是目录原句）。"""
    out = []
    for one in (picks or [])[:MAX_INDICATORS]:
        ind = indicator_by_id(int(one.get("indicator_id")))
        if (level in NURSERY_LEVELS and not ind) or (ind and ind not in load_indicators(level)):
            raise ValueError("指标不属于当前班级的评价体系，请重新选择")
        if not ind or any(o["indicator_id"] == ind["id"] for o in out):
            continue
        grade = _intermediate_grade(one, level)
        bls = [] if grade is not None else norm_lines(ind, level, one.get("behavior_lines"), one.get("behavior_line"))
        out.append({"indicator_id": ind["id"], "indicator_name": ind["name"], "domain": ind["domain"],
                    "subdomain": ind["subdomain"], "medal_id": ind.get("medal_id"),
                    "behavior_lines": bls, "behavior_line": bls[0]["line"] if bls else "",
                    **({"grade": grade} if grade is not None else {})})
    return out


def norm_lines(ind: dict, level: int, lines, fallback: str = "") -> list:
    """把老师勾的表现行为整理成 [{"level":档位,"line":"原句"}]。
       同一条指标下可以勾好几条；勾的句子必须是目录里的原句（档位自动认出来）。"""
    out, seen = [], set()
    raw = list(lines or [])
    if not raw and fallback:
        raw = [fallback]
    for x in raw:
        if isinstance(x, dict):
            txt, lv, gd = str(x.get("line") or "").strip(), x.get("level"), x.get("grade")
        else:
            txt, lv, gd = str(x or "").strip(), None, None
        if not txt:
            continue
        hit = None
        candidates = [128] if level in NURSERY_LEVELS else (
            ([int(lv)] if lv in (1, 3, 5, "1", "3", "5") else []) + [level, 5, 3, 1])
        for cand in candidates:
            if txt in indicator_lines(ind, int(cand)):
                hit = int(cand)
                break
        if hit is None:
            continue
        # 老师判定的实际水平：只允许在这句话所在档位上下挪一格
        # （小班这一条做得特别好 → 表现行为二；中班这一条还差一点 → 也是表现行为二）
        try:
            gd = int(gd)
        except Exception:
            gd = hit
        if level in NURSERY_LEVELS:
            gd = 128
        elif gd not in (hit - 1, hit, hit + 1) or gd < 1 or gd > 5:
            gd = hit
        key = (gd, txt)
        if key in seen:
            continue
        seen.add(key)
        out.append({"level": hit, "line": txt, "grade": gd})
    if not out and lines != [] and level not in NURSERY_LEVELS:
        cur = indicator_lines(ind, level)
        if cur:
            out = [{"level": level, "line": cur[0], "grade": level}]
    return out


def pick_lines(p: dict, level: int) -> list:
    """读一条指标当前勾了哪几句（老数据只有单句，照样读得出来）。"""
    v = p.get("behavior_lines")
    if isinstance(v, list) and v:
        out = []
        for x in v:
            if isinstance(x, dict) and x.get("line"):
                lv = int(x.get("level") or level)
                out.append({"level": lv, "line": x["line"], "grade": int(x.get("grade") or lv)})
            elif isinstance(x, str) and x.strip():
                out.append({"level": level, "line": x.strip(), "grade": level})
        if out:
            return out
    if p.get("behavior_line"):
        return [{"level": level, "line": p["behavior_line"], "grade": level}]
    return []


def _load_indicators(it: dict, level: int) -> list[dict]:
    """取这条记录当前选中的指标列表（老式单指标数据也能读）。"""
    if it.get("indicators"):
        try:
            v = json.loads(it["indicators"])
            if isinstance(v, list):
                return v
        except Exception:
            pass
    if it.get("indicator_id"):
        ind = indicator_by_id(it["indicator_id"])
        if ind:
            bl = it.get("behavior_line") or ""
            return [{"indicator_id": ind["id"], "indicator_name": ind["name"], "domain": ind["domain"],
                     "subdomain": ind["subdomain"], "medal_id": ind.get("medal_id"),
                     "behavior_line": bl,
                     "behavior_lines": norm_lines(ind, level, None, bl)}]
    return []


def _load_modules(it: dict) -> list[dict]:
    if it.get("modules"):
        try:
            v = json.loads(it["modules"])
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return []


def set_indicators(item_id: int, picks: list, level: int, keep_modules: bool = False) -> dict:
    """老师增删整条记录的指标。想挂几条挂几条。
       关联领域默认跟着指标一起变，除非老师已经手工调过（keep_modules）。"""
    out = _clean_picks(picks, level)
    fields = {"indicators": json.dumps(out, ensure_ascii=False),
              "indicator_id": out[0]["indicator_id"] if out else None,
              "indicator_name": out[0]["indicator_name"] if out else None,
              "behavior_line": out[0]["behavior_line"] if out else None,
              "level": level}
    if not keep_modules:
        mods = ai_writer._modules_of(out)
        fields["modules"] = json.dumps(mods, ensure_ascii=False)
    db.update_item(item_id, **fields)
    return db.one("SELECT * FROM items WHERE id=?", (item_id,))


def set_modules(item_id: int, mods: list) -> dict:
    context = db.one("""SELECT c.level FROM items i JOIN batches b ON b.id=i.batch_id
                        JOIN classes c ON c.class_id=b.class_id WHERE i.id=?""", (item_id,))
    level = context["level"] if context else 5
    clean = _clean_modules(mods, level)
    db.update_item(item_id, modules=json.dumps(clean, ensure_ascii=False))
    return db.one("SELECT * FROM items WHERE id=?", (item_id,))


def _clean_modules(mods: list, level: int) -> list:
    choices = modules_for_level(level)
    clean = []
    for m in mods or []:
        if isinstance(m, dict) and m.get("realm") and m.get("module"):
            found = next((x for x in choices if x["realm"] == m["realm"]
                          and x["module"] == m["module"]
                          and x["systemId"] == m.get("systemId", x["systemId"])), None)
            if not found:
                raise ValueError("关联领域不属于当前班级的评价体系")
            one = {"realm": found["realm"], "module": found["module"]}
            if level in NURSERY_LEVELS:
                one["systemId"] = found["systemId"]
            if one not in clean:
                clean.append(one)
    return clean


def set_options(item_id: int, sync_parent: Optional[bool] = None, record_date: Optional[str] = None) -> dict:
    f = {}
    if sync_parent is not None:
        f["sync_parent"] = 1 if sync_parent else 0
    if record_date:
        f["record_date"] = record_date
    if f:
        db.update_item(item_id, **f)
    return db.one("SELECT * FROM items WHERE id=?", (item_id,))


def regenerate(item_id: int, level: int, indicator_id: Optional[int] = None,
               note: Optional[str] = None, text_only: bool = False, speed: str = "", effort: str = ""):
    """重写一条。speed 换了，就是「这条我想换个档再写一遍」。"""
    it = _start_generation(item_id, speed, note, effort=effort)
    mode = (db.one("SELECT mode FROM batches WHERE id=?", (it["batch_id"],)) or {}).get("mode") or "simple"

    def _run():
        import config as _cfg
        _cfg.set_speed(speed)
        current = db.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not current:
            return
        if mode == "table":
            _write_table(current, level, reroll=True)
        else:
            _write(current, level, indicator_id, reroll=True, text_only=text_only)
    _pool.submit(_execute_writing_task, item_id, _run)


def set_indicator(item_id: int, indicator_id: int, level: int, behavior_line: Optional[str] = None):
    ind = indicator_by_id(indicator_id)
    if not ind or ind not in load_indicators(level):
        raise ValueError("指标不属于当前班级的评价体系")
    lines = indicator_lines(ind, level)
    bl = behavior_line if behavior_line in lines else (lines[0] if lines else "")
    db.update_item(item_id, indicator_id=ind["id"], indicator_name=ind["name"], behavior_line=bl, level=level)


# ---------- 发布 ----------
def publish(batch_id: int, item_ids: list[int], ysk: YskClient, progress=None) -> dict:
    batch = db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not batch:
        raise ValueError("这个批次不存在")
    cls = db.one("SELECT * FROM classes WHERE class_id=?", (batch["class_id"],))
    if not cls:
        raise ValueError("请先同步这个批次的班级")
    is_table = batch.get("mode") == "table"
    items = [db.one("SELECT * FROM items WHERE id=?", (iid,)) for iid in dict.fromkeys(item_ids)]
    if any(not it or it["batch_id"] != batch_id for it in items):
        raise ValueError("待发布记录不属于当前批次，请刷新后重试")
    kid, class_id, level = cls["kid"], cls["class_id"], cls["level"]
    ok, fail = 0, 0
    for n, it in enumerate(items):
        iid = it["id"]
        # 逐条刷新审核后的内容，并在同一事务占用，避免两个发布请求各发一份。
        with db.conn() as c:
            row = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if not row:
                fail += 1
                continue
            it = dict(row)
            if it["publish_status"] == "done":
                continue
            if it["publish_status"] in ("publishing", "withdrawing", "uncertain") or it.get("ai_status") == "writing":
                fail += 1
                if progress:
                    progress(n + 1, len(items))
                continue
            if it.get("approved"):
                c.execute("UPDATE items SET publish_status='publishing', publish_error=NULL WHERE id=?", (iid,))
        studs = item_students(it)
        medals: list[int] = []
        medal_map: dict = {}
        rid = None
        try:
            if not it.get("approved"):
                raise ValueError("请先审核并确认这条记录，再发布")
            if not studs or any(s["class_id"] != class_id for s in studs):
                raise ValueError("请为记录选择当前班级的观察对象")
            if not is_table and not (it.get("text") or "").strip():
                raise ValueError("请先填写观察内容")
            record_date = it.get("record_date") or batch.get("record_date")
            sync_parent = bool(it["sync_parent"] if it["sync_parent"] is not None else 1)
            modules = _clean_modules(_load_modules(it), level)
            table_body = None
            if is_table:
                try:
                    table = json.loads(it.get("table_json") or "{}")
                except (TypeError, ValueError):
                    raise ValueError("观察记录表内容损坏，请重新保存后发布")
                table_body = ysk.prepare_table_record(
                    class_id, kid, studs, table, modules=modules,
                    sync_with_parent=sync_parent, record_date=record_date)
            medal_students = [] if is_table and table.get("行为表现", True) is False else studs
            prepared_picks = {}
            # 全部观察对象先校验，避免给前一个孩子打完指标才发现后一个串了体系。
            for st in medal_students:
                selected = picks_for(it, st["student_id"], level)
                if level in NURSERY_LEVELS:
                    selected = _clean_picks(selected, level)
                elif any((indicator_by_id(pk.get("indicator_id")) or {}).get("systemId") == 35 for pk in selected):
                    raise ValueError("托育指标不能发布到小中大班")
                # 新二、四档先完整校验，旧的逐句评分不改写，也不猜一个共同评分。
                selected = [_clean_picks([pk], level)[0] if pk.get("grade") is not None else pk
                            for pk in selected]
                if level in NURSERY_LEVELS and any(not pick_lines(pk, level) for pk in selected):
                    raise ValueError("请先为托育指标勾选24—36个月的表现行为")
                prepared_picks[st["student_id"]] = selected
            for st in medal_students:
                mine = []
                for pk in prepared_picks[st["student_id"]]:
                    if pk.get("medal_id"):
                        grade = _intermediate_grade(pk, level)
                        desc = [{"grade_id": GRADE_ID.get(x.get("grade") or x["level"],
                                                          LEVEL_GRADE_ID[level]),
                                 "desc": x["line"]} for x in pick_lines(pk, level)]
                        m = ysk.add_student_medal(class_id, kid, st["student_id"], pk["medal_id"],
                                                  grade if grade is not None else level,
                                                  GRADE_ID[grade] if grade is not None else LEVEL_GRADE_ID[level],
                                                  desc if grade is not None else (desc or ""),
                                                  record_date=it.get("record_date") or batch.get("record_date"))
                        mine.append(m)
                        medals.append(m)
                medal_map[st["student_id"]] = mine
            fvos = []
            for _p in item_paths(it)[:9]:
                try:
                    fvos.append(ysk.upload_image(_p))
                except Exception as _e:
                    log.warning("上传照片失败 %s：%s", _p, _e)
            if not fvos and (item_paths(it) or not is_table):
                raise RuntimeError("照片上传失败")
            if is_table:
                rid = ysk.add_table_record(table_body, fvos, medal_map)
            else:
                rid = ysk.add_record(class_id, kid,
                                 [{"student_id": s["student_id"], "child_id": s["child_id"],
                                   "name": s["name"], "avatar": s["avatar"]} for s in studs],
                                 it["text"], fvos, medals,
                                 modules=modules,
                                 sync_with_parent=sync_parent,
                                 record_date=record_date,
                                 medal_map=medal_map)
            db.update_item(iid, publish_status="done", record_id=rid,
                           record_type="TABLE" if is_table else "SIMPLE",
                           medal_ids=json.dumps(medals),
                           medal_id_remote=(medals[0] if medals else None))
            ok += 1
        except TablePublishUncertain as e:
            # 请求可能已提交，不能删掉已关联的指标，也不能自动再发一条。
            db.update_item(iid, publish_status="uncertain", publish_error=str(e),
                           record_type="TABLE", medal_ids=json.dumps(medals))
            fail += 1
        except Exception as e:
            log.exception("发布失败 item %s", iid)
            # 回滚已打的指标，避免站上留下"有指标没记录"的半条
            for m in medals if rid is None else []:
                try:
                    ysk.delete_student_medal(m)
                except Exception:
                    pass
            db.update_item(iid, publish_status="error", publish_error=str(e))
            fail += 1
        if progress:
            progress(n + 1, len(items))
    return {"ok": ok, "fail": fail}


def unpublish(item_id: int, ysk: YskClient):
    # 只有显式的单条撤回入口调用这里；占用状态阻止并发删除丢掉发布编号。
    with db.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError("这条记录不存在")
        it = dict(row)
        if it.get("publish_status") in ("publishing", "withdrawing", "uncertain"):
            raise ValueError("发布或撤回结果尚未确认，请先在幼师口袋核对")
        c.execute("UPDATE items SET publish_status='withdrawing' WHERE id=?", (item_id,))
    try:
        is_table = it.get("record_type") == "TABLE"
        if it["record_id"]:
            if is_table:
                ysk.delete_table_record(it["record_id"])
            else:
                # 旧版表格也曾按 SIMPLE 发布，缺少类型的历史记录仍走旧接口。
                ysk.delete_record(it["record_id"])
        ids = []
        if it.get("medal_ids"):
            try:
                ids = [i for i in json.loads(it["medal_ids"]) if i]
            except Exception:
                ids = []
        if not ids and it["medal_id_remote"]:
            ids = [it["medal_id_remote"]]
        for m in [] if is_table else ids:   # 表格接口已同时撤回关联指标
            try:
                ysk.delete_student_medal(m)
            except Exception as e:
                log.warning("撤销指标 %s 失败：%s", m, e)
    except Exception:
        db.update_item(item_id, publish_status="uncertain", publish_error="撤回结果待核对，请先在幼师口袋检查")
        raise
    db.update_item(item_id, publish_status=None, record_id=None, record_type=None,
                   medal_id_remote=None, medal_ids=None, publish_error=None)


def rewrite_table_field(item_id: int, key: str, want: str = "", effort: str = "") -> dict:
    it = _start_generation(item_id, effort=effort)
    return _execute_writing_task(item_id, lambda: _rewrite_table_field(it, key, want))


def _rewrite_table_field(it: dict, key: str, want: str = "") -> dict:
    """老师对记录表里的某一段不满意，只重写这一段，其他几段原样留着。

       整份重新生成有两个毛病：观察内容白等一遍，老师自己改过的字还会被盖掉。
       所以这里只动一段，回来之后合并进原来的 table_json。
    """
    import record_table
    item_id = it['id']
    ai_progress.stage('重写所选段落', 0, 1)
    try:
        t = record_table.normalize(json.loads(it.get("table_json") or "{}"))
        if not t:
            raise ValueError("这条还没生成过记录表，先点「AI 生成」")
        b = db.one("SELECT class_id FROM batches WHERE id=?", (it["batch_id"],)) or {}
        c = db.one("SELECT level FROM classes WHERE class_id=?", (b.get("class_id"),)) or {}
        level = c.get("level") or it.get("level") or 5
        nick, calls = _call_names(it)
        picks = _load_indicators(it, level) if t.get("行为表现", True) else []
        out = record_table.rewrite_field(
            t, key, item_paths(it), it["student_name"] or "小朋友", level,
            record_table.writing_note(t, it.get("teacher_note") or ""), picks, nick, tier=_tier_of(it),
            children=calls, want=want)
        t = record_table.normalize({**t, **out})
        _save_table_result(it, t, level)
        return t
    except Exception as e:
        db.update_item(item_id, ai_status="error", ai_error=str(e))
        raise
