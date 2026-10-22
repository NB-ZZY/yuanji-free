"""知识库：官网普通班 36 个指标、官网托育体系及站内 ID 映射。

常规班型使用 indicators.json；托班使用从官网账号核验的 nursery_indicators.json。
"""
import json
import re
from resource_pack import cached as lru_cache, load_json, path_for
from typing import Optional

from config import BUILTIN_DATA_DIR

# 班型 → 表现行为档位（用户规则：小班→1，中班→3，大班→5）
NURSERY_LEVELS = (128,)
LEVEL_LABELS = {1: "小班", 3: "中班", 5: "大班", 128: "托班（24—36个月）"}
CLASS_TYPE_LEVEL = {"JUNIOR": 1, "MIDDLE": 3, "SENIOR": 5, "NURSERY": 128}
LEVEL_GRADE_ID = {1: 20, 3: 22, 5: 24, 128: 128}  # 站内 gradeId（班型对应的主档）
# 表现行为二、四是"两档中间"的水平：二 = 超过小班还不到中班，四 = 超过中班还不到大班。
# 站内 gradeId 是连号的（20/21/22/23/24），所以二=21、四=23。
GRADE_ID = {1: 20, 2: 21, 3: 22, 4: 23, 5: 24, 128: 128}
LEVEL_CN = {1: "一", 3: "三", 5: "五", 128: "24—36个月"}
GRADE_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 128: "24—36个月"}
GRADE_TIP = {
    1: "小班水平",
    2: "超过小班水平，但还不到中班水平",
    3: "中班水平",
    4: "超过中班水平，但还不到大班水平",
    5: "大班水平",
    128: "托育体系 · 24—36个月",
}


def class_type_to_level(class_type: Optional[str], class_name: str = "") -> int:
    class_type = str(class_type or "").upper()
    if class_type in CLASS_TYPE_LEVEL:
        return CLASS_TYPE_LEVEL[class_type]
    # 兜底：按班级名猜
    if "托班" in class_name or "托育" in class_name:
        return 128
    if "小" in class_name:
        return 1
    if "大" in class_name:
        return 5
    return 3


def grade_score(level: int) -> int:
    """内部月龄档 128 与官网评分 2 是两个字段，发布时不可混用。"""
    if level not in GRADE_ID:
        raise ValueError("不支持的年龄档位")
    return 2 if level == 128 else level


def system_id_for_level(level: int) -> int:
    if level not in GRADE_ID:
        raise ValueError("不支持的年龄档位")
    return 35 if level in NURSERY_LEVELS else 9


@lru_cache(maxsize=6)
def load_indicators(level: Optional[int] = None) -> list[dict]:
    if level in NURSERY_LEVELS:
        return load_json('nursery_indicators.json', [])
    if level is not None and level not in (1, 2, 3, 4, 5):
        return []
    items = load_json('indicators.json', [])
    medal = load_medal_map()
    for it in items:
        m = medal.get(it["name"])
        it["medal_id"] = m["medal_id"] if m else None
        it["cover"] = m["cover"] if m else None
    return items


@lru_cache(maxsize=1)
def load_medal_map() -> dict[str, dict]:
    """medal_map.txt: medalId|名称|targetId|封面hash|gradeDescId:gradeId,..."""
    out = {}
    p = path_for('medal_map.txt')
    if p is None:
        return out
    for line in p.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        mid, name, target, cover, grades = line.split("|")
        out[name] = {
            "medal_id": int(mid),
            "target_id": int(target),
            "cover": cover,
            "grade_desc_ids": [tuple(map(int, g.split(":"))) for g in grades.split(",")],
        }
    return out


def indicator_by_name(name: str, level: Optional[int] = None) -> Optional[dict]:
    for it in load_indicators(level):
        if it["name"] == name:
            return it
    return None


def indicator_by_id(iid: int) -> Optional[dict]:
    for it in load_indicators() + load_indicators(128):
        if it["id"] == iid:
            return it
    return None


def indicator_lines(ind: dict, level: int) -> list[str]:
    return ind["levels"].get(str(level), [])


