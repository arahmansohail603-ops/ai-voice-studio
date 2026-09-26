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
    return subprocess.run(
        [sys.executable, "-c", script.format(root=str(_ROOT))],
        capture_output=True,
        text=True,
        timeout=600,
        encoding="utf-8",
        errors="replace",
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


if __name__ == "__main__":
    unittest.main()
