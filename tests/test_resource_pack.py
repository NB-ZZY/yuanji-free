"""Synthetic imports plus generic bundled catalogs; no accounts, people or network."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

_bootstrap = tempfile.TemporaryDirectory(prefix='kidobs-resource-tests-')
os.environ['KIDOBS_DATA_DIR'] = _bootstrap.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import resource_pack as resources
import knowledge
import evidence
import core_indicators

BUILTIN_RESOURCES = Path(__file__).resolve().parents[1] / 'app/data'


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        for name, body in entries:
            z.writestr(name, body if isinstance(body, (str, bytes)) else json.dumps(body, ensure_ascii=False))
    return output.getvalue()


def indicators(name='合成测试项'):
    return [{'id': 1, 'name': name, 'domain': '合成领域', 'subdomain': '合成子项',
             'goal': '', 'levels': {'1': ['合成测试行为']}}]


class ResourcePackTests(unittest.TestCase):
    def setUp(self):
        self.saved_root, self.saved_builtin = resources.ROOT, resources.BUILTIN_DATA_DIR
        self.temp = tempfile.TemporaryDirectory(dir=_bootstrap.name)
        self.root = Path(self.temp.name)
        resources.ROOT = self.root / 'private'
        resources.BUILTIN_DATA_DIR = self.root / 'empty-builtin'
        resources.invalidate()

    def tearDown(self):
        resources.ROOT, resources.BUILTIN_DATA_DIR = self.saved_root, self.saved_builtin
        resources.invalidate()
        self.temp.cleanup()

    def test_empty_libraries_are_usable(self):
        self.assertEqual(knowledge.load_indicators(), [])
        self.assertEqual(knowledge.load_indicators(128), [])
        self.assertEqual(core_indicators.candidates('', '中班', ''), [])
        self.assertEqual(evidence.catalog(), [])
        self.assertEqual(evidence.build_evidence(3, story='测试'), [])
        self.assertEqual(evidence.build_evidence(128, story='测试'), [])
        self.assertEqual(evidence.browse()['total'], 0)
        self.assertEqual(resources.status()['files'], [])

    def test_distributed_reference_library_loads_with_valid_source_coordinates(self):
        resources.BUILTIN_DATA_DIR = BUILTIN_RESOURCES
        resources.invalidate()
        self.assertEqual(len(core_indicators.items()), 208)
        self.assertEqual(len(knowledge.load_indicators(128)), 13)
        self.assertEqual(sum(len(item['levels']['128']) for item in knowledge.load_indicators(128)), 87)
        self.assertEqual(len(knowledge.load_medal_map()), 36)
        ordinary = knowledge.load_indicators()
        self.assertEqual(len(ordinary), 36)
        self.assertEqual(sum(len(lines) for item in ordinary for lines in item['levels'].values()), 341)
        self.assertEqual(sorted(item['id'] for item in ordinary), list(range(1, 37)))
        self.assertTrue(all(item['goal'] == '' for item in ordinary))
        self.assertGreater(len(evidence.catalog()), 208)
        nursery = evidence._nursery_catalog()
        self.assertEqual(len(nursery), 87)
        self.assertEqual({item['source_kind'] for item in nursery}, {'nursery_indicator'})
        self.assertEqual({item['behavior_level'] for item in nursery}, {128})
        self.assertNotIn('nursery_development', {item['source_kind'] for item in evidence.catalog()})
        source = BUILTIN_RESOURCES / 'nursery_indicators.json'
        raw = json.loads(source.read_text('utf-8'))
        expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        for item in nursery:
            coordinates = item['source_coordinates']
            original = raw[coordinates['item_index']]['gradeDescVos'][coordinates['grade_desc_index']]
            self.assertEqual(item['text'], original['gradeDesc'])
            self.assertEqual(item['source_sha256'], expected_hash)
        for path in resources.files().values():
            self.assertTrue(path.is_file())

    def test_removed_monthly_resources_are_rejected(self):
        from resources_api import router
        self.assertFalse(any('monthly' in route.path for route in router.routes))
        self.assertNotIn('monthly_packs', resources.status())
        for name in ('monthly/example.json', 'monthly/SKILL.md'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                resources.import_zip(archive([(name, '{}')]))

    def test_full_approved_pack_import_preserves_source_bytes_and_loaders(self):
        entries = [(p.relative_to(BUILTIN_RESOURCES).as_posix(), p.read_bytes())
                   for p in BUILTIN_RESOURCES.rglob('*')
                   if p.is_file() and resources.allowed(p.relative_to(BUILTIN_RESOURCES).as_posix())]
        resources.import_zip(archive(entries))
        for name, original in entries:
            self.assertEqual(resources.path_for(name).read_bytes(), original)
        self.assertEqual(len(knowledge.load_indicators()), 36)
        self.assertEqual(len(core_indicators.items()), 208)
        self.assertEqual(len(evidence._nursery_catalog()), 87)

    def test_partial_import_preserves_existing_resources(self):
        resources.import_zip(archive([('ref/core_健康.md', '# 合成测试\n这是测试文字。')]))
        resources.import_zip(archive([('indicators.json', indicators())]))
        self.assertIn('ref/core_健康.md', resources.files())
        self.assertEqual(evidence.browse()['total'], 1)
        self.assertEqual(len(knowledge.load_indicators()), 1)

    def test_import_clears_cached_indicators(self):
        self.assertEqual(knowledge.load_indicators(), [])
        resources.import_zip(archive([('indicators.json', indicators('合成甲'))]))
        self.assertEqual(knowledge.load_indicators()[0]['name'], '合成甲')
        resources.import_zip(archive([('indicators.json', indicators('合成乙'))]))
        self.assertEqual(knowledge.load_indicators()[0]['name'], '合成乙')

    def test_invalid_pack_leaves_active_snapshot_unchanged(self):
        resources.import_zip(archive([('indicators.json', indicators())]))
        before = (resources.ROOT / 'active.json').read_bytes()
        invalid = indicators()
        invalid[0]['levels'] = {'2': ['不允许的档位原句']}
        with self.assertRaises(ValueError):
            resources.import_zip(archive([('indicators.json', invalid)]))
        self.assertEqual((resources.ROOT / 'active.json').read_bytes(), before)
        self.assertEqual(knowledge.load_indicators()[0]['name'], '合成测试项')

    def test_duplicate_indicator_ids_rejected(self):
        with self.assertRaises(ValueError):
            resources.import_zip(archive([('indicators.json', indicators() + indicators())]))
        self.assertFalse((resources.ROOT / 'active.json').exists())

    def test_path_traversal_and_code_files_rejected(self):
        for name in ('../indicators.json', '/indicators.json', 'C:/indicators.json',
                     'monthly\\test.json', 'monthly/../indicators.json',
                     'app/config.py', 'writing/SKILL.md', 'private-key.pem'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                resources.import_zip(archive([(name, '{}')]))
        self.assertFalse((self.root / 'indicators.json').exists())

    def test_symlinks_rejected(self):
        entry = zipfile.ZipInfo('indicators.json')
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            resources.import_zip(archive([(entry, 'outside.json')]))

    def test_repeated_paths_rejected(self):
        with self.assertRaises(ValueError):
            resources.import_zip(archive([('indicators.json', indicators()), ('indicators.json', indicators())]))

    def test_archive_and_expansion_limits(self):
        with self.assertRaises(ValueError):
            resources.import_zip(b'x' * (resources.MAX_ARCHIVE + 1))
        with self.assertRaises(ValueError):
            resources.import_zip(archive([('ref/core_健康.md', 'x' * (resources.MAX_FILE + 1))]))
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('ref/core_健康.md', 'x' * (2 * 1024 * 1024))
        with self.assertRaises(ValueError):
            resources.import_zip(out.getvalue())

    def test_reference_cache_refreshes_and_sources_are_kept(self):
        self.assertEqual(evidence.catalog(), [])
        item = {'id': 'synthetic-reference', 'source': '合成测试来源', 'heading': '测试条目',
                'domain': '健康领域', 'text': '这是测试中编写的文字，不是教育原文。',
                'age_band': '4-5岁', 'source_kind': 'official_guide', 'location': '合成位置'}
        resources.import_zip(archive([('ref/official-guide.json', {'chunks': [item]})]))
        self.assertEqual(evidence.by_id('synthetic-reference')['text'], item['text'])
        self.assertEqual(evidence.browse()['total'], 1)

    def test_core_coordinates_and_hash_are_validated_before_activation(self):
        original = '合成标题｜合成正文\n'
        item = {'id': 'synthetic-core', 'domain': '健康', 'title': '合成标题', 'text': '合成正文',
                'source': '自编测试', 'source_file': '健康.txt', 'source_lines': [1],
                'source_cells': [
                    {'role': 'title', 'line': 1, 'column': 1, 'start': 0, 'end': 4, 'original': '合成标题'},
                    {'role': 'text', 'line': 1, 'column': 2, 'start': 0, 'end': 4, 'original': '合成正文'}]}
        index = {'source_sha256': {'健康.txt': hashlib.sha256(original.encode()).hexdigest()}, 'items': [item]}
        resources.import_zip(archive([('ref/core-experience/core_indicators.json', index),
                                      ('ref/core-experience/core/健康.txt', original)]))
        self.assertEqual(core_indicators.items()[0]['text'], '合成正文')
        before = (resources.ROOT / 'active.json').read_bytes()
        with self.assertRaises(ValueError):
            resources.import_zip(archive([('ref/core-experience/core/健康.txt', '改动正文')]))
        self.assertEqual((resources.ROOT / 'active.json').read_bytes(), before)
        self.assertEqual(core_indicators.items()[0]['text'], '合成正文')

    def test_wrong_nursery_age_cannot_replace_current_catalog(self):
        payload = indicators()[0]
        payload['levels'] = {'127': ['合成测试行为']}
        payload['system_id'] = 35
        with self.assertRaises(ValueError):
            resources.import_zip(archive([('nursery_indicators.json', [payload])]))
        self.assertEqual(knowledge.load_indicators(128), [])

    def test_api_import_closes_upload_and_returns_clear_error(self):
        from fastapi import HTTPException, UploadFile
        from resources_api import import_resources
        uploaded = UploadFile(file=io.BytesIO(b'not-a-zip'), filename='test.zip')
        with self.assertRaises(HTTPException) as error:
            import_resources(uploaded)
        self.assertEqual(error.exception.status_code, 400)
        self.assertTrue(uploaded.file.closed)

    def test_every_ordinary_id_roundtrips_to_site_payload_without_network(self):
        import pipeline
        import ysk_client
        resources.BUILTIN_DATA_DIR = BUILTIN_RESOURCES
        resources.invalidate()
        mapping = knowledge.load_medal_map()
        calls = []

        def capture(path, body):
            calls.append((path, body))
            return [{'id': 1}]

        # Avoid even constructing a logged-in client; this is payload construction only.
        client = ysk_client.YskClient.__new__(ysk_client.YskClient)
        client._post = capture
        with patch.object(ysk_client, 'MOCK', False), patch('requests.Session.request', side_effect=AssertionError('network forbidden')):
            for item in knowledge.load_indicators():
                self.assertEqual(item['medal_id'], mapping[item['name']]['medal_id'])
                self.assertEqual(knowledge.indicator_by_id(item['id'])['name'], item['name'])
                self.assertEqual(item['systemId'], 9)
                for grade in (1, 2, 3, 4, 5):
                    base_level = grade if grade in (1, 3, 5) else 3
                    raw = {'indicator_id': item['id']}
                    if grade in (2, 4):
                        raw.update(grade=grade, behavior_lines=[], behavior_line='')
                    else:
                        raw['behavior_lines'] = [{'level': grade, 'grade': grade,
                                                 'line': item['levels'][str(grade)][0]}]
                    clean = pipeline._clean_picks([raw], base_level)[0]
                    self.assertEqual(clean['indicator_id'], item['id'])
                    self.assertEqual(clean['medal_id'], mapping[item['name']]['medal_id'])
                    lines = [{'grade_id': knowledge.GRADE_ID[line['level']], 'desc': line['line']}
                             for line in clean['behavior_lines']]
                    result = client.add_student_medal(1, 2, 3, clean['medal_id'], grade,
                                                      knowledge.GRADE_ID[grade], lines, '2026-01-01')
                    self.assertEqual(result, 1)
                    _, payload = calls[-1]
                    self.assertEqual(payload['medalId'], mapping[item['name']]['medal_id'])
                    child = payload['studentMedalPerformanceStatusVos'][0]
                    self.assertEqual(child['grade'], grade)
                    if grade in (2, 4):
                        self.assertEqual(child['dataVos'], [])
                    else:
                        self.assertEqual(child['dataVos'], [{'gradeId': knowledge.GRADE_ID[grade],
                                                            'gradeDesc': item['levels'][str(grade)][0]}])
        self.assertEqual(len(calls), 180)


if __name__ == '__main__':
    unittest.main()
