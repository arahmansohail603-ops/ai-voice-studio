"""GUI tests for the Voices screen bulk prefetch.

Fetching all 177 Piper voices is a ~11 GB, hours-long transfer, so the parts
worth pinning are the ones a long transfer gets wrong: that the button only
counts work that is genuinely left, that a second run touches nothing, that
stopping or closing keeps every voice already finished, and that the user is
asked once rather than having 11 GB spent on them silently.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import types
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from PyQt5.QtCore import QCoreApplication, QLibraryInfo
from PyQt5.QtWidgets import QApplication, QMessageBox

from app.core import piper_voices
from app.core.piper_voices import PiperVoiceLibrary

_PLATFORM_LIBRARIES = ("qoffscreen.dll", "libqoffscreen.so", "libqoffscreen.dylib")

_APPLICATION = None
_GUI = False


def _offscreen_available() -> bool:
    base = QLibraryInfo.location(QLibraryInfo.PluginsPath)
    platforms = Path(base) / "platforms"
    return any((platforms / name).exists() for name in _PLATFORM_LIBRARIES)


def setUpModule():
    global _APPLICATION, _GUI
    existing = QCoreApplication.instance()
    if existing is not None and not isinstance(existing, QApplication):
        return
    if not _offscreen_available():
        return
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APPLICATION = existing or QApplication(["voices-prefetch"])
    _GUI = True


def _pump(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while True:
        _APPLICATION.processEvents()
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _md5(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()


def _bulk_fixture() -> tuple[bytes, dict[str, bytes]]:
    """A catalog of four voices that each have a model and a config."""
    voices: dict = {}
    blobs: dict[str, bytes] = {}
    for index, family in enumerate(("ur", "hi", "cy", "en")):
        key = f"{family}_XX-v{index}-medium"
        model = f"model-{key}".encode() * 32
        config = json.dumps({"language": {"code": key}}).encode()
        model_path = f"{family}/{key}/medium/{key}.onnx"
        config_path = f"{family}/{key}/medium/{key}.onnx.json"
        blobs[model_path] = model
        blobs[config_path] = config
        voices[key] = {
            "key": key,
            "name": f"v{index}",
            "language": {
                "code": key,
                "family": family,
                "name_native": key,
                "name_english": family.upper(),
                "country_english": "Test",
            },
            "quality": "medium",
            "num_speakers": 1,
            "files": {
                model_path: {"size_bytes": len(model), "md5_digest": _md5(model)},
                config_path: {"size_bytes": len(config), "md5_digest": _md5(config)},
            },
        }
    return json.dumps(voices).encode("utf-8"), blobs


_CATALOG, _BLOBS = _bulk_fixture()


class _Server:
    def __init__(self) -> None:
        self._blobs = {"voices.json": _CATALOG, **_BLOBS}
        #: Every path served, so a test can prove a re-run hit nothing.
        self.requests: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                key = self.path.lstrip("/")
                server.requests.append(key)
                body = server._blobs.get(key)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class ProgressScaleTests(unittest.TestCase):
    """Regression: QProgressBar takes a C++ int, so a byte total over ~2.1 GB
    raised OverflowError *inside a Qt slot*, and PyQt5 turns that into
    qFatal() -> abort(). The whole app died the instant the user pressed
    "Download all", with no bytes written and no error shown. The full catalog
    is ~11 GB, so this is the normal case, not an edge case."""

    def test_progress_value_maps_bytes_onto_a_bounded_scale(self) -> None:
        from app.gui.widgets import PROGRESS_SCALE, progress_value

        self.assertEqual(progress_value(0, 10_640_000_000), 0)
        self.assertEqual(progress_value(10_640_000_000, 10_640_000_000), PROGRESS_SCALE)
        self.assertEqual(progress_value(5_320_000_000, 10_640_000_000), PROGRESS_SCALE // 2)

    def test_progress_value_survives_totals_far_past_the_int32_ceiling(self) -> None:
        from app.gui.widgets import PROGRESS_SCALE, progress_value

        huge = 2**31 - 1
        for total in (huge + 1, 10_640_000_000, 2**40):
            for received in (0, total // 3, total, total + 99):
                value = progress_value(received, total)
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, PROGRESS_SCALE)

    def test_progress_value_clamps_a_zero_or_negative_total(self) -> None:
        from app.gui.widgets import progress_value

        self.assertEqual(progress_value(500, 0), 0)
        self.assertEqual(progress_value(500, -1), 0)


class VoicesPrefetchTests(unittest.TestCase):
    server: ClassVar[_Server]

    @classmethod
    def setUpClass(cls):
        if not _GUI:
            raise unittest.SkipTest("an offscreen Qt application is required")
        cls.server = _Server()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        if not _GUI:
            self.skipTest("an offscreen Qt application is required")
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.voice_dir = Path(self._tmp.name)

        patches = (
            unittest.mock.patch.object(
                piper_voices, "VOICES_INDEX_URL", self.server.base + "voices.json"
            ),
            unittest.mock.patch.object(piper_voices, "_FILE_BASE", self.server.base),
            unittest.mock.patch.object(piper_voices, "PIPER_VOICE_DIR", self.voice_dir),
            unittest.mock.patch.object(piper_voices, "_RETRY_BACKOFF_SECONDS", (0.0,) * 3),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        self.library = PiperVoiceLibrary(self.voice_dir)
        self.entries = self.library.catalog()  # also writes the cached index
        self.window = types.SimpleNamespace(
            app=self,
            notify=lambda *a, **k: None,
            schedule=lambda fn, *a, **k: fn(*a, **k),
            toast=lambda *a, **k: None,
            set_busy=lambda *a, **k: None,
            show_screen=self._show_screen,
        )
        self.shown: list[tuple[str, dict]] = []

        from app.gui.screens.voices import VoicesScreen

        self.screen = VoicesScreen.__new__(VoicesScreen)
        VoicesScreen.__init__(self.screen, None, self.window)
        # __init__ builds its own library from the module default.
        self.screen._library = self.library
        self.screen._entries = list(self.entries)
        self.addCleanup(self._dispose)

    def _show_screen(self, key, **kwargs):
        self.shown.append((key, kwargs))

    def _dispose(self):
        try:
            self.screen.shutdown()
        except RuntimeError:
            pass
        try:
            self.screen.deleteLater()
            _APPLICATION.processEvents()
        except RuntimeError:
            pass

    # ------------------------------------------------------------ the button
    def test_the_button_counts_only_the_work_that_is_left(self):
        self.screen._update_bulk_button()
        self.assertTrue(self.screen.download_all_btn.isEnabled())
        self.assertIn("4 left", self.screen.download_all_btn.text())

    def test_the_button_disables_once_every_voice_is_installed(self):
        self.library.install_all(self.entries)
        self.screen._update_bulk_button()
        self.assertFalse(self.screen.download_all_btn.isEnabled())
        self.assertIn("Every voice", self.screen.download_all_btn.text())

    # ------------------------------------------------------- the actual run
    def test_download_all_installs_every_voice_through_the_screen(self):
        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.Yes
        ):
            self.screen._on_download_all()

        self.assertTrue(_pump(lambda: not self.screen.prefetching),
                        "the bulk download never finished")
        self.assertEqual(len(self.library.installed()), 4)
        self.assertIn("ready", self.screen.summary.text())

    def test_declining_the_prompt_starts_nothing(self):
        # What _on_catalog_loaded does when the list arrives.
        self.screen._update_bulk_button()
        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.No
        ):
            self.screen._on_download_all()

        _pump(lambda: True, timeout=0.2)
        self.assertFalse(self.screen.prefetching)
        self.assertEqual(self.library.installed(), set())
        self.assertTrue(self.screen.download_all_btn.isEnabled(),
                        "declining once must not remove the button for good")

    def test_a_second_run_downloads_nothing(self):
        self.library.install_all(self.entries)
        self.server.requests.clear()
        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.Yes
        ):
            self.screen._on_download_all()

        self.assertTrue(_pump(lambda: not self.screen.prefetching),
                        "the re-run never finished")
        self.assertEqual(self.server.requests, [],
                         "a completed prefetch went back to the network")
        self.assertEqual(len(self.library.installed()), len(self.entries))

    def test_stopping_leaves_the_finished_voices_in_place(self):
        self.screen._entries = []
        self.screen.start_prefetch()  # no entries: a no-op, not a crash
        self.assertFalse(self.screen.prefetching)
        self.assertEqual(self.library.installed(), set())

    def test_a_retry_is_visible_in_the_status_line(self):
        # Over a ~3 h transfer a stalled bar looks like a hung app. The stage has
        # to reach the screen, otherwise the user cannot tell whether to wait.
        self.screen._update_bulk_button()
        self.screen.start_prefetch()
        self.screen._on_bulk_progress(10, 100, "retrying", "en_US-lessac-medium")
        self.assertIn("retrying", self.screen.bulk_status.text())
        self.assertIn("en_US-lessac-medium", self.screen.bulk_status.text())

    def test_the_header_is_not_rewritten_on_every_chunk(self):
        self.screen._update_bulk_button()
        self.screen.start_prefetch()
        seen: list[str] = []
        original = self.screen._show_message

        def record(text, *args, **kwargs):
            seen.append(text)
            original(text, *args, **kwargs)

        self.screen._show_message = record
        for received in range(0, 100, 5):
            self.screen._on_bulk_progress(received, 100, "downloading", "voice-x")
        # An 11 GB run emits thousands of ticks; only the first may touch the
        # shared header, which the voice list and the other tabs also write to.
        self.assertLessEqual(len(seen), 2)

    def test_shutdown_stops_a_running_bulk_worker(self):
        stopped = types.SimpleNamespace(
            isRunning=lambda: True, stop=lambda: None, wait=lambda ms: None
        )
        calls: list[int] = []
        stopped.stop = lambda: calls.append(1)
        self.screen._bulk_worker = stopped
        self.screen.shutdown()
        self.assertEqual(calls, [1], "the worker was not asked to stop")
        self.screen._bulk_worker = None

    def test_shutdown_with_no_worker_is_safe(self):
        self.screen._bulk_worker = None
        self.screen.shutdown()

    # ------------------------------------------- the 11 GB aggregate progress
    def test_starting_a_full_catalog_prefetch_does_not_overflow_the_bar(self):
        # The exact line that killed the app: bulk_bar.setRange(0, <bytes>).
        from app.gui.widgets import PROGRESS_SCALE

        whole_catalog = 10_640_000_000
        self.assertGreater(whole_catalog, 2**31 - 1, "fixture no longer exceeds int32")
        with unittest.mock.patch.object(
            self.library, "remaining", return_value=(len(self.entries), whole_catalog)
        ):
            self.screen._update_bulk_button()
            self.screen.start_prefetch()  # used to raise OverflowError here
        self.assertEqual(self.screen.bulk_bar.maximum(), PROGRESS_SCALE)
        self.assertTrue(self.screen.prefetching)

    def test_bulk_progress_accepts_byte_counts_past_the_int32_ceiling(self):
        from app.gui.widgets import PROGRESS_SCALE

        self.screen._update_bulk_button()
        self.screen.start_prefetch()
        total = 10_640_000_000
        # Every one of these used to raise OverflowError from the slot.
        for received in (0, 1, 1_000_000_000, total // 2, total, total + 1_000):
            self.screen._on_bulk_progress(received, total, "downloading", "voice-x")
            self.assertEqual(self.screen.bulk_bar.maximum(), PROGRESS_SCALE)
            self.assertLessEqual(self.screen.bulk_bar.value(), PROGRESS_SCALE)
        self.assertEqual(self.screen.bulk_bar.value(), PROGRESS_SCALE)


class PrefetchOfferTests(unittest.TestCase):
    """The one-time first-run prompt."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.voice_dir = Path(self._tmp.name)
        self.marker = self.voice_dir / ".piper-prefetch-offered"
        patches = (
            unittest.mock.patch.object(
                piper_voices, "PIPER_VOICE_DIR", self.voice_dir
            ),
            unittest.mock.patch(
                "app.config.PIPER_PREFETCH_MARKER", self.marker
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.library = PiperVoiceLibrary(self.voice_dir)
        self.window = types.SimpleNamespace(
            show_screen=lambda key, **kw: self.shown.append((key, kw))
        )
        self.shown: list[tuple[str, dict]] = []

    def _cache_catalog(self) -> None:
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        (self.voice_dir / "voices.json").write_bytes(_CATALOG)

    def _offer(self):
        from app.gui.screens.voices import offer_bulk_prefetch

        offer_bulk_prefetch(self.window)

    def test_nothing_is_asked_before_the_catalog_is_cached(self):
        # No voices.json: the offer must stay silent rather than hit the network
        # on the startup path. The caller warms the cache in the background.
        with unittest.mock.patch.object(
            QMessageBox, "question", side_effect=AssertionError("must not ask")
        ):
            self._offer()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.shown, [])

    def test_the_helper_reports_whether_the_offer_was_already_made(self):
        from app.gui.screens.voices import prefetch_already_offered

        self.assertFalse(prefetch_already_offered())
        self.marker.write_text("offered\n", encoding="utf-8")
        self.assertTrue(prefetch_already_offered())

    def test_an_unreadable_marker_counts_as_already_asked(self):
        # A data folder we cannot stat must not turn into a prompt on every
        # single launch.
        from app.gui.screens.voices import prefetch_already_offered

        class _Denied:
            def exists(self):
                raise OSError("access denied")

        with unittest.mock.patch("app.config.PIPER_PREFETCH_MARKER", _Denied()):
            self.assertTrue(prefetch_already_offered())

    def test_the_marker_suppresses_a_second_prompt(self):
        self._cache_catalog()
        self.marker.write_text("offered\n", encoding="utf-8")
        with unittest.mock.patch.object(
            QMessageBox, "question", side_effect=AssertionError("must not ask")
        ):
            self._offer()
        self.assertEqual(self.shown, [])

    def test_yes_opens_the_voices_screen_and_starts_the_download(self):
        self._cache_catalog()
        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.Yes
        ):
            self._offer()
        self.assertEqual(self.shown, [("voices", {"prefetch": True})])
        self.assertTrue(self.marker.exists(), "the prompt would be shown again")

    def test_no_records_the_marker_so_it_is_not_asked_again(self):
        self._cache_catalog()
        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.No
        ):
            self._offer()
        self.assertEqual(self.shown, [])
        self.assertTrue(self.marker.exists())

    def test_a_fully_installed_library_is_never_offered(self):
        self._cache_catalog()
        # Written straight to disk: this case is about the *offer*, and going
        # through a download would need the catalog pointed at a local server.
        for path, payload in _BLOBS.items():
            (self.voice_dir / Path(path).name).write_bytes(payload)
        self.assertEqual(len(self.library.installed()), 4)
        with unittest.mock.patch.object(
            QMessageBox, "question", side_effect=AssertionError("must not ask")
        ):
            self._offer()
        self.assertEqual(self.shown, [])


