"""AI 写作模块（内置"观察记录写作"技能）。

一次调用完成：看图 + 结合老师简述 → 选当前体系的指标 → 选当前档位的表现行为 →
写一段约 100 字、第一人称"你"的评价。输出 JSON，便于程序落库与老师审核。
"""
import base64
import io
import json
import logging
import re
import time
from difflib import SequenceMatcher
from typing import Optional

import requests
from PIL import Image, ImageOps

import punct
from writing_skill import COMMON_PROMPT, FINAL_REVIEW, nursery_prompt
from config import MOCK, ZHIPU_BASE, ZHIPU_MODEL, ZHIPU_THINKING, zhipu_key
from knowledge import (LEVEL_CN, LEVEL_LABELS, NURSERY_LEVELS, compact_catalog, indicator_by_id,
                       indicator_by_name, indicator_lines, load_indicators)

log = logging.getLogger("ai")

SKILL_PROMPT = COMMON_PROMPT + punct.RULE + """你是一名幼儿园老师，正在为班里的孩子写随手记。你必须严格依据用户给出的指标目录工作，指标只能从目录里选，不能自己编。

# 输入
1. 一张孩子活动的照片（看孩子在做什么、用什么材料、身体姿态、和谁在一起）。
2. 老师的简短说明（可能为空）。说明里明确的事实和观察重点优先；其中的推测仍是推测，写作要求不等于已经发生的行为。
3. 孩子姓名、班型。

# 任务
1. 若输入提供了老师已关联的完整指标列表，保留全部选择（包括明确不选），不得删减、替换或自行增加。未给出已关联列表时，根据照片和说明选 0~2 条：证据最充分的放第一条；第二条也须有明确依据，证据不足可不选。
2. 自动选择表现行为须有相应证据，使用当前档位的目录原句；只能关联主题、不能证明具体表现时，behavior_line 留空待老师确认。老师已选的行为、档位和留空待确认项必须保留，不要补选或更改。
3. 用一句话客观描述孩子的行为（behavior，第三人称，只写已知事实，不评价；通常 20~40 字，信息少可更短）。
4. 写一段评价 text：
   - 一段，不分行，不加标题。长度按本次篇幅参考，信息少可以短一些，不为凑字数补事实。
   - 用第二人称"你"，温和、口语化，像老师蹲下来跟孩子讲话。
   - 抓住一个有依据的动作或变化，点明这次使用的办法、调整或需要；合适时补一句具体的小建议。不要固定写成“细节＋肯定＋期待”，也不必硬塞完整分析链。
   - 只有静态姿势时，可以只写所见状态；合适时提一个以后可尝试的方向，不强求解释学习策略或当前不足。
   - 照片看不到、老师未提供的过程或对话只能作为推测，必须写明“推测，待老师确认”，不能当成事实或原话。
   - 身高、体重等实测数值只能使用老师明确提供的数据；不得凭照片或指标目录编造测量数值、增长幅度或达标结论，也不能把这些测量事实当作合理推测。
   - 把指标意图自然融进去，但不照抄指标原文，不出现"指标""表现行为""领域"这些词。
   - 用词平实、具体，不用比喻、不用拟人、不用"棒棒的""小天使""真棒"之类的空话，不堆形容词。
   - 以每个孩子的实际证据确定侧重点，检查同批句式重复，不能只替换姓名。事实相近可以有相近表达，除记录涉及孩子的称呼外不出现其他真实人名。
   - 如果要叫孩子的名字，只用"平时怎么叫他"里给的那个称呼（小名），不要写全名。

# 范例
指标：能通过观察图书的连续画面，大致说出故事的主要情节（阅读小能手·表现行为三）
说明：孩子在看图画书，能说出图中人物的动作，还讲了小熊走进森林找东西吃。
text：你说出了图中人物的动作，还讲到小熊去森林找吃的。你在把画面里的事情连起来。接着翻一页，看看小熊又遇见了什么。
说明只提供了话的意思，范例不把它改成孩子的直接引语，也不据此断言已经能完整复述故事。

# 输出
只输出一个 JSON 对象，不要任何多余文字：
{"indicators": [{"indicator_id": 数字, "indicator_name": "指标名", "behavior_line": "目录原句"}],
 "behavior": "客观行为描述", "text": "评价文字", "confidence": 0到1的小数, "reason": "选这些指标的原因（一句话）"}
indicators 保留老师已关联的列表；自动选择时放 0~2 条，第一条是主要的。没有合适指标时返回空列表，仍可依据已知事实写正文。
confidence 仅表示模型对指标匹配的自估把握，不是幼儿能力得分，也不是达标概率；没有关联指标时填 0。
"""


def _style_block() -> str:
    """老师自己的写作口吻，不能覆盖事实边界。"""
    try:
        import style
        return style.prompt_block()
    except Exception:
        return ""


def _img_px() -> int:
    """给模型看的照片边长。观察记录只要看清孩子在干什么，768 足够，
    再大将增加图片输入的 token 用量；此处统一限制发送尺寸。"""
    try:
        from config import ai_conf
        v = int(ai_conf().get("img_px") or 768)
        return max(384, min(1600, v))
    except Exception:
        return 768


def _img_b64(path: str, max_side: int = 0) -> str:
    max_side = max_side or _img_px()
    im = Image.open(path).convert("RGB")
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


MAX_IMAGES = 4          # 一条记录最多把 4 张图交给模型（再多又慢又贵，收益很小）



