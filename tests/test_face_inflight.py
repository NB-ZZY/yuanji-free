"""Recognition deduplication with synthetic rows; no files, faces or cloud calls."""
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import plat

_scratch = tempfile.TemporaryDirectory(prefix='kidobs-face-inflight-')
with patch.object(plat, 'data_root', return_value=Path(_scratch.name)):
    import pipeline
    import session_state


class SyntheticStore:
    def __init__(self):
        self.item = {'id': 1, 'batch_id': 10, 'student_id': None,
                     'face_status': 'error', 'ai_error': 'synthetic previous error'}
        self.lock = threading.RLock()

    def one(self, sql, args):
        with self.lock:
            if 'FROM items' in sql:
                return dict(self.item)
            if 'FROM classes' in sql:
                return {'level': 3}
            return {'id': 10, 'class_id': 20}

    def rows(self, sql, args):
        with self.lock:
            return [dict(self.item)]

    def students_of(self, _):
        return []

    def update_item(self, iid, **values):
        assert iid == 1
        with self.lock:
            self.item.update(values)

    def exec(self, sql, args):
        if "UPDATE items SET face_status='error'" in sql and self.item['face_status'] == 'pending':
            self.update_item(1, face_status='error')


class ControlledExecutor:
    def __init__(self):
        self.calls = []
        self.queued = threading.Event()
        self.fail_submit = False

    def submit(self, function, *args):
        if self.fail_submit:
            raise RuntimeError('synthetic submission failure')
        future = Future()
        self.calls.append((future, function, args))
        self.queued.set()
        return future

    def run(self, index=0):
        future, function, args = self.calls[index]
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)


class FaceInflight(unittest.TestCase):
    def setUp(self):
        self.store = SyntheticStore()
        self.executor = ControlledExecutor()
        self.fail_recognition = False
        self.recognized = 0

        def recognize(item, *_):
            self.recognized += 1
            if self.fail_recognition:
                raise RuntimeError('synthetic recognition failure')
            self.store.update_item(item['id'], face_status='noface')

        for name, value in (('db', self.store), ('_face_pool', self.executor),
                            ('_face_inflight', {}), ('_recognize', recognize),
                            ('_apply_brief', lambda *_: None)):
            guard = patch.object(pipeline, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('禁止真实网络'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_single_and_batch_repeated_clicks_share_one_task(self):
        self.assertTrue(pipeline.rescan(1)['started'])
        self.assertFalse(pipeline.rescan(1)['started'])
        self.assertEqual(pipeline.rescan_batch(10), 0)
        self.assertEqual(len(self.executor.calls), 1)
        self.executor.run()
        self.assertEqual(self.recognized, 1)
        self.assertEqual(pipeline._face_inflight, {})

    def test_initial_batch_and_manual_rescan_share_one_future(self):
        self.store.update_item(1, face_status='pending')
        initial = threading.Thread(target=pipeline._process_batch, args=(10,))
        initial.start()
        try:
            self.assertTrue(self.executor.queued.wait(2))
            self.assertFalse(pipeline.rescan(1)['started'])
            self.assertEqual(pipeline.rescan_batch(10), 0)
            self.assertEqual(len(self.executor.calls), 1)
        finally:
            if self.executor.calls:
                self.executor.run()
            initial.join(2)
        self.assertFalse(initial.is_alive())
        self.assertEqual(self.recognized, 1)

    def test_delayed_initial_coordinator_does_not_repeat_completed_manual_scan(self):
        pipeline.rescan(1)
        self.executor.run()
        pipeline._process_batch(10)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.recognized, 1)

    def test_concurrent_clicks_share_one_task(self):
        barrier = threading.Barrier(8)

        def click(_):
            barrier.wait(2)
            return pipeline.rescan(1)['started']

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(click, range(8)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.executor.calls), 1)

    def test_failed_future_releases_slot_for_retry(self):
        self.fail_recognition = True
        pipeline.rescan(1)
        self.executor.run()
        self.assertIsInstance(self.executor.calls[0][0].exception(), RuntimeError)
        self.assertEqual(pipeline._face_inflight, {})
        self.fail_recognition = False
        self.assertTrue(pipeline.rescan(1)['started'])
        self.executor.run(1)
        self.assertEqual(self.recognized, 2)

    def test_cancelled_future_releases_slot_for_retry(self):
        pipeline.rescan(1)
        self.assertTrue(self.executor.calls[0][0].cancel())
        self.assertEqual(pipeline._face_inflight, {})
        self.assertTrue(pipeline.rescan(1)['started'])
        self.executor.run(1)
        self.assertEqual(self.recognized, 1)

    def test_submission_failure_restores_previous_state(self):
        before = dict(self.store.item)
        self.executor.fail_submit = True
        with self.assertRaises(RuntimeError):
            pipeline.rescan(1)
        self.assertEqual(self.store.item, before)
        self.assertEqual(pipeline._face_inflight, {})
        self.executor.fail_submit = False
        self.assertTrue(pipeline.rescan(1)['started'])

    def test_pending_from_previous_process_can_be_retried(self):
        self.store.update_item(1, face_status='pending')
        self.assertTrue(pipeline.rescan(1)['started'])
        self.assertEqual(len(self.executor.calls), 1)

    def test_real_account_executor_holds_lease_until_work_finishes(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def recognize(item, *_):
            started.set()
            if not release.wait(2):
                raise RuntimeError('test did not release task')
            self.store.update_item(item['id'], face_status='noface')

        with session_state.AccountExecutor(max_workers=1) as executor:
            with patch.object(pipeline, '_face_pool', executor), patch.object(pipeline, '_recognize', recognize):
                try:
                    self.assertTrue(pipeline.rescan(1)['started'])
                    self.assertTrue(started.wait(2))
                    future = pipeline._face_inflight[1]
                    future.add_done_callback(lambda _: finished.set())
                    self.assertTrue(session_state.busy())
                    self.assertFalse(pipeline.rescan(1)['started'])
                finally:
                    release.set()
                self.assertTrue(finished.wait(2))
                self.assertEqual(pipeline._face_inflight, {})
                self.assertFalse(session_state.busy())


if __name__ == '__main__':
    unittest.main()
