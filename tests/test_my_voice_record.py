"""Taking a reference voice sample must be a single button.

The reference flow asked for two clicks and a file decision: press "Record
Sample", watch a timer, press a separate "Stop", and if you had no sample file
to hand, work out which file to pick in a file dialog. On top of that, the
recording was rejected *after* the fact for being too short, so a 1-second take
wrote a file that was then thrown away with an error the user had to interpret.

The fixes, all covered here:

1. One button. It starts a take, and the take ends by itself at
   ``MAX_SAMPLE_SECONDS`` -- no Stop press, and it loads itself.
2. The button stays clickable while recording so a short take can still be
   ended by hand. ``BusyButton.set_busy(True)`` greys the button out, which
   would have made that impossible.
3. A take shorter than ``MIN_SAMPLE_SECONDS`` is refused before it is written,
   measured in real samples so it agrees with ``validate_reference_sample``.
4. "Voice cloning is switched off" is a permanent label, not a toast. As a
   toast it vanished in seconds, so a perfectly good sample appeared to do
   nothing -- which reads as a broken sample, not a disabled feature.

Only the collaborators each method touches are faked; the screen is built with
``__new__`` so these stay fast and independent of the full widget tree.
"""
from __future__ import annotations

import os
import queue
import types
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.voice_cloner import MAX_SAMPLE_SECONDS, MIN_SAMPLE_SECONDS

from PyQt5.QtCore import QCoreApplication, QLibraryInfo
from PyQt5.QtWidgets import QApplication

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
    _APPLICATION = existing or QApplication(["my-voice-record"])
    _GUI = True


class _FakeRecorder:
    """Records a fixed-length take and reports a fixed elapsed time."""

    def __init__(
        self,
        seconds: float = 10.0,
        samplerate: int = 44100,
        recording: bool = False,
    ):
        self.samplerate = samplerate
        self.channels = 1
        self.device = 1
        self._recording = recording
        self._seconds = seconds
        self.level_queue: queue.Queue = queue.Queue()
        self.saved: list[tuple[Path, str]] = []
        self.elapsed_value = seconds

    def start(self) -> None:
        self._recording = True

    def stop(self) -> None:
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def capture(self):
        frames = int(self._seconds * self.samplerate)
        return np.zeros((frames, self.channels), dtype=np.float32)

    def elapsed(self) -> float:
        return self.elapsed_value

    def save(self, path, fmt: str = "wav") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFFfake")
        self.saved.append((path, fmt))
        return path


