"""Reusable GUI widgets: player bar, badges, busy buttons, toasts, meters."""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from app.gui import theme


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def bind_future(root, future: Future, on_success: Callable, on_error: Callable) -> None:
    """Poll a background future and dispatch the result onto the Tk loop."""

    def _poll() -> None:
        if future.done():
            exc = future.exception()
            if exc is not None:
                on_error(exc)
                return
            on_success(future.result())
            return
        root.after(80, _poll)

    root.after(80, _poll)


class MethodBadge(ctk.CTkLabel):
    """Small colored pill identifying how an item was produced."""

    COLORS = {
        "edge-tts": (theme.SUCCESS, "#153226"),
        "pyttsx3": (theme.WARNING, "#33260f"),
        "clone": (theme.ACCENT, theme.ACCENT_SOFT),
        "fallback-clone": (theme.WARNING, "#33260f"),
        "record": (theme.SUCCESS, "#153226"),
        "stt": (theme.SUCCESS, "#153226"),
    }

    LABELS = {
        "edge-tts": "Neural TTS",
        "pyttsx3": "Offline TTS",
        "clone": "Voice Clone",
        "fallback-clone": "Fallback Voice",
        "record": "Voice Note",
        "stt": "Transcription",
    }

    def __init__(self, master, method: str, **kwargs):
        fg, bg = self.COLORS.get(method, (theme.SUBTEXT, theme.INPUT_BG))
        text = self.LABELS.get(method, method)
        super().__init__(
            master,
            text=f"  {text}  ",
            fg_color=bg,
            text_color=fg,
            corner_radius=10,
            font=theme.font(12, "bold"),
            **kwargs,
        )


class BusyButton(ctk.CTkButton):
    """A button that shows busy state (disabled + progress text)."""

    def __init__(self, master, text: str = "Button", **kwargs):
        self._rest_text = text
        super().__init__(master, text=text, **kwargs)
        self._busy = False

    def set_busy(self, busy: bool, running_text: Optional[str] = None) -> None:
        self._busy = busy
        if busy:
            self.configure(state="disabled", text=running_text or "Working…")
        else:
            self.configure(state="normal", text=self._rest_text)

    @property
    def busy(self) -> bool:
        return self._busy


class Toast(ctk.CTkLabel):
    """Transient notification that slides under the top-right corner."""

    def __init__(self, master: ctk.CTkBaseClass):
        super().__init__(
            master,
            text="",
            fg_color=theme.INPUT_BG,
            corner_radius=8,
            font=theme.font(13),
        )
        self._job: Optional[str] = None

    def show(self, message: str, kind: str = "info", ms: int = 2600) -> None:
        color = {
            "info": theme.TEXT,
            "ok": theme.SUCCESS,
            "warn": theme.WARNING,
            "error": theme.DANGER,
        }.get(kind, theme.TEXT)
        self.configure(text=message, text_color=color)
        self.lift()
        self.place(relx=1.0, x=-16, y=16, anchor="ne")
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(ms, self.hide)

    def hide(self) -> None:
        self.place_forget()


class MicLevelMeter(ctk.CTkProgressBar):
    """Small live-input meter fed by the recorder's level queue."""

    def __init__(self, master, width: int = 180, **kwargs):
        super().__init__(
            master,
            width=width,
            height=10,
            corner_radius=5,
            fg_color=theme.INPUT_BG,
            progress_color=theme.ACCENT,
            mode="determinate",
            **kwargs,
        )
        self.set(0)

    def push(self, level: float) -> None:
        level = max(0.0, min(1.0, level * 14.0))
        self.set(level)


