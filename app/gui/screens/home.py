"""Home dashboard: quick navigation cards + engine health status."""
from __future__ import annotations

import customtkinter as ctk

from app.gui import theme
from app.gui.widgets import Screen


class HomeScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkLabel(
            self, text="Welcome to AI Voice Studio",
            font=theme.font(20, "bold"), text_color=theme.TEXT, anchor="w",
        )
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ctk.CTkLabel(
            self, text="Convert text to natural speech, record your voice, transcribe, and more.",
            font=theme.font(13), text_color=theme.SUBTEXT, anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 18))

        # ---- quick launch cards in a 2-column grid -------------------------
        launcher = ctk.CTkFrame(self, fg_color="transparent")
        launcher.grid(row=2, column=0, sticky="ew")
        launcher.grid_columnconfigure((0, 1), weight=1, uniform="launch")

        self._cards = {}
        cards = [
            ("text_to_speech", "Text to Speech",
             "Type or paste text and convert it to natural-sounding speech."),
            ("voice_recorder", "Voice Recorder",
             "Record your own voice, pause, playback and save a voice note."),
            ("speech_to_text", "Speech to Text",
             "Speak into the mic and get live text, then send it to TTS."),
            ("my_voice", "My Voice",
             "Create a voice profile from an authorised recording and speak with it."),
            ("history", "History",
             "Browse every generated speech, voice note and transcription."),
            ("settings", "Settings",
             "Configure voices, devices, output folders and data."),
        ]
        for i, (key, title, desc) in enumerate(cards):
            row, col = divmod(i, 2)
            card = self._make_card(launcher, key, title, desc)
            card.grid(row=row, column=col, sticky="ew", padx=8, pady=8)
            self._cards[key] = card

        # ---- engine status -------------------------------------------------
        ctk.CTkLabel(
            self, text="Engine status", font=theme.font(16, "bold"),
            text_color=theme.TEXT, anchor="w",
        ).grid(row=3, column=0, sticky="ew", pady=(22, 4))

        status = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        status.grid(row=4, column=0, sticky="ew")
        status.grid_columnconfigure(1, weight=1)

        rows = [
            ("tts", "Text-to-Speech engine"),
            ("mic", "Microphone"),
            ("stt", "Recognition engine"),
            ("clone", "Voice cloning model"),
            ("ffmpeg", "MP3 support (ffmpeg)"),
        ]
        self._status_vars = {}
        for i, (key, label) in enumerate(rows):
            ctk.CTkLabel(
                status, text=label, font=theme.font(13), text_color=theme.SUBTEXT, anchor="w",
            ).grid(row=i, column=0, sticky="w", padx=(18, 24), pady=9)
            value_lbl = ctk.CTkLabel(
                status, text="…", font=theme.font(13, "bold"), text_color=theme.SUBTEXT, anchor="w",
            )
            value_lbl.grid(row=i, column=1, sticky="w", pady=9)
            self._status_vars[key] = value_lbl

        self.status_clone_lbl = self._status_vars

    # ------------------------------------------------------------------ UI
    def _make_card(self, master, key, title, desc):
        card = ctk.CTkFrame(master, fg_color=theme.CARD_BG, corner_radius=14, height=120)
        card.grid_propagate(False)
        ctk.CTkLabel(
            card, text=title, font=theme.font(15, "bold"), text_color=theme.TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 2))
        ctk.CTkLabel(
            card, text=desc, font=theme.font(12), text_color=theme.SUBTEXT,
            wraplength=300, justify="left", anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 6))
        ctk.CTkButton(
            card, text="Open →", width=100, height=30,
            command=lambda k=key: self.app.show_screen(k),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER, font=theme.font(13, "bold"),
        ).grid(row=2, column=0, sticky="w", padx=18, pady=(2, 12))
        return card

    # ------------------------------------------------------------ lifecycle
    def on_show(self, **kwargs) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        from app.core import audio_utils
        from app.core.voice_cloner import CloneState

        tts = self.app.tts
        mode = "edge-tts" if tts.mode == tts.MODE_EDGE else "pyttsx3 (offline)"
        color = theme.SUCCESS if tts.mode == tts.MODE_EDGE else theme.WARNING
        self._set("tts", f"{mode} · {len(tts.voices())} voices", color)

        mic = self.app.recorder.has_microphone or self.app.stt.has_microphone
        self._set("mic", "Ready" if mic else "No device found",
                  theme.SUCCESS if mic else theme.DANGER)

        stt = self.app.stt.engine
        self._set("stt", f"{stt} (Google engine)", theme.SUCCESS)

        state = self.app.cloner.state
        state_text = {
            CloneState.NOT_LOADED: "Not loaded — loads on first use",
            CloneState.LOADING: "Loading…",
            CloneState.READY: "Ready",
            CloneState.ERROR: "Unavailable (see My Voice)",
        }.get(state, str(state))
        state_color = {
            CloneState.READY: theme.SUCCESS,
            CloneState.LOADING: theme.WARNING,
            CloneState.ERROR: theme.DANGER,
        }.get(state, theme.SUBTEXT)
        self._set("clone", state_text, state_color, "  ·  falls back to neural voice" if state != CloneState.READY else "")

        ff = audio_utils.ffmpeg_available()
        self._set("ffmpeg", "Available — MP3 export enabled" if ff else "Missing — WAV only",
                  theme.SUCCESS if ff else theme.WARNING)

    def _set(self, key: str, text: str, color: str, suffix: str = "") -> None:
        lbl = self._status_vars.get(key)
        if lbl is not None:
            lbl.configure(text=text + suffix, text_color=color)
