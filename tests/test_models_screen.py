"""GUI tests for the Models screen.

The regression this file exists for: ``_Row.set_installed`` called a
``BusyButton.is_busy()`` method that never existed. The resulting
``AttributeError`` was raised inside a queued Qt slot, which aborts the whole
process instead of showing a message, so a perfectly good download killed the
app. These tests drive a real download through the screen and assert the row
and summary end up correct, which fails loudly if that breaks again.
"""
from __future__ import annotations

import hashlib
import io
import os
import random
import threading
import time
import types
import unittest
import unittest.mock
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from PyQt5.QtCore import QCoreApplication, QLibraryInfo, QTimer
from PyQt5.QtWidgets import QApplication, QMessageBox

from app.core.errors import AppError

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
    _APPLICATION = existing or QApplication(["models-screen"])
    _GUI = True


def _pump(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while True:
        _APPLICATION.processEvents()
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _build_archive() -> bytes:
    """A model-shaped zip. The payload is deliberately incompressible so the
    extractor's zip-bomb ratio guard does not reject the fixture."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, seed in (("model/am/final.mdl", 1), ("model/graph/words.fst", 2)):
            zf.writestr(name, random.Random(seed).randbytes(120000))
    return buffer.getvalue()


class _Fixture:
    """A local HTTP server plus a signed-free catalog pointing at it."""

    def __init__(self) -> None:
        self._archive = _build_archive()
        self._half = len(self._archive) // 2
        self._blobs = {
            "part0.zip": self._archive[: self._half],
            "part1.zip": self._archive[self._half :],
        }
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                name = self.path.rsplit("/", 1)[-1]
                body = fixture._blobs.get(name)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.digest = hashlib.sha256(self._archive).hexdigest()

    def spec(self) -> dict:
        assets = []
        for index, (name, blob) in enumerate(sorted(self._blobs.items())):
            assets.append(
                {
                    "name": name,
                    "url": f"https://github.com/example/models/{name}",
                    "size_bytes": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "part": index,
                }
            )
        return {
            "id": "stt-vosk-en",
            "kind": "stt",
            "name": "Vosk English small",
            "version": "0.15",
            "engine": "vosk",
            "languages": ["en-US"],
            "install_dir": "vosk-models/small_en-us",
            "marker_files": ["am", "graph"],
            "size_bytes": len(self._archive),
            "archive": {"format": "zip", "sha256": self.digest},
            "assets": assets,
        }

    def point_at_local_server(self, manager) -> None:
        real_open = manager._open

        def local_open(request, timeout):
            if "github.com/" in request.full_url:
                tail = request.full_url.split("github.com/", 1)[-1]
                request.full_url = f"{self.base}/{tail}"
            return real_open(request, timeout)

        manager._open = local_open

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class ModelsScreenTests(unittest.TestCase):
    fixture: ClassVar[_Fixture]

    @classmethod
    def setUpClass(cls):
        if not _GUI:
            raise unittest.SkipTest("an offscreen Qt application is required")
        cls.fixture = _Fixture()
        cls.addClassCleanup(cls.fixture.close)

    def setUp(self):
        if not _GUI:
            self.skipTest("an offscreen Qt application is required")
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        from app.core.model_catalog import parse_catalog
        from app.core.model_manager import ModelManager
        from app.gui.screens.models import ModelsScreen

        self.manager = ModelManager(self.root / "models", self.root / "staging")
        self.manager.set_catalog(
            parse_catalog(
                {
                    "type": "model_catalog",
                    "version": 1,
                    "generated_at": "2026-01-01T00:00:00Z",
                    "models": [self.fixture.spec()],
                }
            )
        )
        self.fixture.point_at_local_server(self.manager)

        self.window = types.SimpleNamespace(
            app=self,
            notify=lambda *a, **k: None,
            schedule=lambda fn, *a, **k: fn(*a, **k),
            toast=lambda *a, **k: None,
            set_busy=lambda *a, **k: None,
            show_screen=lambda *a, **k: None,
        )
        self.screen = ModelsScreen.__new__(ModelsScreen)
        ModelsScreen.__init__(self.screen, None, self.window)
        # __init__ resets the manager, so inject the fixture after building.
        self.screen._manager = self.manager
        self.addCleanup(self._dispose)

    def _dispose(self):
        for worker in list(getattr(self.screen, "_workers", {}).values()):
            try:
                self.manager.cancel()
                worker.wait(5000)
            except RuntimeError:
                pass
        try:
            self.screen.deleteLater()
            _APPLICATION.processEvents()
        except RuntimeError:
            pass

    # ----------------------------------------------------------------- tests
    def test_row_starts_not_installed(self):
        self.screen._rebuild()
        row = self.screen._rows["stt-vosk-en"]
        self.assertIn("Not installed", row.state_lbl.text())
        self.assertFalse(row.delete_btn.isVisible())
        self.assertFalse(row.progress.isVisible())

    def test_a_multi_gigabyte_model_does_not_overflow_the_progress_bar(self):
        # QProgressBar.setRange takes a C++ int, so a model over ~2.1 GB raised
        # OverflowError inside this slot and PyQt5 aborted the whole process.
        # A 4 GB XTTS/Qwen-sized model is a routine download, not an edge case.
        from app.gui.widgets import PROGRESS_SCALE

        self.screen._rebuild()
        row = self.screen._rows["stt-vosk-en"]
        total = 4_300_000_000
        self.assertGreater(total, 2**31 - 1, "fixture no longer exceeds int32")
        for received in (0, 1, total // 2, total, total + 1):
            row.show_progress("downloading", received, total, "half way")
            self.assertEqual(row.progress.maximum(), PROGRESS_SCALE)
            self.assertLessEqual(row.progress.value(), PROGRESS_SCALE)
        self.assertEqual(row.progress.value(), PROGRESS_SCALE)
        self.assertEqual(row.progress.format(), "100%")

    def test_download_through_the_screen_installs_and_updates_the_row(self):
        self.screen._rebuild()
        row = self.screen._rows["stt-vosk-en"]

        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.No
        ):
            self.screen._on_download("stt-vosk-en")

        self.assertIn("stt-vosk-en", self.screen._workers)
        finished = _pump(lambda: not self.screen._workers)
        self.assertTrue(finished, "the download never finished")

        self.assertTrue(self.manager.is_installed("stt-vosk-en"))
        self.assertIn("Installed", row.state_lbl.text())
        self.assertIn("Vosk English small is ready.", self.screen.summary.text())
        # The row must be back to a usable state, not stuck in busy mode.
        self.assertFalse(row.action_btn.busy)
        self.assertFalse(row.progress.isVisible())
        self.assertFalse(row.cancel_btn.isVisible())
        self.assertTrue(row.delete_btn.isVisibleTo(row.parentWidget()))

    def test_rebuild_after_install_reports_installed(self):
        result = self.manager.install("stt-vosk-en", lambda *a: None)
        self.assertTrue(result.installed)
        self.screen._rebuild()
        row = self.screen._rows["stt-vosk-en"]
        self.assertIn("Installed", row.state_lbl.text())
        self.assertIn("1 model(s) installed.", self.screen.summary.text())

    def test_install_failure_shows_the_message_and_keeps_the_row_usable(self):
        broken = self.fixture.spec()
        broken["assets"] = [dict(broken["assets"][0])]
        broken["assets"][0]["sha256"] = "00" * 32
        broken["id"] = "stt-broken"
        from app.core.model_catalog import parse_catalog

        self.manager.set_catalog(
            parse_catalog(
                {
                    "type": "model_catalog",
                    "version": 1,
                    "generated_at": "2026-01-01T00:00:00Z",
                    "models": [self.fixture.spec(), broken],
                }
            )
        )
        self.screen._rebuild()

        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.No
        ):
            self.screen._on_download("stt-broken")
        self.assertTrue(_pump(lambda: "stt-broken" not in self.screen._workers))

        row = self.screen._rows["stt-broken"]
        self.assertNotIn("Installed", row.state_lbl.text())
        self.assertFalse(row.action_btn.busy)
        self.assertFalse(row.progress.isVisible())
        self.assertFalse(self.manager.is_installed("stt-broken"))

    def test_empty_catalog_shows_the_hint(self):
        from app.core.model_catalog import parse_catalog

        self.manager.set_catalog(
            parse_catalog(
                {
                    "type": "model_catalog",
                    "version": 1,
                    "generated_at": "2026-01-01T00:00:00Z",
                    "models": [],
                }
            )
        )
        self.screen._rebuild()
        self.assertTrue(self.screen.empty.isVisibleTo(self.screen))
        self.assertEqual(self.screen._rows, {})

    def test_refresh_does_not_block_the_gui_thread(self):
        """A slow catalog fetch must not stop the event loop from turning."""
        release = threading.Event()
        started = threading.Event()

        def slow_refresh():
            started.set()
            release.wait(10.0)
            return self.manager.catalog

        self.manager.refresh = slow_refresh

        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        try:
            self.screen._on_refresh()
            # _on_refresh must return before the (blocked) fetch finishes.
            self.assertTrue(started.wait(5.0), "the worker never started")
            self.assertFalse(self.screen.refresh_btn.isEnabled())
            self.assertIsNotNone(self.screen._refresh_worker)

            deadline = time.monotonic() + 2.0
            while len(ticks) < 5 and time.monotonic() < deadline:
                _APPLICATION.processEvents()
            self.assertGreaterEqual(
                len(ticks), 5, "the GUI thread stalled during the refresh"
            )

            release.set()
            self.assertTrue(
                _pump(lambda: self.screen._refresh_worker is None),
                "the refresh worker never reported back",
            )
        finally:
            timer.stop()
            release.set()
        self.assertTrue(self.screen.refresh_btn.isEnabled())
        self.assertIn("Catalog updated", self.screen.summary.text())

    def test_refresh_failure_is_reported_and_re_enables_the_button(self):
        def boom():
            raise AppError("no internet")

        self.manager.refresh = boom
        self.screen._on_refresh()
        self.assertTrue(
            _pump(lambda: self.screen._refresh_worker is None),
            "the failing refresh never reported back",
        )
        self.assertTrue(self.screen.refresh_btn.isEnabled())
        self.assertIn("no internet", self.screen.summary.text())

    # ---------------------------------------------------------------- updates
    def _publish_version(self, version: str):
        """Re-point the manager at the same model published at ``version``."""
        from app.core.model_catalog import parse_catalog

        spec = self.fixture.spec()
        spec["version"] = version
        self.manager.set_catalog(
            parse_catalog(
                {
                    "type": "model_catalog",
                    "version": 1,
                    "generated_at": "2026-01-01T00:00:00Z",
                    "models": [spec],
                }
            )
        )
        return self.manager.catalog.require("stt-vosk-en")

    def test_a_new_catalog_version_is_reported_as_an_update(self):
        result = self.manager.install("stt-vosk-en", lambda *a: None)
        self.assertTrue(result.installed)
        self.screen._rebuild()
        row = self.screen._rows["stt-vosk-en"]
        self.assertIn("Installed", row.state_lbl.text())
        self.assertEqual(row.action_btn.text(), "Re-download")

        self._publish_version("0.16")
        self.screen._rebuild()
        row = self.screen._rows["stt-vosk-en"]
        self.assertIn("Update available", row.state_lbl.text())
        self.assertIn("v0.16", row.state_lbl.text())
        self.assertIn("installed v0.15", row.state_lbl.text())
        self.assertEqual(row.action_btn.text(), "Update")
        self.assertIn("1 update(s) available.", self.screen.summary.text())
        self.assertGreater(
            self.manager.catalog.total_download_bytes(self.manager.models_root), 0
        )

    def test_up_to_date_download_is_a_no_op(self):
        self.manager.install("stt-vosk-en", lambda *a: None)
        self.screen._rebuild()
        row = self.screen._rows["stt-vosk-en"]
        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.No
        ):
            self.screen._on_download("stt-vosk-en")
        # Declining the re-download prompt must not start anything.
        self.assertEqual(self.screen._workers, {})
        self.assertEqual(row.action_btn.text(), "Re-download")
        self.assertIn("up to date", self.screen.summary.text())

    def test_updating_replaces_the_installed_files(self):
        self.manager.install("stt-vosk-en", lambda *a: None)
        self._publish_version("0.16")
        self.screen._rebuild()
        row = self.screen._rows["stt-vosk-en"]
        self.assertEqual(row.action_btn.text(), "Update")

        with unittest.mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.Yes
        ):
            self.screen._on_download("stt-vosk-en")
        self.assertIn("stt-vosk-en", self.screen._workers)
        self.assertTrue(_pump(lambda: not self.screen._workers))

        spec = self.manager.catalog.require("stt-vosk-en")
        self.assertEqual(spec.installed_version(self.manager.models_root), "0.16")
        self.assertEqual(spec.update_state(self.manager.models_root), "current")
        self.assertIn("Installed", row.state_lbl.text())
        self.assertEqual(row.action_btn.text(), "Re-download")
        # No updates are left once the newer version is on disk.
        self.screen._rebuild()
        self.assertNotIn("update(s) available", self.screen.summary.text())


if __name__ == "__main__":
    unittest.main()
