from __future__ import annotations

import ctypes
import hashlib
import importlib
import os
import sys
from collections.abc import Callable
from ctypes import wintypes
from datetime import datetime, timezone

from app import config
from app.core.native_runtime import preload_native_runtime
from app.licensing import (
    EncryptedLicenseStore,
    LicenseConfigurationError,
    LicenseError,
    LicenseExpiredError,
    LicenseHttpClient,
    LicenseManager,
)
from app.licensing.crypto import parse_time

# Must run before the PyQt5 import below. Windows binds a DLL by base name, so
# whichever C++/OpenMP runtime loads first is the one every other native library
# then binds against. Qt first + CTranslate2 (Argos) later fails with
# [WinError 1114] on the first translation, on both the TTS and STT screens.
preload_native_runtime()

try:
    from PyQt5.QtCore import QMutex, QMutexLocker, QObject, QThread, QTimer, pyqtSignal
    from PyQt5.QtNetwork import QLocalServer, QLocalSocket
    from PyQt5.QtWidgets import (
        QApplication,
        QDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
    )
except ImportError:
    QObject = QThread = QTimer = QMutex = QMutexLocker = None
    QLocalServer = QLocalSocket = None
    pyqtSignal = None
    QApplication = QDialog = QHBoxLayout = QLabel = QLineEdit = None
    QProgressBar = QPushButton = QVBoxLayout = None


def _startup_failed(title: str, message: str) -> None:
    """Report a startup failure on screen, not only on stderr.

    A packaged build has no console (``build.py`` passes ``--noconsole``), so a
    message written to stderr goes nowhere: the user clicks the shortcut, no
    window appears, and nothing explains why. Every path that returns 1 before
    the main window exists has to raise a dialog instead, or the app looks
    broken rather than unconfigured.

    Degrades to stderr when Qt itself is missing -- that is precisely the
    preflight case -- and never lets a failure of either channel mask the real
    error. The two channels are guarded separately on purpose: a packaged build
    can have a detached stderr, and losing the dialog because the log write
    failed would reintroduce the exact silent exit this exists to remove.
    """
    try:
        print(f"{config.APP_NAME}: {title}\n\n{message}", file=sys.stderr)
    except Exception:  # pragma: no cover - stderr can be detached or closed
        pass
    if QApplication is None:
        return
    try:
        from PyQt5.QtWidgets import QApplication as _App, QMessageBox

        # A QMessageBox before QApplication exists would abort, so build the
        # smallest possible one just to carry the message.
        owns_app = _App.instance() is None
        app = _App([]) if owns_app else None
        try:
            QMessageBox.critical(None, f"{config.APP_NAME} - {title}", message)
        finally:
            if app is not None:
                app.quit()
    except Exception:  # pragma: no cover - never mask the real error
        pass


def _preflight() -> bool:
    required = (
        ("PyQt5", "PyQt5"),
        ("cryptography", "cryptography"),
        ("keyring", "keyring"),
    )
    missing = []
    for module_name, package_name in required:
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(package_name)
    if missing:
        packages = " ".join(missing)
        _startup_failed(
            "Missing dependencies",
            f"{config.APP_NAME} needs {packages} to start.\n\n"
            "Install the dependencies first:\n\n"
            f"    pip install {packages}\n\n"
            "Then run:\n\n"
            "    python main.py",
        )
        return False
    return True


_LINGERING_TASKS: set = set()

#: GetLastError() value Windows returns when a named mutex already exists.
_ERROR_ALREADY_EXISTS = 183


def _single_instance_key() -> str:
    digest = hashlib.sha256(str(config.DATA_DIR).encode("utf-8")).hexdigest()[:16]
    return f"ai-voice-studio-license-{digest}"


