"""Community configuration. No publisher credentials or remote entitlement service."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

APP_NAME = 'kidobs-community'
APP_TITLE = '园记自由版｜基于幼师口袋评价系统开发的开源助手'
VERSION = '1.7.5'
BASE_DIR = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / 'static'
BUILTIN_DATA_DIR = BASE_DIR / 'data'

def _user_data_dir():
    import plat
    override = os.environ.get('KIDOBS_DATA_DIR', '').strip()
    return Path(override).expanduser().resolve() if override else plat.data_root() / APP_NAME

DATA_DIR = _user_data_dir()
PHOTO_DIR = DATA_DIR / 'photos'
UPLOAD_DIR = DATA_DIR / 'uploads'
DB_PATH = DATA_DIR / 'kidobs.db'
LOG_PATH = DATA_DIR / 'kidobs.log'
for _directory in (DATA_DIR, PHOTO_DIR, UPLOAD_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

SETTINGS_PATH = DATA_DIR / 'settings.json'
_lock = threading.RLock()
_speed = threading.local()
_DEFAULTS = {
    'provider': 'zhipu', 'base_url': 'https://open.bigmodel.cn/api/paas/v4',
    'model': 'glm-5.3-flash', 'api_key': '', 'thinking': 'default',
    'face_enabled': False, 'tencent_id': '', 'tencent_key': '', 'tencent_region': 'ap-shanghai',
}
_ENV = {
    'provider': 'KIDOBS_AI_PROVIDER', 'base_url': 'KIDOBS_AI_BASE',
    'model': 'KIDOBS_AI_MODEL', 'api_key': 'KIDOBS_AI_KEY', 'thinking': 'KIDOBS_AI_THINKING',
    'face_enabled': 'KIDOBS_FACE_ENABLED', 'tencent_id': 'KIDOBS_TC_ID',
    'tencent_key': 'KIDOBS_TC_KEY', 'tencent_region': 'KIDOBS_TC_REGION',
}

def _stored():
    if not SETTINGS_PATH.exists():
        return dict(_DEFAULTS)
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ValueError('本地 API 设置无法读取，请检查 settings.json。') from exc
    if not isinstance(value, dict):
        raise ValueError('本地 API 设置格式错误。')
    return {**_DEFAULTS, **{k: v for k, v in value.items() if k in _DEFAULTS}}

def settings():
    with _lock:
        value = _stored()
    for key, env in _ENV.items():
        if os.environ.get(env, '').strip():
            value[key] = (os.environ[env].strip().lower() in ('1', 'true', 'yes', 'on')
                          if key == 'face_enabled' else os.environ[env].strip())
    # Existing documented provider-specific environment variables remain usable.
    if not os.environ.get('KIDOBS_AI_KEY'):
        alias = 'KIDOBS_' + str(value['provider']).upper().replace('-', '_')
        if os.environ.get(alias):
            value['api_key'] = os.environ[alias].strip()
    return value

def public_settings():
    value = settings()
    public = {k: v for k, v in value.items() if k not in ('api_key', 'tencent_id', 'tencent_key')}
    public['face_enabled'] = bool(value['face_enabled'] and value['tencent_id'] and value['tencent_key'])
    for key in ('api_key', 'tencent_id', 'tencent_key'):
        public[key + '_set'] = bool(value.get(key))
    public['data_dir'] = str(DATA_DIR)
    public['env_overrides'] = [k for k, env in _ENV.items() if os.environ.get(env, '').strip()]
    alias = 'KIDOBS_' + str(value['provider']).upper().replace('-', '_')
    if os.environ.get(alias) and 'api_key' not in public['env_overrides']:
        public['env_overrides'].append('api_key')
    return public

def save_settings(changes):
    if not isinstance(changes, dict):
        raise ValueError('设置必须是 JSON 对象。')
    extra = set(changes) - set(_DEFAULTS) - {'clear_api_key', 'clear_tencent'}
    if extra:
        raise ValueError('不支持的设置字段。')
    with _lock:
        value = _stored()
        for key, item in changes.items():
            if key.startswith('clear_'):
                if type(item) is not bool:
                    raise ValueError('清除选项必须为 true 或 false。')
                continue
            if key == 'face_enabled':
                if type(item) is not bool:
                    raise ValueError('人脸识别开关必须为 true 或 false。')
            elif not isinstance(item, str) or len(item) > (4096 if key in ('api_key', 'tencent_key') else 500):
                raise ValueError('设置字段格式或长度不正确。')
            else:
                item = item.strip()
                if any(ord(c) < 32 for c in item):
                    raise ValueError('设置中不能包含控制字符。')
            if key in ('api_key', 'tencent_id', 'tencent_key') and not item:
                continue
            value[key] = item
        if changes.get('clear_api_key'):
            value['api_key'] = ''
        if changes.get('clear_tencent'):
            value['tencent_id'] = value['tencent_key'] = ''
            value['face_enabled'] = False
        if value['face_enabled']:
            sid = os.environ.get('KIDOBS_TC_ID', '').strip() or value['tencent_id']
            secret = os.environ.get('KIDOBS_TC_KEY', '').strip() or value['tencent_key']
            if not (sid and secret):
                raise ValueError('启用人脸识别前，请填写腾讯云 SecretId 和 SecretKey。')
        if value['provider'] not in ('zhipu', 'deepseek', 'openai-compatible'):
            raise ValueError('请选择智谱、DeepSeek 或 OpenAI 兼容接口。')
        if value['thinking'] not in ('default', 'off', 'low', 'high', 'max'):
            raise ValueError('不支持的思考档位。')
        endpoint = urlsplit(value['base_url'])
        if (endpoint.scheme not in ('https', 'http') or not endpoint.hostname or
                endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or
                (endpoint.scheme == 'http' and endpoint.hostname not in ('localhost', '127.0.0.1', '::1'))):
            raise ValueError('API 地址须为 HTTPS；本机服务可使用 http://127.0.0.1。不要把密钥放在地址中。')
        if not value['model']:
            raise ValueError('请填写模型名称。')
        temporary = None
        try:
            # mkstemp restricts access at creation; no readable temporary secret file.
            descriptor, name = tempfile.mkstemp(prefix='.settings-', suffix='.tmp', dir=DATA_DIR)
            temporary = Path(name)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, SETTINGS_PATH)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return public_settings()

ZHIPU_BASE = _DEFAULTS['base_url']
ZHIPU_MODEL = _DEFAULTS['model']
ZHIPU_THINKING = {'type': 'enabled', 'effort': 'low'}
TENCENT_REGION = _DEFAULTS['tencent_region']
YSK_BASE = 'https://www.youshikoudai.com'
YSK_OSS_BUCKET = '1bplus-product-01'
YSK_OSS_ENDPOINT = 'https://1bplus-product-01.oss-cn-hangzhou.aliyuncs.com'
YSK_IMAGE_HOST = 'https://image.youshikoudai.com'
FACE_MODEL_VERSION = '3.0'
FACE_MATCH_THRESHOLD = 70.0
FACE_MAX_PER_PERSON = 5
MOCK = os.environ.get('KIDOBS_MOCK') == '1'

def tencent_creds():
    value = settings()
    return value['tencent_id'], value['tencent_key'], value['tencent_region']

def face_enabled():
    value = settings()
    return bool(value['face_enabled'] and value['tencent_id'] and value['tencent_key'])

def zhipu_key():
    return settings()['api_key']

def set_speed(value):
    _speed.v = value if value in ('fast', 'deep') else ''

def cur_speed():
    return getattr(_speed, 'v', '')

def ai_modes():
    value = settings()
    conf = {'provider': value['provider'], 'base': value['base_url'], 'model': value['model'],
            'thinking': None, 'json_mode': value['provider'] == 'deepseek'}
    effort = value['thinking']
    if effort in ('low', 'high', 'max'):
        conf['thinking'] = {'type': 'enabled', 'effort': effort}
    if value['provider'] == 'zhipu' and value['model'].lower().startswith('glm-5.3'):
        conf['thinking'] = conf['thinking'] or {'type': 'enabled', 'effort': 'low'}
    return {'fast': dict(conf), 'deep': dict(conf), 'default': 'fast'}

def ai_conf(mode=''):
    import ai_progress
    captured = ai_progress.settings() if not mode else None
    if captured:
        return captured
    if mode not in ('fast', 'deep'):
        mode = cur_speed()
    if mode not in ('fast', 'deep'):
        try:
            import db
            mode = db.get_setting('ai_mode', '')
        except Exception:
            mode = ''
    if mode not in ('fast', 'deep'):
        mode = 'fast'
    conf = ai_modes()[mode]
    return {**conf, 'mode': mode, 'key': settings()['api_key']}

def effort_options(conf=None):
    conf = conf or ai_conf()
    provider, model = conf.get('provider', ''), conf.get('model', '').lower()
    if provider == 'zhipu' and model.startswith('glm-5.3'):
        levels, hint = ['low', 'high', 'max'], '该模型始终开启思考。'
    elif provider == 'deepseek' and model in ('deepseek-flash', 'deepseek-pro', 'deepseek-v4-flash', 'deepseek-v4-pro'):
        levels, hint = ['off', 'low', 'high', 'max'], '思考越充分，等待时间通常越长。'
    else:
        levels, hint = [], '使用本机 API 设置；通用兼容模型不附加未知的思考参数。'
    thinking = conf.get('thinking') or {}
    default = thinking.get('effort', 'off')
    if default not in levels:
        default = levels[0] if levels else 'default'
    return {'model': conf.get('model', ''), 'levels': levels, 'default': default, 'hint': hint}