def _catalog_block(level: int) -> str:
    """指标目录按年龄档隔离，放进固定前缀。"""
    return (nursery_prompt(level) + f"\n\n# 指标目录（仅列出{_level_note(level)}对应的官网原文）\n"
            + compact_catalog(level))


def _level_note(level: int) -> str:
    if level not in LEVEL_LABELS:
        raise ValueError("不支持的班级年龄档位，请重新同步班级")
    label = LEVEL_LABELS[level]
    return label if level in NURSERY_LEVELS else f"{label}（表现行为{LEVEL_CN[level]}档）"


def _check_picks(picks, level: int):
    """生成、重写共用入口：在调用模型前挡住串体系的旧选择。"""
    _level_note(level)
    catalog = {ind["id"]: ind for ind in load_indicators(level)}
    for pick in picks or []:
        ind = catalog.get(pick.get("indicator_id"))
        if not ind:
            raise ValueError("已关联指标不属于当前班级的评价体系，请重新选择")
        if pick.get('grade') is not None:
            if level in NURSERY_LEVELS or pick['grade'] not in (2, 4):
                raise ValueError("仅常规体系可单独选择表现行为二或四")
            if pick.get('behavior_lines') or pick.get('behavior_line'):
                raise ValueError("表现行为二、四不选择具体表现原句")
        if level not in NURSERY_LEVELS:
            continue
        for key, expected in (("indicator_name", ind["name"]), ("domain", ind["domain"]),
                              ("subdomain", ind["subdomain"]), ("medal_id", ind["medal_id"]),
                              ("systemId", 35), ("system_id", 35)):
            if key in pick and pick[key] != expected:
                raise ValueError("托育指标信息不匹配，请重新选择")
        valid = indicator_lines(ind, level)
        if pick.get("behavior_line") and pick["behavior_line"] not in valid:
            raise ValueError("托班表现行为必须使用24—36个月档的官网原句")
        for behavior in pick.get("behavior_lines") or []:
            if (not isinstance(behavior, dict)
                    or behavior.get("level", level) not in (128, "128")
                    or behavior.get("grade", level) not in (128, "128")
                    or behavior.get("line") not in valid):
                raise ValueError("托班表现行为必须使用24—36个月档的官网原句")


def _default_indicator(level: int) -> dict:
    catalog = load_indicators(level)
    ind = indicator_by_id(4860 if level in NURSERY_LEVELS else 16)
    return ind or next(ind for ind in catalog if indicator_lines(ind, level))


def _msgs(fixed: str, varying: str, image_path) -> list:
    """按「固定的在前、变动的在后、照片垫底」拼消息。

    DeepSeek 这类前缀缓存是从第一个字开始逐字比对的，撞上第一处不同，
    **后面全部作废**。所以：
      · system 里只放每条都一模一样的东西（写作规矩、指标目录、篇幅、老师风格）；
      · 变动的（这次的写法、孩子姓名、老师说明）放 user 的文本里；
      · 照片每张都不同，必须垫在最后 —— 它要是排在前面，后面那几千 token
        的指标目录就白付钱了。
    """
    return [{"role": "system", "content": fixed},
            {"role": "user", "content": [{"type": "text", "text": varying}] + _img_parts(image_path)}]


def _paths(image_path) -> list:
    """允许传一个路径，也允许传一串路径（同一个孩子的多张照片 / 作品照）。"""
    if isinstance(image_path, (list, tuple)):
        return [str(x) for x in image_path if x][:MAX_IMAGES]
    return [str(image_path)] if image_path else []


def _img_parts(image_path) -> list:
    ps = _paths(image_path)
    out = []
    for i, p in enumerate(ps):
        try:
            out.append({"type": "image_url", "image_url": {"url": _img_b64(p)}})
        except Exception as e:
            log.warning("第 %d 张图读不了：%s", i + 1, e)
    return out


# 说明一样的几条，靠这八个方向岔开写法，不然五条读起来像一条
# ---------- 同一批不重样：事先给每条派一套写法 ----------
# 以前靠的是"让后写的看见前面写了什么，明说别重样"——那要求一条一条串着写，
# 二十几张照片得跑五六分钟，而且批量栏那个按钮本来就是并发的，等于没防。
# 现在按序号分配可选措辞方向，证据不支持的方向必须跳过。五个维度各转各的，
# 四十条之内不会撞车，相邻两条平均有 4.8 个维度不一样。
# 这样彼此不用看见对方，可以放开并发，反而更快。

OPENINGS = [
    "从他手上的那个动作起头，第一句就写他在干什么。",
    "从他身体的样子起头，站姿、手的位置、眼睛看哪儿。",
    "从旁边的环境或者材料起头，再落到他身上。",
    "资料有前后变化时，可以从这次的变化起头；没有就写已知动作。",
    "资料有同伴互动时，可以从有记录的来往起头；没有就跳过。",
    "可以从资料已有的材料或动作细节起头，不新增次数和数值。",
    "资料记下停顿或调整时，可以从那里起头，不猜测内心。",
    "资料同时提供过程和结果时，可以先说结果再说做法。",
]
RHYTHMS = [
    "句子短一点，一句一个动作，干脆利落，别拖。",
    "长短交错着写，先铺一句长的交代过程，再用一句短的收住。",
    "按事情的联系自然分句，一句话说清一件事，不一逗到底。",
]
ENDINGS = [
    "最后收在一个画面上，把他当时的样子定格住，不作评论。",
    "最后收在一句具体的下一步，他接下来可以试试什么。",
    "最后收在对能力的肯定上，而且要说清楚好在哪里，不要空夸。",
    "最后收在一个小小的期待上，落到具体的事情上，别喊口号。",
]
FOCUS = [
    "着重写他的动作和手上的细节。",
    "写资料中有记录的表情或目光方向，不由此断定情绪和专注程度。",
    "有遇到困难和调整的记录才写这些，没有就保留已有表现。",
    "着重写他和材料、和场地之间的关系。",
    "资料明确给出先后时才写前后联系，不补等待时长和尝试次数。",
]