class AudioPlayerBar(ctk.CTkFrame):
    """A compact player sharing the application-wide AudioPlayer instance."""

    def __init__(self, master, player, **kwargs):
        super().__init__(master, fg_color=theme.INPUT_BG, corner_radius=12, **kwargs)
        self.player = player
        self._file: Optional[Path] = None
        self._last_playing_state: Optional[str] = None
        self._seeking = False

        self._btn_play = ctk.CTkButton(
            self, text="▶ Play", width=92, height=32,
            command=self._toggle, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            font=theme.font(13, "bold"),
        )
        self._btn_play.grid(row=0, column=0, padx=(10, 4), pady=6)

        self._btn_stop = ctk.CTkButton(
            self, text="⏹ Stop", width=72, height=32,
            command=self._stop, font=theme.font(13),
            fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
        )
        self._btn_stop.grid(row=0, column=1, padx=4, pady=6)

        self._lbl_time = ctk.CTkLabel(self, text="00:00 / 00:00", font=theme.font(12), text_color=theme.SUBTEXT)
        self._lbl_time.grid(row=0, column=2, padx=10)

        self._slider = ctk.CTkSlider(self, from_=0, to=100, number_of_steps=100, height=14)
        self._slider.set(0)
        self._slider.configure(command=self._on_slider)
        self._slider.grid(row=0, column=3, sticky="ew", padx=(4, 16), pady=6)
        self.grid_columnconfigure(3, weight=1)

        self._poll()

    # ------------------------------------------------------------ public
    def set_file(self, path: str | Path, autoplay: bool = True) -> None:
        p = Path(path)
        try:
            self.player.load(p)
        except Exception as exc:
            raise exc
        self._file = p
        self._lbl_time.configure(text=f"00:00 / {fmt_clock(self.player.duration)}")
        self._slider.set(0)
        if autoplay:
            self.player.play()

    def clear(self) -> None:
        if self.player.path == self._file:
            self.player.stop()
        self._file = None
        self._lbl_time.configure(text="00:00 / 00:00")
        self._slider.set(0)

    # ------------------------------------------------------------ actions
    def _toggle(self) -> None:
        if self.player.path != self._file or self._file is None:
            return
        if self.player.state == self.player.PLAYING:
            self.player.pause()
        else:
            self.player.play()

    def _stop(self) -> None:
        if self.player.path == self._file:
            self.player.stop()

    def _on_slider(self, value) -> None:
        if self.player.path == self._file:
            self.player.seek_to(float(value) / 100.0 * self.player.duration)

    # ------------------------------------------------------------ polling
    def _poll(self) -> None:
        try:
            active = self.player.path == self._file and self._file is not None
            ended = False
            if active:
                self.player.pump()
                ended = self.player.state == self.player.IDLE and self.player.duration > 0
                pos = self.player.position()
                self._lbl_time.configure(
                    text=f"{fmt_clock(pos)} / {fmt_clock(self.player.duration)}"
                )
                if not self._seeking and self.player.duration > 0:
                    frac = pos / self.player.duration * 100.0
                    if abs(self._slider.get() - frac) > 1.0:
                        self._slider.set(min(100.0, frac))
                if self.player.state == self.player.PLAYING:
                    self._btn_play.configure(text="⏸ Pause")
                elif self.player.state == self.player.PAUSED:
                    self._btn_play.configure(text="▶ Play")
                elif ended:
                    self._btn_play.configure(text="▶ Play")
            else:
                if self._file is not None:
                    self._lbl_time.configure(text="00:00 / 00:00")
                    self._slider.set(0)
                    self._file = None
                self._btn_play.configure(text="▶ Play")
        except Exception:
            pass
        self.after(200, self._poll)


class Screen(ctk.CTkFrame):
    """Base class for all sidebar screens."""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._status_holder = None

    def on_show(self, **kwargs) -> None:
        """Hook called every time the screen becomes visible."""

    def on_hide(self) -> None:
        """Hook called when the screen leaves focus (used to stop loops)."""

    def set_status(self, label_widget, text: str, color: str = theme.SUBTEXT) -> None:
        if label_widget is not None:
            label_widget.configure(text=text, text_color=color)

    def toast(self, message: str, kind: str = "info") -> None:
        self.app.toast(message, kind)

    def run_between(self, future: Future, on_success: Callable, on_error: Callable) -> None:
        bind_future(self, future, on_success, on_error)
