"""离线教育依据检索。知识条文用于解释行为，不替代幼儿现场证据。"""
from collections import Counter
from copy import deepcopy
from resource_pack import cached as lru_cache, load_json, path_for, files
import hashlib
import json
import math
import re

from config import BUILTIN_DATA_DIR
from knowledge import indicator_by_id, indicator_lines
import core_indicators

AGES = {1: '3-4岁', 3: '4-5岁', 5: '5-6岁'}
DOMAINS = ('健康领域', '语言领域', '社会领域', '科学领域', '艺术领域')
NURSERY_AGES = {128: '24—36个月'}
NURSERY_DOMAINS = ('动作与习惯', '情感与社会', '认知与探索', '语言与沟通')
NURSERY_SOURCES = ('nursery_development', 'nursery_indicator')
# These aliases select a literal source paragraph; they never change its text.
# Each group links the same concrete action in teacher language and source prose.
NURSERY_TOPICS = {
    4858: (('身高', '身长', '量身高', '长高'), ('体重',)),
    4859: (('攀爬', '梯子'), ('楼梯', '扶栏'), ('跑步', '边跑', '躲避'),
           ('扔球', '投球', '扔向'), ('踢球',), ('单脚',), ('童车', '骑车'),
           ('木板', '平衡'), ('双脚', '跳下', '往下跳', '起跳', '跳跃')),
    4860: (('敲击', '锤子', '木桩'), ('穿珠', '串珠', '木珠', '珠子'),
           ('彩泥', '揉', '搓', '捏', '泥'), ('画线', '用笔'),
           ('堆叠', '垒高', '小木块'), ('剪刀', '剪纸'), ('折纸', '折叠')),
    4861: (('吃饭', '进食', '勺子', '扶碗'), ('如厕', '小便', '大小便', '尿'),
           ('洗手',), ('刷牙',), ('戴帽', '穿脱', '鞋子', '纽扣'),
           ('车辆', '有车', '躲避危险')),
    4862: (('分离', '离开', '照看'), ('养育者', '互动'), ('独自玩耍', '独自玩'),
           ('陌生人', '熟悉的环境')),
    4863: (('内疚', '尴尬', '尿湿'), ('故事中的情绪', '情绪反应'), ('流泪', '同情', '抱抱'),
           ('害怕', '怕怕', '情绪'), ('哭泣', '哭闹', '大声哭', '哭的次数')),
    4864: (('性别', '自我', '独立完成', '自己来'), ('惯例', '睡觉前'),
           ('和同伴玩', '和同伴一起', '喜欢的同伴', '玩伴', '朋友'), ('分享',), ('帮忙', '收拾'),
           ('是非', '不可以', '规则')),
    4865: (('颜色', '红色', '蓝色', '黄色'), ('声音', '门铃', '雷声'),
           ('触觉', '摸出', '布袋'), ('动物', '小狗', '小猫'),
           ('专注', '专心'), ('记起', '记忆'), ('图形', '圆形', '三角形'),
           ('数数', '唱数', '点数', '按数取物', '手口一致')),
    4866: (('大小', '杯子', '容器'), ('上下的位置', '里外', '方位', '上面', '下面', '里边', '外边'),
           ('分类', '归类', '按类'), ('打开', '封口'), ('一个球', '全部'),
           ('家里人', '家人名字', '居住地')),
    4867: (('节奏', '强弱', '快慢'), ('唱歌', '歌曲', '音乐', '模仿操'),
           ('彩泥', '美工', '命名'), ('积木', '拼搭', '垒高', '高楼'),
           ('扮演', '假装', '角色'), ('替代物', '当作', '以物代物')),
    4868: (('听懂', '指令', '连续动作'), ('方言',), ('词语', '时间的词'),
           ('用处', '日用品'), ('职业', '警察', '医生')),
    4869: (('词汇', '形容', '描述'), ('代词',), ('量词',), ('短句', '简单句'),
           ('谢谢', '再见', '礼貌'), ('姓名', '年龄', '性别'),
           ('表达需要', '表达需求', '我要'), ('模仿成人', '句子'),
           ('疑问', '提问', '这是什么', '为什么')),
    4870: (('故事', '情节'), ('词或短句', '儿歌', '背诵'),
           ('看书', '绘本', '阅读'), ('翻书',), ('爱护书', '放回原处', '合拢')),
}
NOTICE = '条文是理解和支持幼儿的参考，不是该幼儿已经做到的证明，也不是统一达标线。一次未表现不等于不会。'
# 检索同义线索只决定查哪一条；返回的正文始终来自原文件。
TOPICS = (
    ('具有一定的平衡能力', ('双脚', '起跳', '落地', '站稳', '平衡', '跨跳', '跳圈', '攀爬', '钻爬', '躲闪', '拍球', '接球')),
    ('具有一定的力量和耐力', ('力量', '耐力', '搬动', '搬运', '抬起', '提起', '提袋', '悬垂', '投掷')),
    ('手的动作灵活协调', ('剪刀', '剪纸', '折纸', '筷子', '画线', '手指', '精细动作')),
    ('情绪安定愉快', ('情绪', '生气', '哭泣', '难过', '安抚', '平静', '发脾气')),
    ('具有基本的生活自理能力', ('穿衣', '穿脱', '鞋带', '整理', '收拾', '纽扣')),
    ('具有初步的安全意识', ('安全', '危险', '保护', '求救', '陌生人')),
    ('认真听并能听懂常用语言', ('倾听', '听讲', '听完', '听懂', '听故事')),
    ('愿意讲话并能清楚地表达', ('表达', '讲述', '讲清', '说话', '提问', '讲故事')),
    ('喜欢听故事', ('绘本', '图画书', '看书', '阅读')),
    ('愿意与人交往', ('交往', '加入游戏', '邀请', '伙伴')),
    ('能与同伴友好相处', ('轮流', '商量', '协商', '合作', '争抢', '分享', '冲突')),
    ('关心尊重他人', ('关心', '照顾', '帮忙', '帮助同伴', '弟弟妹妹', '帮助别人')),
    ('遵守基本的行为规范', ('规则', '约定', '轮候', '排队', '归还')),
    ('喜欢探究', ('探索', '好奇', '提问', '观察变化')),
    ('具有初步的探究能力', ('猜测', '验证', '实验', '比较', '记录发现', '磁铁', '下沉', '浮起', '斜坡', '坡度', '滚动', '滚珠', '轨道', '滚落', '沉浮')),
    ('数量关系', ('数数', '数量', '多少', '计数', '排序')),
    ('形状与空间', ('形状', '图形', '空间', '搭建', '积木', '搭桥')),
    ('喜欢进行艺术活动', ('唱歌', '跳舞', '绘画', '画画', '表演', '节奏')),
    ('具有初步的艺术表现', ('创作', '配色', '线条', '涂画', '伴奏', '扮演')),
)
STOP = set('老师 幼儿 孩子 同学 活动 观察 记录 分析 这次 今天 本次 进行 可以 能够 已经 需要 自己 一个 一起 时候 这样 那样 表现 比较 知道 发现 愿意 时候 我们 他们 目标 发展 能力 指标'.split())


