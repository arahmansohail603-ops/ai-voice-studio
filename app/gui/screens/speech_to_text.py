"""Speech-to-Text screen: live listening, transcription, copy, send to TTS."""
from __future__ import annotations

import customtkinter as ctk

from app.core.errors import (MicPermissionError, MissingDependencyError,
                             NetworkError)
from app.gui import theme
from app.gui.widgets import BusyButton, Screen

COMMON_LANGUAGES = [
    "en-US", "en-GB", "es-ES", "fr-FR", "de-DE", "it-IT", "pt-BR",
    "hi-IN", "ja-JP", "ko-KR", "zh-CN", "ar-SA", "ru-RU", "nl-NL",
]


class SpeechToTextScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ------------------------------------------------------------ controls
        controls = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        controls.grid(row=0, column=0, sticky="ew")
        controls.grid_columnconfigure(5, weight=1)

        self.mic_status = ctk.CTkLabel(
            controls, text="Idle.", font=theme.font(13, "bold"), text_color=theme.SUBTEXT,
        )
        self.mic_status.grid(row=0, column=0, columnspan=6, sticky="w", padx=16, pady=(12, 4))

        self.listen_btn = BusyButton(
            controls, text="Start Listening", command=self._toggle,
            width=160, height=38, font=theme.font(14, "bold"),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )
        self.listen_btn.grid(row=1, column=0, padx=(16, 8), pady=(0, 12))

        ctk.CTkLabel(controls, text="Language:", font=theme.font(13),
                     text_color=theme.SUBTEXT).grid(row=1, column=1, padx=(8, 4), pady=(0, 12))
        self.lang_menu = ctk.CTkOptionMenu(
            controls, values=COMMON_LANGUAGES, width=120, dynamic_resizing=False,
            font=theme.font(13), fg_color=theme.INPUT_BG, button_color=theme.ACCENT,
            command=self._on_language,
        )
        self.lang_menu.set(self.app.stt.language)
        self.lang_menu.grid(row=1, column=2, padx=4, pady=(0, 12))

        ctk.CTkLabel(controls, text="Engine:", font=theme.font(13),
                     text_color=theme.SUBTEXT).grid(row=1, column=3, padx=(8, 4), pady=(0, 12))
        self.engine_menu = ctk.CTkOptionMenu(
            controls, values=self.app.stt.available_engines(), width=110,
            dynamic_resizing=False, font=theme.font(13),
            fg_color=theme.INPUT_BG, button_color=theme.ACCENT, command=self._on_engine,
        )
        self.engine_menu.grid(row=1, column=4, padx=4, pady=(0, 12))
        self.engine_menu.set(self.app.stt.engine)

        self.copy_btn = ctk.CTkButton(
            controls, text="⧉ Copy", command=self._copy, width=90, height=38,
            font=theme.font(13), fg_color=theme.INPUT_BG,
            border_width=1, border_color=theme.BORDER,
        )
        self.copy_btn.grid(row=1, column=5, sticky="e", padx=(8, 16), pady=(0, 12))

        self.send_btn = ctk.CTkButton(
            controls, text="Send to Text-to-Speech", command=self._send_to_tts,
            width=170, height=38, font=theme.font(13, "bold"),
            fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
        )
        self.send_btn.grid(row=1, column=6, sticky="e", padx=(8, 16), pady=(0, 12))

        # -------------------------------------------------------- transcription
        transcript = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        transcript.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        transcript.grid_rowconfigure(1, weight=1)
        transcript.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            transcript, text="Live transcription", font=theme.font(14, "bold"),
            text_color=theme.TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 4))

        self.output = ctk.CTkTextbox(
            transcript, wrap="word", corner_radius=10, fg_color=theme.INPUT_BG,
            text_color=theme.TEXT, border_width=1, border_color=theme.BORDER,
            font=theme.font(14),
        )
        self.output.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

        self.status_lbl = ctk.CTkLabel(
            self, text="Google recognition requires internet. Whisper runs offline.",
            font=theme.font(12), text_color=theme.SUBTEXT, anchor="w",
        )
        self.status_lbl.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        self._listening = False
        self._session_entries = 0

    # ------------------------------------------------------------ lifecycle
    def on_show(self, **kwargs) -> None:
        self._update_mic_indicator()

    def on_hide(self) -> None:
        if self._listening:
            self.app.stt.stop_listening()
            self._listening = False
            self.listen_btn.set_busy(False, "🎙 Start Listening")
            self.mic_status.configure(text="Stopped.", text_color=theme.SUBTEXT)

    def _update_mic_indicator(self) -> None:
        if self.app.stt.has_microphone:
            self.mic_status.configure(text="Microphone detected.", text_color=theme.SUCCESS)
        else:
            self.mic_status.configure(
                text="No microphone found — enable access in Windows privacy settings.",
                text_color=theme.DANGER,
            )

    # ------------------------------------------------------------- handlers
    def _on_language(self, value: str) -> None:
        self.app.stt.language = value
        self.app.settings.set("stt", "language", value)

    def _on_engine(self, value: str) -> None:
        self.app.stt.engine = value
        self.app.settings.set("stt", "engine", value)

    def _toggle(self) -> None:
        if self._listening:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        self.listen_btn.set_busy(True, "Listening…")
        self._session_entries = 0

        def on_text(text):
            self.app.schedule(lambda: self._append(text))

        def on_status(status):
            self.app.schedule(lambda: self._set_busy_status(status))

        def on_error(exc):
            self.app.schedule(lambda: self._on_error(exc))

        started = self.app.stt.start_listening(on_text, on_status, on_error)
        if started:
            self._listening = True
            self.mic_status.configure(text="Listening…", text_color=theme.SUCCESS)
        else:
            self.listen_btn.set_busy(False)
            self.mic_status.configure(text="Could not start listening.", text_color=theme.DANGER)

    def _stop(self) -> None:
        self.app.stt.stop_listening()
        self._listening = False
        self.listen_btn.set_busy(False, "🎙 Start Listening")
        self.mic_status.configure(text="Stopped.", text_color=theme.SUBTEXT)
        self.status_lbl.configure(
            text=f"{self._session_entries} phrase(s) transcribed this session.",
            text_color=theme.SUCCESS,
        )

    def _set_busy_status(self, status: str) -> None:
        self.status_lbl.configure(text=status, text_color=theme.WARNING)
        if "Recognising" in status:
            self.listen_btn.set_busy(True, "Recognising…")

    def _append(self, text: str) -> None:
        self._session_entries += 1
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.listen_btn.set_busy(True, "Listening…")
        self.mic_status.configure(text="Listening…", text_color=theme.SUCCESS)
        self.status_lbl.configure(
            text=f"Recognised: {self._session_entries} phrase(s) so far.",
            text_color=theme.SUCCESS,
        )

    def _on_error(self, exc: Exception) -> None:
        self._listening = False
        self.listen_btn.set_busy(False, "🎙 Start Listening")
        if isinstance(exc, MicPermissionError):
            self.mic_status.configure(text=str(exc).split("\n")[0], text_color=theme.DANGER)
        elif isinstance(exc, NetworkError):
            self.status_lbl.configure(text=str(exc), text_color=theme.DANGER)
        elif isinstance(exc, (MissingDependencyError,)):
            self.status_lbl.configure(text=str(exc).split("\n")[0], text_color=theme.DANGER)
        else:
            self.status_lbl.configure(text=f"Recognition issue: {exc}", text_color=theme.DANGER)

    # ------------------------------------------------------------- actions
    def _current_text(self) -> str:
        return self.output.get("1.0", "end-1c").strip()

    def _copy(self) -> None:
        text = self._current_text()
        if not text:
            self.toast("Nothing to copy yet.", "warn")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.toast("Copied to clipboard.", "ok")

    def _send_to_tts(self) -> None:
        text = self._current_text()
        if not text:
            self.toast("Transcribe something first.", "warn")
            return
        if self._listening:
            self._stop()
        self.app.send_text_to_tts(text)
        self.toast("Text sent to Text-to-Speech.", "ok")
