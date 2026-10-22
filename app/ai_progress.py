"""Observation task progress. Only counts/status are public; never retain reasoning text."""
from contextlib import contextmanager
from copy import deepcopy
import threading
import time

_local = threading.local()
_lock = threading.RLock()
_tasks = {}


def prepare(item_id, effort, config):
    with _lock:
        # Finished entries are disposable. Active work is never evicted.
        if len(_tasks) > 1000:
            for key in list(_tasks):
                if _tasks[key]['public']['status'] in ('done', 'error'):
                    del _tasks[key]
                    if len(_tasks) <= 800:
                        break
        task = {'config': deepcopy(config), 'requests': {}, 'public': {
            'status': 'queued', 'stage': '等待写作', 'completed': 0, 'total': 0,
            'effort': effort, 'model': config.get('model', ''), 'phase': 'queued',
            'received_chars': 0, 'requests_started': 0, 'requests_completed': 0,
            'updated_at': time.time()}}
        _tasks[int(item_id)] = task
        return task


def snapshot(item_id):
    with _lock:
        task = _tasks.get(int(item_id))
        return dict(task['public']) if task else None


@contextmanager
def scope(item_id):
    with _lock:
        task = _tasks.get(int(item_id))
    previous = getattr(_local, 'task', None)
    _local.task = task
    try:
        if task:
            with _lock:
                task['public'].update(status='running', phase='preparing', stage='整理观察资料')
        yield
    finally:
        _local.task = previous


def wrap(fn, *args, **kwargs):
    """Capture the same request settings/progress when dispatching an auxiliary thread."""
    task = getattr(_local, 'task', None)
    def run():
        previous = getattr(_local, 'task', None)
        _local.task = task
        try:
            return fn(*args, **kwargs)
        finally:
            _local.task = previous
    return run


def settings():
    task = getattr(_local, 'task', None)
    return deepcopy(task['config']) if task else None


def stage(label, completed=None, total=None):
    task = getattr(_local, 'task', None)
    if task:
        with _lock:
            task['public'].update(stage=label, updated_at=time.time())
            if total is not None:
                task['public']['total'] = max(0, int(total))
            if completed is not None:
                task['public']['completed'] = max(0, int(completed))


def begin_request():
    task = getattr(_local, 'task', None)
    if not task:
        return None
    with _lock:
        task['public']['requests_started'] += 1
        rid = task['public']['requests_started']
        task['requests'][rid] = {'chars': 0, 'phase': 'waiting'}
        task['public'].update(phase='waiting', updated_at=time.time())
        return (task, rid)


def received(token, phase, chars=0):
    if token is None:
        return
    task, rid = token
    with _lock:
        if task['public']['status'] in ('done', 'error'):
            return
        task['requests'][rid].update(phase=phase)
        task['requests'][rid]['chars'] += int(chars)
        task['public'].update(phase=phase, updated_at=time.time())
        task['public']['received_chars'] += int(chars)


def end_request(token, ok=True):
    if token is None:
        return
    task, rid = token
    with _lock:
        task['requests'].pop(rid, None)
        if task['public']['status'] in ('done', 'error'):
            return
        if ok:
            task['public']['requests_completed'] += 1
        phases = [r['phase'] for r in task['requests'].values()]
        task['public'].update(phase=phases[-1] if phases else 'checking', updated_at=time.time())


def finish(item_id, ok):
    with _lock:
        task = _tasks.get(int(item_id))
        if not task:
            return
        public = task['public']
        public.update(status='done' if ok else 'error', phase='done' if ok else 'error',
                      stage='生成完成，等待老师审核' if ok else '生成失败，原稿已保留', updated_at=time.time())
        if ok and public['total']:
            public['completed'] = public['total']
