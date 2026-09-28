"""The native runtime load order must stay CTranslate2-before-Qt.

Windows binds a DLL by base name, so the first C++/OpenMP runtime loaded into
the process is the one every other native library inherits. Qt5 and
CTranslate2 ship different builds of that runtime, and with Qt imported first
the first Argos translation fails with ``[WinError 1114]`` -- on *both* the
Text to Speech and Speech to Text screens, because both share one translator.
Every package is installed and current, so nothing a user tries fixes it.

These tests pin the guard in ``app.core.native_runtime`` and, more importantly,
prove the end-to-end contract: translation still works after Qt is loaded.

The subprocess tests deliberately assert the *working* order rather than that
the bad order fails. If a future Qt or CTranslate2 release happens to make the
bad order legal, that is an improvement to record, not a regression to fail on.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

from app.core import native_runtime

_ROOT = Path(__file__).resolve().parent.parent

# Proves the real contract: preload, then Qt, then a real offline translation.
_TRANSLATE_AFTER_QT = """
import sys
sys.path.insert(0, {root!r})
from app.core.native_runtime import preload_native_runtime
assert preload_native_runtime(), "native runtime preload failed"
from PyQt5.QtWidgets import QApplication
app = QApplication([])
from app.core.translator import Translator
out = Translator("ur").translate("Hello, how are you today?")
assert out and out.strip(), "translation returned nothing"
print(out.strip())
"""

# The hazard this guard exists for. Never asserted to fail -- only reported.
_TRANSLATE_AFTER_QT_UNGUARDED = """
import sys
sys.path.insert(0, {root!r})
from PyQt5.QtWidgets import QApplication
app = QApplication([])
from app.core.translator import Translator
print(Translator("ur").translate("Hello, how are you today?").strip())
"""


def _urdu_available() -> bool:
    try:
        from app.core.translator import Translator
    except Exception:  # pragma: no cover - defensive
        return False
    try:
        return Translator.available() and "ur" in Translator.supported_codes()
    except Exception:  # pragma: no cover - defensive
        return False


def _run(script: str) -> subprocess.CompletedProcess:
    # The child inherits this process's stdout, which Windows binds to the
    # active ANSI code page (cp1252 here). The guarded script translates into
    # Urdu and prints it, so without this the child died on a
    # UnicodeEncodeError at the print -- after the translation had already
    # succeeded -- and the test reported a broken native-runtime guard that
    # was in fact working. ``main._force_utf8_streams()`` is the app's own
    # version of this fix; these scripts do not enter through ``main``, so they
    # need it applied for them.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-c", script.format(root=str(_ROOT))],
        capture_output=True,
        text=True,
        timeout=600,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


class PreloadNativeRuntimeTests(unittest.TestCase):
    """Unit-level behaviour of the guard itself."""

    def setUp(self) -> None:
        self._was_loaded = native_runtime.native_runtime_ready()

    def tearDown(self) -> None:
        native_runtime._loaded = self._was_loaded

    def test_preload_succeeds_and_is_idempotent(self) -> None:
        first = native_runtime.preload_native_runtime()
        self.assertTrue(first, "ctranslate2 is installed but the preload failed")
        self.assertTrue(native_runtime.native_runtime_ready())
        # Calling again must be free and must not change the answer.
        self.assertTrue(native_runtime.preload_native_runtime())

    def test_missing_module_is_a_no_op_not_a_crash(self) -> None:
        native_runtime._loaded = False
        with unittest.mock.patch.object(
            native_runtime, "soft_import_detail", return_value=(None, ImportError("x"))
        ):
            self.assertFalse(native_runtime.preload_native_runtime())
        self.assertFalse(native_runtime.native_runtime_ready())

    def test_dll_failure_is_swallowed_for_the_translator_to_report(self) -> None:
        """A broken install must still reach the user as ``DllLoadError``.

        The preload runs at import time, so raising here would replace a clear,
        actionable translation error with a bare traceback.
        """
        native_runtime._loaded = False
        boom = OSError(1114, "DLL initialization routine failed")
        with unittest.mock.patch.object(
            native_runtime, "soft_import_detail", return_value=(None, boom)
        ):
            self.assertFalse(native_runtime.preload_native_runtime())
        self.assertFalse(native_runtime.native_runtime_ready())

    def test_failing_warm_call_reports_not_ready(self) -> None:
        """Importing the extension is not enough; the runtime must really start."""
        native_runtime._loaded = False
        module = unittest.mock.Mock()
        module.get_supported_compute_types.side_effect = RuntimeError("nope")
        with unittest.mock.patch.object(
            native_runtime, "soft_import_detail", return_value=(module, None)
        ):
            self.assertFalse(native_runtime.preload_native_runtime())
        self.assertFalse(native_runtime.native_runtime_ready())

    def test_warm_tolerates_module_without_the_query(self) -> None:
        # ``object()`` has no such attribute; ``Mock(spec=[])`` models a module
        # that genuinely does not expose it. A bare ``Mock()`` would not -- it
        # invents every attribute on demand and would look like a success.
        self.assertFalse(native_runtime._warm_native_runtime(object()))
        self.assertFalse(
            native_runtime._warm_native_runtime(unittest.mock.Mock(spec=[]))
        )


class MainImportOrderTests(unittest.TestCase):
    """``main`` must establish the runtime before it imports PyQt5."""

    def test_importing_main_preloads_before_qt(self) -> None:
        script = """
