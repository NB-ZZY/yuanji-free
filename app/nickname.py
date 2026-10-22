"""小名库：从幼师口袋的历史观察记录里，把老师平时对每个孩子的称呼挖出来。

老师写记录时很少写全名，多半是"桉桉""小豆子"这样的叫法。
把这些挖出来存在本地，之后 AI 写评价时就用小名称呼，读起来才像自己班的老师写的。
挖不出来的就留空，写作时退回用大名，老师也可以在花名册里手工填。
"""
import json
import logging
import re

import ai_writer
import db

log = logging.getLogger("nickname")

PROMPT = """你在帮幼儿园老师整理"孩子的小名"。

下面给你几个孩子的大名，以及老师过去写他们时的记录片段。请判断：老师在文字里
对这个孩子的固定称呼是什么（通常是叠字或大名里的一两个字，比如"示例姓名"叫"桉桉"、
"张沐莀"叫"莀莀"）。

规则：
- 只认记录里真实出现过的称呼，不要自己编。
- 记录里只用"你"、没有出现任何名字的，返回空字符串。
- 出现的是全名（和大名一模一样），也返回空字符串。
- 同一个孩子出现多种叫法，取出现次数最多的那个。

只输出 JSON，不要多余文字：
{"结果": [{"name": "大名", "nickname": "小名或空字符串"}]}
"""


SURNAMES2 = {"欧阳", "上官", "司马", "诸葛", "夏侯", "皇甫", "尉迟", "public"}


def suggest(name: str) -> str:
    """挖不到时给个建议：幼儿园最常见的叫法是名字最后一个字叠用（示例姓名→汐汐）。
       只是建议，老师改一下就行。"""
    name = (name or "").strip()
    if len(name) < 2:
        return ""
    return name[-1] * 2


def _texts(node, out: dict):
    """宽松遍历返回的 JSON，收集「只标注了一个孩子」的记录正文。"""
    if isinstance(node, list):
        for v in node:
            _texts(v, out)
        return
    if not isinstance(node, dict):
        return
    sids = node.get("studentIds")
    if isinstance(sids, list):
        sids = [s for s in sids if isinstance(s, int)]
        content = node.get("content")
        if len(sids) == 1 and isinstance(content, str) and content.strip():
            out.setdefault(sids[0], []).append(content.strip())
    for v in node.values():
        _texts(v, out)


def mine(class_id: int, ysk, max_pages: int = 60, progress=None) -> dict:
    students = {s["student_id"]: s for s in db.students_of(class_id)}
    texts: dict[int, list[str]] = {}
    pages = 0
    errors = []
    for page in range(1, max_pages + 1):
        try:
            data = ysk.class_records(class_id, page)
        except Exception as e:
            errors.append(f"第 {page} 页读取失败：{e}")
            break
        before = sum(len(v) for v in texts.values())
        _texts(data, texts)
        pages = page
        if sum(len(v) for v in texts.values()) == before and page > 3:
            break
        if progress:
            progress(page, max_pages + 6)
        if all(len(texts.get(sid, [])) >= 6 for sid in students):
            break

    todo = [(sid, s["name"], texts.get(sid, [])) for sid, s in students.items() if texts.get(sid)]
    found, blank = {}, []
    for i in range(0, len(todo), 6):
        chunk = todo[i:i + 6]
        blocks = []
        for sid, name, ts in chunk:
            frag = "\n".join("- " + re.sub(r"\s+", " ", t)[:120] for t in ts[:6])
            blocks.append(f"【大名】{name}\n{frag}")
        try:
            raw = ai_writer._chat([{"role": "system", "content": PROMPT},
                                   {"role": "user", "content": "\n\n".join(blocks)}],
                                  max_tokens=1500, temperature=0.2)
            got = ai_writer._parse_json(raw).get("结果") or []
        except Exception as e:
            errors.append(str(e))
            continue
        by_name = {name: sid for sid, name, _ in chunk}
        for one in got:
            if not isinstance(one, dict):
                continue
            nm, nick = str(one.get("name") or "").strip(), str(one.get("nickname") or "").strip()
            sid = by_name.get(nm)
            if not sid:
                continue
            # 小名不能比大名还长，也不能等于大名
            if nick and nick != students[sid]["name"] and len(nick) <= 6:
                db.set_nickname(sid, nick)
                found[nm] = nick
            else:
                blank.append(nm)
        if progress:
            progress(max_pages + i // 6 + 1, max_pages + 6)
    for sid, s in students.items():
        if s["name"] not in found and s["name"] not in blank:
            blank.append(s["name"])
    blank = sorted(set(n for n in blank if n not in found))
    return {"pages": pages, "found": found, "blank": blank,
            "suggest": {s["name"]: suggest(s["name"]) for s in students.values() if s["name"] in blank},
            "errors": errors[:10]}
