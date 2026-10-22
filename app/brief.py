"""把老师随口说的一段话，变成每条记录各自的「情况说明」和指标。

老师站在教室里，脑子里是这样的一句话：
「今天量身高，牛牛量完说自己又长高了，桉桉帮着扶尺子。」

以前这句话只能整段塞进「情况说明」，一批二十条记录共用同一句，写出来自然一个味儿。
现在先让模型把它拆开 —— 谁做了什么、说了什么、落在哪条指标上 —— 再按认出来的脸，
把对应的那一小段分给对应的孩子。没点到名的孩子，用整段的背景（"今天在量身高"）。

另外这里守着一道门：老师要是在框里问天气、聊八卦、粘贴一段无关的东西，
模型只回一句"这不像在说孩子"，不往下走 —— 省得白花 token，也省得写出莫名其妙的评价。
"""
import hashlib
import json
import logging
import re
from typing import Optional

import db
from knowledge import (LEVEL_LABELS, NURSERY_LEVELS, compact_catalog, indicator_by_id,
                       indicator_lines, load_indicators)
from writing_skill import nursery_prompt

log = logging.getLogger("brief")

MAX_INPUT = 800          # 再长的就是在贴文章了，截断
MAX_SCENES = 12

PROMPT = """你在帮幼儿园老师整理她刚才口述的观察情况。你只做一件事：把这段话拆成"哪个孩子、做了什么、说了什么、落在哪条指标上"，交给后面的程序去写评价。

# 先判断这段话是不是在说孩子
是在说孩子在园里做了什么、说了什么、遇到了什么（哪怕只有半句、只有一个场景），就继续往下做。
如果这段话跟观察记录没关系 —— 比如在问你问题、聊天气、聊家常、让你写别的东西、粘了一段与孩子无关的文字、
或者干脆是一串没有意义的字符 —— **立刻停下**，只输出：
{"ok": false, "why": "一句话，告诉老师这里该说什么（例如：这里写孩子在做什么就行，比如「今天量身高，牛牛量完说自己又长高了」）"}
不要试图从无关的话里硬找观察点，也不要回答老师的问题。

# 是在说孩子，就这样拆
1. activity：整段话的背景，一句话（10 字以内），比如"晨间测量身高""建构区搭积木"。所有孩子共用。
2. scenes：点到名字的孩子，一人一条（同一句话里提到两个孩子做同一件事，就写两条，names 各写各的）。
   - names：这段话里对这个孩子的称呼，原样抄下来（老师写"牛牛"就写"牛牛"，不要改成全名）。
   - note：客观复述老师说的这个孩子的行为，第三人称；简短资料可以 20~40 字，有过程时保留理解学习所需的尝试、变化、帮助和结果，不为压缩删掉这些证据。
     老师明确提供原话才引用，转述不改成直接引语。推测和待确认标记须保留，不能把猜测写成事实。没有提供的细节不编。
   - quote：孩子说的原话，没有就留空字符串。
   - indicators：这个场景能体现的指标，**0~2 条**，从下面的目录里选，证据足才选，宁可不选也不要硬凑。
3. all：没被点到名字的孩子共用的那一份（note 就是把 activity 说成一句话，indicators 通常留空数组；
   老师如果说的是"全班都在……"，就把指标放这里）。

# 指标规则
- indicator_id 必须是目录里的数字，indicator_name 必须和目录一致。
- behavior_line 必须是该指标下当前档位表现行为的**原句，一字不差**，抄不准就把这条指标去掉。

# 输出
只输出一个 JSON 对象，不要任何多余文字、不要解释：
{"ok": true, "activity": "…",
 "scenes": [{"names": ["牛牛"], "note": "…", "quote": "…",
             "indicators": [{"indicator_id": 数字, "indicator_name": "…", "behavior_line": "目录原句"}]}],
 "all": {"note": "…", "indicators": []}}
"""

_cache: dict = {}        # 同一段话点两次，不再花第二次钱


def _key(text: str, level: int) -> str:
    return hashlib.sha1(f"{level}::{text}".encode("utf-8")).hexdigest()


# ---------- 认名字 ----------
def _roster(class_id: int) -> list[dict]:
    return db.students_of(class_id)


