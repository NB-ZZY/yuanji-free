"""Optional, local-only curriculum/reference packs. Imported files never execute."""
from contextlib import contextmanager
from functools import lru_cache, wraps
import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import threading
from uuid import uuid4
import zipfile

from config import BUILTIN_DATA_DIR, DATA_DIR

ROOT = DATA_DIR / 'resource-packs'
LOCK = threading.RLock()
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 128
DOMAINS = ('健康', '语言', '社会', '科学', '数学', '艺术')
FIXED = {'indicators.json', 'nursery_indicators.json', 'medal_map.txt',
         'ref/official-guide.json', 'ref/provided-observation-guide.json',
         'ref/nursery-development.json', 'ref/core-experience/core_indicators.json'}
FIXED.update(f'ref/core_{d}.md' for d in DOMAINS)
FIXED.update(f'ref/core-experience/core/{d}.txt' for d in DOMAINS)


def cached(maxsize=1):
    """A cache and pack switching share one lock, preventing stale re-insertion."""
    def decorate(fn):
        memo = lru_cache(maxsize=maxsize)(fn)
        @wraps(fn)
        def call(*args, **kwargs):
            with LOCK:
                return memo(*args, **kwargs)
        call.cache_clear = memo.cache_clear
        return call
    return decorate


def allowed(name):
    return name in FIXED


def _active():
    pointer = ROOT / 'active.json'
    if not pointer.exists():
        return None
    try:
        version = json.loads(pointer.read_text('utf-8'))['version']
        if not isinstance(version, str) or not re.fullmatch(r'[a-f0-9]{32}', version):
            raise ValueError()
        directory = ROOT / version
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError()
        return directory
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError('本地资料包索引损坏，请重新导入资料包。') from exc


def path_for(name):
    if not allowed(name):
        raise ValueError('不支持的资料文件。')
    with LOCK:
        active = _active()
        choices = ([active / name] if active else []) + [BUILTIN_DATA_DIR / name]
        return next((p for p in choices if p.is_file() and not p.is_symlink()), None)


def files(prefix=''):
    with LOCK:
        active = _active()
        out = {}
        for directory in (BUILTIN_DATA_DIR, active):
            if directory and directory.is_dir():
                for p in directory.rglob('*'):
                    name = p.relative_to(directory).as_posix()
                    if p.is_file() and not p.is_symlink() and allowed(name) and name.startswith(prefix):
                        out[name] = p
        return out


def load_json(name, default):
    with LOCK:
        p = path_for(name)
        return json.loads(p.read_text('utf-8-sig')) if p else default


def status():
    with LOCK:
        inventory = files()
        return {'files': [{'path': name, 'bytes': p.stat().st_size} for name, p in sorted(inventory.items())],
                'has_indicators': 'indicators.json' in inventory,
                'has_nursery_indicators': 'nursery_indicators.json' in inventory,
                'message': (f'已加载 {len(inventory)} 份内置及本机资料。你导入的资料仅保存在当前电脑。'
                            if inventory else '暂无可用资料。可以导入有权使用的参考资料；没有依据时引用栏留空。')}


def _string(value, label, limit=200000, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise ValueError(f'{label}须为有效文字。')


def _list(value, label, limit=5000):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f'{label}须为列表且不超过{limit}项。')


def _ids(items, label):
    ids = [item.get('id') if isinstance(item, dict) else None for item in items]
    if any(not isinstance(i, (str, int)) or isinstance(i, bool) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError(f'{label}的编号格式错误或重复。')


def _validate_indicators(data, nursery=False):
    _list(data, '指标', 2000)
    _ids(data, '指标')
    for item in data:
        if type(item['id']) is not int or item['id'] <= 0:
            raise ValueError('指标须使用真实的正整数编号。')
        for key in ('name', 'domain', 'subdomain', 'goal'):
            _string(item.get(key), '指标' + key, empty=(key == 'goal'))
        levels = item.get('levels')
        if not isinstance(levels, dict) or any(k not in ({'128'} if nursery else {'1', '3', '5'}) for k in levels):
            raise ValueError('指标的年龄档不正确；托班仅支持24—36个月档128。')
        for lines in levels.values():
            _list(lines, '表现原句', 300)
            for line in lines:
                _string(line, '表现原句', 5000)
        if nursery and (item.get('system_id', item.get('systemId')) != 35 or '128' not in levels):
            raise ValueError('托班指标必须属于托育体系35及月龄档128。')
        if nursery:
            for key in ('medal_id', 'target_id'):
                if type(item.get(key)) is not int or item[key] <= 0:
                    raise ValueError('托班指标缺少真实的能力项映射。')
            for key in ('gradeVos', 'gradeDescVos'):
                _list(item.get(key), '托班' + key, 300)
            if not item['gradeVos'] or any(not isinstance(g, dict) or g.get('id') != 128 for g in item['gradeVos']):
                raise ValueError('托班gradeVos仅允许24—36个月档128。')
            for row in item['gradeDescVos']:
                if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                        or row.get('gradeId') != 128 or row.get('medalId') != item['medal_id']):
                    raise ValueError('托班具体表现映射损坏或混入其他月龄。')
                _string(row.get('gradeDesc'), '托班具体表现', 5000)


def _validate_chunks(data, expected):
    if not isinstance(data, dict):
        raise ValueError('知识依据必须为JSON对象。')
    _list(data.get('chunks'), '知识条目')
    _ids(data['chunks'], '知识条目')
    for item in data['chunks']:
        for key in ('id', 'source', 'heading', 'domain', 'text', 'age_band', 'source_kind', 'location'):
            _string(item.get(key), '知识条目' + key)
        if item['source_kind'] not in expected:
            raise ValueError('知识条目的来源类型与文件不一致。')
        item.setdefault('source_url', '')


