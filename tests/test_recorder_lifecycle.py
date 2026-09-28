"""The Voice Recorder screen must not leave the microphone open.

The regression: ``on_hide`` stopped only its display timer and left the shared
recorder running. Because that recorder outlives the screen, clicking away --
or closing the window, which routes through ``on_hide`` too -- left PortAudio
streaming with nothing on screen able to stop it: the OS microphone indicator
stayed on and ``_buffers`` grew by 50 ms every block, roughly 635 MB an hour,
for the rest of the process. Closing the window did not help, so the only way
out was Task Manager. ``MyVoiceScreen.on_hide`` already stopped the same shared
recorder, so this was an oversight on one screen rather than a design choice.

A fake recorder stands in for the real one so the test needs no microphone and
no audio hardware.
"""
from __future__ import annotations

import os
import queue
import unittest
import unittest.mock

from PyQt5.QtCore import QCoreApplication, QLibraryInfo
from PyQt5.QtWidgets import QApplication, QWidget

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
    _APPLICATION = existing or QApplication(["recorder-lifecycle"])
    _GUI = True


class _FakeRecorder:
    """Records whether the screen let go of the microphone."""

    def __init__(self, recording: bool) -> None:
        self._recording = recording
        self.stop_calls = 0
        self.level_queue: queue.Queue = queue.Queue()

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_paused(self) -> bool:
        return False

    @property
    def capture(self):
        return None

    def start(self) -> None:
        self._recording = True

    def stop(self) -> None:
        self.stop_calls += 1
        self._recording = False

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def elapsed(self) -> float:
        return 0.0

    def check_microphone(self, raise_error: bool = False) -> None:
        return None

    def save(self, *args, **kwargs) -> None:
        pass


class _FakeApp:
    def __init__(self, recorder: _FakeRecorder) -> None:
        self.recorder = recorder
        self.player = None
        self.toasts: list[tuple[str, str]] = []

    def toast(self, message: str, kind: str = "info") -> None:
        self.toasts.append((message, kind))


class VoiceRecorderLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _GUI:
            self.skipTest("no offscreen Qt platform plugin available")

    def _screen(self, recording: bool):
        from app.gui.screens.voice_recorder import VoiceRecorderScreen

        recorder = _FakeRecorder(recording)
        app = _FakeApp(recorder)
        master = QWidget()
        self.addCleanup(master.deleteLater)
        screen = VoiceRecorderScreen(master, app)
        self.addCleanup(screen.deleteLater)
        return screen, recorder

    def test_leaving_the_screen_releases_the_microphone(self) -> None:
        screen, recorder = self._screen(recording=True)
        screen.on_hide()
        self.assertEqual(
            recorder.stop_calls,
            1,
            "on_hide left the shared recorder running: the mic stayed open and "
            "its buffers kept growing with no control left to stop it",
        )
        self.assertFalse(recorder.is_recording)

    def test_an_idle_recorder_is_left_alone(self) -> None:
        screen, recorder = self._screen(recording=False)
        screen.on_hide()
        self.assertEqual(recorder.stop_calls, 0)

    def test_buttons_are_reset_so_a_return_visit_is_usable(self) -> None:
        screen, recorder = self._screen(recording=True)
        screen.record_btn.set_busy(True, "● Recording…")
        screen.pause_btn.setEnabled(True)
        screen.stop_btn.setEnabled(True)

        screen.on_hide()

        self.assertFalse(screen.record_btn.busy)
        self.assertFalse(screen.pause_btn.isEnabled())
        self.assertFalse(
            screen.stop_btn.isEnabled(),
            "Stop stayed enabled with nothing recording, so the next press "
            "answered 'Nothing to stop.' instead of doing anything",
        )

    def test_hiding_twice_is_harmless(self) -> None:
        screen, recorder = self._screen(recording=True)
        screen.on_hide()
        screen.on_hide()
        self.assertEqual(recorder.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