def compact_catalog(level: int) -> str:
    """给模型看的精简指标目录（只含当前班型档位的表现行为）。"""
    lines = []
    cur = None
    for it in load_indicators(level):
        if it["domain"] != cur:
            cur = it["domain"]
            lines.append(f"\n## {cur}")
        beh = " / ".join(indicator_lines(it, level))
        lines.append(f"[{it['id']}] {it['name']}（{it['subdomain']}·{it['goal']}）：{beh}")
    return "\n".join(lines)


def domains(level: Optional[int] = None) -> list[str]:
    seen = []
    for it in load_indicators(level):
        if it["domain"] not in seen:
            seen.append(it["domain"])
    return seen


def modules_for_level(level: int) -> list[dict]:
    out = []
    for ind in load_indicators(level):
        module = {"systemId": system_id_for_level(level), "realm": ind["domain"],
                  "module": ind.get("record_module") or ind["subdomain"]}
        if module not in out:
            out.append(module)
    return out


def requested_indicators(note: str, level: int) -> dict:
    """补齐老师明确要求的指标；普通活动描述仍由模型判断。

    只接受目录中唯一匹配的名称/用语。匹配用全部年龄档，行为只取当前档；
    身高等有多个参考原句时不擅自选性别或判定达标，留给老师勾选。
    """
    action = r"匹配|关联|链接|勾选|选择|选取|添加|补充|保留"
    negative = r"不要|不需要|不想|不建议|不能|无需|不用|不必|别|不"
    removal = r"去掉|删除|移除|排除|取消"
    command = re.compile(
        rf"(?P<no>{negative}|{removal})?(?:再|自动|随便|随意)?(?:{action})"
        rf"|(?P<remove>{removal})"
    )
    catalog = load_indicators(level)
    decisions = {}
    for clause in re.split(r"[，,。；;\n]", str(note or "")):
        # ponytail: 有选择/否定范围歧义的句子交给模型，不在本地猜测自然语言。
        if re.search(r"或|还是|任选|二选一|不要只|不用只|不必只|别只|是否|要不要|需不需要|[？?]", clause):
            continue
        commands = list(command.finditer(clause))
        requests = []
        for i, match in enumerate(commands):
            end = commands[i + 1].start() if i + 1 < len(commands) else len(clause)
            target = clause[match.end():end]
            # 支持“身高和站姿的指标都要关联”，不把前面的活动背景当指令。
            if not target.strip() and "指标" in clause[:match.start()]:
                target = re.sub(r"(?:都|也)?(?:要|需要)?$", "", clause[:match.start()])
            requests.append((target, bool(match.group("no") or match.group("remove"))))
        if not commands and "指标" in clause:
            match = re.search(rf"(?:{negative})(?P<target>.+)", clause)
            if match:
                requests.append((match.group("target"), True))
        for target, excluded in requests:
            for term in re.split(r"以及|还有|并且|和|与|及|、|/|\+|&", target):
                term = re.sub(r"^[\s：:的把将请]+", "", term)
                term = re.sub(r"(?:相关|对应)?的?(?:指标|表现行为)(?:都|也)?(?:要|需要)?$", "", term.strip())
                term = term.strip(" \t的‘’“”\"'《》【】[]（）()")
                if len(term) < 2:
                    continue
                matches = [ind for ind in catalog if term == ind["name"]]
                if not matches:
                    matches = [ind for ind in catalog if term in " ".join(
                        [ind["name"], ind["goal"]] +
                        [line for lines in ind["levels"].values() for line in lines
                         if not line.endswith(("：", ":"))])]
                if len(matches) != 1:
                    continue
                ind = matches[0]
                lines = [line for line in indicator_lines(ind, level) if not line.endswith(("：", ":"))]
                selected = [line for line in lines if term in line]
                line = selected[0] if len(selected) == 1 else (lines[0] if len(lines) == 1 else "")
                decisions[ind["id"]] = (excluded, {
                    "indicator_id": ind["id"], "indicator_name": ind["name"],
                    "domain": ind["domain"], "subdomain": ind["subdomain"],
                    "medal_id": ind.get("medal_id"), "behavior_line": line,
                    "behavior_lines": [{"level": level, "line": line, "grade": level}] if line else [],
                    **({"system_id": 35, "systemId": 35} if level in NURSERY_LEVELS else {}),
                })
    return {"picks": [pick for excluded, pick in decisions.values() if not excluded],
            "excluded": [iid for iid, (excluded, _) in decisions.items() if excluded]}