class _FakeLabel:
    def __init__(self, text: str = ""):
        self._text = text
        self.styles: list[str] = []

    def setText(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text

    def setStyleSheet(self, style: str) -> None:
        self.styles.append(style)


class _FakeButton:
    def __init__(self):
        self.idle_text = "Record & Use"
        self.enabled = True

    def set_idle_text(self, text: str) -> None:
        self.idle_text = text
        self.enabled = True

    def set_busy(self, busy: bool, running_text: str | None = None) -> None:
        self.enabled = not busy

    def setEnabled(self, value: bool) -> None:
        self.enabled = value

    def isEnabled(self) -> bool:
        return self.enabled


class _FakeTimer:
    def __init__(self):
        self.interval = 80
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class _FakeSettings:
    def __init__(self, **values):
        self._values = values

    def get(self, section: str, key: str, default=None):
        return self._values.get(key, default)

    def set(self, section: str, key: str, value) -> None:
        self._values[key] = value


def _build_screen(recorder, *, enabled: bool = True, **extra):
    from app.gui.screens.my_voice import MyVoiceScreen

    screen = MyVoiceScreen.__new__(MyVoiceScreen)
    screen.app = types.SimpleNamespace(
        recorder=recorder,
        settings=_FakeSettings(enabled=enabled, consent=True, engine="xtts"),
        toasts=[],
    )
    screen.toast = lambda message, kind="info": screen.app.toasts.append(
        (message, kind)
    )
    screen.ref_record_btn = _FakeButton()
    screen.ref_timer = _FakeLabel("00:00.0")
    screen.consent_lbl = _FakeLabel("")
    screen.ref_lbl = _FakeLabel("No voice sample loaded yet.")
    screen.clone_status_lbl = _FakeLabel("Model not loaded.")
    screen.ref_meter = types.SimpleNamespace(pushes=[], value=99,
                                             setValue=lambda v: None)
    screen._ref_track_timer = _FakeTimer()
    screen._ref_tracking = False
    screen._ref_peak_seen = False
    screen._consent_var = types.SimpleNamespace(get=lambda: True)
    screen._generating = False
    screen.gen_text = types.SimpleNamespace(get=lambda: "")
    screen.ref_path = None
    screen.profile_path = None
    screen.create_profile_btn = _FakeButton()
    screen.accepted: list[Path] = []
    screen._accept_reference = lambda path: screen.accepted.append(Path(path))
    # Left unbound on purpose: the stop path is the thing under test, and
    # _ref_tick must reach the real method. _install_stop_probe wraps it when a
    # test needs to observe the auto-stop flag.
    screen.stop_calls: list[bool] = []
    for name, value in extra.items():
        setattr(screen, name, value)
    return screen


def _install_stop_probe(screen):
    """Wrap the real _stop_ref_record so the auto flag can be observed."""
    real = type(screen)._stop_ref_record.__get__(screen, type(screen))
    calls: list[bool] = []

    def probe(auto: bool = False):
        calls.append(auto)
        return real(auto)

    screen._stop_ref_record = probe
    screen.stop_calls = calls
    return screen


class SingleButtonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)

    def _patch_output(self):
        from app.gui.screens import my_voice

        return unittest.mock.patch.multiple(
            my_voice.file_service,
            category_dir=lambda name: self.out,
            timestamp_stem=lambda prefix, fmt="%Y%m%d_%H%M%S": f"{prefix}_test",
        )

    def test_a_take_stops_itself_at_the_maximum_and_loads(self):
        recorder = _FakeRecorder(seconds=MAX_SAMPLE_SECONDS, recording=True)
        recorder.elapsed_value = MAX_SAMPLE_SECONDS
        screen = _install_stop_probe(
            _build_screen(recorder, _ref_tracking=True, _ref_peak_seen=True)
        )

        with self._patch_output():
            screen._ref_tick()

        self.assertEqual(screen.stop_calls, [True])
        self.assertEqual(len(recorder.saved), 1)
        self.assertEqual(len(screen.accepted), 1)
        self.assertEqual(screen.accepted[0].suffix, ".wav")
        self.assertEqual(screen.ref_record_btn.idle_text, "Record & Use")

    def test_the_take_is_not_cut_short_before_the_maximum(self):
        recorder = _FakeRecorder(seconds=10.0)
        recorder.elapsed_value = 12.0
        screen = _build_screen(recorder, _ref_tracking=True)

        screen._ref_tick()

        self.assertEqual(screen.stop_calls, [])
        self.assertEqual(recorder.saved, [])

    def test_starting_a_take_keeps_the_button_clickable(self):
        """set_busy(True) would disable it, leaving no way to stop by hand."""
        recorder = _FakeRecorder(seconds=0.0)
        screen = _build_screen(recorder)

        screen._toggle_ref_record()

        self.assertEqual(screen.ref_record_btn.idle_text, "Stop & Use")
        self.assertTrue(screen.ref_record_btn.isEnabled())
        self.assertTrue(screen._ref_tracking)
        self.assertEqual(screen._ref_track_timer.started, 1)

    def test_stopping_restores_the_original_label(self):
        recorder = _FakeRecorder(seconds=10.0)
        screen = _build_screen(recorder, _ref_peak_seen=True)
        screen.ref_record_btn.set_idle_text("Stop & Use")

        with self._patch_output():
            screen._stop_ref_record()

        self.assertEqual(screen.ref_record_btn.idle_text, "Record & Use")
        self.assertEqual(len(screen.accepted), 1)

    def test_a_take_under_the_minimum_is_not_written(self):
        recorder = _FakeRecorder(seconds=MIN_SAMPLE_SECONDS - 1.0)
        screen = _build_screen(recorder, _ref_peak_seen=True)

        with self._patch_output():
            screen._stop_ref_record()

        self.assertEqual(recorder.saved, [])
        self.assertEqual(screen.accepted, [])
        self.assertTrue(any("Too short" in msg for msg, _ in screen.app.toasts))

    def test_a_silent_take_is_reported_and_not_written(self):
        recorder = _FakeRecorder(seconds=10.0)
        screen = _build_screen(recorder, _ref_peak_seen=False)

        with self._patch_output():
            screen._stop_ref_record()

        self.assertEqual(recorder.saved, [])
        self.assertTrue(
            any("no sound" in msg for msg, _ in screen.app.toasts),
            screen.app.toasts,
        )

    def test_the_countdown_warns_when_nothing_is_heard(self):
        recorder = _FakeRecorder(seconds=10.0)
        recorder.elapsed_value = MAX_SAMPLE_SECONDS - 1.5
        screen = _build_screen(recorder, _ref_tracking=True, _ref_peak_seen=False)

        screen._ref_tick()

        self.assertIn("No sound yet", screen.consent_lbl.text())
        self.assertEqual(screen.stop_calls, [])

    def test_the_countdown_is_positive_once_sound_arrives(self):
        recorder = _FakeRecorder(seconds=10.0)
        recorder.elapsed_value = MAX_SAMPLE_SECONDS - 1.5
        screen = _build_screen(recorder, _ref_tracking=True, _ref_peak_seen=True)

        screen._ref_tick()

        self.assertIn("left", screen.consent_lbl.text())
        self.assertNotIn("No sound", screen.consent_lbl.text())


