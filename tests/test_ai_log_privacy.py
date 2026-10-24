"""Synthetic privacy regression; block transport and never read teacher data."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import plat

_scratch = tempfile.TemporaryDirectory(prefix='kidobs-ai-log-')
with patch.object(plat, 'data_root', return_value=Path(_scratch.name)):
    import ai_writer as ai

MARKER = '合成隐私标记_幼儿姓名与观察正文_不得写入日志'


class AiLogPrivacy(unittest.TestCase):
    def setUp(self):
        for target, options in (
            ('ai_writer.MOCK', {'new': False}),
            ('ai_writer._style_block', {'return_value': ''}),
            ('requests.sessions.Session.request', {'side_effect': AssertionError('禁止真实网络')}),
        ):
            guard = patch(target, **options)
            guard.start()
            self.addCleanup(guard.stop)

    def test_generation_failure_logs_metadata_only(self):
        raw = json.dumps({'feedback': MARKER}, ensure_ascii=False)
        with patch.object(ai, '_chat', return_value=raw), self.assertLogs('ai', 'WARNING') as logs:
            with self.assertRaises(RuntimeError):
                ai.write_observation(None, MARKER, 3, MARKER, picks=[])
        output = '\n'.join(logs.output)
        self.assertNotIn(MARKER, output)
        self.assertIn('ValueError', output)
        self.assertIn(str(len(raw)), output)

    def test_other_warning_paths_do_not_echo_exception_messages(self):
        calls = (
            lambda: ai.pick_indicators(None, MARKER, 3, MARKER),
            lambda: ai.rewrite_text(MARKER, 3, '', '', MARKER, picks=[]),
        )
        for call in calls:
            with self.subTest(call=call), patch.object(ai, '_chat', side_effect=ValueError(MARKER)):
                with self.assertLogs('ai', 'WARNING') as logs, self.assertRaises(RuntimeError):
                    call()
                self.assertNotIn(MARKER, '\n'.join(logs.output))
        with patch.object(ai, '_img_b64', side_effect=OSError(MARKER)):
            with self.assertLogs('ai', 'WARNING') as logs:
                self.assertEqual(ai._img_parts(MARKER), [])
            self.assertNotIn(MARKER, '\n'.join(logs.output))

    def test_provider_error_body_is_not_propagated_to_callers(self):
        response = Mock(status_code=401)
        response.json.return_value = {'error': {'message': MARKER}}
        response.text = MARKER
        conf = {'provider': 'openai-compatible', 'base': 'https://example.invalid/v1',
                'model': 'synthetic-model', 'key': 'dummy-not-a-real-key'}
        with patch('config.ai_conf', return_value=conf), patch('ai_progress.begin_request', return_value=None):
            with patch.object(ai.requests, 'post', return_value=response):
                with self.assertRaises(RuntimeError) as error:
                    ai._chat_full([{'role': 'user', 'content': MARKER}])
        self.assertNotIn(MARKER, str(error.exception))
        self.assertIn('401', str(error.exception))
        response.json.assert_not_called()

    def test_unknown_indicator_does_not_echo_generated_payload(self):
        with self.assertRaises(ValueError) as error:
            ai._validate({'indicators': [{'indicator_id': 999999999, 'indicator_name': MARKER}]}, 3)
        self.assertNotIn(MARKER, str(error.exception))


if __name__ == '__main__':
    unittest.main()
