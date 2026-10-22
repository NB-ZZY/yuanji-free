"""篇幅档位：简要 / 普通 / 详细。

同一档位下，小班的孩子事情少、话也少，字数往下压一点；大班反过来往上放一点。
字数不是硬指标，是给模型的"手感"，所以给的是区间，不是一个死数。
"""
from knowledge import LEVEL_LABELS, NURSERY_LEVELS

TIERS = {"brief": "简要", "normal": "普通", "detail": "详细"}
DEFAULT = "normal"


def norm(tier: str) -> str:
    return tier if tier in TIERS else DEFAULT


def cn(tier: str) -> str:
    return TIERS[norm(tier)]


# 随手记正文字数：{档位: {年龄档: (下限, 上限)}}
SIMPLE = {
    "brief":  {1: (55, 75),   3: (60, 85),   5: (70, 95)},
    "normal": {1: (80, 105),  3: (90, 120),  5: (100, 130)},
    "detail": {1: (120, 155), 3: (140, 180), 5: (160, 205)},
}

# 观察表以完整行为过程为主；补写保留内部属性，数字仅用于掌握详略。
TABLE_LIMITS = {"brief": (450, 280, 1), "normal": (800, 450, 3), "detail": (1100, 600, 3)}

_LV = LEVEL_LABELS


def _lv(level: int) -> int:
    if level not in (1, 2, 3, 4, 5, 128):
        raise ValueError("不支持的年龄档位")
    if level in NURSERY_LEVELS:
        return 1
    return level if level in (1, 3, 5) else 5


def simple_block(level: int, tier: str) -> str:
    lo, hi = SIMPLE[norm(tier)][_lv(level)]
    t = cn(tier)
    extra = {
        "brief": "抓住一个关键事实及其意义，不必另加夸赞或期待。",
        "normal": "依据已知行为说明本次的办法、调整或需要；有必要才补一个小建议，不套固定三句。",
        "detail": "已有前后过程时可展开做法怎样变化；没有过程就不补来回。仍旧一段，不分行。",
    }[norm(tier)]
    return (f"\n\n# 篇幅：{t}（这一条优先级高于上面写的任何字数）\n"
            f"- 正文参考 {lo}~{hi} 字，{_LV.get(level, _LV[_lv(level)])}。信息少可以更短，没有最低字数，不补事实凑长度。\n- {extra}\n")


def table_story_block(level: int, tier: str) -> str:
    """详略仅为软参考，直接复用 Skill 的事实和正文约束。"""
    content, _, _ = TABLE_LIMITS[norm(tier)]
    extra = {
        "brief": "实录参考 300–450 字，把一件事情的主要过程写清楚。",
        "normal": "实录参考 500–750 字，用两至三个自然段展开完整事情，重点写怎样做、怎样调整、怎样回应和收尾。",
        "detail": "实录参考 750–1000 字，展开相关的尝试、互动和前后衔接；不拆成流水账，不反复解释同一动作。",
    }[norm(tier)]
    return (f"\n\n# 篇幅参考：{cn(tier)}，{_LV.get(level, _LV[_lv(level)])}\n"
            f"{extra} 通常不超过 {content} 字。\n"
            "允许根据已知事件补全情节，情境补写只保存在内部布尔属性，正文不添加核实提示；不要仍只写一张照片的静态状态。\n"
            "参考字数不是机械配额；老师明确要求不补写时没有最低字数，按已有实况写。不要靠外貌、背景、重复句子或长篇总结增加字数。\n")


def table_analysis_block(level: int, tier: str) -> str:
    """分析和措施按需要写，不能为达到条数扩写。"""
    _, analysis, _ = TABLE_LIMITS[norm(tier)]
    return (f"\n\n# 篇幅参考：{cn(tier)}，{_LV.get(level, _LV[_lv(level)])}\n"
            f"分析通常不超过 {analysis} 字，按证据展开经验、困难或学习需要，不设最低字数，也不强压成一句结论。\n"
            "跟进为一百字以内的一个无编号段落，合并一项支持做法和再观察重点。\n"
            + ({"brief":"分析参考 180–260 字。", "normal":"分析参考 280–420 字。", "detail":"分析参考 400–550 字。"}[norm(tier)]) +
            "分析依事实展开，按新 Skill 写清经验机制、支持条件和边界；不固定段数、不强凑不足或术语，不抄条文，不为凑字数重复。\n")


def table_block(level: int, tier: str) -> str:
    return table_story_block(level, tier) + table_analysis_block(level, tier)