def _core_reference(item, hashes):
    """Keep literal coordinates and names while fitting the existing library UI."""
    return dict(item, kind='core_indicator', source_kind='core_indicator',
                core_domain=item['domain'],
                domain=('科学' if item['domain']=='数学' else item['domain'])+'领域',
                heading=item['title'],
                age_band=item['age']+'岁' if item.get('age') else '未分年龄',
                source_url='', location=item['source'],
                source_sha256=hashes[item['source_file']],
                enabled_for_analysis=True, attainment_assessed=False)


def _grams(text):
    out=set()
    for run in re.findall(r'[\u3400-\u9fff]+|[A-Za-z]{2,}', str(text or '')):
        out.update(run[i:i+2] for i in range(len(run)-1))
    return out-STOP


@lru_cache(maxsize=1)
def _nursery_catalog():
    source = path_for('ref/nursery-development.json')
    return _load_nursery(source) if source else []


def _load_nursery(source):
    data = json.loads(source.read_text('utf-8'))
    sources = {item['file']: item['sha256'] for item in data['source_files']}
    chunks = data['chunks']
    if data['system_id'] != 35 or data['supported_levels'] != [128]:
        raise ValueError('托育依据体系或年龄档不正确')
    if len({c['id'] for c in chunks}) != len(chunks):
        raise ValueError('托育依据编号重复')
    for c in chunks:
        if (c['source_kind'] not in NURSERY_SOURCES or c['behavior_level'] != 128
                or c['age_band'] != NURSERY_AGES[128] or c['domain'] not in NURSERY_DOMAINS
                or c['source_sha256'] != sources.get(c['source_file'])
                or c['attainment_assessed'] is not False):
            raise ValueError('托育依据来源或年龄边界损坏')
    return chunks


