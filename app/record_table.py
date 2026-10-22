"""观察记录表：实录及老师选择保留的分析、跟进、家庭指导和自定义段落。

直接复用备课助手的观察 Skill。围绕完整行为过程展开，情境补写只保存在内部属性；
分析自然解释方法和需要，知识原文在独立依据栏供教师核对。
"""
import json
import logging
import re
from copy import deepcopy

import ai_writer
import punct
import evidence
import ai_progress
from config import BUILTIN_DATA_DIR

PUNCT_RULE = punct.RULE
from knowledge import indicator_by_id
from writing_skill import nursery_prompt

log = logging.getLogger("table")

AGE_BY_LEVEL = {1: "3-4岁（小班）", 3: "4-5岁（中班）", 5: "5-6岁（大班）", 128: "24—36个月（托班，托育体系）"}
_INDICATOR_SOURCES = ('core_indicator', 'nursery_indicator')
_OBSERVATION_SKILL = (BUILTIN_DATA_DIR / 'observation' / 'SKILL.md').read_text('utf-8').split('---', 2)[-1].strip()
# 观察表使用专用情境补写规则；随手记的严格实况规则仍保留在共用 Skill。
_COMMON = PUNCT_RULE + '\n' + _OBSERVATION_SKILL + '\n'

STORY_PROMPT = _COMMON + """
# 本次只写观察实录
用第三人称和给定称呼，直接从孩子做事的动作开始，展开整件事的行为过程、互动和方法变化。
允许结合照片与说明补全前后情节；补写属性只放“情境补写”布尔字段，不在正文添加补写或核实提示。不要停在静态姿势描摹。老师明确要求不补写时按实况写。
不写“在某某活动中／主题中／过程中”等背景套话；不写衣着外貌、颜色花纹、镜头朝向等无关信息。
不写观察目标，不在正文前重复“观察内容”，结尾不加分析或表扬。
只输出 JSON：{"观察内容":"实录正文","情境补写":true或false}。只要补了未提供的过程就填true，全为已提供实况才填false。不输出解释、自查。
"""

ANALYSIS_PROMPT = _COMMON + """
# 本次写观察分析及回应策略
顺着整件事理解孩子的做法、调整与需要，像教师之间自然交流。用一两处关键行为支持理解，不从头重讲实录，不一招一式地对应指标。
优先从本次适龄原始指标中选一至三项：core_indicator 是原表细项，nursery_indicator 是官网托育体系表现。每项说清关键行为支持哪一部分经验、做到什么程度、独立还是提示后做到，以及真实困难或再观察边界，不把没有观察到当成缺陷。
所选核心经验原表细项的名称由程序按原文组装到分析中，不自行改名，不只列名称；托育表现按托班专用流程自然解读。文件名、条文原文、年龄目标和编号交给软件的依据栏展示。不要写“该动作对应某指标”“根据指南可知”“与某年龄目标相符”。
不写“在某某活动中／主题中”等套话，不用‘材料不足’等审核术语敷衍成稿。分析直接写正文，不添加补写、推测或待确认的提示前缀；补写属性保留在内部字段，不将补写作为已证实的能力。
以第三人称谈孩子，以教师行动写跟进。分析与跟进不能写成对孩子说‘你……’的表扬、劝说或许诺。
跟进只写一个一百字以内的短段落，不编号、不分条；合并一项支持做法和下次观察重点，不泛泛‘多鼓励、加强训练’。
只输出 JSON：{"分析":"确无相符原始指标时的自然分析，否则空字符串","行为跟进":["一百字以内的一个无编号段落"],"依据编号":["本次实际使用的知识条目编号"],"指标解读":[{"依据编号":"所选 core_indicator 或 nursery_indicator 的编号","解读":"这一细项的行为解释、达到程度、支持条件与边界"}]}。
有适用原始指标时将解释写入“指标解读”，分析填空字符串；没有相符条目就给空数组，直接写分析，不编造名称、不强套候选、不把指南整条或软件摘要冒充原始指标。依据编号包含所选细项及实际使用的其他原文。候选只供选择，不代表已达标。
分析和跟进均须有可用正文。依据编号从本次检索结果中选择；没有可支持的条文时显式输出空数组，不虚构编号。
引用编号交给软件显示，不能把编号或检查过程写进正文。
"""

