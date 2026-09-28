"""Regressions for bugs the suite did not cover, all found auditing for delivery.

Each test here fails against the code as it was before its fix. They are grouped
by the area that broke.

The model tests deliberately avoid mocking the install path. They stage a real
zip whose size and SHA-256 match the catalog entry, so ``install()`` runs the
genuine verify/extract/swap code and only the one step under test is disturbed.
Mocking all of it would have let the original bug pass.
"""
from __future__ import annotations

import hashlib
import pathlib
import queue
import threading
import unittest
import unittest.mock
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from app import config
from app.core import model_catalog
from app.core import model_manager as model_manager_module
from app.core import translator
from app.core.errors import ModelError
from app.core.model_manager import DownloadCancelled, ModelManager
from app.core.stt_engine import STTEngine
from app.services import file_service


class UrduSourceDetectionTests(unittest.TestCase):
    """Urdu is written in the Arabic script and shares its Unicode block.

    Checking the block on its own claimed every Urdu sentence as Arabic, so the
    Translate screen looked for -- and with consent downloaded -- the ``ar``
    language package, then returned Arabic-model output for Urdu text with no
    error anywhere. For an app aimed at Urdu speakers that is a headline
    correctness failure, and it hid in testing because picking "Urdu" explicitly
    always took the right path.
    """

    def test_urdu_letters_are_not_read_as_arabic(self) -> None:
        # پ چ ژ ں ے ۓ -- letters Urdu uses and standard Arabic does not.
        for sample in ("آپ کیسے ہیں؟", "یہ ٹھیک ہے", "میرا نام راشد ہے۔"):
            with self.subTest(sample=sample):
                self.assertEqual(translator._guess_source(sample), "ur")

    def test_plain_arabic_is_still_arabic(self) -> None:
        for sample in ("مرحبا كيف حالك", "هذه ليست Urdu ٹیسٹ", "شكرا"):
            with self.subTest(sample=sample):
                self.assertEqual(translator._guess_source(sample), "ar")

    def test_other_scripts_are_unaffected(self) -> None:
        cases = {
            "नमस्ते दुनिया": "hi",
            "你好世界": "zh",
            "สวัสดีครับ": "th",
            "Привет мир": "ru",
        }
        for sample, expected in cases.items():
            with self.subTest(sample=sample):
                self.assertEqual(translator._guess_source(sample), expected)

    def test_urdu_and_arabic_are_both_real_packages(self) -> None:
        # Guards the premise of the test above: if "ur" were not actually
        # supported, routing to it would trade one failure for another.
        self.assertIn("ur", translator.SUPPORTED_CODES)
        self.assertIn("ar", translator.SUPPORTED_CODES)


