"""三类写作共用同一份内置流程；随 app/data 一起打包，不访问外部网站。"""
from config import BUILTIN_DATA_DIR

COMMON_PROMPT = (BUILTIN_DATA_DIR / 'writing' / 'SKILL.md').read_text(encoding='utf-8').split('---', 2)[-1].strip() + '\n'
_NURSERY_PROMPT = (BUILTIN_DATA_DIR / 'observation' / 'NURSERY_SKILL.md').read_text(encoding='utf-8').split('---', 2)[-1].strip() + '\n'


def nursery_prompt(level: int) -> str:
    """托班只使用官网 24—36 个月档，其他班级继续沿用原流程。"""
    return '\n' + _NURSERY_PROMPT if level == 128 else ''

FINAL_REVIEW = '\n最终核对：个人风格、旧稿和变换句式建议不能覆盖事实边界；保留推测标记，不为凑篇幅或去重编造事件。只交当前任务规定的结果，不输出自查过程。\n'