class StartupOfferTests(unittest.TestCase):
    """The wiring in app.py that makes the offer actually reach a fresh install."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.voice_dir = Path(self._tmp.name)
        self.marker = self.voice_dir / ".piper-prefetch-offered"
        self._patch = (
            unittest.mock.patch.object(
                piper_voices, "PIPER_VOICE_DIR", self.voice_dir
            ),
            unittest.mock.patch.object(
                piper_voices, "VOICES_INDEX_URL", "http://127.0.0.1:1/voices.json"
            ),
            unittest.mock.patch("app.config.PIPER_PREFETCH_MARKER", self.marker),
        )
        for patch in self._patch:
            patch.start()
            self.addCleanup(patch.stop)

        from app.gui.app import VoiceStudioApp

        # Built by hand: the real constructor builds every screen, wires a
        # recorder and a player, and needs a license. What is under test is
        # _offer_voice_prefetch, which touches none of that.
        self.app = VoiceStudioApp.__new__(VoiceStudioApp)
        self.app.runner = types.SimpleNamespace(run=self._run)
        self.asked: list[int] = []
        self.app.schedule = lambda fn, *a, **k: self.asked.append(1)

    def _run(self, coro):
        # The runner is an asyncio loop; drive the coroutine to completion
        # synchronously so the assertion does not need an event loop.
        asyncio.run(coro)

    def test_a_fresh_install_warms_the_catalog_then_asks_on_the_ui_thread(self):
        self.app._offer_voice_prefetch()
        # The catalog fetch is attempted off-thread...
        self.assertTrue((self.voice_dir).exists())
        # ...but the unreachable index means no question is queued, and crucially
        # no marker is written, so the next launch tries again.
        self.assertEqual(self.asked, [])
        self.assertFalse(self.marker.exists())

    def test_the_question_is_queued_through_schedule_not_shown_inline(self):
        calls: list[object] = []
        with unittest.mock.patch(
            "app.core.piper_voices.PiperVoiceLibrary.catalog",
            lambda self: calls.append(1) or [],
        ):
            self.app._offer_voice_prefetch()
        # A QMessageBox built on the runner's thread is undefined behaviour, so
        # the offer has to be handed to schedule() for the UI thread.
        self.assertEqual(calls, [1], "the catalog was never warmed")
        self.assertEqual(self.asked, [1], "the question was not marshalled")

    def test_nothing_happens_once_the_offer_was_already_made(self):
        self.marker.write_text("offered\n", encoding="utf-8")
        with unittest.mock.patch(
            "app.core.piper_voices.PiperVoiceLibrary.catalog",
            side_effect=AssertionError("must not hit the network"),
        ):
            self.app._offer_voice_prefetch()
        self.assertEqual(self.asked, [])

    def test_the_offer_is_only_started_once(self):
        with unittest.mock.patch(
            "app.core.piper_voices.PiperVoiceLibrary.catalog", lambda self: []
        ):
            self.app._offer_voice_prefetch()
            self.app._offer_voice_prefetch()
        self.assertEqual(self.asked, [1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