class OutputRootTests(unittest.TestCase):
    """A folder that cannot be created must not take the app down with it.

    The old order assigned ``config.OUTPUT_DIR`` before ``mkdir`` could fail, so
    a rejected folder left every category pointing at a directory that did not
    exist. And the settings screen called it straight from a Qt slot, where an
    unhandled ``OSError`` makes PyQt5 call ``qFatal()`` and abort the process.
    """

    def setUp(self) -> None:
        self._root, self._subdirs = config.OUTPUT_DIR, config.OUTPUT_SUBDIRS
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        config.OUTPUT_DIR, config.OUTPUT_SUBDIRS = self._root, self._subdirs

    def test_a_rejected_root_leaves_the_previous_one_in_charge(self) -> None:
        with TemporaryDirectory() as directory:
            good = Path(directory) / "good"
            good.mkdir()
            file_service.set_output_root(good)
            self.assertEqual(config.OUTPUT_DIR, good.resolve())

            # A regular file where a directory component has to be.
            blocker = Path(directory) / "blocked"
            blocker.write_text("not a directory", encoding="utf-8")

            with self.assertRaises(OSError):
                file_service.set_output_root(blocker / "nested")

            self.assertEqual(
                config.OUTPUT_DIR,
                good.resolve(),
                "a failed folder change repointed output at a missing directory",
            )

    def test_a_good_root_creates_every_category(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "out"
            file_service.set_output_root(root)
            self.assertEqual(config.OUTPUT_DIR, root.resolve())
            for name in ("tts", "recordings", "transcripts", "voices", "clones"):
                self.assertTrue(
                    (root / name).is_dir(), f"category folder '{name}' was not created"
                )
                self.assertEqual(config.OUTPUT_SUBDIRS[name], root.resolve() / name)


class SpeechSessionLifecycleTests(unittest.TestCase):
    """Stop then Start must not leave the old recogniser running.

    Both loops read the shared stop event and queue by attribute, and a new
    session rebound both. Clearing the flag un-set the stop the previous
    recogniser was draining against, and rebinding the queue handed it the new
    session's audio, so it never exited: two recognisers split every phrase and
    the transcript came out duplicated and interleaved, leaking a thread per
    stop/start cycle.
    """

    def _engine(self) -> STTEngine:
        engine = STTEngine.__new__(STTEngine)
        engine._stop_event = threading.Event()
        engine._queue = queue.Queue()
        engine._thread = None
        engine._worker = None
        engine._noise_floor = 0.0
        engine.language = "en-US"
        return engine

    def test_each_session_gets_its_own_stop_event_and_queue(self) -> None:
        engine = self._engine()
        handed_out: list[tuple] = []

        def fake_thread(*_args, **kwargs):
            # ``args`` is passed as a keyword, so read it from there rather than
            # binding it positionally.
            handed_out.append(kwargs["args"])
            # Dead threads, so the second start is allowed to proceed.
            return unittest.mock.MagicMock(
                is_alive=unittest.mock.Mock(return_value=False)
            )

        noop = lambda *a, **k: None  # noqa: E731 - callbacks
        with unittest.mock.patch.object(engine, "check_microphone", return_value=True):
            with unittest.mock.patch("app.core.stt_engine.threading.Thread", fake_thread):
                self.assertTrue(engine.start_listening(noop, noop, noop))
                first = (engine._stop_event, engine._queue)
                self.assertTrue(engine.start_listening(noop, noop, noop))
                second = (engine._stop_event, engine._queue)

        self.assertIsNot(
            first[0], second[0], "a new session reused the previous stop event"
        )
        self.assertIsNot(first[1], second[1], "a new session reused the previous queue")

        # The threads must be handed those objects as arguments, not left to read
        # the attributes -- rebinding those is precisely what leaked the old
        # thread. (on_status, on_level, on_error, stop_event, audio_queue)
        for args in handed_out:
            self.assertIn((args[3], args[4]), (first, second))
        self.assertEqual(len(handed_out), 4, "expected two threads per session")

    def test_start_is_refused_while_the_recogniser_is_still_running(self) -> None:
        engine = self._engine()
        engine._thread = unittest.mock.MagicMock()
        engine._thread.is_alive.return_value = False  # capture loop already exited
        engine._worker = unittest.mock.MagicMock()
        engine._worker.is_alive.return_value = True  # recogniser still draining

        with unittest.mock.patch.object(engine, "check_microphone", return_value=True):
            started = engine.start_listening(
                lambda *a, **k: None, lambda *a, **k: None, lambda *a, **k: None
            )

        self.assertFalse(
            started,
            "a new session started while the previous recogniser was still alive",
        )

    def test_stop_signals_and_waits_for_both_threads(self) -> None:
        engine = self._engine()
        engine._thread = unittest.mock.MagicMock()
        engine._worker = unittest.mock.MagicMock()
        engine._thread.is_alive.return_value = True
        engine._worker.is_alive.return_value = True

        engine.stop_listening()

        self.assertTrue(engine._stop_event.is_set())
        engine._thread.join.assert_called_once()
        engine._worker.join.assert_called_once()

    def test_stop_does_not_hang_on_a_thread_that_already_died(self) -> None:
        engine = self._engine()
        engine._thread = unittest.mock.MagicMock()
        engine._worker = unittest.mock.MagicMock()
        engine._thread.is_alive.return_value = False
        engine._worker.is_alive.return_value = False

        engine.stop_listening()

        self.assertTrue(engine._stop_event.is_set())
        engine._thread.join.assert_not_called()
        engine._worker.join.assert_not_called()


class _ModelTestBase(unittest.TestCase):
    """Shared fixture: a manager and a catalog entry with a genuinely valid zip."""

    INSTALL_DIR = "spec-1"
    BACKUP_DIR = ".spec-1.previous"

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.manager = ModelManager(
            models_root=self.root / "models", staging_dir=self.root / "staging"
        )

    def _build_spec(
        self, version: str = "2.0", payload: bytes = b"NEW"
    ) -> model_catalog.ModelSpec:
        """Write a real zip and return a spec that matches it byte for byte.

        Staging this means ``install()`` takes the "previously downloaded archive"
        path: no network, and the real ``_verify_archive``/``_install_from_archive``
        code still runs.
        """
        self.manager.staging_dir.mkdir(parents=True, exist_ok=True)
        archive = self.manager.staging_dir / "spec-1.archive"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            # A single top-level directory, which is what strip_single_root
            # expects a published model to look like.
            handle.writestr(f"{self.INSTALL_DIR}/model.bin", payload)
        blob = archive.read_bytes()
        spec = model_catalog.ModelSpec(
            id="spec-1",
            kind="stt",
            name="Spec One",
            version=version,
            install_dir=self.INSTALL_DIR,
            engine="vosk",
            languages=("en",),
            assets=(
                model_catalog.ModelAsset(
                    name="model.zip",
                    url="https://example.test/model.zip",
                    size_bytes=len(blob),
                    sha256=hashlib.sha256(blob).hexdigest(),
                ),
            ),
            archive_sha256=hashlib.sha256(blob).hexdigest(),
            marker_files=("model.bin",),
        )
        patcher = unittest.mock.patch.object(self.manager, "spec", return_value=spec)
        patcher.start()
        self.addCleanup(patcher.stop)
        return spec

    def _install_existing(self, payload: bytes, version: str) -> Path:
        """Pretend an older version is already installed."""
        target = self.root / "models" / self.INSTALL_DIR
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.bin").write_bytes(payload)
        (target / ".installed.json").write_text(
            f'{{"id": "spec-1", "version": "{version}"}}', encoding="utf-8"
        )
        return target

    @staticmethod
    def _break_replace(*, mode: str) -> object:
        """Fail one specific rename in the swap, leaving the others working.

        The swap renames exactly three times, in order: the old tree to
        ``.spec-1.previous``, the staged tree into place, and -- only if that
        second one failed -- the backup back into place. Selecting by call number
        reaches the rollback and "folder in use" branches without relying on real
        filesystem locks, which behave differently on OneDrive. Failing by target
        *name* instead would also break the rollback, since the restore reuses the
        same destination name as the step it is undoing.
        """
        if mode not in ("backup", "placement"):
            raise ValueError(f"unknown mode {mode!r}")
        fail_on = 1 if mode == "backup" else 2
        real_replace = pathlib.Path.replace
        calls = 0

        def replace(self, target, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == fail_on:
                raise OSError("simulated: file is open by another process")
            return real_replace(self, target, *args, **kwargs)

        return unittest.mock.patch.object(pathlib.Path, "replace", replace)


class CancelLatchTests(_ModelTestBase):
    """Cancel is a latch on a shared manager, so a later attempt must clear it.

    Nothing called ``reset_cancel``, so pressing Cancel once made every
    subsequent install -- any model, for the rest of the session -- raise
    ``DownloadCancelled`` before fetching a single byte. The only recovery was
    restarting the app.
    """

    def test_a_cancelled_install_does_not_poison_the_next_one(self) -> None:
        self.manager.cancel()
        self.assertTrue(self.manager.is_cancelling)

        spec = self._build_spec()
        result = self.manager.install("spec-1", force=True)

        self.assertFalse(self.manager.is_cancelling)
        self.assertTrue(result.installed)
        self.assertEqual((result.install_path / "model.bin").read_bytes(), b"NEW")
        self.assertEqual(spec.version, "2.0")

    def test_cancelling_mid_download_still_stops_that_attempt(self) -> None:
        """Clearing the latch must not disarm a cancel for the download in flight.

        The clear only runs once the previous attempt released the download lock,
        so a cancel pressed while downloading still has to take effect.
        """
        self._build_spec()
        real_archive = self.manager.staging_dir / "spec-1.archive"
        payload = real_archive.read_bytes()

        def download_then_cancel(*_args, **_kwargs):
            # The user hits Cancel while bytes are still arriving.
            self.manager.cancel()
            real_archive.write_bytes(payload)
            return real_archive

        with unittest.mock.patch.object(
            self.manager, "_archive_is_valid", return_value=False
        ), unittest.mock.patch.object(
            self.manager, "_download_archive", side_effect=download_then_cancel
        ):
            with self.assertRaises(DownloadCancelled):
                self.manager.install("spec-1", force=True)

        self.assertFalse(
            (self.root / "models" / self.INSTALL_DIR).exists(),
            "a cancelled download was installed anyway",
        )


class ModelSwapTests(_ModelTestBase):
    """Updating a model must not end with the new one buried inside the old.

    The install removed the old tree first, and that removal swallows its own
    errors. When the old tree survived -- a read-only file, an open handle, a
    OneDrive folder -- ``replace()`` raised and the fallback ``shutil.move()``
    treated the existing target as a directory, moving the freshly written model
    *inside* it. That reported success, the engine kept loading the stale
    top-level files, and the old version marker made the screen report the model
    as up to date forever.
    """

    def test_an_undeletable_old_tree_does_not_swallow_the_new_model(self) -> None:
        target = self._install_existing(b"OLD", "1.0")

        self._build_spec(payload=b"NEW-CONTENT")
        with unittest.mock.patch.object(
            model_manager_module, "_remove", lambda _path: None
        ):
            result = self.manager.install("spec-1", force=True)

        self.assertEqual(
            (result.install_path / "model.bin").read_bytes(),
            b"NEW-CONTENT",
            "the update reported success but the new model was buried inside the old",
        )
        self.assertEqual(
            sorted(result.install_path.glob("**/model.bin")),
            [result.install_path / "model.bin"],
            "the new model ended up nested rather than replacing the old",
        )
        self.assertNotEqual(
            (target / "model.bin").read_bytes(),
            b"OLD",
            "the stale model is still what loads at runtime",
        )
        # The marker must describe the new version, or the screen never offers
        # the update again and the stale-files bug becomes invisible.
        marker = (result.install_path / ".installed.json").read_text(encoding="utf-8")
        self.assertIn('"version": "2.0"', marker)

    def test_a_successful_update_cleans_up_the_backup(self) -> None:
        self._install_existing(b"OLD", "1.0")

        self._build_spec(payload=b"NEW-CONTENT")
        self.manager.install("spec-1", force=True)

        self.assertEqual(
            sorted(p.name for p in (self.root / "models").iterdir()),
            [self.INSTALL_DIR],
            "a leftover .previous directory would be re-scanned as a model",
        )

    def test_a_failed_swap_restores_the_previous_model(self) -> None:
        """Losing a working model because an update failed is worse than never
        offering the update."""
        target = self._install_existing(b"WORKING", "1.0")

        # The backup rename succeeds, then placing the new tree fails both ways:
        # the cross-volume copy fallback has to fail too, or it quietly rescues
        # the install and the rollback never runs.
        self._build_spec(payload=b"NEW-CONTENT")
        with self._break_replace(mode="placement"), unittest.mock.patch.object(
            model_manager_module.shutil, "move", side_effect=OSError("no space left")
        ):
            with self.assertRaises(OSError):
                self.manager.install("spec-1", force=True)

        self.assertTrue(target.is_dir(), "the working model was destroyed")
        self.assertEqual(
            (target / "model.bin").read_bytes(), b"WORKING", "the wrong model came back"
        )
        self.assertEqual(
            sorted(p.name for p in (self.root / "models").iterdir()),
            [self.INSTALL_DIR],
            "a failed update left a stray backup or an empty target behind",
        )

    def test_a_locked_model_reports_a_clear_error_instead_of_corrupting(self) -> None:
        target = self._install_existing(b"WORKING", "1.0")

        # The model is in use, so even the backup rename is refused.
        self._build_spec(payload=b"NEW-CONTENT")
        with self._break_replace(mode="backup"):
            with self.assertRaises(ModelError) as caught:
                self.manager.install("spec-1", force=True)

        message = str(caught.exception)
        self.assertIn("in use", message)
        self.assertIn("try again", message)
        self.assertEqual(
            (target / "model.bin").read_bytes(),
            b"WORKING",
            "a refused update damaged the installed model",
        )
        self.assertFalse(
            (self.root / "models" / self.BACKUP_DIR).exists(),
            "a refused update left the backup behind",
        )


if __name__ == "__main__":
    unittest.main()