@lru_cache(maxsize=1)
def catalog():
    data=load_json('ref/official-guide.json', {'chunks': []})
    chunks=[dict(c, enabled_for_analysis=True) for c in data['chunks']]
    provided=load_json('ref/provided-observation-guide.json', {'chunks': []})
    chunks.extend(dict(c,enabled_for_analysis=True) for c in provided['chunks'])
    original=core_indicators._library()
    chunks.extend(_core_reference(c,original['source_sha256']) for c in core_indicators.items())
    # 保存现有园本整理资料供教师核对。来源未核验前不把摘要当作官方条文送入分析。
    for path in sorted(p for name,p in files('ref/').items() if re.fullmatch(r'ref/core_[^/]+\.md',name)):
        name=path.stem.removeprefix('core_')
        domain=('科学' if name=='数学' else name)+'领域'
        heading=path.stem
        age='未分年龄'
        lines=[]
        def flush():
            body='\n'.join(lines).strip()
            if body:
                chunks.append(dict(id='local-'+hashlib.sha256((path.name+heading+body).encode()).hexdigest()[:14],
                    source='园本核心经验整理（原出处待核验）', heading=heading, age_band=age,domain=domain,text=body,
                    source_kind='local_summary',source_url='',location=path.name,enabled_for_analysis=False))
        for line in path.read_text('utf-8').splitlines():
            if line.startswith('#'):
                flush();lines=[];heading=path.stem+' / '+line.lstrip('# ').strip()
                match=re.search(r'[345]-[456]岁',line)
                age=match.group() if match else '未分年龄'
            else:lines.append(line)
        flush()
    chunks.extend(deepcopy(_nursery_catalog()))
    return chunks


def by_id(chunk_id):
    return next((deepcopy(c) for c in catalog() if c['id']==chunk_id), None)


def browse(domain='', age='', q='', limit=120, source=''):
    chunks=[c for c in catalog() if (not domain or c['domain']==domain)
            and (not source or c['source_kind']==source)
            and (not age or c['age_band']==age or (age in AGES.values() and c['age_band']=='3-6岁'))]
    if q.strip():
        terms=q.strip().split()
        chunks=[c for c in chunks if all(t.casefold() in (c['heading']+' '+c['text']).casefold() for t in terms)]
    return {'items':[dict(c) for c in chunks[:limit]], 'total':len(chunks),
            'notice': NOTICE if catalog() else '尚未导入知识资料。可在设置中导入有权使用的资料；没有依据时不会生成虚构引用。',
            'domains':list(DOMAINS)+list(NURSERY_DOMAINS),
            'ages':list(AGES.values())+list(NURSERY_AGES.values())+['未分年龄'],
            'sources':[entry for entry in [
                {'name':'教育部《3-6岁儿童学习与发展指南》原文','kind':'official_guide','used_for_analysis':True},
                {'name':'教师自行导入的参考资料','kind':'provided_reference','used_for_analysis':True},
                {'name':'六领域原始核心经验表（原表细项，含行列与哈希）','kind':'core_indicator','used_for_analysis':True},
                {'name':'园本核心经验整理（原出处待核验）','kind':'local_summary','used_for_analysis':False},
                {'name':'教师自行导入的托育24—36个月发展参考','kind':'nursery_development','used_for_analysis':True},
                {'name':'幼师口袋托育体系24—36个月指标原文','kind':'nursery_indicator','used_for_analysis':True},
            ] if any(c['source_kind'] == entry['kind'] for c in catalog())]}


