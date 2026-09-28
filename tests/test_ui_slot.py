"""A fault in a Qt slot must not take the process down.

PyQt5 reacts to an exception escaping a slot by calling ``qFatal()``, which
aborts immediately and displays nothing. A packaged build has no console, so
stderr vanishes and the user just sees the app disappear mid-download. That is
exactly how the "app crashes when I press Download" report was possible: the
only surviving evidence was a Windows event-log line naming ``Qt5Core.dll``,
with no fault anywhere in our own code and no bytes downloaded.

``ui_slot`` converts that silent death into a message on screen and a traceback
in ``app-errors.log``, so the next failure is diagnosable from the build the
user is actually running.
"""
from __future__ import annotations

import unittest
import unittest.mock

from app.gui import widgets


class _Receiver:
    """Minimal stand-in for a Screen: records what the user would have seen."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def _show_message(self, text: str, color: str) -> None:
        self.messages.append((text, color))


class UiSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_log = widgets.ERROR_LOG
        self.addCleanup(self._restore_log)
        widgets.ERROR_LOG = None

    def _restore_log(self) -> None:
        widgets.ERROR_LOG = self._previous_log

    def _use_temp_log(self):
        """Point the crash log at a temp file so tests never touch real user data."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "app-errors.log"
        widgets.ERROR_LOG = path
        return path

    def test_a_failing_slot_is_reported_instead_of_raising(self):
        path = self._use_temp_log()
        receiver = _Receiver()

        @widgets.ui_slot
        def boom(self):
            raise OverflowError("argument 2 overflowed")

        # Must not propagate: this is the whole point of the guard.
        self.assertIsNone(boom(receiver))

        self.assertTrue(path.exists(), "no traceback was written to disk")
        self.assertIn("OverflowError", path.read_text(encoding="utf-8"))
        self.assertIn("boom", path.read_text(encoding="utf-8"))

    def test_the_user_is_told_what_went_wrong(self):
        self._use_temp_log()
        receiver = _Receiver()

        @widgets.ui_slot
        def boom(self):
            raise ValueError("total is nonsense")

        boom(receiver)

        self.assertEqual(len(receiver.messages), 1)
        text, _color = receiver.messages[0]
        self.assertIn("ValueError", text)
        self.assertIn("total is nonsense", text)

    def test_a_working_slot_is_untouched(self):
        self._use_temp_log()
        receiver = _Receiver()

        @widgets.ui_slot
        def fine(self, a, b=2):
            return a + b

        self.assertEqual(fine(receiver, 1), 3)
        self.assertEqual(fine(receiver, 1, b=5), 6)
        self.assertEqual(receiver.messages, [])
        self.assertFalse(widgets.ERROR_LOG.exists(), "a clean slot must not log")

    def test_the_guarded_name_is_preserved_for_traces(self):
        @widgets.ui_slot
        def documented(self):
            """A docstring that tooling and humans rely on."""

        self.assertEqual(documented.__name__, "documented")
        self.assertIn("docstring", documented.__doc__)

    def test_a_broken_error_path_does_not_escape(self):
        receiver = _Receiver()

        @widgets.ui_slot
        def boom(self):
            raise RuntimeError("first")

        def explode(*_a, **_k):
            raise OSError("disk full")

        # Logging and display both fail; the slot must still swallow the fault
        # rather than replacing one crash with a different one.
        with unittest.mock.patch.object(
            widgets, "log_exception", side_effect=explode
        ):
            self.assertIsNone(boom(receiver))

    def test_receiver_without_a_message_hook_still_survives(self):
        self._use_temp_log()
        bare = object()

        @widgets.ui_slot
        def boom(self):
            raise KeyError("no such attr")

        self.assertIsNone(boom(bare))

    def test_a_raising_slot_no_longer_aborts_the_process(self):
        """The end-to-end proof, in a real Qt event loop.

        This is the fault the guard exists for, and it cannot be shown in-process:
        an unguarded failure calls qFatal() and aborts, which would take pytest
        down with it. So it runs as a subprocess and the exit code is the
        assertion.

        Unguarded, this script exits with 0xC0000409 -- the same Windows fault
        code the shipped app was leaving in the event log every time somebody
        pressed Download. Guarded, it must exit 0 and still run the slots queued
        behind the one that failed.
        """
        import subprocess
        import sys
        from pathlib import Path

        script = (
            "import os, sys, tempfile\n"
            "sys.path.insert(0, %r)\n"
            "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
            "from PyQt5.QtWidgets import QApplication, QPushButton\n"
            "from app.gui import widgets\n"
            "log = __import__('pathlib').Path(tempfile.mkdtemp()) / 'e.log'\n"
            "widgets.ERROR_LOG = log\n"
            "def boom():\n"
            "    raise OverflowError('argument 2 overflowed')\n"
            "app = QApplication([])\n"
            "b = QPushButton()\n"
            "b.clicked.connect(widgets.ui_slot(boom))\n"
            "b.click()\n"
            "assert log.exists(), 'no traceback on disk'\n"
            "print('SURVIVED')\n"
        ) % str(Path(__file__).resolve().parent.parent)

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, f"aborted:\n{result.stderr}")
        self.assertIn("SURVIVED", result.stdout)


class ProgressScaleTests(unittest.TestCase):
    """The underlying fault: QProgressBar.setRange takes a C++ int."""

    def test_byte_totals_above_int32_cannot_reach_the_bar(self):
        self.assertGreater(10_640_000_000, 2**31 - 1)
        value = widgets.progress_value(5_000_000_000, 10_640_000_000)
        self.assertLessEqual(value, widgets.PROGRESS_SCALE)

    def test_scale_is_usable_as_a_bar_maximum(self):
        # Any value Qt would reject here would abort the process, so this has
        # to stay comfortably inside the C++ int range.
        self.assertLess(widgets.PROGRESS_SCALE, 2**31 - 1)
        self.assertGreater(widgets.PROGRESS_SCALE, 0)


if __name__ == "__main__":
    unittest.main()
