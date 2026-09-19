"""Main application window: sidebar navigation + screen router + shared services."""
from __future__ import annotations

import queue
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from app import config
from app.core.async_runner import AsyncRunner
from app.core.player import AudioPlayer
from app.core.recorder import Recorder
from app.core.stt_engine import STTEngine
from app.core.tts_engine import TTSEngine
from app.core.voice_cloner import VoiceCloner
from app.gui import theme
from app.gui.screens import (
    HistoryScreen,
    HomeScreen,
    MyVoiceScreen,
    SettingsScreen,
    SpeechToTextScreen,
    TextToSpeechScreen,
    VoiceRecorderScreen,
)
from app.gui.widgets import Toast
from app.services.file_service import ensure_dirs
from app.services.history_service import HistoryService
from app.services.settings_service import SettingsService

WINDOW_SIZE = "1240x760"
MIN_SIZE = (1020, 640)

SCREENS = [
    ("home", "Home"),
    ("text_to_speech", "Text to Speech"),
    ("voice_recorder", "Voice Recorder"),
    ("speech_to_text", "Speech to Text"),
    ("my_voice", "My Voice"),
    ("history", "History"),
    ("settings", "Settings"),
]


class VoiceStudioApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__(fg_color=theme.APP_BG)
        theme.apply_theme()
        ensure_dirs()

        # ----------------------------------------------------- shared services
        self.settings = SettingsService().load()
        self.history = HistoryService().load()
        self.runner = AsyncRunner()

        from app.services import file_service

        # apply a previously chosen output folder (if any)
        saved_output = self.settings.get("output", "folder", "")
        if saved_output and Path(saved_output) != file_service.category_dir("tts").parent:
            try:
                file_service.set_output_root(saved_output)
            except Exception:
                pass

        self.recorder = Recorder(
            samplerate=int(self.settings.get("recorder", "samplerate", 44100)),
            channels=int(self.settings.get("recorder", "channels", 1)),
            device=self.settings.get("recorder", "device"),
        )

        self.tts = TTSEngine(self.runner, file_service.category_dir("tts"))
        self.stt = STTEngine(
            language=self.settings.get("stt", "language", "en-US"),
            engine=self.settings.get("stt", "engine", "google"),
        )
        self.cloner = VoiceCloner()
        try:
            self.player = AudioPlayer()
        except Exception:
            self.player = None

        self.toast_widget: Optional[Toast] = None

        self.ui_queue: queue.Queue = queue.Queue()
        self.after(100, self._drain_ui_queue)

        self.title(f"{config.APP_NAME}  ·  v{config.APP_VERSION}")
        self.geometry(WINDOW_SIZE)
        self.minsize(*MIN_SIZE)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._build_sidebar()
        self._build_content()

        self.toast_widget = Toast(self)
        self._screens: dict = {}

        self._current: Optional[str] = None

        # wire filtered screen classes to build lazily
        self._build_screens()
        self.show_screen("home")

        # warm things up in the background
        self.after(300, self._background_startup)

    # ------------------------------------------------------------------ UI
    def _build_sidebar(self) -> None:
        self.sidebar = ctk.CTkFrame(
            self, width=220, corner_radius=0, fg_color=theme.SIDEBAR_BG
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(10, weight=1)

        ctk.CTkLabel(
            self.sidebar,
            text="AI Voice Studio",
            font=theme.font(20, "bold"),
            text_color=theme.TEXT,
        ).grid(row=0, column=0, pady=(24, 2), padx=16)
        ctk.CTkLabel(
            self.sidebar,
            text="Text · Speech · Voice",
            font=theme.font(11),
            text_color=theme.SUBTEXT,
        ).grid(row=1, column=0, pady=(0, 18))

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        for row, (key, label) in enumerate(SCREENS, start=2):
            btn = ctk.CTkButton(
                self.sidebar,
                text=label,
                command=lambda k=key: self.show_screen(k),
                height=40,
                corner_radius=10,
                font=theme.font(14),
                anchor="w",
                fg_color="transparent",
                text_color=theme.TEXT,
                hover_color=theme.CARD_BG,
            )
            btn.grid(row=row, column=0, sticky="ew", padx=12, pady=3)
            self._nav_buttons[key] = btn

        self.version_lbl = ctk.CTkLabel(
            self.sidebar,
            text=f"v{config.APP_VERSION}",
            font=theme.font(11),
            text_color=theme.SUBTEXT,
        )
        self.version_lbl.grid(row=20, column=0, pady=(8, 14))

    def _build_content(self) -> None:
        self.content = ctk.CTkFrame(self, fg_color=theme.APP_BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.title_lbl = ctk.CTkLabel(
            self.content, text="Home", font=theme.font(24, "bold"), text_color=theme.TEXT,
            anchor="w",
        )
        self.title_lbl.grid(row=0, column=0, sticky="ew", padx=28, pady=(18, 6))

        self.subtitle_lbl = ctk.CTkLabel(
            self.content, text="", font=theme.font(13), text_color=theme.SUBTEXT, anchor="w",
        )
        self.subtitle_lbl.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 8))

        self.screen_container = ctk.CTkFrame(self.content, fg_color="transparent")
        self.screen_container.grid(row=2, column=0, sticky="nsew", padx=28, pady=(4, 20))
        self.screen_container.grid_rowconfigure(0, weight=1)
        self.screen_container.grid_columnconfigure(0, weight=1)

    def _build_screens(self) -> None:
        self._screens = {
            "home": HomeScreen(self.screen_container, self),
            "text_to_speech": TextToSpeechScreen(self.screen_container, self),
            "voice_recorder": VoiceRecorderScreen(self.screen_container, self),
            "speech_to_text": SpeechToTextScreen(self.screen_container, self),
            "my_voice": MyVoiceScreen(self.screen_container, self),
            "history": HistoryScreen(self.screen_container, self),
            "settings": SettingsScreen(self.screen_container, self),
        }
        for screen in self._screens.values():
            screen.grid(row=0, column=0, sticky="nsew")
            screen.grid_remove()

    # ------------------------------------------------------------ navigation
    def show_screen(self, key: str, **kwargs) -> None:
        if key not in self._screens:
            return
        if self._current == key and not kwargs:
            return
        if self._current is not None:
            self._screens[self._current].on_hide()
            self._screens[self._current].grid_remove()
        self._current = key
        screen = self._screens[key]
        screen.grid()
        screen.lift()
        screen.on_show(**kwargs)

        self.title_lbl.configure(text=dict(SCREENS)[key], text_color=theme.TEXT)
        subtitles = {
            "home": "Everything in one place.",
            "text_to_speech": "Convert text into natural speech.",
            "voice_recorder": "Capture your own voice.",
            "speech_to_text": "Turn spoken words into text.",
            "my_voice": "Generate speech that sounds like your voice.",
            "history": "All your generated and recorded items.",
            "settings": "Defaults, devices and storage.",
        }
        self.subtitle_lbl.configure(text=subtitles.get(key, ""))

        for nav_key, btn in self._nav_buttons.items():
            if nav_key == key:
                btn.configure(fg_color=theme.ACCENT, text_color="#ffffff")
            else:
                btn.configure(fg_color="transparent", text_color=theme.TEXT)

    def send_text_to_tts(self, text: str) -> None:
        """Hand off transcribed/edited text to the Text-to-Speech screen."""
        self.show_screen("text_to_speech", prefill=text)

    # -------------------------------------------------------------- misc
    def schedule(self, fn: Callable) -> None:
        """Queue a callable to run on the Tk main loop (thread-safe)."""
        self.ui_queue.put(fn)

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    def toast(self, message: str, kind: str = "info") -> None:
        if self.toast_widget is not None:
            self.toast_widget.show(message, kind)

    def _background_startup(self) -> None:
        # Load neural voices in the background; screens handle their own states.
        self.tts.refresh_voices_async(on_done=lambda: None)

    def _on_close(self) -> None:
        if self._current is not None:
            self._screens[self._current].on_hide()
        self.runner.stop()
        if self.player is not None:
            self.player.close()
        self.destroy()


def run() -> None:
    app = VoiceStudioApp()
    app.mainloop()
