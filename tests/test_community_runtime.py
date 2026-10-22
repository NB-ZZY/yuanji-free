"""Offline community startup, settings privacy, and account isolation checks.

Run: python -m unittest discover -s tests -p test_community_runtime.py
Requires httpx for Starlette TestClient. Never uses the operator's APPDATA.
"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

_scratch = tempfile.TemporaryDirectory(prefix='kidobs-community-test-')
os.environ['KIDOBS_DATA_DIR'] = _scratch.name
os.environ['APPDATA'] = _scratch.name
os.environ['LOCALAPPDATA'] = _scratch.name
for _key in list(os.environ):
    if _key.startswith(('KIDOBS_AI_', 'KIDOBS_TC_')) or _key in ('KIDOBS_FACE_ENABLED', 'KIDOBS_MOCK', 'KIDOBS_ZHIPU', 'KIDOBS_DEEPSEEK'):
        os.environ.pop(_key, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

import config
import db
import face_client
import pipeline
import server
import session_state
from fastapi.testclient import TestClient


class CommunityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.data_environment = patch.dict(os.environ, {'KIDOBS_DATA_DIR': str(config.DATA_DIR),
                                                        'APPDATA': str(config.DATA_DIR),
                                                        'LOCALAPPDATA': str(config.DATA_DIR)})
        self.data_environment.start()
        self.addCleanup(self.data_environment.stop)
        config.SETTINGS_PATH.unlink(missing_ok=True)
        db.set_setting('current_uid', None)
        db.set_setting('ysk_cookies', None)
        db.set_setting('current_class', None)
        server._ysk = server.YskClient({})
        server._login_attempt = None
        session_state.generation = 0
        self.assertFalse(session_state.busy())
        # Windows asyncio creates a loopback socket pair; allow only that local transport.
        original_connect = socket.socket.connect
        def local_only(sock, address):
            if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
                raise AssertionError('External network forbidden in offline test')
            return original_connect(sock, address)
        self.network = patch.object(socket.socket, 'connect', local_only)
        self.network.start()
        self.addCleanup(self.network.stop)
        self.http_network = patch('requests.sessions.Session.request', side_effect=AssertionError('Application network forbidden'))
        self.http_network.start()
        self.addCleanup(self.http_network.stop)
        self.api = TestClient(server.app)

    def test_fresh_start_has_settings_without_activation_or_network(self):
        response = self.api.get('/api/status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['edition'], 'community')
        self.assertFalse(response.json()['logged_in'])
        self.assertFalse(response.json()['ai_configured'])
        self.assertNotIn('license', response.json())
        self.assertEqual(self.api.get('/api/community/settings').status_code, 200)
        self.assertEqual(self.api.get('/').status_code, 200)

    def test_settings_never_return_secrets_and_empty_preserves(self):
        response = self.api.post('/api/community/settings', json={'api_key': 'synthetic-only-test-key', 'model': 'test-model'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['api_key_set'])
        self.assertNotIn('synthetic-only-test-key', response.text)
        self.assertNotIn('api_key', response.json())
        self.api.post('/api/community/settings', json={'api_key': '', 'thinking': 'off'})
        self.assertEqual(config.settings()['api_key'], 'synthetic-only-test-key')
        response = self.api.post('/api/community/settings', json={'clear_api_key': True})
        self.assertFalse(response.json()['api_key_set'])

    def test_explicit_clear_tencent(self):
        self.api.post('/api/community/settings', json={'tencent_id': 'synthetic-id', 'tencent_key': 'synthetic-secret', 'face_enabled': True})
        self.assertTrue(config.face_enabled())
        response = self.api.post('/api/community/settings', json={'clear_tencent': True})
        self.assertFalse(response.json()['tencent_id_set'])
        self.assertFalse(response.json()['face_enabled'])
        self.assertNotIn('synthetic-secret', response.text)

    def test_enabling_face_recognition_requires_both_effective_credentials(self):
        response = self.api.post('/api/community/settings', json={'face_enabled': True})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(config.SETTINGS_PATH.exists())
        with patch.dict(os.environ, {'KIDOBS_FACE_ENABLED': '1'}):
            self.assertFalse(self.api.get('/api/community/settings').json()['face_enabled'])
        with patch.dict(os.environ, {'KIDOBS_TC_ID': 'synthetic-env-id', 'KIDOBS_TC_KEY': 'synthetic-env-key'}):
            response = self.api.post('/api/community/settings', json={'face_enabled': True})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['face_enabled'])

    def test_environment_overrides_are_visible_but_keys_are_not(self):
        with patch.dict(os.environ, {'KIDOBS_AI_KEY': 'synthetic-environment-key', 'KIDOBS_AI_MODEL': 'env-model'}):
            response = self.api.get('/api/community/settings')
            self.assertEqual(response.json()['model'], 'env-model')
            self.assertIn('api_key', response.json()['env_overrides'])
            self.assertNotIn('synthetic-environment-key', response.text)
            self.assertEqual(config.ai_conf()['key'], 'synthetic-environment-key')

    def test_invalid_settings_do_not_replace_previous_configuration(self):
        self.api.post('/api/community/settings', json={'api_key': 'synthetic-key'})
        before = config.SETTINGS_PATH.read_bytes()
        for body in ({'base_url': 'https://example.test/v1?key=bad'}, {'base_url': 'http://example.test/v1'},
                     {'base_url': 'https://name:pass@example.test/v1'}, {'face_enabled': 'false'},
                     {'provider': 'unsupported'}, {'clear_api_key': 'true'}, {'unknown': 'anything'}):
            self.assertEqual(self.api.post('/api/community/settings', json=body).status_code, 400)
            self.assertEqual(config.SETTINGS_PATH.read_bytes(), before)

    def test_third_party_web_page_cannot_change_local_keys(self):
        response = self.api.post('/api/community/settings', json={'api_key': 'synthetic'}, headers={'Origin': 'https://unrelated.example'})
        self.assertEqual(response.status_code, 403)
        response = self.api.get('/api/community/settings', headers={'Host': 'unrelated.example'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(config.SETTINGS_PATH.exists())

    def test_private_endpoints_are_absent(self):
        registered = {getattr(route, 'path', '') for route in server.app.routes}
        for prefix in ('/api/license', '/api/update', '/api/client-policy', '/api/dev/'):
            self.assertFalse(any(path.startswith(prefix) for path in registered))
            self.assertEqual(self.api.get(prefix).status_code, 404)

    def test_monthly_feature_is_absent(self):
        registered = {getattr(route, 'path', '') for route in server.app.routes}
        self.assertFalse(any(path.startswith('/api/monthly') for path in registered))
        self.assertFalse((Path(__file__).resolve().parents[1] / 'app' / 'monthly.py').exists())
        for path in ('/api/monthly/catalog', '/api/monthly/1/2026/10',
                     '/api/monthly/1/2026/10/draft', '/api/monthly/1/2026/10/generate',
                     '/api/monthly/1/2026/10/website/1'):
            self.assertEqual(self.api.get(path).status_code, 404)
            self.assertEqual(self.api.post(path, json={}).status_code, 404)

    def test_missing_ai_key_fails_before_network(self):
        import ai_writer
        with self.assertRaisesRegex(ValueError, 'API'):
            ai_writer._chat_full([{'role': 'user', 'content': 'synthetic request'}])

    def test_face_connection_test_does_not_require_platform_login(self):
        def synthetic_connection():
            self.assertTrue(session_state.busy())
            response = self.api.post('/api/community/settings', json={'model': 'changed-during-test'})
            self.assertEqual(response.status_code, 409)
            return {'ok': True, 'groups': 0}
        with patch.object(face_client, 'test_connection', side_effect=synthetic_connection) as connect:
            response = self.api.post('/api/face/test', json={})
        self.assertEqual(response.status_code, 200)
        connect.assert_called_once_with()
        self.assertFalse(session_state.busy())
        self.assertIsNone(db.get_setting('current_uid'))

    def test_setup_allowlist_does_not_open_whole_route_prefixes(self):
        for path in ('/api/ai/private', '/api/resources-private', '/api/login/private', '/api/logout-private',
                     '/api/classes', '/api/batches', '/api/job/anything'):
            self.assertEqual(self.api.get(path).status_code, 401)
        self.assertEqual(self.api.get('/api/resources').status_code, 200)

    def test_resource_import_is_exclusive_and_blocks_settings_changes(self):
        import resource_pack
        def synthetic_import(blob):
            self.assertEqual(blob, b'synthetic local upload')
            self.assertTrue(session_state.busy())
            for path, body in (('/api/community/settings', {'api_key': 'synthetic'}),
                               ('/api/ai/mode', {'mode': 'deep'}),
                               ('/api/ai/effort', {'effort': 'low'})):
                self.assertEqual(self.api.post(path, json=body).status_code, 409)
            self.assertEqual(self.api.get('/api/classes').status_code, 409)
            self.assertEqual(self.api.post('/api/face/test', json={}).status_code, 409)
            return {'ok': True}
        with patch.object(resource_pack, 'import_zip', side_effect=synthetic_import):
            response = self.api.post('/api/resources/import', files={'file': ('synthetic.zip', b'synthetic local upload')})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(session_state.busy())
        self.assertFalse(server._resource_importing)
        self.assertFalse(config.SETTINGS_PATH.exists())
        with session_state.activity(), patch.object(resource_pack, 'import_zip') as importer:
            response = self.api.post('/api/resources/import', files={'file': ('synthetic.zip', b'not inspected')})
            self.assertEqual(response.status_code, 409)
            importer.assert_not_called()

    def test_failed_settings_replace_keeps_old_secret_and_removes_temporary(self):
        config.save_settings({'api_key': 'synthetic-first'})
        before = config.SETTINGS_PATH.read_bytes()
        with patch.object(config.os, 'replace', side_effect=OSError('synthetic replacement failure')):
            with self.assertRaises(OSError):
                config.save_settings({'api_key': 'synthetic-next'})
        self.assertEqual(config.SETTINGS_PATH.read_bytes(), before)
        self.assertEqual(list(config.DATA_DIR.glob('.settings-*.tmp')), [])

    def test_data_and_exports_are_isolated(self):
        import plat
        self.assertEqual(config.APP_NAME, 'kidobs-community')
        self.assertEqual(config.DATA_DIR, Path(_scratch.name))
        self.assertTrue(plat.export_root().is_relative_to(Path(_scratch.name)))

    def test_no_face_service_leaves_manual_selection_without_fake_identity(self):
        iid = db.exec("INSERT INTO items(path,face_status) VALUES(?,?)", ('nonexistent-synthetic-photo.jpg', 'pending'))
        pipeline._recognize({'id': iid, 'path': 'nonexistent-synthetic-photo.jpg'}, 987, {})
        item = db.one('SELECT * FROM items WHERE id=?', (iid,))
        self.assertEqual(item['face_status'], 'manual')
        self.assertFalse(item['student_id'])
        self.assertFalse(item['ai_error'])
        with self.assertRaisesRegex(face_client.FaceError, '未启用'):
            face_client.test_connection()

    def _login(self, uid):
        class FakeClient:
            def __init__(self, cookies):
                self.uid = uid
                self.user = {'id': uid, 'userName': 'Synthetic teacher'}
            def check_login(self):
                return self.user
        attempt = self.api.post('/api/login/begin', json={}).json()['attempt']
        with patch.object(server, 'YskClient', FakeClient):
            response = self.api.post('/api/login/cookies', json={'cookies': {'synthetic': 'test'}, 'attempt': attempt})
        self.assertEqual(response.status_code, 200)
        return response.json()['session_generation']

    def test_account_switch_clears_current_class_but_preserves_records(self):
        previous = self._login(111)
        db.upsert_class(222, 1, 'Synthetic class', 'SENIOR', 5)
        db.set_setting('current_class', 222)
        current = self._login(333)
        self.assertGreater(current, previous)
        self.assertIsNone(db.get_setting('current_class'))
        self.assertIsNotNone(db.one('SELECT * FROM classes WHERE class_id=?', (222,)))
        response = self.api.post('/api/class/222/select', headers={'X-Kidobs-Session': str(previous)}, json={})
        self.assertEqual(response.status_code, 409)

    def test_logout_is_blocked_while_background_activity_is_active(self):
        self._login(111)
        with session_state.activity():
            self.assertEqual(self.api.post('/api/logout', json={}).status_code, 409)
        self.assertEqual(db.get_setting('current_uid'), 111)
        self.assertEqual(self.api.post('/api/logout', json={}).status_code, 200)
        self.assertIsNone(db.get_setting('current_uid'))

    def _scoped_fixture(self):
        import access_guard
        from PIL import Image
        access_guard._cache = None
        server._jobs.clear()
        self.addCleanup(server._jobs.clear)
        for cid in (8101, 8102, 8103):
            db.upsert_class(cid, 8000, 'Synthetic classroom', 'SENIOR', 5)
        records = []
        for index, cid in enumerate((8101, 8102)):
            sid = 8201 + index
            db.upsert_student({'id': sid, 'classId': cid, 'name': 'Synthetic child'})
            path = config.PHOTO_DIR / f'synthetic-access-{index}.png'
            Image.new('RGB', (12, 12), ('red', 'blue')[index]).save(path)
            bid = db.exec('INSERT INTO batches(class_id,note,mode) VALUES(?,?,?)', (cid, 'Synthetic note', 'simple'))
            iid = db.exec('INSERT INTO items(batch_id,path,text,student_id,student_name) VALUES(?,?,?,?,?)',
                          (bid, str(path), 'Synthetic observation', sid, 'Synthetic child'))
            pid = db.exec('INSERT INTO photos(student_id,path) VALUES(?,?)', (sid, str(path)))
            candidate = db.exec('INSERT INTO candidates(student_id,path) VALUES(?,?)', (sid, str(path)))
            trash = db.exec('INSERT INTO local_trash(class_id,payload,deleted_at) VALUES(?,?,?)',
                            (cid, json.dumps({'batch': {'note': 'Synthetic'}, 'items': []}), 0))
            records.append({'cid': cid, 'sid': sid, 'bid': bid, 'iid': iid, 'pid': pid,
                            'candidate': candidate, 'trash': trash, 'path': path})
        self._set_scoped_account(9101, (8101, 8103))
        db.set_setting('current_class', 8103)  # The earlier authorized class remains readable.
        return records

    def _set_scoped_account(self, uid, class_ids):
        class ScopedClient:
            def my_classes(self):
                return [{'classId': cid, 'name': 'Synthetic classroom', 'type': 'SENIOR'} for cid in class_ids]
            def check_login(self):
                return {'id': uid, 'userName': 'Synthetic teacher'}
        server._ysk = ScopedClient()
        db.set_setting('current_uid', uid)
        db.set_setting('ysk_cookies', {'synthetic': 'test'})

    def test_cached_records_and_photos_are_scoped_to_authorized_classes(self):
        own, other = self._scoped_fixture()
        for record, expected in ((own, 200), (other, 403)):
            urls = (f"/api/batch/{record['bid']}", f"/api/item/{record['iid']}/photos",
                    f"/api/item/{record['iid']}/image", f"/api/photo/{record['pid']}",
                    f"/api/student/{record['sid']}/cover", f"/api/candidate/{record['candidate']}",
                    f"/api/batches?class_id={record['cid']}", f"/api/trash?class_id={record['cid']}")
            for url in urls:
                self.assertEqual(self.api.get(url).status_code, expected, url)
            self.assertEqual(self.api.get('/api/file', params={'path': str(record['path'])}).status_code, expected)
        before = db.one('SELECT * FROM items WHERE id=?', (other['iid'],))
        for kind, identity in (('batch', other['bid']), ('item', other['iid']), ('photo', other['pid']),
                               ('candidate', other['candidate'])):
            self.assertEqual(self.api.delete(f'/api/{kind}/{identity}').status_code, 403)
        self.assertEqual(self.api.post(f"/api/trash/{other['trash']}/restore").status_code, 403)
        self.assertEqual(self.api.patch(f"/api/item/{other['iid']}", json={'text': 'must not change'}).status_code, 403)
        self.assertEqual(db.one('SELECT * FROM items WHERE id=?', (other['iid'],)), before)
        self._set_scoped_account(9102, (8102,))
        self.assertEqual(self.api.get(f"/api/batch/{other['bid']}").status_code, 200)
        self.assertEqual(self.api.get(f"/api/batch/{own['bid']}").status_code, 403)

    def test_secondary_ids_cannot_modify_another_class(self):
        own, other = self._scoped_fixture()
        before = db.one('SELECT * FROM items WHERE id=?', (own['iid'],))
        for suffix, body in (('students', {'student_ids': [other['sid']]}),
                             ('photo/link', {'targets': [other['iid']]}),
                             ('face_assign', {'student_id': other['sid'], 'face_idx': 0})):
            self.assertEqual(self.api.post(f"/api/item/{own['iid']}/{suffix}", json=body).status_code, 403)
        self.assertEqual(self.api.patch(f"/api/item/{own['iid']}", json={'text': 'must not change', 'student_id': other['sid']}).status_code, 403)
        self.assertEqual(self.api.post(f"/api/batch/{own['bid']}/bulk", json={'action': 'delete', 'item_ids': [other['iid']]}).status_code, 403)
        for action, value in (('students_add', [other['sid']]), ('copy_indicators', other['iid'])):
            self.assertEqual(self.api.post(f"/api/batch/{own['bid']}/bulk", json={
                'action': action, 'item_ids': [own['iid']], 'value': value}).status_code, 403)
        self.assertEqual(self.api.post(f"/api/class/{own['cid']}/nicknames/bulk", json={'items': [
            {'student_id': own['sid'], 'nickname': 'must not change'},
            {'student_id': other['sid'], 'nickname': 'must not change'}]}).status_code, 403)
        self.assertEqual(db.one('SELECT * FROM items WHERE id=?', (own['iid'],)), before)
        self.assertIsNone(db.one('SELECT nickname FROM students WHERE student_id=?', (own['sid'],))['nickname'])
        with patch.object(pipeline, 'create_batch') as create:
            response = self.api.post('/api/batch', data={'class_id': other['cid']},
                                     files={'files': ('synthetic.png', own['path'].read_bytes(), 'image/png')})
            self.assertEqual(response.status_code, 403)
            create.assert_not_called()
        response = self.api.patch(f"/api/item/{own['iid']}", json={'text': 'Own update allowed'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['text'], 'Own update allowed')

    def test_preview_cannot_read_keys_and_pending_jobs_exports_are_account_scoped(self):
        import plat
        import roster
        own, other = self._scoped_fixture()
        config.save_settings({'api_key': 'synthetic-local-private-value'})
        response = self.api.get('/api/file', params={'path': str(config.SETTINGS_PATH)})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('synthetic-local-private-value', response.text)
        pending = roster.pending_root() / 'synthetic.jpg'
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_bytes(b'synthetic pending')
        first_export = plat.export_root()
        server._jobs['synthetic-task'] = {'status': 'done', 'result': 'private', '_owner_uid': '9101'}
        self.assertEqual(self.api.get('/api/job/synthetic-task').status_code, 200)
        self.assertEqual(len(self.api.get('/api/pending').json()), 1)
        with patch('export_doc.batch_to_docx', return_value=first_export / 'synthetic.docx') as exporter:
            self.assertEqual(self.api.get(f"/api/batch/{other['bid']}/export").status_code, 403)
            exporter.assert_not_called()
            self.assertEqual(self.api.get(f"/api/batch/{own['bid']}/export").status_code, 200)
        self._set_scoped_account(9102, (8102,))
        self.assertNotEqual(plat.export_root(), first_export)
        self.assertEqual(self.api.get('/api/pending').json(), [])
        self.assertEqual(self.api.get('/api/file', params={'path': str(pending)}).status_code, 403)
        self.assertEqual(self.api.delete('/api/pending', params={'temp': str(pending)}).status_code, 403)
        self.assertEqual(self.api.get('/api/job/synthetic-task').status_code, 404)
        self.assertTrue(pending.is_file())


if __name__ == '__main__':
    unittest.main()
