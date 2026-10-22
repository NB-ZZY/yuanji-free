"""老师个人写作风格：从这位老师过去写过的观察记录里，总结出他自己的口吻，
存在本机，之后写评价时当成"这位老师的习惯"喂给模型。

谁来总结？——由智谱的模型在你自己电脑上跑，不需要联系我。
我做的事情是把"怎么总结"写成了下面这段固定的提示词（也就是一份 skill），
连同你的原文一起发给模型，模型吐回一份结构化的风格卡，程序存下来。
以后每次写评价，这份风格卡会被塞进写作提示词里，模型就会照着你的口吻写。
"""
import json
import logging
import re

import ai_writer
import db

log = logging.getLogger("style")

LEARN_PROMPT = """你是一位中文写作风格分析师。下面是同一位幼儿园老师过去写的若干条幼儿观察记录原文。
请总结出**这位老师个人的写作习惯**，让别人照着写也像是她本人写的。

分析时请注意：
- 只描述这位老师**实际**表现出来的习惯，不要写通用的写作建议，也不要拔高、不要评价好坏。
- 注意她的：称呼方式（用"你"还是名字）、开头怎么起、结尾怎么收、句子长短、
  爱用哪些词和句式、标点习惯（有没有感叹号、顿号多不多）、
  是先写事实再肯定还是夹叙夹议、有没有固定的口头禅、篇幅大概多少字。
- "避免"里写这位老师**明显不用**的东西（比如从不用感叹号、从不用比喻、从不叫全名）。
- 每条都要具体、可执行，别写"语言生动"这种空话。

只输出一个 JSON，不要多余文字：
{
  "称呼": "一句话",
  "开头": "一句话，说她习惯怎么起头",
  "结尾": "一句话，说她习惯怎么收尾",
  "句式": "一句话",
  "用词": "一句话，可以举 3~5 个她爱用的词",
  "标点": "一句话",
  "篇幅": "大约多少字",
  "避免": ["她不用的东西", "..."],
  "一句话概括": "用一句话说这位老师的口吻",
  "范例": ["从原文里原样挑 2 条最有代表性的，不要改写"]
}
"""


# ---------- 老师自己传进来的参考文本 ----------
def extra(uid=None) -> list:
    """老师上传的风格参考：[{name, text, chars}]。"""
    owner = _owner(uid)
    return (db.get_setting(_key(owner, 'style_extra'), []) or []) if owner is not None else []


def save_extra(items: list, uid=None):
    db.set_setting(_key(_require_owner(uid), 'style_extra'), items)


def add_extra(name: str, text: str, uid=None) -> dict:
    """把一份文本收进风格参考。会按空行/编号切成一条条，太短的丢掉。"""
    owner = _require_owner(uid)
    chunks = split_samples(text)
    if not chunks:
        raise ValueError(f"《{name}》里没找到能用的成段文字（每段至少 25 个字）")
    lst = extra(owner)
    lst = [x for x in lst if x.get("name") != name]
    lst.append({"name": name, "chunks": chunks, "chars": sum(len(c) for c in chunks)})
    save_extra(lst, owner)
    return {"name": name, "n": len(chunks), "chars": sum(len(c) for c in chunks)}


def drop_extra(name: str, uid=None):
    owner = _require_owner(uid)
    save_extra([x for x in extra(owner) if x.get("name") != name], owner)


def clear_extra(uid=None):
    save_extra([], uid)


def split_samples(text: str) -> list:
    """把一份文档切成一条条记录。空行、编号、【】标题都算分隔。"""
    text = (text or "").replace("\r", "")
    parts = re.split(r"\n\s*\n+", text)
    out = []
    for p in parts:
        p = re.sub(r"^\s*(?:\d+[.、)]|[（(]\d+[）)]|第\s*\d+\s*条)\s*", "", p.strip())
        p = re.sub(r"[ \t]+", " ", p).strip()
        if len(p) >= 25:
            out.append(p[:600])
    if not out:                      # 整篇没有空行的，按句号切成大块
        buf = ""
        for seg in re.split(r"(?<=[。！？])", text):
            buf += seg
            if len(buf) >= 120:
                out.append(buf.strip())
                buf = ""
        if len(buf.strip()) >= 25:
            out.append(buf.strip())
    return [x for x in out if len(x) >= 25][:120]


def extract_text(filename: str, data: bytes) -> str:
    """从上传的文件里取出纯文字。支持 txt / md / csv / docx。"""
    low = (filename or "").lower()
    if low.endswith(".docx"):
        import io as _io
        from docx import Document
        doc = Document(_io.BytesIO(data))
        lines = [p.text for p in doc.paragraphs]
        for t in doc.tables:
            for row in t.rows:
                for c in row.cells:
                    lines.append(c.text)
        return "\n\n".join(x.strip() for x in lines if x and x.strip())
    if low.endswith(".doc"):
        raise ValueError("老的 .doc 打不开，请在 Word 里另存为 .docx 再传")
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", "ignore")