def _aliases(st: dict) -> list[str]:
    """这个孩子可能被叫成什么。"""
    name = (st.get("name") or "").strip()
    out = [name]
    nick = (st.get("nickname") or "").strip()
    if nick:
        out.append(nick)
    if len(name) >= 2:
        out.append(name[-1] * 2)         # 示例姓名 → 汐汐
        out.append(name[-2:])            # 示例姓名 → 芸汐
        out.append(name[1:])             # 去掉姓
    return [x for x in dict.fromkeys(out) if x]


def match_name(raw: str, roster: list[dict]) -> Optional[dict]:
    """老师嘴里的称呼 → 名单里的那个孩子。认不准就返回 None，交给老师自己点。"""
    raw = re.sub(r"[\s，,。、]+", "", raw or "")
    if not raw:
        return None
    hits = []
    for st in roster:
        al = _aliases(st)
        if raw in al:
            return st
        if any(raw in a or a in raw for a in al if len(a) >= 2):
            hits.append(st)
    return hits[0] if len(hits) == 1 else None      # 有歧义就不猜


# ---------- 解析 ----------
def parse(text: str, class_id: int, level: int) -> dict:
    """一段话 → {"ok":True, activity, scenes[...], all, unknown}。ok=False 时带 why。"""
    if level not in LEVEL_LABELS:
        raise ValueError("不支持的班级年龄档位，请重新同步班级")
    text = (text or "").strip()
    if len(text) < 2:
        return {"ok": False, "why": "先说一句今天在做什么，比如「今天量身高，牛牛量完说自己又长高了」。"}
    if len(text) > MAX_INPUT:
        text = text[:MAX_INPUT]

    from config import MOCK
    if MOCK:
        return _mock(text, class_id, level)

    ck = _key(text, level)
    if ck in _cache:
        got = json.loads(json.dumps(_cache[ck]))     # 拷一份，别让调用方改脏缓存
    else:
        import ai_writer
        level_cn = LEVEL_LABELS.get(level, "中班")
        user = (f"老师说的话：\n{text}\n\n"
                f"班型：{level_cn}\n\n# 指标目录（只列了{level_cn}对应的表现行为）\n{compact_catalog(level)}")
        raw = ai_writer._chat([{"role": "system", "content": PROMPT + nursery_prompt(level)},
                               {"role": "user", "content": user}],
                              max_tokens=2500, temperature=0.2)
        got = ai_writer._parse_json(raw)
        _cache[ck] = json.loads(json.dumps(got))
        if len(_cache) > 40:
            _cache.pop(next(iter(_cache)))

    if not got.get("ok"):
        why = str(got.get("why") or "").strip()
        return {"ok": False, "why": why or "这段话看着不像在说孩子。这里写孩子在做什么就行，"
                                          "比如「今天量身高，牛牛量完说自己又长高了」。"}

    roster = _roster(class_id)
    scenes, unknown = [], []
    for sc in (got.get("scenes") or [])[:MAX_SCENES]:
        names = [str(x).strip() for x in (sc.get("names") or []) if str(x).strip()]
        ids, shown = [], []
        for n in names:
            st = match_name(n, roster)
            if st:
                if st["student_id"] not in ids:
                    ids.append(st["student_id"])
                    shown.append(st["name"])
            else:
                if n not in unknown:
                    unknown.append(n)
        note = _clean(sc.get("note"))
        quote = _clean(sc.get("quote"))
        if quote and quote not in note:
            # 孩子的原话是评价里最值钱的一句，别让它掉在 quote 字段里没人用
            note = (note + f"，他说：{quote}").strip("，")
        if not note and not ids:
            continue
        scenes.append({"names": names, "student_ids": ids, "student_names": shown,
                       "note": note, "quote": quote,
                       "indicators": _picks(sc.get("indicators"), level)})

    allv = got.get("all") or {}
    return {"ok": True,
            "text": text,
            "activity": _clean(got.get("activity"))[:30],
            "scenes": scenes,
            "all": {"note": _clean(allv.get("note")), "indicators": _picks(allv.get("indicators"), level)},
            "unknown": unknown}


def _mock(text: str, class_id: int, level: int) -> dict:
    """沙箱自测用：不联网，按名单里的头两个孩子编两个场景。"""
    if "不相关" in text or "写诗" in text:
        return {"ok": False, "why": "这里写孩子在做什么就行，比如「今天量身高，牛牛量完说自己又长高了」。"}
    roster = _roster(class_id)[:2]
    ind = indicator_by_id(4860 if level in NURSERY_LEVELS else 16)
    line = (indicator_lines(ind, level) or [""])[0]
    picks = _picks([{"indicator_id": ind["id"], "indicator_name": ind["name"], "behavior_line": line}], level)
    scenes = []
    for n, st in enumerate(roster):
        scenes.append({"names": [st["name"]], "student_ids": [st["student_id"]],
                       "student_names": [st["name"]],
                       "note": f"在活动区里{'专注地搭积木' if n == 0 else '帮同伴递材料'}",
                       "quote": "", "indicators": picks if n == 0 else []})
    return {"ok": True, "text": text, "activity": text[:12] or "区域游戏",
            "scenes": scenes, "all": {"note": text[:30], "indicators": []}, "unknown": []}


