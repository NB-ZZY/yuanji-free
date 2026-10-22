"""保守整理中文正文的标点，不改写观察证据。

原话、数字、单位、书名和段落缩进都属于正文信息。这里不删符号、不补
句子、不替换语气，也不把结构化输出变成正文；JSON 应由调用方先解析。
"""
import json
import re


_CJK = r"\u3400-\u9fff\uf900-\ufaff\U00020000-\U0003134f"
_CHINESE = re.compile(f"[{_CJK}]")
_MAP = {",": "，", ".": "。", ";": "；", ":": "：", "?": "？", "!": "！"}
# 原话保持原样；未闭合的中文引语也不擅自补写、重新配对。
_QUOTES = re.compile(r'''(“[^”]*(?:”|$)|‘[^’]*(?:’|$)|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')


def _normalize_prose(text: str) -> str:
    """仅整理紧邻中文的半角标点，保留数字和外文的写法。"""
    out = []
    for i, ch in enumerate(text):
        if ch not in _MAP:
            out.append(ch)
            continue
        before = text[:i].rstrip(" \t")[-1:]
        after = text[i + 1:].lstrip(" \t")[:1]
        # 小数、时间、比例、千位分隔和省略号都不能按句子标点改写。
        numeric = before.isdigit() and after.isdigit()
        dots = ch == "." and (before == "." or after == ".")
        chinese = _CHINESE.fullmatch(before) or _CHINESE.fullmatch(after)
        out.append(_MAP[ch] if chinese and not numeric and not dots else ch)
    return "".join(out)


def clean(text: str) -> str:
    """清理正文排版；保留原话、数值、语气和全角段首缩进。"""
    if not text:
        return text
    s = str(text)
    if s.lstrip().startswith(("{", "[")):
        try:
            if isinstance(json.loads(s), (dict, list)):
                return s
        except (ValueError, TypeError):
            pass

    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 空格可能连接外文词或数字与单位，不能全删；保留全角段首缩进。
    s = re.sub(r"[ \t]+(?=\n|$)", "", s).strip(" \t\n")
    parts = _QUOTES.split(s)
    return "".join(part if i % 2 else _normalize_prose(part)
                   for i, part in enumerate(parts))


def clean_list(items) -> list:
    return [clean(x) for x in (items or []) if x is not None and str(x).strip()]


RULE = """
# 中文表达与标点
- 正文使用自然、规范的中文标点。问句用问号，感叹语气可用感叹号；冒号、括号、
  书名号和引号按语义使用。不要为了统一格式把它们换成逗号或句号。
- 教师提供的幼儿原话忠实保留，包括提问、感叹和停顿；不要编造引语，不要改变原意。
  新组织的引用通常用中文引号“”，嵌套引用用‘’；作品名称用《》。
- 数值、符号和单位按原始依据保留，如 110.8cm、-2℃、8:30、50%、3～5；
  不得删掉小数点、百分号、负号或范围符号，不为排版改变数值。
- 保留必要的词间空格、段落和原有缩进。
- 这些要求只针对正文字符串。输出 JSON 时，键名、双引号、冒号、逗号等语法
  必须保持合法 JSON，不能替换成中文标点。
"""
