import os
import threading
import time
import types
import unittest
from unittest.mock import patch

from PyQt5.QtCore import QCoreApplication, QLibraryInfo
from PyQt5.QtWidgets import QApplication, QDialog

import main

_PLATFORM_LIBRARIES = ("qoffscreen.dll", "libqoffscreen.so", "libqoffscreen.dylib")

_APPLICATION = None
_GUI = False


def _offscreen_available() -> bool:
    base = QLibraryInfo.location(QLibraryInfo.PluginsPath)
    if not os.path.isdir(base):
        return False
    platforms = os.path.join(base, "platforms")
    return any(
        os.path.exists(os.path.join(platforms, name)) for name in _PLATFORM_LIBRARIES
    )


def setUpModule():
    global _APPLICATION, _GUI
    existing = QCoreApplication.instance()
    if existing is not None and not isinstance(existing, QApplication):
        return
    if not _offscreen_available():
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APPLICATION = existing or QApplication(["license-dialog"])
    _GUI = True


def _pump(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        _APPLICATION.processEvents()
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _lease_state():
    return {"envelope": {"payload": {"expires_at": "2030-01-01T00:00:00+00:00"}}}


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


def _manager(state=None, gate=None, started=None):
    def activate(key, metadata=None):
        if started is not None:
            started.set()
        if gate is not None:
            gate.hold()
        if isinstance(state, Exception):
            raise state
        return _lease_state() if state is None else state

    return types.SimpleNamespace(activate=activate)


@unittest.skipUnless(main.QThread is not None, "PyQt5 is required")
class ActivationDialogTests(unittest.TestCase):
    def setUp(self):
        if not _GUI:
            self.skipTest("an offscreen Qt application is required")
        timeout = patch.object(main.config, "LICENSE_REQUEST_TIMEOUT", 0.05)
        timeout.start()
        self.addCleanup(timeout.stop)

    def _dialog(self, manager):
        dialog = main._ActivationDialog(manager, "expired")
        self.addCleanup(self._dispose, dialog)
        return dialog

    def _dispose(self, dialog):
        if dialog.task_active:
            dialog.stop_task(main._stop_timeout_ms())
        dialog.deleteLater()
        QApplication.instance().processEvents()

    def _blocking_dialog(self, state=None):
        gate = _Gate()
        started = threading.Event()
        dialog = self._dialog(_manager(state=state, gate=gate, started=started))
        self.addCleanup(gate.release)
        dialog.key_edit.setText("LIC-1")
        dialog._activate()
        self.assertTrue(started.wait(5))
        return dialog, gate

    def _settle(self, dialog, timeout=5.0):
        self.assertTrue(_pump(lambda: dialog._task is None, timeout))
        _APPLICATION.processEvents()

    def test_activation_thread_is_tracked_until_it_finishes(self):
        dialog, gate = self._blocking_dialog()
        self.assertIsNotNone(dialog._task)
        self.assertTrue(dialog.task_active)
        self.assertEqual(dialog._revision, 1)
        gate.release()
        self._settle(dialog)
        self.assertIsNone(dialog._task)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertIsInstance(dialog.state, dict)

    def test_dialog_cannot_be_rejected_while_a_task_runs(self):
        dialog, gate = self._blocking_dialog(state=ValueError("key is not valid"))
        dialog.show()
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        gate.release()
        self._settle(dialog)
        self.assertEqual(dialog.status.text(), "key is not valid")
        dialog.reject()
        self.assertFalse(dialog.isVisible())

    def test_successful_activation_is_accepted(self):
        state = _lease_state()
        dialog = self._dialog(_manager(state=state))
        dialog.key_edit.setText("LIC-1")
        dialog._activate()
        self._settle(dialog)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertIs(dialog.state, state)

    def test_invalid_activation_result_is_reported(self):
        dialog = self._dialog(_manager(state={"envelope": {}}))
        dialog.key_edit.setText("LIC-1")
        dialog._activate()
        self._settle(dialog)
        self.assertIsNone(dialog.state)
        self.assertEqual(
            dialog.status.text(), "The license server returned an invalid lease"
        )
        self.assertTrue(dialog.activate_button.isEnabled())
        self.assertTrue(dialog.cancel_button.isEnabled())

    def test_failed_activation_reports_the_server_message(self):
        dialog = self._dialog(_manager(state=ValueError("key is not valid")))
        dialog.key_edit.setText("LIC-1")
        dialog._activate()
        self._settle(dialog)
        self.assertIsNone(dialog.state)
        self.assertEqual(dialog.status.text(), "key is not valid")
        self.assertTrue(dialog.activate_button.isEnabled())

    def test_stale_activation_result_is_ignored(self):
        state = _lease_state()
        dialog = self._dialog(_manager(state=state))
        dialog._revision = 4
        dialog._activation_finished(3, state)
        self.assertIsNone(dialog.state)
        self.assertNotEqual(dialog.result(), QDialog.Accepted)
        dialog._activation_failed(3, "stale failure")
        self.assertEqual(dialog.status.text(), "expired")
        self.assertIsNone(dialog._task)

    def test_stop_task_ends_the_activation_thread(self):
        dialog, gate = self._blocking_dialog()
        self.assertTrue(dialog.stop_task(main._stop_timeout_ms()))
        self.assertFalse(dialog.task_active)
        self.assertIsNone(dialog._task)
        gate.release()
        self.assertFalse(_pump(lambda: dialog.result() == QDialog.Accepted, 0.2))
        self.assertIsNone(dialog.state)
        dialog._activate()
        self.assertIsNone(dialog._task)

    def test_activation_is_ignored_after_the_dialog_is_closed(self):
        dialog, gate = self._blocking_dialog()
        dialog.stop_task(main._stop_timeout_ms())
        gate.release()
        dialog._activate()
        self.assertIsNone(dialog._task)
        self.assertEqual(dialog._revision, 1)


if __name__ == "__main__":
    unittest.main()