def _nursery_evidence(level, picks, story, note, limit):
    """Use only verified 24–36 month prose, with no photo-only candidate pool."""
    if level not in NURSERY_AGES:
        return []
    pool = _nursery_catalog()
    selected = set()
    selected_lines = set()
    for pick in picks or []:
        if not isinstance(pick, dict):
            continue
        try:
            iid = int(pick.get('indicator_id', 0))
        except (TypeError, ValueError):
            continue
        if iid not in NURSERY_TOPICS:
            continue
        selected.add(iid)
        lines = pick.get('behavior_lines') or []
        selected_lines.update(re.sub(r'\s+', '', row['line']) for row in lines
                              if isinstance(row, dict) and isinstance(row.get('line'), str))
        if isinstance(pick.get('behavior_line'), str):
            selected_lines.add(re.sub(r'\s+', '', pick['behavior_line']))
    # A selected line only affects ranking if it is actually in the current
    # website age column. Cross-age/custom text cannot become a source entry.
    exact = {c['id'] for c in pool if c['source_kind'] == 'nursery_indicator'
             and re.sub(r'\s+', '', c['text']) in selected_lines}
    query = re.sub(r'\s+', '', f'{note or ""} {story or ""}')
    tokens = _grams(query)
    signaled = {iid for iid, groups in NURSERY_TOPICS.items()
                if any(any(term in query for term in group) for group in groups)}
    if not selected and not signaled:
        return []
    ranked = []
    for c in pool:
        iid = c['indicator_id']
        if iid not in selected | signaled:
            continue
        body = re.sub(r'\s+', '', c['text'])
        hits = sum(any(term in query for term in group) and any(term in body for term in group)
                   for group in NURSERY_TOPICS[iid])
        shared = len(tokens & _grams(body))
        score = hits * 8 + shared
        if not hits and shared < 2 and iid not in selected:
            continue
        if not score and c['source_kind'] != 'nursery_indicator':
            continue
        ranked.append((c['id'] in exact, score, c))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]['id']))
    # Keep one current website paragraph per explicit selection. Other entries
    # need an actual behavior clue; never fill the limit just because it exists.
    result = []
    for iid in sorted(selected):
        found = next((c for _, _, c in ranked
                      if c['indicator_id'] == iid and c['source_kind'] == 'nursery_indicator'), None)
        if found:
            result.append(found)
    seen = {c['id'] for c in result}
    for exact_line, score, c in ranked:
        if c['id'] not in seen and (exact_line or score >= 8):
            result.append(c)
            seen.add(c['id'])
    return [deepcopy(c) for c in result[:max(1, min(int(limit), 6))]]