def _claim_named_mutex(name: str):
    """Atomically claim ``name`` for this process.

    Returns a holder on success, ``False`` when another instance already holds
    it, and ``None`` when the mutex API is unavailable so the caller can fall
    back to the named-pipe probe.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    except (AttributeError, OSError):
        return None

    handle = kernel32.CreateMutexW(None, True, f"Local\\{name}")
    if not handle:
        return None
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    return _MutexClaim(handle, kernel32)


class _MutexClaim:
    """Holds a named-mutex handle for the lifetime of the process.

    The handle is never released explicitly during normal use: the kernel drops
    it when the process exits, so a crash cannot leave a stale claim behind and
    a restart is never blocked by the previous run. :meth:`close` exists for
    tests and for callers that want to release it early, and matches the
    ``QLocalServer`` interface this replaces.
    """

    def __init__(self, handle: int, kernel32) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel32.CloseHandle(handle)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter teardown
            pass


def _claim_single_instance():
    if QLocalServer is None or QLocalSocket is None:
        return True
    key = _single_instance_key()
    claim = _claim_named_mutex(key)
    if claim is not None:
        return claim if claim is not False else None
    # Fallback for platforms without the mutex API. Two launches inside the
    # same second used to both probe, both conclude "not running", and the
    # loser would then call removeServer() -- tearing down the winner's claim
    # and letting two apps write settings.json at once. Only clear the pipe
    # after a *second* probe confirms nobody is answering on it.
    probe = QLocalSocket()
    probe.connectToServer(key)
    if probe.waitForConnected(250):
        probe.abort()
        return None
    server = QLocalServer()
    if not server.listen(key):
        server.close()
        QLocalServer.removeServer(key)
        recheck = QLocalSocket()
        recheck.connectToServer(key)
        taken = recheck.waitForConnected(250)
        recheck.abort()
        if taken:
            return None
        server = QLocalServer()
        if not server.listen(key):
            return None
    return server


if QThread is not None:
    DEADLINE_RETRY_MS = 250
    STOP_GRACE_MS = 1000

    def _stop_timeout_ms() -> int:
        return int(config.LICENSE_REQUEST_TIMEOUT * 1000) + 1000

    def _task_active(task) -> bool:
        return task is not None and task.isRunning()

    def _retire_task(task, timeout_ms: int) -> bool:
        if task is None:
            return True
        if task.stop(timeout_ms):
            return True
        _LINGERING_TASKS.add(task)
        return False

    class _LicenseTask(QThread):
        succeeded = pyqtSignal(int, object)
        failed = pyqtSignal(int, str)

        def __init__(
            self,
            operation: Callable[[], object],
            parent: QObject | None = None,
            revision: int = 0,
        ) -> None:
            super().__init__(parent)
            self._operation = operation
            self._revision = revision
            self._lock = QMutex()
            self._cancelled = False

        @property
        def revision(self) -> int:
            return self._revision

        @property
        def cancelled(self) -> bool:
            with QMutexLocker(self._lock):
                return self._cancelled

        def cancel(self) -> None:
            with QMutexLocker(self._lock):
                self._cancelled = True
            self.requestInterruption()

        def stop(self, timeout_ms: int) -> bool:
            self.cancel()
            self.quit()
            if not self.isRunning():
                return True
            if self.wait(timeout_ms):
                return True
            self.terminate()
            return self.wait(STOP_GRACE_MS)

        def _aborted(self) -> bool:
            return self.cancelled or self.isInterruptionRequested()

        def run(self) -> None:
            if self._aborted():
                return
            try:
                result = self._operation()
            except Exception as exc:
                if not self._aborted():
                    self.failed.emit(self._revision, str(exc) or exc.__class__.__name__)
            else:
                if not self._aborted():
                    self.succeeded.emit(self._revision, result)

    class _ActivationDialog(QDialog):
        def __init__(self, manager: LicenseManager, message: str = "") -> None:
            super().__init__()
            self.manager = manager
            self.state: dict | None = None
            self._task: _LicenseTask | None = None
            self._revision = 0
            self._closing = False
            self.setWindowTitle(f"{config.APP_NAME} Activation")
            self.setModal(True)
            self.setMinimumWidth(470)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 24, 24, 20)
            layout.setSpacing(12)

            title = QLabel("Activate your license", self)
            title.setStyleSheet("font-size: 20px; font-weight: 600;")
            layout.addWidget(title)

            description = QLabel(
                "Enter the license key supplied with your purchase. "
                "The key is bound to this device on first activation.",
                self,
            )
            description.setWordWrap(True)
            layout.addWidget(description)

            self.key_edit = QLineEdit(self)
            self.key_edit.setPlaceholderText("LIC-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX")
            self.key_edit.setEchoMode(QLineEdit.Password)
            self.key_edit.returnPressed.connect(self._activate)
            layout.addWidget(self.key_edit)

            self.status = QLabel(
                message or "Activation is required to use the application.", self
            )
            self.status.setWordWrap(True)
            layout.addWidget(self.status)

            self.progress = QProgressBar(self)
            self.progress.setRange(0, 0)
            self.progress.setTextVisible(False)
            self.progress.hide()
            layout.addWidget(self.progress)

            buttons = QHBoxLayout()
            self.activate_button = QPushButton("Activate", self)
            self.activate_button.clicked.connect(self._activate)
            self.cancel_button = QPushButton("Cancel", self)
            self.cancel_button.clicked.connect(self.reject)
            buttons.addWidget(self.activate_button)
            buttons.addWidget(self.cancel_button)
            layout.addLayout(buttons)

        @property
        def task_active(self) -> bool:
            return _task_active(self._task)

        def stop_task(self, timeout_ms: int) -> bool:
            self._closing = True
            task, self._task = self._task, None
            return _retire_task(task, timeout_ms)

        def _current(self, revision: int) -> bool:
            return (
                not self._closing
                and revision == self._revision
                and self._task is not None
                and self._task.revision == revision
            )

        def _activate(self) -> None:
            if self._task is not None or self._closing:
                return
            key = self.key_edit.text().strip()
            if not key:
                self.status.setText("Enter a license key first.")
                return
            self.activate_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.progress.show()
            self.status.setText("Contacting the license server…")
            self._revision += 1
            task = _LicenseTask(
                lambda: self.manager.activate(
                    key,
                    metadata={"app_version": config.APP_VERSION},
                ),
                self,
                self._revision,
            )
            task.succeeded.connect(self._activation_finished)
            task.failed.connect(self._activation_failed)
            task.finished.connect(self._task_finished)
            self._task = task
            task.start()

        def _activation_finished(self, revision: int, state: object) -> None:
            if not self._current(revision):
                return
            if (
                not isinstance(state, dict)
                or not isinstance(state.get("envelope"), dict)
                or not state.get("envelope", {}).get("payload")
            ):
                self._activation_failed(
                    revision, "The license server returned an invalid lease"
                )
                return
            self.state = state
            self.accept()

        def _task_finished(self) -> None:
            task, self._task = self._task, None
            if task is not None:
                task.deleteLater()

        def _activation_failed(self, revision: int, message: str) -> None:
            if not self._current(revision):
                return
            self.progress.hide()
            self.activate_button.setEnabled(True)
            self.cancel_button.setEnabled(True)
            self.status.setText(message)

        def reject(self) -> None:
            if self._task is None:
                super().reject()

    class _LicenseController(QObject):
        def __init__(self, manager: LicenseManager, application: QApplication) -> None:
            super().__init__()
            self.manager = manager
            self.application = application
            self.window = None
            self._task: _LicenseTask | None = None
            self._revision = 0
            self._dialog: _ActivationDialog | None = None
            self._deadline_pending = False
            self._timer = QTimer(self)
            self._timer.setInterval(max(300, config.LICENSE_REFRESH_SECONDS) * 1000)
            self._timer.timeout.connect(self._refresh)
            self._deadline_timer = QTimer(self)
            self._deadline_timer.timeout.connect(self._deadline_reached)
            self._deadline_expired = False
            self._grace_expires_at = None

        @property
        def task_active(self) -> bool:
            return _task_active(self._task)

        def stop_task(self, timeout_ms: int) -> bool:
            task, self._task = self._task, None
            return _retire_task(task, timeout_ms)

        def _busy(self) -> bool:
            if self.task_active:
                return True
            dialog = self._dialog
            return dialog is not None and dialog.task_active

        def _begin_task(self, operation) -> _LicenseTask:
            self._revision += 1
            task = _LicenseTask(operation, self, self._revision)
            self._task = task
            return task

        def _current(self, revision: int) -> bool:
            return (
                revision == self._revision
                and self._task is not None
                and self._task.revision == revision
            )

        def start(self) -> bool:
            try:
                state = self.manager.current()
            except LicenseExpiredError:
                return self._start_refresh()
            except (LicenseError, RuntimeError, ValueError) as exc:
                return self._activate(str(exc))
            if state is None:
                return self._activate()
            return self._open_window(state)

        def _start_refresh(self) -> bool:
            if self._task is not None:
                return True
            task = self._begin_task(self.manager.refresh)
            task.succeeded.connect(self._start_refresh_finished)
            task.failed.connect(self._start_refresh_failed)
            task.finished.connect(self._task_finished)
            task.start()
            return True

        def _start_refresh_finished(self, revision: int, state: object) -> None:
            if not self._current(revision):
                return
            if not isinstance(state, dict):
                self._start_refresh_failed(
                    revision, "The license server returned an invalid lease"
                )
                return
            if not self._open_window(state):
                self.application.quit()

        def _start_refresh_failed(self, revision: int, message: str) -> None:
            if not self._current(revision):
                return
            self._timer.stop()
            self._deadline_timer.stop()
            self._deadline_pending = False
            if not self._activate(message):
                self.application.quit()

        def _open_window(self, state: dict | None = None) -> bool:
            from app.gui.app import VoiceStudioApp

            if state is not None and not self._arm_refresh(state):
                self.application.quit()
                return False
            self.window = VoiceStudioApp()
            self.window.show()
            return True

        def _arm_refresh(self, state: dict) -> bool:
            if not isinstance(state, dict):
                return False
            try:
                payload = state["envelope"]["payload"]
                last_server_time = parse_time(state["last_server_time"])
                expires_at = parse_time(payload["expires_at"])
                grace_expires_at = parse_time(payload["grace_expires_at"])
                current = max(datetime.now(timezone.utc), last_server_time)
                self._grace_expires_at = grace_expires_at
                if grace_expires_at <= current:
                    return False
                remaining = max(0, int((expires_at - current).total_seconds()))
                configured = max(300, config.LICENSE_REFRESH_SECONDS)
                interval = max(30, min(configured, max(30, remaining // 2)))
                deadline = max(1, int((grace_expires_at - current).total_seconds()))
            except (KeyError, TypeError, ValueError, OverflowError):
                return False
            self._deadline_expired = False
            self._deadline_pending = False
            self._timer.setInterval(min(interval * 1000, 2_147_483_647))
            self._deadline_timer.setInterval(min(deadline * 1000, 2_147_483_647))
            self._timer.start()
            self._deadline_timer.start()
            return True

        def _activate(self, message: str = "") -> bool:
            dialog = _ActivationDialog(self.manager, message)
            self._dialog = dialog
            try:
                accepted = dialog.exec_() == QDialog.Accepted
                state = dialog.state
            finally:
                self._dialog = None
                if dialog.task_active:
                    dialog.stop_task(_stop_timeout_ms())
            if not accepted:
                if self.window is not None:
                    self.window.close()
                return False
            if not isinstance(state, dict) or not self._arm_refresh(state):
                if self.window is not None:
                    self.window.setEnabled(False)
                self.application.quit()
                return False
            if self.window is None:
                return self._open_window(state)
            self.window.setEnabled(True)
            self.window.raise_()
            self.window.activateWindow()
            return True

        def _refresh(self) -> None:
            if self._task is not None:
                return
            task = self._begin_task(self.manager.refresh)
            task.succeeded.connect(self._refresh_finished)
            task.failed.connect(self._refresh_failed)
            task.finished.connect(self._task_finished)
            task.start()

        def _task_finished(self) -> None:
            task, self._task = self._task, None
            if task is not None:
                task.deleteLater()
            self._resume_deadline()

        def _refresh_finished(self, revision: int, state: object) -> None:
            if not self._current(revision):
                return
            if not isinstance(state, dict) or not self._arm_refresh(state):
                self._refresh_failed(
                    revision, "The license server returned an invalid lease"
                )
                return
            if self.window is not None:
                self.window.setEnabled(True)
                self.window.toast("License lease refreshed", "ok")

        def _refresh_failed(self, revision: int, message: str) -> None:
            if not self._current(revision):
                return
            self._timer.stop()
            self._deadline_timer.stop()
            self._deadline_pending = False
            if self.window is not None:
                self.window.setEnabled(False)
            if not self._activate(message):
                self.application.quit()

        def _deadline_reached(self) -> None:
            if self._grace_expires_at is not None:
                remaining = int(
                    (
                        self._grace_expires_at - datetime.now(timezone.utc)
                    ).total_seconds()
                )
                if remaining > 0:
                    self._deadline_timer.setInterval(
                        min(max(remaining, 1) * 1000, 2_147_483_647)
                    )
                    self._deadline_timer.start()
                    return
            if self._deadline_expired:
                return
            if self._busy():
                self._defer_deadline()
                return
            self._deadline_expired = True
            self._deadline_pending = False
            self._timer.stop()
            self._deadline_timer.stop()
            if self.window is not None:
                self.window.setEnabled(False)
            if not self._activate("The license lease has expired"):
                self.application.quit()

        def _defer_deadline(self) -> None:
            self._deadline_pending = True
            self._deadline_timer.setInterval(DEADLINE_RETRY_MS)
            self._deadline_timer.start()

        def _resume_deadline(self) -> None:
            if not self._deadline_pending or self._deadline_expired:
                return
            if self._busy():
                return
            self._deadline_pending = False
            self._deadline_reached()

        def shutdown(self) -> None:
            self._timer.stop()
            self._deadline_timer.stop()
            self._deadline_pending = False
            self._revision += 1
            timeout = _stop_timeout_ms()
            dialog, self._dialog = self._dialog, None
            if dialog is not None:
                dialog.stop_task(timeout)
            self.stop_task(timeout)


def _build_manager() -> LicenseManager:
    client = LicenseHttpClient(
        config.LICENSE_SERVER_URL,
        config.LICENSE_PUBLIC_KEYS,
        timeout=config.LICENSE_REQUEST_TIMEOUT,
    )
    return LicenseManager(client, EncryptedLicenseStore())


def _notify_already_running() -> None:
    """Tell the user the app is open instead of exiting without a word."""
    print(
        f"{config.APP_NAME} is already running for this data directory.",
        file=sys.stderr,
    )
    if QApplication is None:
        return
    try:
        QApplication.setActiveWindow(None)
        from PyQt5.QtWidgets import QMessageBox

        QMessageBox.information(
            None,
            f"{config.APP_NAME} is already running",
            f"{config.APP_NAME} is already open.\n\n"
            "Look for its window in the taskbar. Only one copy can run at a "
            "time, so that they do not both write to your settings and history.",
        )
    except Exception:  # pragma: no cover - never block a clean exit on a dialog
        pass


def _force_utf8_streams() -> None:
    """Stop non-ASCII text from killing the process on a Windows console.

    Python binds stdout/stderr to the active ANSI code page, which is cp1252
    here. Writing Urdu to stdout then raises UnicodeEncodeError and takes the
    whole app down mid-session, while stderr quietly emits ``\\uXXXX`` escapes
    so the log becomes unreadable. ``backslashreplace`` keeps logging lossless
    for the characters that still cannot be encoded.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):  # pragma: no cover - closed/detached stream
            pass