class CloneDisabledNoticeTests(unittest.TestCase):
    def test_a_permanent_line_says_cloning_is_off(self):
        screen = _build_screen(_FakeRecorder(), enabled=False)

        screen._refresh_clone_state()

        self.assertIn("switched OFF", screen.clone_status_lbl.text())
        self.assertTrue(screen.clone_status_lbl.styles)

    def test_creating_a_profile_is_refused_while_cloning_is_off(self):
        screen = _build_screen(_FakeRecorder(), enabled=False)
        screen.ref_path = Path("sample.wav")

        screen._create_profile()

        self.assertIsNone(screen.profile_path)
        self.assertIn("switched OFF", screen.clone_status_lbl.text())
        self.assertTrue(any("switched off" in m for m, _ in screen.app.toasts))

    def test_loading_the_model_is_refused_while_cloning_is_off(self):
        screen = _build_screen(_FakeRecorder(), enabled=False)

        def explode(*args, **kwargs):
            raise AssertionError("the model must not be touched while disabled")

        screen.app.active_cloner = types.SimpleNamespace(
            state=None, error=None, load_background=explode
        )

        screen._load_clone_model()

        self.assertIn("switched OFF", screen.clone_status_lbl.text())

    def test_generating_says_the_result_will_be_a_system_voice(self):
        screen = _build_screen(_FakeRecorder(), enabled=False)

        screen._generate()

        self.assertIn("switched OFF", screen.clone_status_lbl.text())
        self.assertTrue(any("system voice" in m for m, _ in screen.app.toasts))


class SampleLimitTests(unittest.TestCase):
    def test_the_ui_and_the_validator_share_one_set_of_limits(self):
        from app.core.voice_cloner import validate_reference_sample

        self.assertEqual(MIN_SAMPLE_SECONDS, 3.0)
        self.assertEqual(MAX_SAMPLE_SECONDS, 40.0)
        self.assertEqual(
            validate_reference_sample.__defaults__, (3.0, 40.0)
        )

    def test_a_real_short_file_is_still_rejected_by_the_validator(self):
        from app.core.errors import CloneModelError
        from app.core.voice_cloner import validate_reference_sample

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.wav"
            from app.core import audio_utils

            audio_utils.save_recording(
                np.zeros((int(1.0 * 44100), 1), dtype=np.float32), 44100, path
            )
            with self.assertRaises(CloneModelError):
                validate_reference_sample(path)


class BusyButtonToggleTests(unittest.TestCase):
    """The label helper must not grey the button out, which set_busy does."""

    def setUp(self):
        if not _GUI:
            self.skipTest("an offscreen Qt application is required")
        from app.gui.widgets import BusyButton

        self.button = BusyButton(text="Record & Use")

    def test_idle_text_keeps_the_button_enabled(self):
        self.button.set_idle_text("Stop & Use")

        self.assertEqual(self.button.text(), "Stop & Use")
        self.assertTrue(self.button.isEnabled())

    def test_set_busy_still_disables_as_before(self):
        self.button.set_busy(True, "Recording…")

        self.assertFalse(self.button.isEnabled())

    def test_set_idle_text_updates_the_resting_label(self):
        self.button.set_idle_text("Stop & Use")
        self.button.set_busy(False)

        self.assertEqual(self.button.text(), "Stop & Use")
        self.assertTrue(self.button.isEnabled())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