def _reference_evidence(level, picks=None, story='', note='', limit=6):
    """返回可溯源 chunks；level 必须是班级年龄档 1/3/5，不以勾选的行为档冒充年龄。"""
    age=AGES.get(level)
    if not age:return []
    clues=[str(story or ''),str(note or '')]
    activity_query=' '.join(clues)
    domains=set()
    selected_ids=set()
    selected_goals=[]
    for pick in picks or []:
        if not isinstance(pick,dict):continue
        try:ind=indicator_by_id(int(pick.get('indicator_id',0)))
        except (TypeError,ValueError):continue
        if not ind:continue
        domains.add(ind['domain'])
        selected_ids.add(ind['id'])
        selected_goals.append(ind.get('goal',''))
        clues.extend([ind['name'],ind.get('goal',''),ind.get('subdomain','')])
        # 只用当前班级年龄档原文作检索线索，不使用模型传回或老师跨档勾选的行来改变年龄。
        clues.extend(indicator_lines(ind,level))
    query=' '.join(clues).strip()
    tokens=_grams(query)
    if not tokens:return []
    candidates=[c for c in catalog() if c['source_kind']!='core_indicator' and c['enabled_for_analysis']
                and (c['age_band'] in (age,'3-6岁') or (c['source_kind']=='provided_reference'
                     and c.get('indicator_id') in selected_ids and c.get('behavior_level')==level))
                and (not domains or c['domain'] in domains)]
    # 活动线索来自实录/老师说明；整列指标包含多个行为，不能把这些行为都误作本次已发生。
    topic_strength={title:sum(term in activity_query for term in terms) for title,terms in TOPICS}
    frequency=Counter(token for c in candidates for token in _grams(c['heading']+' '+c['text']))
    groups={}
    provided=[]
    for c in candidates:
        heading_tokens=_grams(c['heading']); body_tokens=_grams(c['text'])
        shared=tokens & (heading_tokens|body_tokens)
        score=sum(math.log(1+len(candidates)/(frequency[t]+1))*(1.5 if t in heading_tokens else 1) for t in shared)
        if c['source_kind']=='provided_reference':
            provided.append((score,c))
            continue
        key=c['heading'].removesuffix(' / 教育建议')
        group=groups.setdefault(key,{'age':None,'advice':[],'score':0,'signal':0,'shared':0})
        topic_hits=sum(strength for title,strength in topic_strength.items() if title in key)
        selected=any(goal and goal in key for goal in selected_goals)
        group['signal']=max(group['signal'],topic_hits+3*selected)
        group['score']=max(group['score'],score)
        group['shared']=max(group['shared'],len(shared))
        if c['age_band']==age:
            group['age']=c
        else:
            # 标题是同组共有信息，建议之间只按正文的具体联系排序。
            body_score=sum(math.log(1+len(candidates)/(frequency[t]+1)) for t in tokens & body_tokens)
            group['advice'].append((body_score,c))
    relevant=[(key,g) for key,g in groups.items() if g['signal']]
    if not relevant:
        # 没有明确活动或已选目标时才退回文字检索；无法建立具体联系就留空。
        # 要求同时有目标名称线索，避免“看了一会儿”等叙事碎片单独触发社会/生活条文。
        relevant=[(key,g) for key,g in groups.items() if g['shared']>=2 and g['score']>=5
                  and len(tokens & _grams(re.sub(r'^.*?目标\d+\s*','',key)))>=2]
    relevant.sort(key=lambda item:(-item[1]['signal'],-item[1]['score'],item[0]))
    bound=max(1,min(limit,12))
    # 教师已选的真实指引不会因官方长段落得分较高而消失。
    provided.sort(key=lambda item:(-item[0],item[1]['id']))
    result=[dict(c) for _,c in provided[:bound]]
    selected_groups=[]
    for _,group in relevant:
        if len(result)>=bound:break
        if group['age']:
            result.append(dict(group['age']))
            selected_groups.append(group)
    # 先保证各主线目标有当前年龄原文，再各取最多一条相关建议；不凑满条数。
    for group in selected_groups:
        if len(result)>=bound:break
        advice=sorted(group['advice'],key=lambda item:(-item[0],item[1]['id']))
        if advice and advice[0][0]>0:
            result.append(dict(advice[0][1]))
    return result


def build_evidence(level, picks=None, story='', note='', limit=6):
    """Offer up to three literal core items, keeping older reference IDs usable.

    Teacher facts rank before the drafted story. A class age is retrieval
    context, never a language/health stage or proof of attainment. Generic
    descriptions do not create a context-only candidate pool here.
    """
    if level in NURSERY_AGES:
        return _nursery_evidence(level, picks, story, note, limit)
    if level not in AGES:
        return []
    bound=max(1,min(int(limit),6))
    legacy=_reference_evidence(level,picks,story,note,limit=bound)
    domains=[]
    for pick in picks or []:
        if not isinstance(pick,dict):
            continue
        try:
            selected=indicator_by_id(int(pick.get('indicator_id',0)))
        except (TypeError,ValueError):
            continue
        if selected and selected['domain'] not in domains:
            domains.append(selected['domain'])
    class_name={1:'小班',3:'中班',5:'大班'}[level]
    identifiers=[]
    # Original teacher text supplies priority, not model-written narratives or
    # the full behavior list belonging to a selected website indicator.
    for text in (note,story):
        if not str(text or '').strip():
            continue
        for item in core_indicators.candidates(' '.join(domains),class_name,str(text),max_items=40):
            if not item['context_only'] and item['id'] not in identifiers:
                identifiers.append(item['id'])
    lookup={c['id']:c for c in catalog() if c['source_kind']=='core_indicator'}
    core=[deepcopy(lookup[ident]) for ident in identifiers]
    if not core:
        return legacy
    provided=[c for c in legacy if c['source_kind']=='provided_reference']
    other=[c for c in legacy if c['source_kind']!='provided_reference']
    available=bound-len(provided)
    reserve=1 if other and available>1 else 0
    chosen=core[:min(3,available-reserve)]
    # Existing explicit website selections keep their original reference. New
    # core names complement them and never replace or mark them as achieved.
    return (provided+chosen+other)[:bound]
