"""Traceable observation indicators from the six original core-experience tables.

The JSON is a reviewed coordinate index, not a rewrite of the source tables.
Only whitespace is normalized for names/search. Age bands and development stages
remain separate. Candidate retrieval never assigns attainment to a child.
"""
from __future__ import annotations

from copy import deepcopy
from resource_pack import cached as lru_cache, path_for
import hashlib
import json
from pathlib import Path
import re
import sys

from config import BUILTIN_DATA_DIR

KDIR = BUILTIN_DATA_DIR / 'ref' / 'core-experience'
AGE_OF_LEVEL = {'小班': '3-4', '中班': '4-5', '大班': '5-6'}
DOMAINS = ('健康', '数学', '社会', '科学', '艺术', '语言')
VENUES = {
    '科探': ('科学', '数学'), '科学': ('科学',), '自然角': ('科学',),
    '沙水': ('科学', '社会', '健康'), '沙池': ('科学', '社会', '健康'),
    '益智': ('数学', '科学'), '建构': ('科学', '数学', '社会'),
    '建造': ('科学', '数学', '社会'), '积木': ('科学', '数学', '社会'),
    '美工': ('艺术', '健康'), '创意': ('艺术', '健康'), '绘画': ('艺术',),
    '手工': ('艺术', '健康'), '阅读': ('语言',), '图书': ('语言',),
    '表演': ('艺术', '语言'), '音乐': ('艺术',),
    '娃娃家': ('社会', '语言'), '角色': ('社会', '语言'),
    '医院': ('社会', '语言'), '超市': ('社会', '数学'),
    '运动': ('健康',), '体育': ('健康',), '户外': ('健康', '社会'),
    '生活': ('健康', '社会'),
}


def compact(text: str) -> str:
    """Remove source layout whitespace; never repair OCR characters."""
    return re.sub(r'\s+', '', str(text or ''))


@lru_cache(maxsize=1)
def _library() -> dict:
    source = path_for('ref/core-experience/core_indicators.json')
    return _load_library(source.parent) if source else {'source_sha256': {}, 'items': []}


def _load_library(directory) -> dict:
    """Verify the complete source/index unit before a private pack is activated."""
    directory = Path(directory)
    data = json.loads((directory / 'core_indicators.json').read_text('utf-8'))
    source_lines = {}
    for name, expected in data['source_sha256'].items():
        path = directory / 'core' / name
        if path.parent.resolve() != (directory / 'core').resolve():
            raise ValueError('Invalid original table path')
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != expected:
            raise ValueError(f'Original core table changed; recheck index: {name}')
        source_lines[name] = blob.decode('utf-8-sig').splitlines()
    seen = set()
    for item in data['items']:
        if item['id'] in seen or item['domain'] not in DOMAINS:
            raise ValueError('Invalid or duplicate original indicator')
        seen.add(item['id'])
        if item.get('age') and item.get('stage'):
            raise ValueError('An age band must not replace a development stage')
        if item.get('age') and item['age'] not in AGE_OF_LEVEL.values():
            raise ValueError('Invalid source age band')
        original = source_lines[item['source_file']]
        coordinates = item['source_cells']
        for ref in coordinates:
            cells = original[ref['line'] - 1].split('｜')
            value = cells[ref['column'] - 1]
            if value[ref['start']:ref['end']] != ref['original']:
                raise ValueError(f"Broken source coordinate: {item['id']}")
        title_refs = [ref for ref in coordinates if ref['role'] == 'title']
        text_refs = [ref for ref in coordinates if ref['role'] == 'text']
        # A title and its expression must each be one actual source substring.
        # No damaged/missing columns or cross-line prose are reconstructed here.
        if len(title_refs) != 1 or len(text_refs) != 1:
            raise ValueError('An indicator must have one literal title and expression')
        if compact(title_refs[0]['original']).rstrip('。；;') != item['title']:
            raise ValueError('Indicator title is not literal source text')
        if text_refs[0]['original'].strip() != item['text']:
            raise ValueError('Indicator expression is not literal source text')
        age_refs = [ref for ref in coordinates if ref['role'] == 'age']
        stage_refs = [ref for ref in coordinates if ref['role'] == 'stage']
        if item.get('age'):
            if len(age_refs) != 1 or '-'.join(re.findall(r'\d', age_refs[0]['original'])) != item['age']:
                raise ValueError('Indicator age is not a literal source age label')
        elif age_refs:
            raise ValueError('Unexpected age coordinate')
        if item.get('stage'):
            if len(stage_refs) != 1 or compact(stage_refs[0]['original']) != item['stage']:
                raise ValueError('Indicator stage is not a literal source stage label')
        elif stage_refs:
            raise ValueError('Unexpected stage coordinate')
        if item['source_lines'] != sorted({ref['line'] for ref in coordinates}):
            raise ValueError('Source line list does not match literal coordinates')
        for i, note in enumerate(item.get('source_notes', ())):
            note_refs = [ref for ref in coordinates if ref['role'] == f'note:{i}']
            if len(note_refs) != 1 or note['text'] != note_refs[0]['original']:
                raise ValueError('Additional source note is not a literal substring')
            ref = note_refs[0]
            if (note['source_file'] != item['source_file'] or
                    note['source_line'] != ref['line'] or note['source_column'] != ref['column']):
                raise ValueError('Additional source note coordinates are inconsistent')
    return data


