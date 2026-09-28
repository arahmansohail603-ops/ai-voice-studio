from __future__ import annotations

import functools
import sys
import traceback
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QTextEdit,
    QWidget,
)

from app.gui import theme


#: Where a crash traceback is written. A shipped build runs with --noconsole,
#: so stderr goes nowhere and a slot that aborts leaves the user with nothing
#: but a vanished window.
ERROR_LOG = None


def _error_log_path():
    global ERROR_LOG
    if ERROR_LOG is None:
        try:
            from app.config import DATA_ROOT

            ERROR_LOG = DATA_ROOT / "app-errors.log"
        except Exception:  # noqa: BLE001 - never fail because of logging
            ERROR_LOG = Path("app-errors.log")
    return ERROR_LOG


def log_exception(context: str, exc_info) -> str:
    """Append a traceback to the error log and return a one-line summary."""
    exc_type, exc, tb = exc_info
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        path = _error_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n===== {datetime.now().isoformat(timespec='seconds')} {context} =====\n"
            )
            handle.write(text)
    except Exception:  # noqa: BLE001 - logging must never raise
        pass
    return f"{exc_type.__name__}: {exc}"


def ui_slot(func):
    """Run a Qt slot without letting an exception take the process down.

    PyQt5 reacts to an exception escaping a slot by calling ``qFatal()``, which
    aborts immediately and shows nothing. A packaged build has no console, so
    the user just sees the app disappear mid-download and has no way to report
    what happened. This is how the "app crashes when I press Download" class of
    bug stays invisible: the real fault only exists as a Windows event-log entry
    naming a DLL. Catching here turns a silent death into a message on screen
    plus a traceback on disk.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:  # noqa: BLE001 - the whole point is to survive
            try:
                _report_slot_failure(args[0] if args else None, func)
            except Exception:  # noqa: BLE001
                # Reporting failed too: the disk may be full, the data folder
                # unwritable, the widget half-deleted. Raising from here would
                # abort the process from inside its own error path, so give up
                # quietly rather than trade one crash for a worse one.
                pass
            return None

    return wrapper


def _report_slot_failure(receiver, func) -> None:
    """Log a slot failure and try to tell the user. May itself fail."""
    summary = log_exception(f"{func.__module__}.{func.__qualname__}", sys.exc_info())
    if receiver is None:
        return
    for name in ("_show_message", "set_status"):
        handler = getattr(receiver, name, None)
        if not callable(handler):
            continue
        try:
            if name == "_show_message":
                handler(f"Something went wrong: {summary}", theme.DANGER)
            else:
                handler(None, f"Something went wrong: {summary}")
            return
        except Exception:  # noqa: BLE001 - fall through to the next hook
            continue


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


#: Progress bars work in a bounded 0..PROGRESS_SCALE range rather than in bytes.
#:
#: QProgressBar.setRange takes a C++ ``int``, so any byte total above
#: 2**31-1 (~2.1 GB) raises OverflowError. The full Piper catalog is ~11 GB, and
#: a multi-GB TTS model is just as capable of crossing the line. That
#: OverflowError is raised inside a Qt slot, and PyQt5 turns an unhandled
#: exception in a slot into ``qFatal()`` -> ``abort()``: the whole app dies with
#: no error shown. Expressing progress as a fraction removes the ceiling
#: entirely, and reads the same for a 4 MB voice and an 11 GB catalog.
PROGRESS_SCALE = 1000


def progress_value(received: int, total: int) -> int:
    """Map a byte count onto the 0..PROGRESS_SCALE bar range, clamped."""
    if total <= 0:
        return 0
    return max(0, min(PROGRESS_SCALE, int(received * PROGRESS_SCALE / total)))


def bind_future(
    root: QObjectLike, future: Future, on_success: Callable, on_error: Callable
) -> None:
    timer = QTimer(root)
    timer.setInterval(80)

    def poll() -> None:
        if not future.done():
            return
        timer.stop()
        timer.deleteLater()
        try:
            result = future.result()
        except BaseException as exc:
            on_error(exc)
        else:
            on_success(result)

    timer.timeout.connect(poll)
    timer.start()


class QObjectLike(QWidget):
    pass


class TextEdit(QTextEdit):
    def insert(self, position: str = "1.0", text: str = "") -> None:
        cursor = self.textCursor()
        if position == "1.0":
            cursor.movePosition(cursor.Start)
        self.setTextCursor(cursor)
        self.insertPlainText(text)

    def get(self, start: str = "1.0", end: str = "end-1c") -> str:
        if start == "1.0" and end == "end-1c":
            return self.toPlainText()
        cursor = self.textCursor()
        cursor.movePosition(cursor.Start)
        if end == "end-1c":
            cursor.movePosition(cursor.End)
            cursor.movePosition(cursor.PreviousCharacter)
        return cursor.selectedText().replace("\u2029", "\n")

    def delete(self, start: str = "1.0", end: str = "end") -> None:
        cursor = self.textCursor()
        cursor.movePosition(cursor.Start)
        if end == "end":
            cursor.movePosition(cursor.End)
            cursor.removeSelectedText()
            self.setTextCursor(cursor)
            return
        cursor.movePosition(cursor.End)
        cursor.movePosition(cursor.PreviousCharacter)
        cursor.movePosition(cursor.Start, cursor.KeepAnchor)
        cursor.removeSelectedText()
        self.setTextCursor(cursor)

    def edit_modified(self, value: bool = True) -> None:
        self.document().setModified(value)


class MethodBadge(QLabel):
    COLORS = {
        "qwen3-tts": (theme.SUCCESS, "#153226"),
        "pyttsx3": (theme.WARNING, "#33260f"),
        "clone": (theme.ACCENT, theme.ACCENT_SOFT),
        "qwen-clone": (theme.ACCENT, theme.ACCENT_SOFT),
        "fallback-clone": (theme.WARNING, "#33260f"),
        "record": (theme.SUCCESS, "#153226"),
        "stt": (theme.SUCCESS, "#153226"),
    }

    LABELS = {
        "qwen3-tts": "Qwen3-TTS",
        "pyttsx3": "Offline TTS",
        "clone": "Voice Clone",
        "qwen-clone": "Qwen Voice Clone",
        "fallback-clone": "Fallback Voice",
        "record": "Voice Note",
        "stt": "Transcription",
    }

    def __init__(self, master: QWidget | None, method: str, **kwargs):
        super().__init__(master)
        foreground, background = self.COLORS.get(
            method, (theme.SUBTEXT, theme.INPUT_BG)
        )
        text = self.LABELS.get(method, method)
        self.setText(f"  {text}  ")
        self.setFont(theme.font(12, "bold"))
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.setStyleSheet(
            f"QLabel {{ color: {foreground}; background: {background}; "
            f"border-radius: 10px; padding: 4px 8px; }}"
        )
        if kwargs.get("width"):
            self.setFixedWidth(int(kwargs["width"]))


class BusyButton(QPushButton):
    def __init__(self, master: QWidget | None = None, text: str = "Button", **kwargs):
        command = kwargs.pop("command", None)
        background = kwargs.pop("fg_color", kwargs.pop("background", theme.CARD_BG))
        hover = kwargs.pop("hover_color", kwargs.pop("hover", None))
        color = kwargs.pop("text_color", kwargs.pop("color", theme.TEXT))
        radius = kwargs.pop("corner_radius", 8)
        border = kwargs.pop("border_color", None)
        border_width = kwargs.pop("border_width", 0)
        width = kwargs.pop("width", None)
        height = kwargs.pop("height", None)
        state = kwargs.pop("state", None)
        super().__init__(master)
        self._rest_text = text
        self.setText(text)
        if command is not None:
            self.clicked.connect(command)
        if width:
            self.setMinimumWidth(int(width))
        if height:
            self.setFixedHeight(int(height))
        if state in ("disabled", False):
            self.setEnabled(False)
        self.setFont(kwargs.pop("font", theme.font(13)))
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            theme.button_style(
                background, hover, color, int(radius), border, int(border_width)
            )
        )

    def set_busy(self, busy: bool, running_text: str | None = None) -> None:
        self._busy = busy
        self.setEnabled(not busy)
        self.setText((running_text or "Working…") if busy else self._rest_text)

    def set_idle_text(self, text: str) -> None:
        """Change the resting label without disabling the button.

        ``set_busy(True)`` greys the button out, which is wrong for a toggle the
        user has to press a second time to stop. This swaps the label and keeps
        the button clickable, and ``set_busy(False)`` still restores it.
        """
        self._rest_text = text
        if not getattr(self, "_busy", False):
            self.setText(text)

    @property
    def busy(self) -> bool:
        return getattr(self, "_busy", False)


class Toast(QLabel):
    def __init__(self, master: QWidget):
        super().__init__(master)
        self._job: QTimer | None = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.setFont(theme.font(13))
        self.setWordWrap(True)
        self.setMaximumWidth(360)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT}; background: {theme.INPUT_BG}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 8px; "
            "padding: 10px 14px; }"
        )
        self.hide()

    def show(self, message: str = "", kind: str = "info", ms: int = 2600) -> None:
        color = {
            "info": theme.TEXT,
            "ok": theme.SUCCESS,
            "warn": theme.WARNING,
            "error": theme.DANGER,
        }.get(kind, theme.TEXT)
        self.setText(message)
        # The closing brace is a single "}" on purpose: only an f-string turns
        # "}}" into one brace, and this last fragment is a plain string. With
        # "}}" here Qt rejected the whole sheet ("Could not parse stylesheet of
        # object Toast") and the toast rendered unstyled.
        self.setStyleSheet(
            f"QLabel {{ color: {color}; background: {theme.INPUT_BG}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 8px; "
            "padding: 10px 14px; }"
        )
        self.adjustSize()
        self._place()
        self.raise_()
        self.setVisible(True)
        if self._job is not None:
            self._job.stop()
        self._hide_timer.start(ms)

    def _place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.move(max(0, parent.width() - self.width() - 16), 16)

    def hide(self) -> None:
        self.setVisible(False)


class MicLevelMeter(QProgressBar):
    def __init__(self, master: QWidget, width: int = 180, **kwargs):
        super().__init__(master)
        self.setRange(0, 100)
        self.setValue(0)
        self.setTextVisible(False)
        self.setFixedHeight(10)
        self.setFixedWidth(int(width))
        self.setStyleSheet(theme.progress_style())

    def set(self, value: float) -> None:
        number = float(value)
        if number <= 1:
            number *= 100
        self.setValue(int(max(0, min(100, number))))

    def push(self, level: float) -> None:
        self.set(max(0.0, min(1.0, float(level) * 14.0)) * 100)


class AudioPlayerBar(QFrame):
    def __init__(self, master: QWidget, player, **kwargs):
        super().__init__(master)
        self.player = player
        self._file: Path | None = None
        self._seeking = False
        self.setStyleSheet(theme.frame_style(theme.INPUT_BG, 12))
        layout = QGridLayout(self)
        layout.setContentsMargins(10, 6, 16, 6)
        layout.setHorizontalSpacing(4)
        layout.setVerticalSpacing(0)

        self._btn_play = QPushButton("▶ Play", self)
        self._btn_play.setFixedSize(92, 32)
        self._btn_play.setFont(theme.font(13, "bold"))
        self._btn_play.setStyleSheet(
            theme.button_style(theme.ACCENT, theme.ACCENT_HOVER, theme.ON_ACCENT, 8)
        )
        self._btn_play.clicked.connect(self._toggle)
        layout.addWidget(self._btn_play, 0, 0)

        self._btn_stop = QPushButton("⏹ Stop", self)
        self._btn_stop.setFixedSize(72, 32)
        self._btn_stop.setFont(theme.font(13))
        self._btn_stop.setStyleSheet(
            theme.button_style(
                theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 8, theme.BORDER, 1
            )
        )
        self._btn_stop.clicked.connect(self._stop)
        layout.addWidget(self._btn_stop, 0, 1)

        self._lbl_time = QLabel("00:00 / 00:00", self)
        self._lbl_time.setFont(theme.font(12))
        self._lbl_time.setStyleSheet(theme.label_style(theme.SUBTEXT))
        layout.addWidget(self._lbl_time, 0, 2)

        self._slider = QSlider(Qt.Horizontal, self)
        self._slider.setRange(0, 100)
        self._slider.setValue(0)
        self._slider.setFixedHeight(16)
        self._slider.valueChanged.connect(self._on_slider)
        layout.addWidget(self._slider, 0, 3)
        layout.setColumnStretch(3, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._poll)
        # Started in set_file, not here. All nine screens are built eagerly at
        # startup, so starting in __init__ meant three of these bars polling 15
        # times a second for the whole process lifetime, on hidden screens, with
        # no file loaded. clear() stops it again.

    @staticmethod
    def _value(obj, name: str, default=0):
        value = getattr(obj, name, default)
        return value() if callable(value) else value

    def set_file(self, path: str | Path, autoplay: bool = True) -> None:
        selected = Path(path)
        self.player.load(selected)
        self._file = selected
        self._lbl_time.setText(
            f"00:00 / {fmt_clock(self._value(self.player, 'duration'))}"
        )
        self._slider.setValue(0)
        self._timer.start()
        if autoplay:
            self.player.play()

    def clear(self) -> None:
        self._timer.stop()
        if self._file is not None and self._same_path(
            self._value(self.player, "path"), self._file
        ):
            self.player.stop()
        self._file = None
        self._lbl_time.setText("00:00 / 00:00")
        self._slider.setValue(0)
        self._btn_play.setText("▶ Play")

    @staticmethod
    def _same_path(left, right) -> bool:
        if left is None or right is None:
            return False
        try:
            return Path(left).resolve() == Path(right).resolve()
        except Exception:
            return str(left) == str(right)

    def _toggle(self) -> None:
        if self._file is None or not self._same_path(
            self._value(self.player, "path"), self._file
        ):
            return
        state = self._value(self.player, "state", "")
        if state == self.player.PLAYING:
            self.player.pause()
        else:
            self.player.play()

    def _stop(self) -> None:
        if self._file is not None and self._same_path(
            self._value(self.player, "path"), self._file
        ):
            self.player.stop()

    def _on_slider(self, value: int) -> None:
        if self._seeking:
            return
        duration = float(self._value(self.player, "duration", 0.0))
        if self._file is not None and duration > 0:
            self.player.seek_to(float(value) / 100.0 * duration)

    def _poll(self) -> None:
        try:
            active = self._file is not None and self._same_path(
                self._value(self.player, "path"), self._file
            )
            ended = False
            if active:
                pump = getattr(self.player, "pump", None)
                if callable(pump):
                    ended = bool(pump())
                state = self._value(self.player, "state", "")
                duration = float(self._value(self.player, "duration", 0.0))
                position = float(self._value(self.player, "position", 0.0))
                self._lbl_time.setText(f"{fmt_clock(position)} / {fmt_clock(duration)}")
                if not self._seeking and duration > 0:
                    fraction = min(100.0, max(0.0, position / duration * 100.0))
                    if abs(self._slider.value() - fraction) > 1.0:
                        self._seeking = True
                        self._slider.setValue(int(fraction))
                        self._seeking = False
                if state == self.player.PLAYING:
                    self._btn_play.setText("⏸ Pause")
                elif state == self.player.PAUSED or ended:
                    self._btn_play.setText("▶ Play")
            else:
                if self._file is not None:
                    self._lbl_time.setText("00:00 / 00:00")
                    self._slider.setValue(0)
                    self._file = None
                self._btn_play.setText("▶ Play")
        except Exception:
            pass


class Screen(QWidget):
    def __init__(self, master: QWidget, app):
        super().__init__(master)
        self.app = app
        self._status_holder = None
        self.setStyleSheet("QWidget { background: transparent; }")

    def on_show(self, **kwargs) -> None:
        return None

    def on_hide(self) -> None:
        return None

    def shutdown(self) -> None:
        """Called once when the app is closing.

        Separate from :meth:`on_hide` because a long job must survive the user
        switching tabs, but must not outlive the process: a QThread still
        running when its widget is destroyed takes the process down with it.
        """
        return None

    def set_status(
        self, label_widget: QWidget | None, text: str, color: str = theme.SUBTEXT
    ) -> None:
        if label_widget is not None and hasattr(label_widget, "setText"):
            label_widget.setText(text)
            if hasattr(label_widget, "setStyleSheet"):
                label_widget.setStyleSheet(theme.label_style(color))

    def toast(self, message: str, kind: str = "info") -> None:
        self.app.toast(message, kind)

    def run_between(
        self, future: Future, on_success: Callable, on_error: Callable
    ) -> None:
        bind_future(self, future, on_success, on_error)

    def after(self, milliseconds: int, callback: Callable) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(callback)
        timer.start(int(milliseconds))
        return timer

    @staticmethod
    def after_cancel(timer: QTimer | None) -> None:
        if timer is not None:
            timer.stop()
            timer.deleteLater()
