"""Exercise all writing paths over real loopback HTTP, without an external AI key.

Run with the normal runtime dependencies:
    python -m unittest discover -s tests -p test_ai_local_http.py -v

The fake provider uses ThreadingHTTPServer, while application code uses its real
requests transport. Each case starts a fresh Python process and data directory;
no MOCK mode, teacher data, third-party login, or external network is used.
"""
from collections import deque
from contextlib import contextmanager
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_KEY = 'local-http-test-key-not-a-provider-secret'
NOTE = '小禾，你把几片纸叠在一起，用手掌压平折痕，又把两边往上折，让小纸桥能立住。纸桥倒下后，你重新摆好底边，再轻轻放上纸团，继续寻找让它站稳的办法。'
STORY = '小禾拿起一张纸，将两条长边向中间折拢，用指尖沿着折痕来回压了几次。他把折好的纸立在桌面上，松手后纸片向一侧倒下，便重新摊开底边，换了一个方向摆放。他又从纸盒里取出一张纸，垫在下面，扶住两侧，把纸团轻轻放到上面。发现中间弯了下去，他拿走纸团，将纸片折成连续的折线，再放回原处试了一次。'
ANALYSIS = '小禾没有只重复摆放纸片，而是根据倒下和弯折的结果调整底边与折法。这些变化提供了理解他如何比较办法的线索。纸片能否保持稳定还受到材料厚薄和摆放方式影响，单凭这次尝试不宜推定他已经掌握所有相关方法，后续可以继续看他怎样选择和说明自己的做法。'
FOLLOWUP = '提供厚薄不同的纸，让小禾按自己的办法继续搭建；教师保留两种折法的结果，留意他是否会比较稳定程度，并说明再次调整的理由。'
@contextmanager
def provider(replies):
    """Replies are predetermined content, wrapped in a Chat Completions response."""
    queue = deque(replies)
    received = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            payload = json.loads(raw.decode('utf-8'))
            with lock:
                received.append({'path': self.path, 'body': payload,
                                 'authorization': self.headers.get('Authorization', '')})
                content = queue.popleft() if queue else None
            if content is None or self.path != '/v1/chat/completions':
                status, result = 400, {'error': {'message': 'Unexpected request to local test provider'}}
            else:
                status = 200
                result = {'id': 'local-test-completion', 'object': 'chat.completion',
                          'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                                       'finish_reason': 'stop'}],
                          'usage': {'prompt_tokens': 100, 'completion_tokens': 100}}
            data = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}/v1', received, queue
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def worker(case, endpoint):
    """Runs only in the fresh child process, before any application imports."""
    import socket
    original_connect, original_resolve = socket.socket.connect, socket.getaddrinfo
    local_hosts = ('127.0.0.1', '::1', 'localhost')

    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in local_hosts:
            raise AssertionError('External connection forbidden in local HTTP test')
        return original_connect(sock, address)

    def local_resolve(host, *args, **kwargs):
        if host not in local_hosts:
            raise AssertionError('External DNS forbidden in local HTTP test')
        return original_resolve(host, *args, **kwargs)

    socket.socket.connect, socket.getaddrinfo = local_connect, local_resolve
    sys.path.insert(0, str(ROOT / 'app'))
    import config
    # Exercise the supported no-reference-resource case even if a maintainer
    # installs an optional pack in their checkout. Preserve only writing rules.
    import shutil
    empty_resources = config.DATA_DIR / 'empty-test-resources'
    for relative in ('writing/SKILL.md', 'observation/SKILL.md',
                     'observation/NURSERY_SKILL.md'):
        source = config.BUILTIN_DATA_DIR / relative
        if source.is_file():
            destination = empty_resources / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    config.BUILTIN_DATA_DIR = empty_resources
    import db
    import ai_writer
    import evidence
    import record_table
    from PIL import Image, ImageDraw
    assert not config.MOCK
    assert not ai_writer.MOCK
    assert config.DATA_DIR == Path(os.environ['KIDOBS_DATA_DIR'])
    db.init()
    config.save_settings({'provider': 'openai-compatible', 'base_url': endpoint,
                          'model': 'local-test-vision', 'api_key': SYNTHETIC_KEY, 'thinking': 'high'})
    photo = config.DATA_DIR / 'synthetic-paper.jpg'
    image = Image.new('RGB', (96, 64), 'white')
    ImageDraw.Draw(image).rectangle((20, 18, 75, 52), outline='blue', width=3)
    image.save(photo, 'JPEG')
    # This case must not gain references from any optional reference pack.
    assert evidence.catalog() == []
    if case == 'note':
        result = ai_writer.write_observation(photo, '合成幼儿甲', 3, '合成测试：折纸搭桥。',
                                             nickname='小禾', picks=[])
    elif case == 'table':
        story = record_table.write_story(photo, '合成幼儿甲', 3, '合成测试：折纸搭桥，不补写。',
                                         nickname='小禾', picks=[])
        analysis = record_table.write_analysis(story, '合成幼儿甲', 3, '合成测试：折纸搭桥。',
                                               nickname='小禾', picks=[])
        result = {'story': story, 'analysis': analysis}
    else:
        raise ValueError('Unknown test case')
    print(json.dumps(result, ensure_ascii=False))