def items() -> list[dict]:
    """Return source-backed fine items; callers cannot mutate the cached index."""
    return deepcopy(_library()['items'])


def _domains(domain: str) -> list[str]:
    text = compact(domain)
    result = [name for name in DOMAINS if name in text]
    for venue, names in VENUES.items():
        if venue in text:
            result.extend(name for name in names if name not in result)
    return result


def _score(item: dict, brief: str) -> int:
    hits = {word for word in item.get('keywords', ()) if compact(word) in brief}
    score = sum(min(len(word), 4) for word in hits)
    groups = item.get('signals', ())
    if groups and all(any(compact(word) in brief for word in group) for group in groups):
        score += 20
    return score


def _context_pool(pool: list[dict], domains: list[str], count: int, age: str | None = None) -> list[dict]:
    """Spread photo-only choices across mechanisms, not just the first domain."""
    ordered_domains = domains or list(DOMAINS)
    result = []
    seen = set()
    seen_titles = set()
    seen_themes = set()
    # Curated, clear original items provide a bounded photo-only candidate pool.
    # No guess about the photographed action or the child's level is made.
    for round_no in range(count):
        added = False
        for name in ordered_domains:
            candidates_ = [item for item in pool if item['domain'] == name
                           and item['id'] not in seen]
            if not candidates_:
                continue
            # Do not let three language stages or one subject family consume
            # the whole photo pool. Earlier-age expressions remain available:
            # a class age is context, not proof of a child's achieved level.
            candidates_.sort(key=lambda item: (
                (name, item['title']) in seen_titles,
                (name, item.get('theme')) in seen_themes,
                -item.get('context_priority', 0),
                bool(age and item.get('age') and item['age'] != age),
                item['id']))
            item = candidates_[0]
            result.append(item)
            seen.add(item['id'])
            seen_titles.add((name, item['title']))
            seen_themes.add((name, item.get('theme')))
            added = True
            if len(result) >= count:
                return result
        if not added:
            break
    return result


def candidates(domain: str, level: str, brief: str, max_items: int = 12) -> list[dict]:
    """Retrieve applicable original indicators, never mark a child as achieving one.

    Explicit behavior ranks before venue. Known age only breaks close rankings;
    a behavior from an earlier band may still be relevant in an older class.
    Language/health stages are not filtered by class age. Empty/unknown behavior
    returns a varied context-only pool for a vision model to inspect and select.
    """
    count = max(0, min(int(max_items), 40))
    if not count:
        return []
    selected_domains = _domains(domain)
    age = AGE_OF_LEVEL.get(level) or (level if level in AGE_OF_LEVEL.values() else None)
    body = compact(brief)
    pool = items()
    ranked = []
    if body:
        for item in pool:
            score = _score(item, body)
            if score:
                rank = (score, item.get('age') == age if age else False,
                        item['domain'] in selected_domains)
                ranked.append((rank, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if ranked:
        chosen = [item for _, item in ranked[:count]]
        context_only = False
    else:
        contextual = [item for item in pool if not selected_domains or item['domain'] in selected_domains]
        chosen = _context_pool(contextual, selected_domains, count, age)
        context_only = True
    result = []
    for item in chosen:
        item['context_only'] = context_only
        item['match_basis'] = '地点或照片待核对；仅供选择，不构成行为证据' if context_only else '行为相关候选；达到程度须结合事实与支持条件判断'
        item['retrieval_basis'] = 'context' if context_only else 'behavior_terms'
        item['behavior_score'] = 0 if context_only else _score(item, body)
        item['attainment_assessed'] = False
        item['class_age'] = age
        result.append(item)
    return result
