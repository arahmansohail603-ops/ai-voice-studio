import threading
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from PyQt5.QtCore import QCoreApplication, QObject

import main

_APPLICATION = None


class _Gate:
    def __init__(self, limit=30.0):
        self._open = False
        self._limit = limit

    def hold(self):
        deadline = time.monotonic() + self._limit
        while not self._open and time.monotonic() < deadline:
            time.sleep(0.005)

    def release(self):
        self._open = True


def setUpModule():
    global _APPLICATION
    _APPLICATION = QCoreApplication.instance() or QCoreApplication(
        ["license-lifecycle"]
    )


def _flush():
    _APPLICATION.processEvents()


def _lease_state(expires_offset=300, grace_offset=600, server_offset=-10):
    now = datetime.now(timezone.utc)
    return {
        "version": 1,
        "license_key": "LIC-1",
        "device_id": "device-a",
        "envelope": {
            "payload": {
                "expires_at": (now + timedelta(seconds=expires_offset)).isoformat(),
                "grace_expires_at": (now + timedelta(seconds=grace_offset)).isoformat(),
            }
        },
        "last_server_time": (now + timedelta(seconds=server_offset)).isoformat(),
    }


class _Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.succeeded = []
        self.failed = []

    def on_succeeded(self, revision, value):
        self.succeeded.append((revision, value))

    def on_failed(self, revision, message):
        self.failed.append((revision, message))


class _Application:
    def __init__(self):
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1


class _Window:
    def __init__(self):
        self.enabled = []
        self.toasts = []
        self.closed = 0
        self.raised = 0
        self.activated = 0
        self.shown = 0

    def setEnabled(self, value):
        self.enabled.append(value)

    def toast(self, message, level):
        self.toasts.append((message, level))

    def close(self):
        self.closed += 1

    def raise_(self):
        self.raised += 1

    def activateWindow(self):
        self.activated += 1

    def show(self):
        self.shown += 1


class _StubDialog:
    def __init__(self, task=None):
        self._task = task
        self.stopped_with = []

    @property
    def task_active(self):
        return self._task is not None and self._task.isRunning()

    def stop_task(self, timeout_ms):
        self.stopped_with.append(timeout_ms)
        task, self._task = self._task, None
        return main._retire_task(task, timeout_ms)


@unittest.skipUnless(main.QThread is not None, "PyQt5 is required")
class LicenseTaskTests(unittest.TestCase):
    def test_result_is_emitted_with_the_task_revision(self):
        receiver = _Receiver()
        task = main._LicenseTask(lambda: {"lease": True}, None, 9)
        task.succeeded.connect(receiver.on_succeeded)
        task.failed.connect(receiver.on_failed)
        task.start()
        self.assertTrue(task.wait(5000))
        _flush()
        self.assertEqual(receiver.succeeded, [(9, {"lease": True})])
        self.assertEqual(receiver.failed, [])
        self.assertEqual(task.revision, 9)

    def test_failure_is_emitted_with_the_task_revision(self):
        receiver = _Receiver()

        def operation():
            raise ValueError("server said no")

        task = main._LicenseTask(operation, None, 3)
        task.succeeded.connect(receiver.on_succeeded)
        task.failed.connect(receiver.on_failed)
        task.start()
        self.assertTrue(task.wait(5000))
        _flush()
        self.assertEqual(receiver.failed, [(3, "server said no")])
        self.assertEqual(receiver.succeeded, [])

    def test_cancelled_task_does_not_emit_its_result(self):
        gate = _Gate()
        started = threading.Event()

        def operation():
            started.set()
            gate.hold()
            return {"lease": True}

        receiver = _Receiver()
        task = main._LicenseTask(operation, None, 4)
        task.succeeded.connect(receiver.on_succeeded)
        task.failed.connect(receiver.on_failed)
        task.start()
        self.assertTrue(started.wait(5))
        task.cancel()
        gate.release()
        self.assertTrue(task.wait(5000))
        _flush()
        self.assertTrue(task.cancelled)
        self.assertEqual(receiver.succeeded, [])
        self.assertEqual(receiver.failed, [])

    def test_cancelled_task_does_not_emit_its_failure(self):
        gate = _Gate()
        started = threading.Event()

        def operation():
            started.set()
            gate.hold()
            raise ValueError("server said no")

        receiver = _Receiver()
        task = main._LicenseTask(operation, None, 4)
        task.succeeded.connect(receiver.on_succeeded)
        task.failed.connect(receiver.on_failed)
        task.start()
        self.assertTrue(started.wait(5))
        task.cancel()
        gate.release()
        self.assertTrue(task.wait(5000))
        _flush()
        self.assertEqual(receiver.failed, [])
        self.assertEqual(receiver.succeeded, [])

    def test_stop_waits_for_a_cooperative_task(self):
        gate = _Gate()
        started = threading.Event()

        def operation():
            started.set()
            gate.hold()
            return {"lease": True}

        task = main._LicenseTask(operation)
        task.start()
        self.assertTrue(started.wait(5))
        gate.release()
        self.assertTrue(task.stop(5000))
        self.assertFalse(task.isRunning())
        self.assertTrue(task.cancelled)

    def test_stop_is_bounded_when_the_task_ignores_interruption(self):
        started = threading.Event()
        blocker = _Gate()

        def operation():
            started.set()
            blocker.hold()
            return {"lease": True}

        task = main._LicenseTask(operation)
        task.start()
        self.assertTrue(started.wait(5))
        self.assertTrue(task.stop(100))
        self.assertFalse(task.isRunning())
        blocker.release()
        task.wait(5000)

    def test_task_active_tracks_the_running_thread(self):
        self.assertFalse(main._task_active(None))
        task = main._LicenseTask(lambda: None)
        self.assertFalse(main._task_active(task))
        task.start()
        self.assertTrue(task.wait(5000))
        self.assertFalse(main._task_active(task))

    def test_retire_task_parks_a_task_that_cannot_be_stopped(self):
        class _Stubborn:
            def __init__(self):
                self.timeouts = []

            def stop(self, timeout_ms):
                self.timeouts.append(timeout_ms)
                return False

        stubborn = _Stubborn()
        self.addCleanup(main._LINGERING_TASKS.discard, stubborn)
        self.assertFalse(main._retire_task(stubborn, 25))
        self.assertEqual(stubborn.timeouts, [25])
        self.assertIn(stubborn, main._LINGERING_TASKS)

    def test_retire_task_accepts_a_missing_task(self):
        self.assertTrue(main._retire_task(None, 25))