import sys
sys.path.insert(0, {root!r})
import main
from app.core.native_runtime import native_runtime_ready
assert native_runtime_ready(), "main.py imported without preloading the runtime"
assert "ctranslate2" in sys.modules, "ctranslate2 was never imported"
print("ok")
"""
        result = _run(script)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        self.assertIn("ok", result.stdout)

    def test_config_and_licensing_do_not_pull_in_qt(self) -> None:
        """The guard only helps if nothing earlier sneaks Qt in.

        ``main.py`` imports these at module scope above the preload call, so a
        Qt import added there would silently disarm the whole fix.
        """
        script = """
import sys
sys.path.insert(0, {root!r})
import app.config
import app.licensing
assert "PyQt5" not in sys.modules, "config/licensing pulled in Qt before the guard"
print("ok")
"""
        result = _run(script)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )


@unittest.skipUnless(sys.platform == "win32", "Windows DLL base-name binding is the issue")
class TranslationAfterQtTests(unittest.TestCase):
    """End-to-end: the guarded order must really translate."""

    def setUp(self) -> None:
        if not _urdu_available():
            self.skipTest("en->ur Argos package is not installed")

    def test_translation_works_after_qt_is_loaded(self) -> None:
        result = _run(_TRANSLATE_AFTER_QT)
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "translation broke after Qt loaded -- the native runtime guard "
                f"is not holding\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
            ),
        )
        self.assertTrue(result.stdout.strip(), "translation produced no output")

    def test_unguarded_order_is_still_the_hazard_it_was(self) -> None:
        """Informational, never a failure.

        Documented so that if the bad order ever stops working, someone updates
        the module docstring instead of leaving a stale warning behind.
        """
        result = _run(_TRANSLATE_AFTER_QT_UNGUARDED)
        if result.returncode == 0:
            print(
                "\n  note: Qt-before-CTranslate2 now works on this machine; "
                "upstream may have fixed the clash"
            )
        else:
            self.assertNotIn(
                "1114",
                result.stdout,
                msg="expected the failure to surface, not vanish",
            )


class _FakeStream:
    def __init__(self) -> None:
        self.encoding = "cp1252"
        self.errors = "strict"
        self.reconfigured: dict | None = None

    def reconfigure(self, **kwargs) -> None:
        self.reconfigured = kwargs


class StreamEncodingTests(unittest.TestCase):
    """``main`` must widen the console streams before anything can print.

    Every feature in this app can produce non-ASCII text: Urdu and Hindi STT
    output, translated strings, file names from the History screen. Windows
    binds stdout/stderr to the ANSI code page, so one such write raises
    UnicodeEncodeError and takes the whole process down mid-session. The
    window keeps the app looking alive, which makes this very hard to report
    back, so the guard runs before the licensing dialog opens.
    """

    def test_reconfigure_is_applied_to_both_streams(self) -> None:
        import main

        out, err = _FakeStream(), _FakeStream()
        with unittest.mock.patch.object(sys, "stdout", out), unittest.mock.patch.object(
            sys, "stderr", err
        ):
            main._force_utf8_streams()

        for stream in (out, err):
            self.assertIsNotNone(stream.reconfigured, "stream was left on cp1252")
            self.assertEqual(stream.reconfigured["encoding"], "utf-8")
            # Not "replace" or "ignore": that would silently corrupt the text
            # the user is looking at. Anything still unencodable has to stay
            # visible in the log as an escape.
            self.assertEqual(stream.reconfigured["errors"], "backslashreplace")

    def test_a_closed_stream_does_not_stop_startup(self) -> None:
        import main

        class Detached(_FakeStream):
            def reconfigure(self, **kwargs) -> None:
                raise ValueError("underlying buffer has been detached")

        with unittest.mock.patch.object(sys, "stdout", Detached()):
            main._force_utf8_streams()  # must not raise


class StartupFailureIsVisibleTests(unittest.TestCase):
    """A startup failure must reach the screen, not just stderr.

    ``build.py`` passes ``--noconsole``, so in a packaged build stderr is
    discarded. Both pre-window exits -- licensing not configured, and a data
    folder that cannot be created -- used to print there and return 1, which the
    user experiences as a shortcut that does nothing at all. There is no error to
    report and no window to look at, so the app just looks broken.

    The dialog is the whole point of the fix, so these assert it is raised and
    that the text actually explains the problem.
    """

    def _capture_dialog(self, call):
        import main

        shown = []

        class _Box:
            @staticmethod
            def critical(_parent, title, message):
                shown.append((title, message))

        with unittest.mock.patch(
            "PyQt5.QtWidgets.QMessageBox.critical", _Box.critical
        ), unittest.mock.patch.object(main, "QApplication", object()):
            call()
        return shown

    def test_missing_dependencies_raise_a_dialog(self) -> None:
        import main

        def run():
            with unittest.mock.patch.object(main.importlib, "import_module") as fake:
                fake.side_effect = lambda name: (
                    (_ for _ in ()).throw(ImportError(name))
                    if name == "keyring"
                    else None
                )
                self.assertFalse(main._preflight())

        shown = self._capture_dialog(run)
        self.assertEqual(len(shown), 1, "the missing dependency was silent")
        title, message = shown[0]
        self.assertIn("Missing dependencies", title)
        self.assertIn("keyring", message)
        self.assertIn("pip install", message)

    def test_unconfigured_licensing_raises_a_dialog_and_names_the_fix(self) -> None:
        import main
        from app.licensing.errors import LicenseConfigurationError

        def run():
            with unittest.mock.patch.object(
                main,
                "_build_manager",
                side_effect=LicenseConfigurationError("License server URL is invalid"),
            ):
                with unittest.mock.patch.object(main, "QApplication", object()):
                    with unittest.mock.patch.object(
                        main, "_notify_already_running"
                    ), unittest.mock.patch.object(main, "_claim_single_instance", return_value=object()):
                        self.assertEqual(main.main(), 1)

        shown = self._capture_dialog(run)
        self.assertEqual(len(shown), 1, "the licensing failure was silent")
        title, message = shown[0]
        self.assertIn("Licensing is not configured", title)
        # The user needs the actual remedy, not just a failure.
        self.assertIn("AI_VOICE_STUDIO_LICENSE_URL", message)
        self.assertIn("AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS", message)
        self.assertIn("License server URL is invalid", message)

    def test_a_data_folder_failure_raises_a_dialog_naming_the_path(self) -> None:
        import main

        def run():
            from app.services import file_service

            with unittest.mock.patch.object(
                main,
                "_build_manager",
                return_value=unittest.mock.MagicMock(),
            ), unittest.mock.patch.object(
                file_service, "ensure_dirs", side_effect=OSError("Access is denied")
            ):
                with unittest.mock.patch.object(main, "QApplication", object()):
                    with unittest.mock.patch.object(
                        main, "_notify_already_running"
                    ), unittest.mock.patch.object(
                        main, "_claim_single_instance", return_value=object()
                    ):
                        self.assertEqual(main.main(), 1)

        shown = self._capture_dialog(run)
        self.assertEqual(len(shown), 1, "the data folder failure was silent")
        title, message = shown[0]
        self.assertIn("data folder", title)
        self.assertIn("Access is denied", message)

    def test_a_broken_dialog_never_masks_the_real_error(self) -> None:
        """If Qt cannot show the box, the stderr message must still be there."""
        import main

        class _Capture:
            encoding = "utf-8"
            errors = "backslashreplace"

            def __init__(self) -> None:
                self.text = ""

            def write(self, data):
                self.text += data

            def flush(self):
                return None

        err = _Capture()
        with unittest.mock.patch.object(sys, "stderr", err):
            with unittest.mock.patch.object(main, "QApplication", object()):
                with unittest.mock.patch(
                    "PyQt5.QtWidgets.QMessageBox.critical",
                    side_effect=RuntimeError("no display"),
                ):
                    # Must not raise: this runs on the failure path, so a second
                    # exception here would replace a useful error with a
                    # traceback the user cannot see.
                    main._startup_failed("Boom", "the details")

        self.assertIn("Boom", err.text)
        self.assertIn("the details", err.text)

    def test_no_stderr_still_raises_the_dialog(self) -> None:
        """A detached or closed stderr must not cost the user the message.

        This is the packaged-build case in miniature: if the stream write itself
        fails, the dialog is the only remaining channel.
        """
        import main

        class _Broken:
            encoding = "utf-8"

            def write(self, _data):
                raise ValueError("underlying buffer has been detached")

        shown = []
        with unittest.mock.patch.object(sys, "stderr", _Broken()):
            with unittest.mock.patch.object(main, "QApplication", object()):
                with unittest.mock.patch(
                    "PyQt5.QtWidgets.QMessageBox.critical",
                    lambda _p, title, message: shown.append((title, message)),
                ):
                    main._startup_failed("Boom", "the details")

        self.assertEqual(len(shown), 1)
        title, message = shown[0]
        self.assertIn("Boom", title)
        self.assertEqual(message, "the details")


if __name__ == "__main__":
    unittest.main()