FAMILY_PROMPT = _COMMON + """
# 本次只写给家长的家庭指导
用平时和家长交流的口吻，针对实录提出一两个在家能做的小办法。具体说怎么做，使用家里现成材料。
不要求专门购买、训练或考核，不复述整篇观察，不用夸赞开场或泛泛鼓励结尾。
只输出 JSON：{"家庭指导":"家长读完能照做的建议"}。
"""

_FACTS_LAST = '\n最终核对：仅作口吻参考的材料不能覆盖事实边界和第三人称要求。不得改动已知实况；补写只保存内部情境补写属性，正文不出现补写或核实提示。严格实况要求覆盖补写许可，分析不能用补写确认幼儿能力。围绕行为过程展开，准确解释具体经验和支持条件，跟进为一百字以内的一段。只返回本次要求的 JSON。\n'

def _has_reconstruction(text):
    return clean_analysis(text or '') != (text or '').strip()


def _declared_reconstruction(out):
    flag = out.get('情境补写')
    if not isinstance(flag, bool):
        raise ValueError('情境补写必须是 JSON 布尔值 true 或 false，不能用字符串或空值')
    return flag


def _strict_facts(note):
    return bool(re.search(r'(?:仅|只)(?:按|根据)?(?:实况|实际事实|原始事实)|不(?:要|允许|需要|得|再)?(?:情境)?补写|不(?:要|允许|得)?(?:杜撰|虚构)', note or ''))