def style_combo(seed: int) -> dict:
    """按序号派一套写法。步长互质，四十条之内不重复。"""
    seed = int(seed)
    return {
        "opening": OPENINGS[seed % len(OPENINGS)],
        "rhythm": RHYTHMS[(seed * 5) % len(RHYTHMS)],
        "ending": ENDINGS[(seed * 3) % len(ENDINGS)],
        "focus": FOCUS[(seed * 2) % len(FOCUS)],
        "quote": (seed * 3) % 7 < 3,
    }


VARIANTS = OPENINGS          # 老名字留着，别的地方还在引用


def _peer_note(peers: list, variant: int) -> str:
    """同一批说明一样的几条，写法事先就分开了；已经写好的那几条再拿来当反例。"""
    c = style_combo(variant)
    out = ["\n\n# 可选表达方向（证据不支持就跳过，不是必填情节）",
           f"- 起头：{c['opening']}",
           f"- 节奏：{c['rhythm']}",
           f"- 重点：{c['focus']}",
           f"- 收尾：{c['ending']}",
           ("- 老师提供了孩子原话时可以引用；没有原话就只写已知动作，不编造对话。" if c["quote"]
            else "- 可以优先写动作；原话若是理解本次学习的关键证据，仍应保留。")]
    if peers:
        heads = "、".join(f"「{(p or '')[:10]}」" for p in peers if p)
        # 留出结尾，详细档的常见重复正是最后一句评价/建议。
        lines = "\n".join(f"  {i+1}）{p if len(p) <= 260 else p[:160] + '……' + p[-100:]}"
                          for i, p in enumerate(peers))
        out += ["\n# 同一批已经写过这几条，务必写得不一样",
                lines,
                f"\n下面这些开头**已经用过了，换一个**：{heads}",
                "逐句自查开头、句式、收尾和段落格式，不能只替换名字和材料。没有证据时不为求不同编造细节。"]
    return "\n".join(out) + "\n"


def _kids_note(children: list) -> str:
    """一条记录里评好几个孩子时，告诉模型每个都要写到。"""
    ks = [k for k in (children or []) if k]
    if len(ks) <= 1:
        return ""
    return ("\n注意：这一条记录要同时评 " + str(len(ks)) + " 个孩子：" + "、".join(ks) + "。\n"
            "请写成**一段**话（不要分行、不要编号），关注每个孩子，但不按人数凑句子：\n"
            "- 先说这几个人合起来在做的这件事，再一个个说各自在做什么、做得怎么样；\n"
            "- 有个体动作证据才分别说，没有就保留已知的共同事实，不给每人编一个不同动作；\n"
            "- 称呼就用上面给的这几个称呼，顺序也照这个来。\n")


def _multi_note(image_path) -> str:
    n = len(_paths(image_path))
    if n <= 1:
        return ""
    return (f"\n注意：这次给了你 {n} 张照片，是同一条记录的素材，可能包含活动或作品。"
            "结合老师说明写一条记录；图片排列不证明时间顺序，不能自行补成连续事件。"
            "人物、作品归属和前后关系只按实际证据确定，不写看不见的中间过程。\n")


class Truncated(ValueError):
    """模型话没说完就被 max_tokens 掐了 —— 这种要加预算重来，不是换个说法就能好的。"""



def _wants_json(messages: list) -> bool:
    """这一次是不是在要 JSON —— 只有要 JSON 的调用才开 json 模式。"""
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and "JSON" in c.upper():
            return True
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and "JSON" in str(part.get("text", "")).upper():
                    return True
    return False


