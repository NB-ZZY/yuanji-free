"""Exercise actual local API origin/host checks without credentials or external I/O."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

scratch = tempfile.TemporaryDirectory(prefix='kidobs-local-security-')
for key in list(os.environ):
    if key.startswith('KIDOBS_'):
        os.environ.pop(key)
os.environ.update(APPDATA=scratch.name, LOCALAPPDATA=scratch.name,
                  KIDOBS_DATA_DIR=scratch.name, KIDOBS_MOCK='1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import server
from fastapi.testclient import TestClient


class LocalRequestSecurityTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.api = TestClient(server.app, base_url='http://127.0.0.1:8765')
        server._login_attempt = None

    def test_own_window_and_native_transport_work(self):
        for headers in ({}, {'Origin': 'http://127.0.0.1:8765', 'Sec-Fetch-Site': 'same-origin'},
                        {'Referer': 'http://127.0.0.1:8765/', 'Sec-Fetch-Site': 'none'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.api.post('/api/login/begin', headers=headers).status_code, 200)

    def test_host_rebinding_does_not_start_login(self):
        for host in ('unrelated.example', '127.0.0.1.unrelated.example', 'unrelated.example@127.0.0.1'):
            with self.subTest(host=host):
                self.assertEqual(self.api.post('/api/login/begin', headers={'Host': host}).status_code, 403)
        self.assertIsNone(server._login_attempt)

    def test_foreign_missing_or_invalid_origin_cannot_mutate(self):
        cases = [
            {'Origin': 'https://unrelated.example'},
            {'Origin': 'null'},
            {'Origin': 'http://127.0.0.1:8766'},
            {'Origin': 'http://127.0.0.1:invalid'},
            {'Referer': 'https://unrelated.example/page'},
            {'Sec-Fetch-Site': 'cross-site'},
            {'Sec-Fetch-Site': 'same-site'},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                response = self.api.post('/api/login/begin', headers=headers)
                self.assertEqual(response.status_code, 403)
        self.assertIsNone(server._login_attempt)


if __name__ == '__main__':
    unittest.main()