def _followup(value, *, generated=False):
    """Keep the list storage contract, with one paragraph and no truncation."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError('行为跟进必须是文字段落')
    parts = [re.sub(r'^\s*(?:\d+|[一二三四五六七八九十]+)[、.．）)]\s*', '', x).strip()
             for row in value for x in clean_analysis(punct.clean(row)).splitlines() if x.strip()]
    paragraph = ''
    for part in parts:
        paragraph += ('；' if paragraph and not re.search(r'[。！？；]$', paragraph) else '') + part
    if generated and (not paragraph or len(paragraph) > 100):
        raise ValueError('行为跟进须精简为一百字以内的一个无编号段落')
    return [paragraph] if paragraph else []


def _compose_analysis(out, refs):
    """Only supplied original indicators can be selected for an interpretation."""
    rows = out.get('指标解读', [])
    if not isinstance(rows, list) or len(rows) > 3:
        raise ValueError('指标解读须为最多三项的数组')
    lookup = {ref['id']: ref for ref in refs if ref.get('source_kind') in _INDICATOR_SOURCES}
    used = out.get('依据编号', [])
    paragraphs, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('依据编号'), str) or row['依据编号'] not in lookup:
            raise ValueError('指标解读只能选择本次提供的适龄原始指标')
        key = row['依据编号']
        if key in seen or not isinstance(used, list) or key not in used:
            raise ValueError('指标解读编号重复或未列入依据编号')
        seen.add(key)
        title = lookup[key]['title']
        text = _written_text(row.get('解读'), '分析', canonical_titles=(title,))
        explanation = re.sub(r'\s|[：:，,。;；—-]', '', text.replace(title, ''))
        if len(explanation) < 20:
            raise ValueError('指标解读不能只有名称或结论，须解释行为证据、达到程度、支持条件与边界')
        # 托育官网的表现原句在依据栏展示，不把长句强制拼成分析小标题。
        paragraphs.append(text if title in text or lookup[key]['source_kind'] == 'nursery_indicator'
                          else title + '：' + text)
    if paragraphs:
        if seen != {key for key in used if key in lookup}:
            raise ValueError('所有选用的原表细项均须分别解读，不把未解释的指标当成已匹配依据')
        return _written_text('\n\n'.join(paragraphs), '分析',
                             canonical_titles=[lookup[key]['title'] for key in seen])
    if isinstance(used, list) and any(key in lookup for key in used):
        raise ValueError('选用了原表细项时须逐项填写指标解读，解释行为、程度、支持条件和边界')
    return _written_text(out.get('分析'), '分析')


def _table_style() -> str:
    # 随手记学习到的第二人称称呼、开头和范例不混入教师观察记录。
    return _FACTS_LAST


def _written_text(value, label: str, *, canonical_titles=()) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}为空或不是正文文字")
    if re.search(r'^\s*(?:[\[{]|#{1,6}\s)|`|\*\*|</?(?:think|analysis)>', value, re.I):
        raise ValueError(f"{label}包含结构化反馈或格式标记，不是正文")
    allowed_titles = []
    protected = value
    title_tokens = []
    if label == '分析' and canonical_titles:
        originals = {ref['title'] for ref in evidence.catalog()
                     if ref.get('source_kind') in _INDICATOR_SOURCES}
        allowed_titles = sorted(set(canonical_titles) & originals, key=len, reverse=True)
        # Source punctuation is part of the literal name too. Preserve exact
        # titles while normalizing only model-written prose.
        for title in allowed_titles:
            token = '\ue000SOURCE_TITLE_' + str(len(title_tokens)) + '\ue001'
            while token in value:
                token += '\ue001'
            if title in protected:
                protected = protected.replace(title, token)
                title_tokens.append((token, title))
    text = punct.clean(protected)
    for token, title in title_tokens:
        text = text.replace(token, title)
    text = clean_analysis(text)
    issue = ai_writer.repetition_issue(text)
    if not text or issue:
        raise ValueError(issue or f"{label}为空")
    if label in ('观察内容', '分析', '行为跟进'):
        # 真实对话可以出现“你”，叙述和教师分析不能向孩子讲话。
        narration = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"', '', text)
        if allowed_titles:
            # Literal source titles may themselves contain “在实验过程中”.
            # Only verified names are exempt; generated explanation outside
            # those exact names still passes all narration checks below.
            for title in allowed_titles:
                narration = narration.replace(title, '')
        if re.search(r'你|您|咱们', narration):
            raise ValueError(f'{label}请用第三人称和幼儿称呼，不能写成对孩子说话；原始对话可保留')
        if re.search(r'真(?:棒|不错)|棒棒的|小天使', narration):
            raise ValueError(f'{label}请记录表现和有依据的解释，删除空泛表扬')
        if label in ('观察内容', '分析') and re.search(
                r'(?:在|通过)[^。！？；，\n]{0,35}(?:活动|主题|游戏)(?:之?中|里|期间)|在[^。！？；，\n]{1,30}(?:的)?过程中', narration):
            raise ValueError(f'{label}不要写“在某某活动中／过程中”等背景套话，直接写行为或对做法的理解')
        if label == '分析' and re.search(
                r'(?:动作|行为|做法|表现)[^。！？；\n]{0,16}(?:对应|符合|匹配|对照)[^。！？；\n]{0,28}(?:指标|目标|条文|指南)|《[^》]*(?:指南|纲要)[^》]*》|(?:对照|符合)[^。！？；\n]{0,15}岁[^。！？；\n]{0,15}目标', narration):
            raise ValueError('分析不要机械对应指标或引用指南文件名；自然解释做法和需要，出处只放依据编号')
    return text

# 保留旧导入名称。
SKILL_PROMPT = STORY_PROMPT


def reference_block(picks: list, level: int, limit: int = 12000, *, story='', note='', entries=None) -> str:
    """只提供适龄且可溯源的知识原文，按完整条目控制长度。"""
    entries = evidence.build_evidence(level, picks, story, note) if entries is None else entries
    selected, size = [], 0
    for entry in entries:
        text = json.dumps(entry, ensure_ascii=False)
        if size + len(text) > limit:
            break
        selected.append(entry)
        size += len(text)
    return json.dumps(selected, ensure_ascii=False) if selected else ''


def _evidence_metadata(out: dict, refs: list, story: str, analysis: str) -> dict:
    ids = out.get('依据编号')
    if ids is None and not refs:
        ids = []
    if not isinstance(ids, list) or len(ids) > 6 or any(not isinstance(x, str) for x in ids):
        raise ValueError('分析必须给出实际使用的依据编号数组；没有适用条文时给空数组')
    lookup = {x['id']: x for x in refs}
    if any(x not in lookup for x in ids):
        raise ValueError('分析引用了本次知识库没有提供的条目，请只使用所给编号与原文')
    return {'分析依据': [lookup[x] for x in dict.fromkeys(ids)],
            '依据对应实录': clean_story(story), '依据对应分析': analysis}


def _retry_feedback(messages, raw, error):
    if raw:
        messages.append({'role': 'assistant', 'content': raw})
    messages.append({'role': 'user', 'content': str(error) + '。保留已知事实与内部情境补写属性，正文不加核实提示；修正问题后按原格式重写，不输出说明。'})


def _check_reported_speech(text: str, source: str):
    """明确声称孩子说过的引语必须可回查；未来可用的教师提问不受此检查。"""
    pattern = r'(?:曾(?:经)?说|说过|当时(?:说|回答)|原话(?:是|为))[^“「。！？；\n]{0,12}((?:[“「][^”」]+[”」][、，\s]*)+)'
    original_quotes = [re.sub(r'\s+', '', next(x for x in group if x)) for group in
                       re.findall(r'“([^”]+)”|「([^」]+)」|『([^』]+)』|‘([^’]+)’|"([^"\n]+)"', source)]
    for match in re.finditer(pattern, text):
        for quote in re.findall(r'[“「]([^”」]+)[”」]', match.group(1)):
            if not any(re.sub(r'\s+', '', quote) in original for original in original_quotes):
                raise ValueError('文中声称孩子说过“' + quote + '”，但实录和教师说明未将这句话记为原话；删除这一归因，不把叙述用词改作幼儿原话')


def _who_block(child_name: str, level: int, teacher_note: str, picks: list,
               nickname: str = "", children: list = None) -> str:
    """两步都要用的那几行：孩子怎么称呼、几岁、老师说了什么、这次挂了哪些指标。"""
    ai_writer._check_picks(picks, level)
    call = (nickname or "").strip() or child_name
    lines = []
    for p_ in picks or []:
        ind = indicator_by_id(p_.get("indicator_id")) if p_.get("indicator_id") else None
        bl = p_.get("behavior_lines")
        txt = "；".join(x["line"] for x in bl if isinstance(x, dict) and x.get("line")) \
            if isinstance(bl, list) and bl else (p_.get("behavior_line") or "")
        stage = f"表现行为{ {2:'二',4:'四'}[p_['grade']]}，本档没有具体指标原句，不据此编造行为；" if p_.get('grade') in (2,4) else ''
        lines.append(f"- {p_.get('indicator_name','')}（{p_.get('domain','')}·{p_.get('subdomain','')}）"
                     f"：{stage}"
                     f"{txt}"
                     + (f"；这条指标要看的是「{ind['goal']}」" if ind and ind.get("goal") else ""))
    return (f"孩子怎么称呼：{call}\n"
            f"年龄段：{AGE_BY_LEVEL.get(level, '年龄段未确认，不套用年龄目标')}\n"
            f"老师的说明：{(teacher_note or '').strip() or '（没写，全靠照片）'}\n"
            f"这次挂的指标（关联不代表达标）：\n" + ("\n".join(lines) if lines else "（未关联指标，只按已知行为理解，不代选领域或达标项）") + "\n")


def write_story(image_path, child_name: str, level: int, teacher_note: str = "",
                picks: list = None, nickname: str = "", tier: str = "normal",
                children: list = None, peers: list = None, variant: int = 0) -> dict:
    """第一步：依据照片和教师说明写第三人称实录，沿用本次思考设置。"""
    ai_progress.stage('整理观察实录', 0, 3)
    if ai_writer.MOCK:
        return {"观察内容": "（演示）他先挑了四块长条积木……", '情境补写': True}
    import lengths
    photo_note = ('\n照片是同一件事的线索；排列不自动证明时间顺序。合理连接的过程只用内部情境补写属性记录，不另造无关事件或认错人物、作品归属。\n'
                  if len(ai_writer._paths(image_path)) > 1 else '')
    user = (photo_note + _both_kids_note(children)
            + _who_block(child_name, level, teacher_note, picks, nickname, children))
    if peers:
        user += ("\n同批已有实录，仅用于检查是否复用了句子，不沿用其中的人物和事件；"
                 "事实相近可以有相近措辞；补写围绕当前事件，不搬用其他幼儿的情节。\n"
                 + "\n".join(str(p)[:300] for p in peers[:4] if p))
    messages = [
        {"role": "system", "content": STORY_PROMPT + nursery_prompt(level) + lengths.table_story_block(level, tier)
         + _table_style()},
        {"role": "user", "content": ai_writer._img_parts(image_path) + [{"type": "text", "text": user}]},
    ]
    last = None
    for budget in (4000, 7000):
        raw = ''
        try:
            raw = ai_writer._chat(messages, max_tokens=budget, temperature=0.65)
            out = ai_writer._parse_json(raw)
            value = out.get("观察内容")
            if not isinstance(value, str):
                raise ValueError("观察内容不是正文文字")
            flag = _declared_reconstruction(out)
            if _strict_facts(teacher_note) and (flag or _has_reconstruction(value)):
                raise ValueError('老师要求仅按实况、不补写；删除补写过程并返回情境补写 false')
            content = _written_text(clean_story(value), "观察内容")
            return {"观察内容": content, '情境补写': flag or _has_reconstruction(value)}
        except Exception as e:
            last = e
            _retry_feedback(messages, raw, e)
            log.warning("实录生成失败（预算 %d）：%s", budget, e)
    raise RuntimeError(f"观察内容生成失败：{last}")


def write_analysis(story: dict, child_name: str, level: int, teacher_note: str = "",
                   picks: list = None, nickname: str = "", tier: str = "normal",
                   children: list = None) -> dict:
    """第二步：将实录与适龄条文对应，保留实际引用出处供教师核对。"""
    ai_progress.stage('检索适龄知识依据', 1, 3)
    if ai_writer.MOCK:
        return {"分析": "（演示）……", "行为跟进": ["（演示）给他备几块斜面板。"]}
    import lengths
    story = without_analysis_notice(story)
    user = (_who_block(child_name, level, teacher_note, picks, nickname)
            + _both_kids_note(children)
            + "\n# 这次的实录（上一步写好的，别复述，直接往下分析）\n"
            + clean_story(story.get('观察内容', '')) + "\n")
    story_text = clean_story(story.get('观察内容', ''))
    user += '\n内部情境补写属性：' + json.dumps(story.get('情境补写', False)) + '。补写内容未核实，不以其确认能力；教师原始说明和照片直接可见事实才是实况依据。\n'
    refs = evidence.build_evidence(level, picks or [], story_text, teacher_note)
    ref = reference_block(picks or [], level, entries=refs)
    if ref:
        user += "\n# 可核对原文的知识依据（选择能支撑具体解释的条目，另列实际使用编号）\n" + ref
    else:
        user += '\n本次没有检索到适用知识条文。不得冒用指南或年龄标准；依据编号输出空数组。\n'
    messages = [
        {"role": "system", "content": ANALYSIS_PROMPT + nursery_prompt(level) + lengths.table_analysis_block(level, tier)
         + _table_style()},
        {"role": "user", "content": user},
    ]
    last = None
    ai_progress.stage('循证分析与回应', 1, 3)
    # 「想得细」那一档思考的字也算在预算里，所以给得宽一点
    for budget in (8000, 14000):
        raw = ''
        try:
            raw = ai_writer._chat(messages, max_tokens=budget, temperature=0.65)
            out = ai_writer._parse_json(raw)
            fu = _followup(out.get("行为跟进"), generated=True)
            ana = _compose_analysis(out, refs)
            _written_text("\n".join(fu), "行为跟进")
            _check_reported_speech(ana + '\n' + '\n'.join(fu), story_text + '\n' + (teacher_note or ''))
            meta = _evidence_metadata(out, refs, story_text, ana)
            ai_progress.stage('核对成稿', 2, 3)
            return {"分析": ana, "行为跟进": fu, **meta}
        except Exception as e:
            last = e
            _retry_feedback(messages, raw, e)
            log.warning("分析生成失败（预算 %d）：%s", budget, e)
    raise RuntimeError(f"分析和行为跟进生成失败：{last}")


def _both_kids_note(children: list) -> str:
    """一条记录里有好几个孩子时，分析和跟进都不许只写其中一个。

       坑记：以前写分析那一步只拿到「主角一个人的名字」，另一个孩子在实录里明明有，
       到了分析里就没人管了 —— 老师看到的就是「两个孩子只分析了一个」。
    """
    ks = [k for k in (children or []) if k]
    if len(ks) <= 1:
        return ""
    return ("\n这条记录涉及 %d 个孩子：%s。\n"
            "有各自的动作就分别说清，不能只写第一个；补写互动只保留内部属性，不能认错身份或把补写当作已证实能力。\n"
            "相同需要可以共用一条跟进措施，不按人数重复凑句子。\n"
            % (len(ks), "、".join(ks)))


# ---- 单独重写某一段：老师对哪一段不满意就重写哪一段，别的原样不动 ----
_FIELD_RULE = {
    "观察内容": ("STORY", True),
    "分析": ("ANALYSIS", False),
    "行为跟进": ("ANALYSIS", False),
    "家庭指导": ("FAMILY", False),
}


def rewrite_field(table: dict, key: str, image_path, child_name: str, level: int,
                  teacher_note: str = "", picks: list = None, nickname: str = "",
                  tier: str = "normal", children: list = None, want: str = "") -> dict:
    """只重写老师保留的某一段。want 是老师随手写的要求（可以为空）。

       为什么要有它：以前只能整份重写 —— 明明只有分析不满意，观察内容白等一遍，
       老师改过的字还会被盖掉。现在哪一段不满意点哪一段。
    """
    key = (key or "").strip()
    table = normalize(table)
    custom = next((s for s in table.get("自定义段落", []) if key == "custom:" + s["id"]), None)
    if custom is None and (key not in _FIELD_RULE or key not in table):
        raise ValueError("这一段已删除或不存在，请先添加段落")
    title = custom["title"] if custom else key
    output_key = "content" if custom else key

    def result(value):
        if custom is None:
            return {key: value}
        custom["content"] = value
        return {"自定义段落": table["自定义段落"]}

    if ai_writer.MOCK:
        changed = result(["（演示）重写好的跟进"] if key == "行为跟进" else "（演示）重写好的 %s" % title)
        if key == '观察内容':
            changed['情境补写'] = True
        return changed
    import lengths
    which, need_img = _FIELD_RULE[key] if custom is None else ("CUSTOM", False)
    base = {"STORY": STORY_PROMPT, "ANALYSIS": ANALYSIS_PROMPT, "FAMILY": FAMILY_PROMPT}.get(which, _COMMON)
    lb = lengths.table_story_block(level, tier) if which == "STORY" else \
        lengths.table_analysis_block(level, tier) if which == "ANALYSIS" else "\n按段落标题和老师要求写内容，不要复述其他段落，不编造跟进结果，不添加补写或核实提示。"
    sys_txt = (base + nursery_prompt(level) + lb + _table_style()
               + "\n\n# 这一次只做一件事\n"
               f"下面给你的是一份已经写好的记录，老师对其中的「{title}」不满意。\n"
               f"**只重写「{title}」这一段**，其他几段一个字都不要动、也不要输出。不要在正文内写段落标题。\n"
               f"只输出 {{\"{output_key}\": ...}} 这一个 JSON。\n")
    if key == '观察内容':
        sys_txt += '\n另返回“情境补写”布尔值：补写了未提供的过程就为true。只保留内部属性，正文不添加补写或核实提示。'
    cur = to_text(table)
    user = (_who_block(child_name, level, teacher_note, picks, nickname)
            + _both_kids_note(children)
            + "\n# 已经写好的这份记录\n" + cur + "\n"
            + '\n内部情境补写属性：' + json.dumps(table.get('情境补写', False)) + '。旧稿补写不因润色成为实况；分析只用原始事实确认能力。\n')
    refs = []
    if which == "ANALYSIS":
        refs = evidence.build_evidence(level, picks or [], table.get('观察内容', ''), teacher_note)
        ref = reference_block(picks or [], level, entries=refs)
        if ref:
            user += "\n# 分析时可以用的参考（挑相关的用，别整段抄）\n" + ref
    if key == '分析':
        sys_txt += '\n本次另输出“依据编号”和“指标解读”数组；有相符原始指标时仍按所给编号加解读协议交稿，分析填空字符串。没有相符条目时指标解读为空数组，直接写分析。不得编造编号或名称。'
    if (want or "").strip():
        user += ("\n# 老师这次的要求（最高优先级，必须照办）\n" + want.strip() + "\n")
    else:
        user += "\n# 老师没写具体要求，那就换个角度、换个说法重写一遍，别原样再来一次。\n"
    content = ([{"type": "text", "text": user}] if not need_img
               else ai_writer._img_parts(image_path) + [{"type": "text", "text": user}])
    messages = [{"role": "system", "content": sys_txt}, {"role": "user", "content": content}]
    last = None
    ai_progress.stage('重写' + title)
    for budget in (5000, 9000):
        raw = ''
        try:
            raw = ai_writer._chat(messages, max_tokens=budget, temperature=0.7)
            out = ai_writer._parse_json(raw)
            v = out.get(output_key)
            if key == "行为跟进":
                v = _followup(v, generated=True)
                _written_text("\n".join(v), "行为跟进")
                _check_reported_speech('\n'.join(v), table.get('观察内容', '') + '\n' + (teacher_note or ''))
                return result(v)
            else:
                if key == '分析':
                    v = _compose_analysis(out, refs)
                declared = False
                if key == "观察内容" and isinstance(v, str):
                    declared = _declared_reconstruction(out)
                    if _strict_facts((teacher_note or '') + '\n' + (want or '')) and (declared or _has_reconstruction(v)):
                        raise ValueError('老师要求仅按实况、不补写；删除补写过程并返回情境补写 false')
                    v = clean_story(v)
                selected_titles = [ref['title'] for ref in refs
                                   if key == '分析' and ref.get('source_kind') in _INDICATOR_SOURCES
                                   and ref['id'] in out.get('依据编号', [])]
                value = _written_text(v, title, canonical_titles=selected_titles)
                changed = result(value)
                if key == '观察内容':
                    changed['情境补写'] = bool(table.get('情境补写') or declared or _has_reconstruction(out.get(output_key)))
                if key == '分析':
                    _check_reported_speech(value, table.get('观察内容', '') + '\n' + (teacher_note or ''))
                    changed.update(_evidence_metadata(out, refs, table.get('观察内容', ''), value))
                return changed
        except Exception as e:
            last = e
            _retry_feedback(messages, raw, e)
            log.warning("重写「%s」失败（预算 %d）：%s", key, budget, e)
    raise RuntimeError(f"「{key}」重写失败：{last}")


def write(image_path, child_name: str, level: int, teacher_note: str = "",
          picks: list = None, nickname: str = "", tier: str = "normal", children: list = None,
          peers: list = None, variant: int = 0) -> dict:
    """两步合起来写一份完整的记录表（不需要并行的地方就用这个）。"""
    story = write_story(image_path, child_name, level, teacher_note, picks, nickname,
                        tier=tier, children=children, peers=peers, variant=variant)
    ana = write_analysis(story, child_name, level, teacher_note, picks, nickname, tier=tier,
                         children=children)
    return {**story, **ana}


def clean_story(value: str) -> str:
    """只去掉正文开头的格式标题，不替换句子里的正常用词。"""
    return re.sub(r"\A\s*(?:(?:#{1,6}\s*)?(?:【观察内容】|\*\*观察内容\*\*)[：:\s]*|(?:#{1,6}\s*)?观察内容(?:[：:]\s*|\s*\n\s*))+", "", value).strip()


_NOTICE_SPACE = r'[\s\u200b-\u200d\u2060\ufeff]*'
_NOTICE_INDENT = r'[ \t\f\v\u00a0\u1680\u2000-\u200d\u202f\u205f\u2060\u3000\ufeff]*'
_ANALYSIS_NOTICE = re.compile(
    r'(?m)^(?:' + _NOTICE_INDENT + r'[（(]' + _NOTICE_SPACE
    + '(?:' + '|'.join(_NOTICE_SPACE.join(label) for label in ('结合补写情境的分析', '情境补写', '推测')) + ')'
    + _NOTICE_SPACE + r'[，,、]' + _NOTICE_SPACE
    + _NOTICE_SPACE.join('待老师确认') + _NOTICE_SPACE + r'[）)]' + _NOTICE_INDENT + r'(?:\r?\n)?)+')
_NOTICE_QUOTES = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"]*"')


def clean_analysis(value: str) -> str:
    """移除各段首旧系统提示，兼容复制换行与不可见空白，保留引语。"""
    quotes = [match.span() for match in _NOTICE_QUOTES.finditer(value)]
    return _ANALYSIS_NOTICE.sub(
        lambda match: match.group() if any(a < match.end() and match.start() < b for a, b in quotes) else '',
        value).strip()


def without_analysis_notice(table: dict) -> dict:
    """展示旧表时只清指定提示，不因过时的依据/栏目格式阻断显示，也不改原件。"""
    t = deepcopy(table)
    if isinstance(t, dict):
        def clean(value):
            if _has_reconstruction(value):
                t['情境补写'] = True
            return clean_analysis(value)
        for key in ('观察内容', '分析', '家庭指导', '依据对应实录', '依据对应分析'):
            if isinstance(t.get(key), str):
                t[key] = clean(t[key])
        if isinstance(t.get('行为跟进'), list):
            t['行为跟进'] = [clean(value) if isinstance(value, str) else value for value in t['行为跟进']]
        if isinstance(t.get('自定义段落'), list):
            for section in t['自定义段落']:
                if isinstance(section, dict) and isinstance(section.get('content'), str):
                    section['content'] = clean(section['content'])
    return t


def normalize(table: dict) -> dict:
    """旧表兼容与保存校验；缺失的可选段落表示已删除，不自动补回。"""
    if not isinstance(table, dict):
        raise ValueError("观察记录表格式不正确")
    t = without_analysis_notice(table)
    t.pop("观察目标", None)
    if '情境补写' in t and not isinstance(t['情境补写'], bool):
        raise ValueError('情境补写必须是布尔值')
    for key in ("观察内容", "分析", "家庭指导"):
        if key in t:
            if not isinstance(t[key], str):
                raise ValueError(f"{key}必须是文字")
            t[key] = clean_story(t[key]) if key == "观察内容" else clean_analysis(t[key]) if key == "分析" else t[key].strip()
    if '分析' not in t:
        for key in ('分析依据', '依据对应实录', '依据对应分析'):
            t.pop(key, None)
    elif '分析依据' in t:
        refs = t['分析依据']
        if not isinstance(refs, list) or len(refs) > 6 or any(not isinstance(x, dict) for x in refs):
            raise ValueError('分析依据格式无效')
        verified = []
        for row in refs:
            original = evidence.by_id(row.get('id'))
            if not original or not original.get('enabled_for_analysis'):
                raise ValueError('分析依据不在内置知识库中，请重写分析重新匹配')
            if not any(x['id'] == original['id'] for x in verified):
                verified.append(original)
        t['分析依据'] = verified
        for key in ('依据对应实录', '依据对应分析'):
            if key in t and not isinstance(t[key], str):
                raise ValueError('分析依据对应文本无效')
        if '依据对应分析' in t:
            t['依据对应分析'] = clean_analysis(t['依据对应分析'])
    if "行为表现" in t and not isinstance(t["行为表现"], bool):
        raise ValueError("行为表现栏目状态必须为启用或移除")
    for key in ("观察场景", "关联活动"):
        if key not in t:
            continue
        if not isinstance(t[key], list):
            raise ValueError(f"{key}请从官网选项中选择")
        selected = []
        for option in t[key]:
            if not isinstance(option, dict) or any(not isinstance(option.get(k), str) or not option[k].strip() for k in ("groupLabel", "value")):
                raise ValueError(f"{key}选项格式不正确，请重新选择")
            if option.get("customized") not in (None, False):
                raise ValueError(f"{key}请使用官网已有选项")
            value = {"groupLabel": option["groupLabel"].strip(), "value": option["value"].strip(),
                     "customized": False, "systemId": option.get("systemId")}
            if value not in selected:
                selected.append(value)
        t[key] = selected
    if "行为跟进" in t:
        if not isinstance(t['行为跟进'], list) or any(not isinstance(s, str) for s in t['行为跟进']):
            raise ValueError('行为跟进必须是文字段落列表')
        t["行为跟进"] = _followup(t["行为跟进"])
    if "自定义段落" in t:
        if not isinstance(t["自定义段落"], list):
            raise ValueError("自定义段落格式不正确")
        ids = set()
        for s in t["自定义段落"]:
            if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", s["id"]) or s["id"] in ids:
                raise ValueError("自定义段落编号无效或重复")
            ids.add(s["id"])
            if not isinstance(s.get("title"), str) or not 1 <= len(s["title"].strip().encode("utf-16-le")) // 2 <= 20:
                raise ValueError("自定义段落标题请填写 1～20 个字符")
            if not isinstance(s.get("content"), str):
                raise ValueError("自定义段落内容必须是文字")
            s["title"], s["content"] = s["title"].strip(), s["content"].strip()
    return t


def sections(table: dict) -> list:
    t = normalize(table)
    names = {"观察内容": None, "分析": "观察分析", "行为跟进": "回应策略", "家庭指导": "家庭指导"}
    return [(k, "、".join(f"{s['groupLabel']}·{s['value']}" for s in t[k]))
            for k in ("观察场景", "关联活动") if k in t] + \
        [(names[k], t[k]) for k in names if k in t] + \
        [(s["title"], s["content"]) for s in t.get("自定义段落", [])]


def writing_note(table: dict, teacher_note: str) -> str:
    """官网选项是观察背景，不把它扩写为未发生的过程。"""
    contexts = [f"{key}：" + "、".join(s["value"] for s in table.get(key, []))
                for key in ("观察场景", "关联活动") if table.get(key)]
    return "\n".join([teacher_note or ""] + contexts).strip()


def to_text(t: dict) -> str:
    """拼成一整段纯文本，用于发布或导出。"""
    return "\n\n".join(("" if key is None else f"【{key}】\n") +
                       (''.join(value) if isinstance(value, list) else value)
                       for key, value in sections(t) if value)
