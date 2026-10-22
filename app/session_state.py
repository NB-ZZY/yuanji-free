"""Keep an account stable until its requests and queued background work finish."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

lock = threading.RLock()
generation = 0
_active = 0


def retain():
    global _active
    with lock:
        _active += 1


def release():
    global _active
    with lock:
        _active -= 1
        assert _active >= 0


def busy():
    with lock:
        return _active > 0


@contextmanager
def activity():
    retain()
    try:
        yield
    finally:
        release()


class AccountExecutor(ThreadPoolExecutor):
    """Queued futures also hold a lease; cancellation releases it exactly once."""
    def submit(self, fn, /, *args, **kwargs):
        retain()
        try:
            future = super().submit(fn, *args, **kwargs)
        except BaseException:
            release()
            raise
        future.add_done_callback(lambda _: release())
        return future