def _validate_files(directory):
    total = 0
    json_files = {}
    inventory = [p for p in directory.rglob('*') if p.is_file()]
    if len(inventory) > MAX_FILES:
        raise ValueError('资料包文件数量过多。')
    for p in inventory:
        name = p.relative_to(directory).as_posix()
        if not allowed(name) or p.is_symlink():
            raise ValueError('资料包包含不支持的文件。')
        size = p.stat().st_size
        total += size
        if size > MAX_FILE or total > MAX_TOTAL:
            raise ValueError('资料包展开后的体积超过限制。')
        try:
            text = p.read_text('utf-8-sig')
            if '\0' in text:
                raise ValueError('资料不可含二进制内容。')
            data = json.loads(text) if p.suffix == '.json' else None
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('资料须采用UTF-8文字和有效JSON格式。') from exc
        if name in ('indicators.json', 'nursery_indicators.json'):
            _validate_indicators(data, name.startswith('nursery'))
        elif name == 'ref/official-guide.json':
            _validate_chunks(data, {'official_guide'})
        elif name == 'ref/provided-observation-guide.json':
            _validate_chunks(data, {'provided_reference'})
        elif name == 'ref/nursery-development.json':
            _validate_chunks(data, {'nursery_development', 'nursery_indicator'})
        elif name == 'medal_map.txt':
            for line in text.splitlines():
                if not line.strip():
                    continue
                cells = line.split('|')
                if len(cells) != 5 or not cells[0].isdigit() or not cells[2].isdigit():
                    raise ValueError('medal_map.txt每行须为5列映射。')
                if not cells[1] or any(not re.fullmatch(r'\d+:\d+', g) for g in cells[4].split(',')):
                    raise ValueError('medal_map.txt指标名称或表现映射错误。')
        if data is not None:
            # Keep source bytes: reference indexes may contain hashes of JSON files.
            json_files[name] = data
    if 'ref/nursery-development.json' in json_files:
        from evidence import _load_nursery
        _load_nursery(directory / 'ref/nursery-development.json')
    if 'ref/core-experience/core_indicators.json' in json_files:
        from core_indicators import _load_library
        _load_library(directory / 'ref/core-experience')


def invalidate():
    for module, names in {'knowledge': ('load_indicators', 'load_medal_map'),
                          'evidence': ('catalog', '_nursery_catalog'),
                          'core_indicators': ('_library',)}.items():
        loaded = sys.modules.get(module)
        if loaded:
            for name in names:
                fn = getattr(loaded, name, None)
                if hasattr(fn, 'cache_clear'):
                    fn.cache_clear()


@contextmanager
def _staging():
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='import-', dir=ROOT) as temporary:
        staged = Path(temporary)
        if staged.resolve().parent != ROOT.resolve():
            raise ValueError('资料暂存目录不正确。')
        active = _active()
        if active:
            shutil.copytree(active, staged, dirs_exist_ok=True)
        yield staged


def _activate(staged):
    _validate_files(staged)
    version = uuid4().hex
    ready = ROOT / version
    shutil.copytree(staged, ready)
    pointer = ROOT / 'active.json.tmp'
    pointer.write_text(json.dumps({'version': version}), encoding='utf-8')
    os.replace(pointer, ROOT / 'active.json')
    invalidate()
    return status()


def import_zip(blob):
    if not isinstance(blob, bytes) or not blob or len(blob) > MAX_ARCHIVE:
        raise ValueError('请选择不超过16MB的资料ZIP。')
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive, LOCK, _staging() as staged:
            entries = archive.infolist()
            if len(entries) > MAX_FILES * 2:
                raise ValueError('资料包文件数量过多。')
            seen = set()
            total = 0
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                mode = (entry.external_attr >> 16) & 0xFFFF
                if ('\\' in name or ':' in name or path.is_absolute() or
                        any(part in ('.', '..') for part in name.rstrip('/').split('/')) or
                        stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                    raise ValueError('资料包包含不安全的路径或链接。')
                if entry.is_dir():
                    continue
                if not allowed(name) or name.casefold() in seen or entry.flag_bits & 1:
                    raise ValueError('资料包有重复、加密或不支持的文件；仅接受约定的数据文件。')
                seen.add(name.casefold())
                total += entry.file_size
                if (entry.file_size > MAX_FILE or total > MAX_TOTAL or
                        entry.file_size > max(1024 * 1024, entry.compress_size * 200)):
                    raise ValueError('资料包展开体积或压缩比超过限制。')
                with archive.open(entry) as stream:
                    content = stream.read(MAX_FILE + 1)
                if len(content) != entry.file_size or len(content) > MAX_FILE:
                    raise ValueError('资料包文件长度不符。')
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            if not seen:
                raise ValueError('资料包中没有支持的资料文件。')
            return _activate(staged)
    except (zipfile.BadZipFile, RuntimeError, KeyError, TypeError, IndexError,
            UnicodeError, RecursionError, NotImplementedError) as exc:
        raise ValueError('资料包损坏或数据结构不完整，请按资料包说明检查。') from exc
    except FileNotFoundError as exc:
        raise ValueError('资料包缺少索引引用的原表文件，请连同原表一起导入。') from exc


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='导入有权使用的本地资料ZIP，或查看资料状态。')
    parser.add_argument('archive', nargs='?')
    args = parser.parse_args()
    try:
        if args.archive:
            with Path(args.archive).open('rb') as stream:
                result = import_zip(stream.read(MAX_ARCHIVE + 1))
        else:
            result = status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