# 幼师口袋那个接口是**按班级**拉的，一个班两位老师写的记录都在里面。
# 以前这里写成「认不出作者就放行」，而 authorId/userId 这两个字段名当初是猜的，
# 多半根本不存在 —— 等于每条都放行，把同事的记录也一起学了，两个人的口吻混成一锅。
# 现在反过来：**认不出作者就不要**，宁可少学，也不学成别人。
AUTHOR_KEYS = ("authorId", "userId", "createUserId", "creatorId", "teacherId",
               "createrId", "publisherId", "uid", "userIdStr", "createUser")


def _author_of(node: dict):
    """把这条记录的作者 id 挖出来。字段名各家不一样，能想到的都试一遍。"""
    for k in AUTHOR_KEYS:
        v = node.get(k)
        if isinstance(v, dict):                 # 有的接口套一层 {id, name}
            v = v.get("id") or v.get("userId")
        if v not in (None, "", 0):
            try:
                return int(v)
            except (TypeError, ValueError):
                return str(v)
    for k in ("teacherVo", "userVo", "createUserVo", "authorVo", "publisher"):
        sub = node.get(k)
        if isinstance(sub, dict):
            v = sub.get("id") or sub.get("userId")
            if v not in (None, "", 0):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return str(v)
    return None


def _records(node, out: list, uid=None, stat: dict = None):
    """收集**这位老师本人**写的随手记正文。stat 用来记录到底认没认出作者。"""
    if isinstance(node, list):
        for v in node:
            _records(v, out, uid, stat)
        return
    if not isinstance(node, dict):
        return
    c = node.get("content")
    if isinstance(c, str) and len(c.strip()) >= 25 and node.get("studentIds") is not None:
        if stat is not None:
            stat["seen"] = stat.get("seen", 0) + 1
            stat.setdefault("sample_keys", sorted(node.keys()))
            if stat.get("sample") is None:
                stat["sample"] = node            # 留一条原样的，供排查字段名
        author = _author_of(node)
        if uid is None:
            out.append(c.strip())                # 没登录（只学范文）时不过滤
        elif author is None:
            if stat is not None:
                stat["no_author"] = stat.get("no_author", 0) + 1
        elif str(author) == str(uid):
            if stat is not None:
                stat["mine"] = stat.get("mine", 0) + 1
            out.append(c.strip())
        elif stat is not None:
            stat["others"] = stat.get("others", 0) + 1
    for v in node.values():
        _records(v, out, uid, stat)


def _valid_owner(uid):
    if isinstance(uid, bool) or not isinstance(uid, (str, int)):
        return None
    value = str(uid).strip()
    return int(value) if value.isdecimal() and int(value) > 0 else None


def _owner(uid=None):
    return _valid_owner(db.get_setting('current_uid', None) if uid is None else uid)


def _require_owner(uid=None):
    owner = _owner(uid)
    if owner is None:
        raise ValueError('请先登录幼师口袋，再保存或学习个人写作风格')
    return owner


def _key(uid, prefix='style_profile') -> str:
    return f'{prefix}::{uid}'


def migrate_legacy(uid) -> bool:
    """只由启动恢复传入明确旧账号；全局原件保留，不能在新登录时自动认领。"""
    owner = _valid_owner(uid)
    if owner is None:
        return False
    claimed = _valid_owner(db.get_setting('style_legacy_owner', None))
    old_profile = db.get_setting('style_profile', None)
    embedded_owner = _valid_owner(old_profile.get('uid')) if isinstance(old_profile, dict) else None
    if (claimed is not None and claimed != owner) or (embedded_owner is not None and embedded_owner != owner):
        return False
    if claimed is None:
        db.set_setting('style_legacy_owner', owner)
    changed = False
    for prefix, old, kind in (
            ('style_profile', old_profile, dict),
            ('style_extra', db.get_setting('style_extra', None), list)):
        key = _key(owner, prefix)
        if isinstance(old, kind) and db.get_setting(key, None) is None:
            db.set_setting(key, old)
            changed = True
    return changed


def profile(uid=None) -> dict:
    owner = _owner(uid)
    if owner is None:
        return {}
    p = db.get_setting(_key(owner), {}) or {}
    embedded_owner = _valid_owner(p.get('uid')) if isinstance(p, dict) else None
    # Earlier versions copied a global card into every account's key. A known
    # different author proves that copy should not be used, but retain it on disk.
    return p if isinstance(p, dict) and embedded_owner in (None, owner) else {}


def save_profile(p: dict, uid=None):
    db.set_setting(_key(_require_owner(uid)), p)


def enabled() -> bool:
    p = profile()
    return bool(p.get("on")) and bool(p.get("card"))