def main() -> int:
    _force_utf8_streams()
    if not _preflight():
        return 1

    try:
        manager = _build_manager()
    except (LicenseConfigurationError, TypeError, ValueError) as exc:
        _startup_failed(
            "Licensing is not configured",
            f"{config.APP_NAME} could not start because its licensing is not set "
            f"up on this machine.\n\n"
            f"Details: {exc}\n\n"
            "This is a packaging setting, not something you did wrong. Set the "
            "environment variables\n\n"
            "    AI_VOICE_STUDIO_LICENSE_URL\n"
            "    AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS\n\n"
            "or rebuild with app/_build_license_config.py supplying the server "
            "URL and the Ed25519 public key.",
        )
        return 1

    from app.services.file_service import ensure_dirs

    try:
        ensure_dirs()
    except OSError as exc:
        _startup_failed(
            "Cannot prepare the data folder",
            f"{config.APP_NAME} could not prepare its data directory.\n\n"
            f"Details: {exc}\n\n"
            f"It needs to create files under:\n    {getattr(config, 'DATA_DIR', '')}\n\n"
            "Check that the drive is connected and that you have permission to "
            "write there.",
        )
        return 1

    application = QApplication(sys.argv[:1])
    application.setApplicationName(config.APP_NAME)
    application.setApplicationVersion(config.APP_VERSION)
    single_instance = _claim_single_instance()
    if single_instance is None:
        # Say so on screen. Exiting silently on a second launch is what made
        # this look like "the app will not start" -- the user clicks the
        # shortcut, nothing appears, and no message ever explains why.
        _notify_already_running()
        return 1
    controller = _LicenseController(manager, application)
    application.aboutToQuit.connect(controller.shutdown)
    if not controller.start():
        return 1
    exit_code = application.exec_()
    lingering = [task for task in _LINGERING_TASKS if task.isRunning()]
    if lingering:
        sys.stderr.write(
            f"{config.APP_NAME} exited with a licensing request still shutting down.\n"
        )
        sys.stderr.flush()
        os._exit(exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