class LocalAIHTTPTests(unittest.TestCase):
    def run_case(self, case, replies):
        with tempfile.TemporaryDirectory(prefix='kidobs-local-http-') as directory:
            env = dict(os.environ)
            for name in list(env):
                if (name.startswith('KIDOBS_') or name.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')):
                    env.pop(name, None)
            env.update(KIDOBS_DATA_DIR=directory, APPDATA=directory, LOCALAPPDATA=directory,
                       PYTHONIOENCODING='utf-8', NO_PROXY='127.0.0.1,localhost,::1')
            with provider(replies) as (endpoint, requests, remaining):
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', case, endpoint],
                                        cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr[-5000:])
                self.assertFalse(remaining, 'Not all expected provider requests were made')
                parsed = json.loads(result.stdout)
        self.assertEqual(len(requests), len(replies), 'Unexpected retry or missing request')
        for request in requests:
            self.assertEqual(request['path'], '/v1/chat/completions')
            self.assertEqual(request['authorization'], 'Bearer ' + SYNTHETIC_KEY)
            self.assertEqual(request['body']['model'], 'local-test-vision')
            self.assertFalse('thinking' in request['body'], 'Generic provider received an unsupported thinking field')
            self.assertFalse('reasoning_effort' in request['body'], 'Generic provider received an unsupported reasoning_effort field')
            self.assertFalse(request['body'].get('stream', False))
        return parsed, requests

    def assert_image_transmitted(self, request):
        from PIL import Image
        parts = [part for message in request['body']['messages'] if isinstance(message.get('content'), list)
                 for part in message['content'] if part.get('type') == 'image_url']
        self.assertEqual(len(parts), 1)
        data_url = parts[0]['image_url']['url']
        self.assertTrue(data_url.startswith('data:image/jpeg;base64,'))
        decoded = Image.open(io.BytesIO(base64.b64decode(data_url.split(',', 1)[1])))
        decoded.load()
        self.assertEqual(decoded.size, (96, 64))

    def test_note_uses_own_api_and_keeps_explicit_empty_indicators(self):
        reply = json.dumps({'indicators': [], 'text': NOTE, 'behavior': '折纸后调整底边。', 'confidence': 0.8}, ensure_ascii=False)
        result, requests = self.run_case('note', [reply])
        self.assertEqual(result['text'], NOTE)
        self.assertEqual(result['indicators'], [])
        self.assertIsNone(result['indicator_id'])
        self.assert_image_transmitted(requests[0])

    def test_table_story_and_analysis_have_no_fabricated_sources(self):
        replies = [json.dumps({'观察内容': STORY, '情境补写': False}, ensure_ascii=False),
                   json.dumps({'分析': ANALYSIS, '行为跟进': [FOLLOWUP], '依据编号': [], '指标解读': []}, ensure_ascii=False)]
        result, requests = self.run_case('table', replies)
        self.assertEqual(result['story']['观察内容'], STORY)
        self.assertFalse(result['story']['情境补写'])
        self.assertEqual(result['analysis']['分析'], ANALYSIS)
        self.assertEqual(result['analysis']['分析依据'], [])
        self.assertEqual(result['analysis']['行为跟进'], [FOLLOWUP])
        self.assert_image_transmitted(requests[0])
        user_text = requests[1]['body']['messages'][-1]['content']
        self.assertIn('没有检索到适用知识条文', user_text)
        self.assertIn('依据编号输出空数组', user_text)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