def _clean(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()


def _picks(raw, level: int) -> list:
    """校验模型挑的指标：id 要在目录里，非目录原句留空待确认。"""
    if level not in LEVEL_LABELS:
        raise ValueError("不支持的班级年龄档位，请重新同步班级")
    import pipeline
    out = []
    allowed_ids = {ind["id"] for ind in load_indicators(level)}
    for one in (raw or [])[:2]:
        try:
            ind = indicator_by_id(int(one.get("indicator_id")))
        except Exception:
            continue
        if not ind or ind["id"] not in allowed_ids or any(o["indicator_id"] == ind["id"] for o in out):
            continue
        line = _clean(one.get("behavior_line"))
        if line not in indicator_lines(ind, level):
            line = ""
        out.append({"indicator_id": ind["id"], "indicator_name": ind["name"],
                    "behavior_line": line,
                    "behavior_lines": [{"level": level, "line": line}] if line else []})
    return pipeline._clean_picks(out, level)


# ---------- 落到记录上 ----------
def apply_to_batch(bid: int, parsed: dict, level: int) -> dict:
    """把解析结果分给这一批里的每一条记录。认到脸之后调用才有意义。"""
    import pipeline
    if not parsed or not parsed.get("ok"):
        return {"touched": 0, "used": [], "left": []}
    act = parsed.get("activity") or ""
    base = (parsed.get("all") or {}).get("note") or act
    base_picks = (parsed.get("all") or {}).get("indicators") or []
    scenes = parsed.get("scenes") or []

    touched, used = 0, set()
    for it in db.rows("SELECT * FROM items WHERE batch_id=? ORDER BY id", (bid,)):
        if it.get("publish_status") == "published" or it.get("approved"):
            continue
        sids = [s["student_id"] for s in pipeline.item_students(it)]
        mine = [(i, sc) for i, sc in enumerate(scenes)
                if sc["student_ids"] and set(sc["student_ids"]) & set(sids)]
        if mine:
            parts = [act] if act else []
            for i, sc in mine:
                used.add(i)
                who = "、".join(sc["student_names"]) or "、".join(sc["names"])
                # 一张照片就一个孩子时不用报名字；合影才需要分清谁是谁
                parts.append(f"{who}：{sc['note']}" if (who and len(sids) > 1) else sc["note"])
        else:
            # 这张照片里的孩子，老师一个都没点到名 —— 就给整段的背景，别把别人的事安在他头上
            parts = [base or act]
        note = "；".join(x for x in dict.fromkeys(parts) if x)
        if note:
            db.update_item(it["id"], teacher_note=note)

        # 指标：一条记录只认出一个孩子，就整条挂上；认出好几个，各挂各的
        if len(mine) == 1 and len(sids) <= 1 and mine[0][1]["indicators"]:
            pipeline.set_indicators(it["id"], mine[0][1]["indicators"], level)
        else:
            for _, sc in mine:
                for sid in sc["student_ids"]:
                    if sid in sids and sc["indicators"]:
                        pipeline.set_student_indicators(it["id"], sid, sc["indicators"], level)
            if not mine and base_picks and sids:
                pipeline.set_indicators(it["id"], base_picks, level)
        touched += 1

    left = [{"names": sc["names"], "note": sc["note"]}
            for i, sc in enumerate(scenes) if i not in used]
    return {"touched": touched,
            "used": sorted(used),
            "left": left,
            "unknown": parsed.get("unknown") or []}


def save(bid: int, parsed: dict):
    try:
        db.exec("UPDATE batches SET brief=? WHERE id=?",
                (json.dumps(parsed, ensure_ascii=False), bid))
    except Exception as e:
        log.warning("存解析结果失败：%s", e)


def load(bid: int) -> dict:
    b = db.one("SELECT brief FROM batches WHERE id=?", (bid,)) or {}
    try:
        return json.loads(b.get("brief") or "{}")
    except Exception:
        return {}