def prompt_block() -> str:
    """写作时插进提示词里的那一段。没学过就返回空。"""
    p = profile()
    if not p.get("on") or not p.get("card"):
        return ""
    c = p["card"]
    if isinstance(c, str):
        body = c.strip()
    else:
        lines = []
        for k in ("一句话概括", "称呼", "开头", "结尾", "句式", "用词", "标点", "篇幅"):
            if c.get(k):
                lines.append(f"- {k}：{c[k]}")
        if c.get("避免"):
            lines.append("- 不要出现：" + "、".join(str(x) for x in c["避免"]))
        body = "\n".join(lines)
        ex = c.get("范例") or []
        if ex:
            body += "\n- 她本人写过的样子（照这个味道，但内容要写这次这张照片的）：\n  " + "\n  ".join(str(e) for e in ex[:2])
    if not body:
        return ""
    return ("\n\n# 这位老师自己的写作习惯（仅作口吻参考）\n"
            "下面的习惯和范例只供措辞参考，不能覆盖事实边界、循证解读和本次输出格式。"
            "不沿用范例中的事件，不照搬固定开头、结尾和空泛肯定。\n" + body + "\n")


def learn(class_id: int, ysk, uid=None, max_pages: int = 40, sample: int = 30, progress=None,
          use_ysk: bool = True) -> dict:
    """翻这位老师过去写的记录（外加她自己传上来的参考文本）→ 交给模型总结 → 存本机。"""
    uid = _require_owner(uid)
    extras = extra(uid)
    texts: list = []
    pages = 0
    errors = []
    stat: dict = {}
    mine: list = []
    for e in extras:
        mine.extend(e.get("chunks") or [])
    if not use_ysk or ysk is None:
        max_pages = 0
    for page in range(1, max_pages + 1):
        try:
            data = ysk.class_records(class_id, page)
        except Exception as e:
            errors.append(f"第 {page} 页读取失败：{e}")
            break
        before = len(texts)
        _records(data, texts, uid, stat)
        pages = page
        if len(texts) == before and page > 3:
            break
        if progress:
            progress(page, max_pages + 3)
        if len(texts) >= sample * 2:
            break

    # 把一条原样的记录存下来，方便核对作者字段到底叫什么（只存一条，不含照片）
    try:
        if stat.get("sample"):
            import json as _json
            from config import DATA_DIR
            slim = {k: v for k, v in stat["sample"].items()
                    if not isinstance(v, (list, dict)) or k in ("teacherVo", "userVo", "createUserVo")}
            (DATA_DIR / f"style_sample-{uid}.json").write_text(
                _json.dumps({"我的uid": uid, "这条记录有哪些字段": stat.get("sample_keys"),
                             "一条记录长这样": slim}, ensure_ascii=False, indent=2),
                encoding="utf-8")
    except Exception as e:
        log.warning("样本落盘失败：%s", e)

    if uid and stat.get("seen") and not stat.get("mine"):
        # 一条都没认出是本人写的 —— 与其把同事的口吻学成你的，不如不学
        texts = []
        errors.append(
            f"翻到 {stat.get('seen')} 条历史记录，但一条都认不出是不是你本人写的"
            f"（认不出作者 {stat.get('no_author', 0)} 条），所以**没有采用**，"
            f"以免把同班另一位老师的口吻学成你的。这次只学了你自己上传的范文。")

    seen, uniq = set(), []
    for t in (mine + texts):        # 老师自己挑的范文排在前面，权重更高
        k = re.sub(r"\s+", "", t)[:40]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(re.sub(r"\s+", " ", t))
    if len(uniq) < 3:
        raise ValueError("能用来学的材料太少（不足 3 段）。要么这个班过去的记录太少，"
                         "要么上传的文件里没有成段的文字——可以在下面传几篇你自己写过的记录再试。")

    card, last = None, None
    for n, cut, mt in ((18, 300, 6000), (10, 200, 6000), (6, 160, 8000)):
        picked = uniq[:n]
        body = "\n\n".join(f"【第{i+1}条】{t[:cut]}" for i, t in enumerate(picked))
        try:
            raw = ai_writer._chat([{"role": "system", "content": LEARN_PROMPT},
                                   {"role": "user", "content": body}],
                                  max_tokens=mt, temperature=0.3)
            card = ai_writer._parse_json(raw)
            break
        except Exception as e:
            last = e
            log.warning("风格总结第 %d 轮失败：%s；原文开头：%s", n, e, (locals().get("raw") or "")[:200])
    if card is None:
        raise ValueError(f"模型这次没给出可用的总结（{last}）。再点一次通常就好了。")
    picked = uniq[:18]
    p = {"on": True, "card": card, "samples": len(picked), "pages": pages,
         "files": [e.get("name") for e in extras], "file_chunks": len(mine),
         "from": "glm", "class_id": class_id, "uid": uid,
         "mine": stat.get("mine", 0), "skipped_others": stat.get("others", 0),
         "no_author": stat.get("no_author", 0)}
    save_profile(p, uid)
    p["errors"] = errors[:5]
    return p