@unittest.skipUnless(main.QThread is not None, "PyQt5 is required")
class LicenseControllerTests(unittest.TestCase):
    def setUp(self):
        timeout = patch.object(main.config, "LICENSE_REQUEST_TIMEOUT", 0.05)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.application = _Application()
        self.controller = main._LicenseController(
            types.SimpleNamespace(refresh=lambda: _lease_state()), self.application
        )
        self.controller.window = _Window()
        self.addCleanup(self.controller.shutdown)

    def _start_blocking_task(self, revision=1):
        gate = _Gate()
        started = threading.Event()

        def operation():
            started.set()
            gate.hold()
            return {"lease": True}

        task = main._LicenseTask(operation, None, revision)
        task.start()
        self.assertTrue(started.wait(5))

        def cleanup():
            gate.release()
            task.wait(5000)

        self.addCleanup(cleanup)
        return task

    def _expire_deadline(self):
        self.controller._grace_expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=30
        )
        activations = []
        self.controller._activate = lambda message="": (
            activations.append(message) or True
        )
        return activations

    def test_current_refresh_result_is_applied(self):
        state = _lease_state()
        task = main._LicenseTask(lambda: state, None, 1)
        self.controller._task = task
        self.controller._revision = 1
        self.controller._refresh_finished(1, state)
        self.assertEqual(self.controller.window.enabled, [True])
        self.assertEqual(
            self.controller.window.toasts, [("License lease refreshed", "ok")]
        )
        self.assertTrue(self.controller._timer.isActive())

    def test_stale_refresh_result_is_ignored(self):
        state = _lease_state()
        task = main._LicenseTask(lambda: state, None, 1)
        self.controller._task = task
        self.controller._revision = 2
        self.controller._refresh_finished(1, state)
        self.assertEqual(self.controller.window.enabled, [])
        self.assertEqual(self.controller.window.toasts, [])
        self.assertFalse(self.controller._timer.isActive())

    def test_stale_refresh_failure_does_not_demand_activation(self):
        activations = []
        self.controller._activate = lambda message="": (
            activations.append(message) or True
        )
        task = main._LicenseTask(lambda: None, None, 1)
        self.controller._task = task
        self.controller._revision = 2
        self.controller._refresh_failed(1, "The server is unreachable")
        self.assertEqual(activations, [])
        self.assertEqual(self.controller.window.enabled, [])
        self.assertEqual(self.application.quit_calls, 0)

    def test_results_are_stale_after_shutdown(self):
        state = _lease_state()
        task = main._LicenseTask(lambda: state, None, 1)
        self.controller._task = task
        self.controller._revision = 1
        activations = []
        self.controller._activate = lambda message="": (
            activations.append(message) or True
        )
        self.controller.shutdown()
        self.controller._refresh_finished(1, state)
        self.controller._refresh_failed(1, "The server is unreachable")
        self.assertEqual(self.controller.window.enabled, [])
        self.assertEqual(self.controller.window.toasts, [])
        self.assertEqual(activations, [])
        self.assertEqual(self.application.quit_calls, 0)

    def test_refresh_starts_a_single_tracked_task(self):
        calls = []
        self.controller.manager = types.SimpleNamespace(
            refresh=lambda: calls.append(1) or _lease_state()
        )
        self.controller._refresh()
        task = self.controller._task
        self.assertIsNotNone(task)
        self.assertEqual(task.revision, 1)
        self.controller._refresh()
        self.assertIs(self.controller._task, task)
        self.assertTrue(task.wait(5000))
        self.assertEqual(calls, [1])

    def test_deadline_activation_is_deferred_while_a_task_runs(self):
        activations = self._expire_deadline()
        task = self._start_blocking_task()
        self.controller._task = task
        self.controller._deadline_reached()
        self.assertEqual(activations, [])
        self.assertTrue(self.controller._deadline_pending)
        self.assertFalse(self.controller._deadline_expired)
        self.assertEqual(
            self.controller._deadline_timer.interval(), main.DEADLINE_RETRY_MS
        )
        self.assertTrue(self.controller._deadline_timer.isActive())
        self.assertEqual(self.controller.window.enabled, [])

    def test_deferred_deadline_activation_runs_after_the_task_finishes(self):
        activations = self._expire_deadline()
        task = self._start_blocking_task()
        self.controller._task = task
        self.controller._deadline_reached()
        self.controller._task_finished()
        self.assertEqual(activations, ["The license lease has expired"])
        self.assertTrue(self.controller._deadline_expired)
        self.assertFalse(self.controller._deadline_pending)
        self.assertEqual(self.controller.window.enabled, [False])

    def test_deadline_activation_runs_immediately_without_a_task(self):
        activations = self._expire_deadline()
        self.controller._deadline_reached()
        self.assertEqual(activations, ["The license lease has expired"])
        self.assertTrue(self.controller._deadline_expired)
        self.assertFalse(self.controller._deadline_timer.isActive())

    def test_deadline_is_deferred_while_the_activation_dialog_works(self):
        activations = self._expire_deadline()
        task = self._start_blocking_task()
        self.controller._dialog = _StubDialog(task)
        self.controller._deadline_reached()
        self.assertEqual(activations, [])
        self.assertTrue(self.controller._deadline_pending)
        self.controller._dialog.stop_task(main._stop_timeout_ms())
        self.assertFalse(task.isRunning())
        self.controller._dialog = None
        self.controller._resume_deadline()
        self.assertEqual(activations, ["The license lease has expired"])

    def test_shutdown_stops_the_refresh_and_activation_threads(self):
        refresh_task = self._start_blocking_task()
        activation_task = self._start_blocking_task()
        dialog = _StubDialog(activation_task)
        self.controller._task = refresh_task
        self.controller._dialog = dialog
        self.controller.shutdown()
        self.assertFalse(refresh_task.isRunning())
        self.assertFalse(activation_task.isRunning())
        self.assertIsNone(self.controller._task)
        self.assertIsNone(self.controller._dialog)
        self.assertEqual(dialog.stopped_with, [main._stop_timeout_ms()])
        self.assertFalse(self.controller._timer.isActive())
        self.assertFalse(self.controller._deadline_timer.isActive())

    def test_shutdown_without_a_task_is_harmless(self):
        self.controller.shutdown()
        self.assertIsNone(self.controller._task)
        self.assertIsNone(self.controller._dialog)
        self.assertEqual(self.application.quit_calls, 0)


class SingleInstanceTests(unittest.TestCase):
    def test_only_one_local_server_claim_is_granted(self):
        first = main._claim_single_instance()
        second = main._claim_single_instance()
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(second)
        finally:
            if first is not None:
                first.close()


if __name__ == "__main__":
    unittest.main()