def _chat_full(messages: list, max_tokens: int = 4000, temperature: float = 0.8,
               think=None, json_mode=None) -> tuple:
    """返回 (正文, 结束原因)。GLM-5.3 是强制思考的，思考也算在 max_tokens 里，
       所以预算给小了会出现"思考完了，正文只写了一半"甚至正文全空的情况。"""
    from config import ai_conf
    cf = ai_conf()
    if not str(cf.get('key') or '').strip():
        raise ValueError('请先在设置中填写自己的 AI API 密钥和模型。')
    body = {"model": cf["model"], "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature}
    prov = (cf.get("provider") or "").lower()
    th = cf.get("thinking")
    effort = cf.get("observation_effort")
    if effort in ('high', 'max'):
        # Reasoning and final text share this budget. Higher effort must not use
        # the small budget formerly reserved for a no-thinking helper call.
        body['max_tokens'] = max(max_tokens, 8192 if effort == 'high' else 16384)
    # An explicit teacher choice applies to every stage, including helpers that
    # historically passed think=False. Other callers retain defaults.
    if effort and effort != 'default':
        th = None if effort == 'off' else {'type': 'enabled', 'effort': effort}
    elif think is False:
        th = None
    if prov == 'zhipu' and str(cf.get('model', '')).lower().startswith('glm-5.3'):
        th = dict(th or {'type': 'enabled', 'effort': 'low'})
        th['type'] = 'enabled'
    if prov in ('deepseek', 'zhipu'):
        if th:
            th = dict(th)
            eff = th.pop('effort', None) or cf.get('reasoning_effort')
            if eff in ('low', 'high', 'max'):
                body['reasoning_effort'] = eff
            body['thinking'] = th
        elif prov == 'deepseek':
            body['thinking'] = {'type': 'disabled'}
    import ai_progress
    token = ai_progress.begin_request()
    streaming = token is not None and prov in ('zhipu', 'deepseek')
    if streaming:
        body['stream'] = True
    if cf.get("json_mode") and (json_mode if json_mode is not None else _wants_json(messages)):
        # 模型自己保证吐合法 JSON，就不用再靠我们那套"截断了再重试一遍"补救了 ——
        # 一次重试就是白白多等一分钟，这是老师觉得慢的主要来源。
        body["response_format"] = {"type": "json_object"}
    r = None
    try:
        for wait in (0, 3, 8, 20):
            if wait:
                ai_progress.received(token, 'retrying')
                time.sleep(wait)
            kwargs = {'stream': True} if streaming else {}
            r = requests.post(f"{cf['base'].rstrip('/')}/chat/completions",
                              headers={"Authorization": f"Bearer {cf['key']}"},
                              json=body, timeout=(30, 180), **kwargs)
            if r.status_code in (429, 500, 502, 503, 504):
                log.warning("接口忙（%s），稍后重试", r.status_code)
                if wait != 20:
                    r.close()
                continue
            break
        if r.status_code != 200:
            try:
                msg = r.json().get('error', {}).get('message')
            except Exception:
                msg = r.text[:200]
            raise RuntimeError(f"写作接口错误 {r.status_code}：{msg}")
        j = _read_chat_response(r, streaming, token)
    except Exception:
        ai_progress.end_request(token, ok=False)
        raise
    else:
        ai_progress.end_request(token)
    finally:
        if r is not None:
            r.close()
    # 记一笔真实的缓存命中 —— 以后调 prompt 有没有效果，看日志就知道，不用猜
    try:
        u = j.get("usage") or {}
        hit = u.get("prompt_cache_hit_tokens")
        miss = u.get("prompt_cache_miss_tokens")
        if hit is not None or miss is not None:
            h, m = int(hit or 0), int(miss or 0)
            log.info("用量：入 %d（命中 %d / 未命中 %d，命中率 %.0f%%）出 %d",
                     h + m, h, m, (h * 100.0 / (h + m)) if (h + m) else 0,
                     int(u.get("completion_tokens") or 0))
        elif u:
            log.info("用量：入 %s 出 %s", u.get("prompt_tokens"), u.get("completion_tokens"))
    except Exception:
        pass
    ch = (j.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    txt = msg.get("content") or ""
    # reasoning_content 是模型的推理过程，不能拿它替代交给老师的正文。
    return txt, (ch.get("finish_reason") or "")


def _read_chat_response(response, streaming, token):
    """Collect the final answer from SSE; reasoning chunks only change status."""
    import ai_progress
    if not streaming or 'application/json' in response.headers.get('content-type', '').lower():
        result = response.json()
        content = ((result.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        ai_progress.received(token, 'receiving', len(content) if isinstance(content, str) else 0)
        return result
    response.encoding = 'utf-8'
    content, finish, usage = [], '', {}
    for raw in response.iter_lines(decode_unicode=True):
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        if not raw or not raw.startswith('data:'):
            continue
        data = raw[5:].strip()
        if data == '[DONE]':
            break
        packet = json.loads(data)
        if packet.get('error'):
            raise RuntimeError('模型流式响应失败，请重试')
        usage = packet.get('usage') or usage
        for choice in packet.get('choices') or []:
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            # Do not store, emit, or log reasoning_content, even when content is empty.
            if delta.get('reasoning_content'):
                ai_progress.received(token, 'thinking')
            part = delta.get('content')
            if isinstance(part, str) and part:
                content.append(part)
                ai_progress.received(token, 'receiving', len(part))
            if choice.get('finish_reason'):
                finish = choice['finish_reason']
    if not finish:
        raise Truncated('模型连接提前结束，未收到完整正文，请重试')
    return {'choices': [{'message': {'content': ''.join(content)}, 'finish_reason': finish}], 'usage': usage}


def _chat(messages: list, max_tokens: int = 4000, temperature: float = 0.8,
          think=None, json_mode=None) -> str:
    txt, fin = _chat_full(messages, max_tokens, temperature, think=think, json_mode=json_mode)
    if fin == "length":
        raise Truncated("模型输出被截断，请重试")
    if not isinstance(txt, str) or not txt.strip():
        raise ValueError("模型未返回可用正文，请重试")
    return txt


_END_OK = ",:}] \t\r\n"


def _fix_inner_quotes(s: str) -> str:
    """模型写评价时爱引用孩子的话。规矩里写的是中文引号，但它时不时手滑打成英文的 " ——
       一个没转义的半角引号就能让整串 JSON 从那里断掉（日志里那条
       「Expecting ',' delimiter: line 1 column 103」就是它）。
       这里按"这个引号后面跟的是不是 , : } ]"来判断它到底是收尾的引号，
       还是掉在句子中间的，掉在中间的就换成中文引号。"""
    out, i, in_str, esc, opening = [], 0, False, False, True
    n = len(s)
    while i < n:
        c = s[i]
        if not in_str:
            out.append(c)
            if c == '"':
                in_str, esc, opening = True, False, True
            i += 1
            continue
        if esc:
            out.append(c); esc = False; i += 1; continue
        if c == "\\":
            out.append(c); esc = True; i += 1; continue
        if c == '"':
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j >= n or s[j] in ",:}]":
                out.append(c); in_str = False            # 正经的收尾引号
            else:
                out.append("“" if opening else "”")      # 掉在句子中间的，换成中文引号
                opening = not opening
            i += 1
            continue
        out.append(c); i += 1
    return "".join(out)


def _balanced(s: str) -> str:
    r"""从第一个 { 开始，按括号配对截出一个完整对象。
       贪婪的 \{.*\} 会把后面多余的话一起吞进来，被截断时又一个都匹配不上。"""
    start = s.find("{")
    if start < 0:
        return ""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:]          # 没配平 —— 多半是被截断了


def _close_open(s: str) -> str:
    """被截断的 JSON：把没关的引号和括号补上，能救一点是一点。"""
    stack, in_str, esc = [], False, False
    for c in s:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]" and stack:
            stack.pop()
    out = s.rstrip().rstrip(",")
    if in_str:
        out += '"'
    for c in reversed(stack):
        out += "}" if c == "{" else "]"
    return out


def _parse_json(s: str) -> dict:
    """把模型吐的东西解析成 JSON。它不会每次都规规矩矩，所以这里层层兜底。"""
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip())
    body = _balanced(s)
    if not body:
        raise ValueError("模型没有返回 JSON")
    truncated = not body.rstrip().endswith("}")
    for cand in (body,
                 _fix_inner_quotes(body),
                 re.sub(r",\s*([}\]])", r"\1", _fix_inner_quotes(body)),
                 _close_open(_fix_inner_quotes(body))):
        try:
            got = json.loads(cand)
            if isinstance(got, dict):
                return got
        except Exception:
            continue
    if truncated:
        raise Truncated("JSON 写到一半就断了")
    raise ValueError("模型没有返回 JSON")


def _clean_text(value) -> str:
    """先验证正文，再清标点，避免把对象/思考/格式标记洗成一段看似正常的文字。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("评价正文为空或格式错误")
    value = value.strip()
    if value.startswith("```"):
        match = re.fullmatch(r"```(?:json|text)?\s*\n?(.*?)\s*```", value, re.S | re.I)
        if not match:
            raise ValueError("评价正文包含未完成的代码块")
        value = match.group(1).strip()
    if value.startswith("{"):
        # 重写接口偶尔仍返回结构化结果，只取完整对象中明确的 text 字段。
        try:
            payload = json.loads(value)
        except (TypeError, ValueError) as e:
            raise ValueError("评价正文是无效的结构化回复") from e
        value = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("模型只返回了反馈信息，没有评价正文")
        value = value.strip()
    if (re.search(r"[{}\[\]`]|\*\*|</?(?:think|analysis)>|\b(?:reasoning_content|indicator_id)\b", value, re.I)
            or re.search(r"(?:^|\n)\s*[\"']?(?:feedback|reason|analysis|text|content|message)[\"']?\s*[:：]", value, re.I)
            or re.search(r"(?m)^\s*(?:#{1,6}\s|[-*]\s|\d+(?:\.(?!\d)|、)\s*)", value)
            or re.search(r"^(?:好的[，,！!。]|当然可以|以下是|下面是|根据(?:你的|您的)要求|"
                         r"抱歉|对不起|请(?:提供|补充)|无法(?:根据|识别|生成|完成)|我(?:会|将|需要|已|想)|"
                         r"(?:分析|思考|评价(?:文字|内容)?|反馈|正文)[：:])", value)
            or re.search(r"(?m)^\s*(?:(?:自查(?:结果|报告|反馈|说明)?|(?:修改|重写|调整)(?:说明|反馈)|"
                         r"(?:检查|处理)结果)\s*[：:]|自查(?:通过|未发现|无重复)|"
                         r"已(?:按|根据).{0,15}(?:要求|建议))", value)):
        raise ValueError("模型返回了说明、列表或结构化内容，需要重新生成正文")
    text = punct.clean(re.sub(r"[\r\n]+", " ", value))
    if len(re.findall(r"[\u4e00-\u9fff]", text)) < 10:
        raise ValueError("评价正文不完整")
    return text


def repetition_issue(text: str, peers: list = None, names: list = None) -> str:
    """保守地检查整段、重复句子和惯用结构；返回重写原因，空串表示通过。"""
    def norm(value):
        value = str(value or "")
        for name in names or []:
            if name:
                value = value.replace(name, "你")
        value = re.sub(r"^[^，。！？]{1,5}，(?=你)", "", value)
        return re.sub(r"[^\w\u4e00-\u9fff]", "", value)

    def clauses(value):
        return [n for part in re.split(r"[，。；！？\n]", value or "")
                if len(n := norm(part)) >= 8]

    def frames(value):
        # ponytail: 仅识别常见套话结构；更多风格判断交给模型自查，不按字集合误判同一主题。
        return tuple(label for label, pattern in (
            ("顺序", r"先.{2,35}再.{2,35}最后"),
            ("同时", r"一边.{2,30}一边"),
            ("转折", r"没有.{2,30}而是"),
            ("肯定", r"你(?:能|已经|学会).{2,25}(?:也能|真不错|很好|特别好)"),
            ("评述", r"老师看到你能|说明你|越来越.{0,5}(?:稳|熟练|好)|又.{0,5}(?:稳|熟练|进步)"),
            ("期待", r"(?:下次|以后).{0,15}(?:可以|试试|继续)"),
        ) if re.search(pattern, value))

    def stock_phrases(value):
        return tuple(re.findall(r"老师看到你能|说明你|越来越.{0,5}(?:稳|熟练|好)|"
                                r"又.{0,5}(?:稳|熟练|进步)|(?:下次|以后)(?:可以)?(?:试试|继续)", value))

    mine = norm(text)
    parts = clauses(text)
    if len(set(parts)) < len(parts):
        return "一段中出现了重复句子，请合并并改写"
    for index, part in enumerate(parts):
        if any(min(len(part), len(other)) >= 12 and
               SequenceMatcher(None, part, other, autojunk=False).ratio() >= 0.88
               for other in parts[:index]):
            return "一段中有几乎相同的句式，请合并重复表达"
    if len(mine) < 20:
        return ""
    for peer in peers or []:
        other = norm(peer)
        if len(other) < 20:
            continue
        match = SequenceMatcher(None, mine, other, autojunk=False)
        if match.ratio() >= 0.78:
            return "与已有评价整体过于相似，请更换句式和叙述顺序"
        other_parts = clauses(peer)
        if any(len(part) >= 10 and part in other_parts for part in parts):
            return "与已有评价复用了完整句子，请重写开头、收尾或重复句"
        longest = match.find_longest_match().size
        if longest >= 24 and longest >= min(len(mine), len(other)) * 0.3:
            return "与已有评价复用了较长文字，请按本条事实重新组织语言"
        structure = frames(text)
        if len(structure) >= 2 and structure == frames(peer):
            # 两种常见句式本身不足以判重，还须复用具体短句或连续的套话骨架。
            phrases = stock_phrases(text)
            same_phrases = len(phrases) >= 3 and phrases == stock_phrases(peer)
            similar_clause = any(SequenceMatcher(None, part, other_part, autojunk=False).ratio() >= 0.68
                                 for part in parts for other_part in other_parts)
            if same_phrases or similar_clause:
                return "与已有评价复用了相同的句式组合，请改变组织方式和收尾"
    return ""


def _check_text(value, peers=None, names=None) -> str:
    text = _clean_text(value)
    issue = repetition_issue(text, peers, names)
    if issue:
        raise ValueError(issue)
    return text


def _picks_note(picks: list) -> str:
    if not picks:
        return "\n老师未关联指标，只依据照片和说明写正文，不能自行新增指标。\n"
    lines = []
    for p in picks:
        behaviors = [one.get("line", "") for one in p.get("behavior_lines", [])]
        if not behaviors and p.get("behavior_line"):
            behaviors = [p["behavior_line"]]
        stage = f"表现行为{ {2:'二',4:'四'}[p['grade']]}，本档没有具体指标原句；不据此编造行为或断言达标" if p.get('grade') in (2,4) else ''
        lines.append(f"- {p['indicator_name']}：{stage or '；'.join(behaviors) or '未选择具体表现行为，不推断达标'}")
    return ("\n# 老师已关联的完整指标（保留全部，不删减、不替换）\n" + "\n".join(lines)
            + "\n文字结合有依据的行为，不凭指标名称断言身高、体重等测量结果达标。\n")


def _validate(out: dict, level: int, picks=None, require_text: bool = True) -> dict:
    """把模型输出对齐到知识库：指标必须存在，表现行为必须是该档位原句。"""
    _check_picks(picks, level)
    catalog = load_indicators(level)
    allowed_ids = {ind["id"] for ind in catalog}
    raw_list = out.get("indicators")
    explicit_empty = isinstance(raw_list, list) and not raw_list
    if not isinstance(raw_list, list):
        raw_list = [{"indicator_id": out.get("indicator_id"), "indicator_name": out.get("indicator_name"),
                     "behavior_line": out.get("behavior_line")}]
    picked = []
    for one in raw_list[:2]:
        if not isinstance(one, dict):
            continue
        ind = None
        if one.get("indicator_id") is not None:
            try:
                ind = indicator_by_id(int(one["indicator_id"]))
            except Exception:
                ind = None
        if ind is not None and ind["id"] not in allowed_ids:
            continue
        if ind is None and one.get("indicator_name"):
            ind = indicator_by_name(str(one["indicator_name"]).strip(), level)
        if ind is None:
            nm = str(one.get("indicator_name") or "")
            for it in catalog:
                if nm and (it["name"] in nm or nm in it["name"]):
                    ind = it
                    break
        if ind is None or any(p["indicator_id"] == ind["id"] for p in picked):
            continue
        picked.append(_one(ind, level, str(one.get("behavior_line") or "")))
    if picks is not None:
        picked = picks
    if not picked and picks is None and not explicit_empty:
        raise ValueError(f"模型选择的指标不在知识库中：{out.get('indicators') or out.get('indicator_name')}")
    text = _clean_text(out.get("text")) if require_text else ""
    first = picked[0] if picked else {"indicator_id": None, "indicator_name": "", "domain": "",
                                     "medal_id": None, "behavior_line": ""}
    return {
        "indicators": picked,
        "modules": _modules_of(picked),
        "indicator_id": first["indicator_id"], "indicator_name": first["indicator_name"],
        "domain": first["domain"], "medal_id": first["medal_id"], "level": level,
        "behavior_line": first["behavior_line"],
        "behavior": punct.clean(str(out.get("behavior") or "")),
        "text": text, "confidence": float(out["confidence"]) if out.get("confidence") is not None else 0.0,
        "reason": str(out.get("reason") or ""),
    }


def _one(ind: dict, level: int, bl: str) -> dict:
    if not ind or ind["id"] not in {item["id"] for item in load_indicators(level)}:
        raise ValueError("指标不属于当前班级的评价体系，请重新选择")
    lines = indicator_lines(ind, level)
    bl = bl.strip()
    if bl not in lines:
        # 不能把空值或模型改写过的句子猜成“已达成”的目录原句。
        bl = ""
    return {"indicator_id": ind["id"], "indicator_name": ind["name"], "domain": ind["domain"],
            "subdomain": ind["subdomain"], "medal_id": ind.get("medal_id"), "behavior_line": bl,
            "behavior_lines": ([{"level": level, "line": bl}] if bl else []),
            **({"system_id": 35, "systemId": 35} if level in NURSERY_LEVELS else {})}


def _modules_of(picked: list) -> list:
    """关联领域直接由选中的指标推出来（领域=domain，模块=subdomain），老师可以再增删。"""
    out = []
    for p in picked:
        ind = indicator_by_id(p.get("indicator_id")) or {}
        m = {"realm": p["domain"], "module": ind.get("record_module") or p["subdomain"]}
        if ind.get("systemId") == 35 or p.get("system_id") == 35 or p.get("systemId") == 35:
            m["systemId"] = 35
        if m not in out:
            out.append(m)
    return out


PICK_PROMPT = """你是一名幼儿园老师。看这张照片和老师的说明，从下面的指标目录里挑出这次观察最能体现的指标。

规则：
- 挑 0~2 条。证据最充分的放第一条；只有当第二条同样有明确证据时才挑第二条，证据不足允许空列表。
- 指标只能从目录里选，不能自己编。
- 只有证据支持具体表现时才选当前档位的原句（behavior_line 与目录原文一字不差）；仅主题相关时可关联指标，behavior_line 留空待确认，不靠目录反推事实。
- 关联不等于达标，未观察到不等于不会，单次完成不等于稳定掌握。不用推测的过程、表情含义或虚构的先后来证明行为。
- 没有老师提供的实测数据，不能凭照片判断身高、体重数值或是否达标；自动挑选时优先选有可见行为依据的指标。

只输出 JSON，不要多余文字：
{"indicators":[{"indicator_id":数字,"indicator_name":"指标名","behavior_line":"目录原句"}],"reason":"一句话"}
"""


def pick_indicators(image_path, child_name: str, level: int, teacher_note: str = "") -> list:
    """只挑指标、不写文字。观察记录表要先知道落在哪个领域，才知道该调哪份参考资料。"""
    _check_picks(None, level)
    if MOCK:
        ind = _default_indicator(level)
        return [_one(ind, level, indicator_lines(ind, level)[0])]
    fixed = PICK_PROMPT + _catalog_block(level)          # 固定，进缓存
    varying = (_multi_note(image_path) + f"\n孩子：{child_name}\n"
               f"班型：{_level_note(level)}\n"
               f"老师说明：{teacher_note.strip() or '（无）'}\n下面是这次的照片。")
    messages = _msgs(fixed, varying, image_path)
    last = None
    for budget in (3500, 8000):
        try:
            out = _parse_json(_chat(messages, max_tokens=budget, temperature=0.4))
            return _validate({"indicators": out.get("indicators")}, level, require_text=False)["indicators"]
        except Exception as e:
            last = e
            log.warning("挑指标失败（预算 %d）：%s", budget, e)
    raise RuntimeError(f"指标匹配失败：{last}")


def write_observation(image_path, child_name: str, level: int, teacher_note: str = "",
                      forced_indicator_id: Optional[int] = None, nickname: str = "",
                      tier: str = "normal", children: list = None,
                      peers: list = None, variant: int = 0, picks: list = None) -> dict:
    """主入口。forced_indicator_id：老师在审核页手动改指标后重写时使用。"""
    _check_picks(picks, level)
    if forced_indicator_id:
        ind = indicator_by_id(forced_indicator_id)
        forced = _one(ind, level, "")
        if picks is None:
            picks = [forced]
    if MOCK:
        ind = indicator_by_id(forced_indicator_id) if forced_indicator_id else _default_indicator(level)
        if not ind or ind["id"] not in {item["id"] for item in load_indicators(level)}:
            raise ValueError("指标不属于当前班级的评价体系，请重新选择")
        line = indicator_lines(ind, level)[0]
        _p = [_one(ind, level, line)] if picks is None else picks
        return _validate({
            "indicators": _p, "modules": _modules_of(_p),
            "indicator_id": ind["id"], "indicator_name": ind["name"], "domain": ind["domain"],
            "medal_id": ind.get("medal_id"), "level": level, "behavior_line": line,
            "behavior": f"{child_name}在活动区专注地进行操作。",
            "text": f"{child_name}，你今天一直盯着手里的材料，试了一次又一次，最后真的把它搭起来了。你没有着急，也没有喊老师帮忙，而是自己慢慢想办法，这一点特别好。下次遇到难一点的，也可以像今天这样，先试试看。",
            "confidence": 0.9, "reason": "MOCK",
        }, level, picks=picks)
    call_name = (nickname or "").strip() or child_name
    import lengths
    # —— 固定前缀：每条都一模一样，整段都能命中缓存 ——
    # 写作规矩 → 老师自己的风格 → 篇幅 → 指标目录。这四样只随「班型/档位/这位老师」变，
    # 同一位老师批量写一个班，就是逐字相同的一大段。
    skill = SKILL_PROMPT
    if level in NURSERY_LEVELS:
        skill = re.sub(r"\n# 范例\n.*?(?=\n# 输出\n)", "", skill, flags=re.S)
    fixed = (skill + _style_block() + lengths.simple_block(level, tier)
             + _catalog_block(level) + FINAL_REVIEW)
    # —— 变动部分：这次的写法、这个孩子、老师这次写的说明 ——
    varying = (_peer_note(peers, variant) + _multi_note(image_path) + _kids_note(children)
               + f"\n孩子姓名：{child_name}\n平时怎么叫他：{call_name}"
                 f"（文中若要提到名字，只用这个称呼，不要用全名）\n"
                 f"班型：{_level_note(level)}\n"
                 f"老师说明：{teacher_note.strip() or '（无）'}\n")
    if picks is not None:
        varying += _picks_note(picks)
    varying += "\n交稿前自查重复句子、句式组合和收尾，检查是否夹带说明或推测未标注；修好后只交最终结果。"
    varying += "\n下面是这次的照片。"
    messages = _msgs(fixed, varying, image_path)
    last_err = None
    for attempt, budget in enumerate((4500, 9000, 9000)):
        try:
            msgs = messages if not last_err else messages + [
                {"role": "user", "content": f"上次自查未通过：{last_err}。请重新组织语言，保持原有事实，"
                                            "只输出要求的完整 JSON 对象，正文不得包含反馈或自查过程。"}]
            raw = _chat(msgs, max_tokens=budget)
            out = _parse_json(raw)
            res = _validate(out, level, picks=picks)
            res["text"] = _check_text(res["text"], peers, [call_name, child_name] + (children or []))
            return res
        except Exception as e:
            last_err = e
            # 把模型到底吐了什么记下来 —— 只记开头，够看出是断了还是引号坏了
            log.warning("AI 第 %d 次失败：%s；模型原文开头：%s", attempt + 1, e,
                        re.sub(r"\s+", " ", (locals().get("raw") or ""))[:200])
    raise RuntimeError(f"AI 生成失败：{last_err}")


def rewrite_text(child_name: str, level: int, indicator_name: str, behavior_line: str, behavior: str,
                 teacher_note: str = "", nickname: str = "", tier: str = "normal", picks: list = None,
                 children: list = None, peers: list = None, previous_text: str = "") -> str:
    """只重写评价文字（老师点"换一段"）。不看图，省时省钱。"""
    _check_picks(picks, level)
    if level in NURSERY_LEVELS and picks is None and (indicator_name or behavior_line):
        ind = indicator_by_name(indicator_name, level)
        if not ind or (behavior_line and behavior_line not in indicator_lines(ind, level)):
            raise ValueError("重写所用指标不属于托育体系24—36个月档，请重新选择")
    if MOCK:
        return f"{child_name}，这一次你换了一种方法，先把大的放在下面，再一块块往上加，果然稳了。你自己发现了问题、自己想到了办法，这就是最了不起的地方。下次还可以试试更高的。"
    call_name = (nickname or "").strip() or child_name
    references = list(peers or []) + ([previous_text] if previous_text else [])
    prompt = (f"孩子怎么称呼：{call_name}\n班型档位：{_level_note(level)}\n"
              f"观察到的行为：{behavior}\n老师说明：{teacher_note or '（无）'}\n\n"
              + (_picks_note(picks) if picks is not None else
                 f"指标：{indicator_name}\n对应表现行为：{behavior_line}\n")
              + _kids_note(children) + _peer_note(references, 11)
              + "\n请重写一段以‘你’称呼孩子的评价。先自查重复句式、开头、结尾和格式，修好后只交正文。")
    import lengths
    # 重写只要正文，不能复用含“输出一个 JSON 对象”的完整生成提示词。
    system = COMMON_PROMPT + punct.RULE + """你是一名幼儿园老师，为孩子重写一段随手记评价。
只输出可直接使用的一段中文正文，不分行、不编号、不加标题、解释、分析或格式标记。
用“你”温和地对孩子说话，围绕老师说明和观察事实描述具体行为，把关联指标的意图自然融入。
只在已给事实里选有意义的办法、变化或需要，不把旧稿中的能力评价当作新的观察证据。
不照抄指标原句，不空夸。身高体重等实测数值只能用老师明确提供的数据，不编造测量数值、增长幅度或达标结论。没有证据的过程或对话若需合理还原，必须明确写“推测，待老师确认”。
与已有评价比较开头、句式组合和收尾，不能只换孩子姓名、材料或形容词。优先改变表达，不为求不同编造事实。
老师说明中的观察重点优先；只使用提供的孩子称呼；多人记录每人都要写到。
"""
    messages = [{"role": "system", "content": system + nursery_prompt(level) + lengths.simple_block(level, tier) + _style_block() + FINAL_REVIEW},
                {"role": "user", "content": prompt}]
    last_err = None
    for budget in (3000, 6000, 6000):
        try:
            msgs = messages if not last_err else messages + [
                {"role": "user", "content": f"上次自查未通过：{last_err}。请重新写一段正文，不能输出自查反馈。"}]
            raw = _chat(msgs, max_tokens=budget, temperature=0.9, json_mode=False)
            return _check_text(raw, references, [child_name, call_name] + (children or []))
        except Exception as e:
            last_err = e
            log.warning("重写评价未通过：%s", e)
    raise RuntimeError(f"重写未完成，已保留原文：{last_err}")
