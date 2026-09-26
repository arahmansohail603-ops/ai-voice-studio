from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from app.gui import theme
from app.gui.widgets import Screen


class HomeScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QLabel("Welcome to AI Voice Studio", self)
        header.setFont(theme.font(20, "bold"))
        header.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        layout.addWidget(header)

        description = QLabel(
            "Convert text to natural speech, record your voice, transcribe, and more.",
            self,
        )
        description.setFont(theme.font(13))
        description.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        layout.addWidget(description)
        layout.addSpacing(10)

        launcher = QFrame(self)
        launcher.setStyleSheet("QFrame { background: transparent; border: none; }")
        launcher_layout = QGridLayout(launcher)
        launcher_layout.setContentsMargins(0, 0, 0, 0)
        launcher_layout.setHorizontalSpacing(16)
        launcher_layout.setVerticalSpacing(16)
        for column in range(2):
            launcher_layout.setColumnStretch(column, 1)
        self._cards = {}
        cards = [
            (
                "text_to_speech",
                "Text to Speech",
                "Type or paste text and convert it to natural-sounding speech.",
            ),
            (
                "voice_recorder",
                "Voice Recorder",
                "Record your own voice, pause, playback and save a voice note.",
            ),
            (
                "speech_to_text",
                "Speech to Text",
                "Speak into the mic and get live text, then send it to TTS.",
            ),
            (
                "my_voice",
                "My Voice",
                "Create a voice profile from an authorised recording and "
                "speak with it.",
            ),
            (
                "history",
                "History",
                "Browse every generated speech, voice note and transcription.",
            ),
            (
                "settings",
                "Settings",
                "Configure voices, devices, output folders and data.",
            ),
        ]
        for index, (key, title, description_text) in enumerate(cards):
            row, column = divmod(index, 2)
            card = self._make_card(launcher, key, title, description_text)
            launcher_layout.addWidget(card, row, column)
            self._cards[key] = card
        layout.addWidget(launcher)
        layout.addSpacing(16)

        status_title = QLabel("Engine status", self)
        status_title.setFont(theme.font(16, "bold"))
        status_title.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        layout.addWidget(status_title)
        layout.addSpacing(4)

        status = QFrame(self)
        status.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        status_layout = QGridLayout(status)
        status_layout.setContentsMargins(18, 8, 18, 8)
        status_layout.setHorizontalSpacing(24)
        status_layout.setVerticalSpacing(0)
        status_layout.setColumnStretch(1, 1)
        rows = [
            ("tts", "Text-to-Speech engine"),
            ("mic", "Microphone"),
            ("stt", "Recognition engine"),
            ("clone", "Voice cloning model"),
            ("ffmpeg", "MP3 support (ffmpeg)"),
        ]
        self._status_vars = {}
        for row, (key, label_text) in enumerate(rows):
            label = QLabel(label_text, status)
            label.setFont(theme.font(13))
            label.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
            status_layout.addWidget(label, row, 0)
            value = QLabel("…", status)
            value.setFont(theme.font(13, "bold"))
            value.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
            status_layout.addWidget(value, row, 1)
            self._status_vars[key] = value
        layout.addWidget(status)
        layout.addStretch(1)
        self.status_clone_lbl = self._status_vars

    def _make_card(self, master, key, title, description):
        card = QFrame(master)
        card.setMinimumHeight(120)
        card.setStyleSheet(theme.frame_style(theme.CARD_BG, 14))
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(4)
        title_label = QLabel(title, card)
        title_label.setFont(theme.font(15, "bold"))
        title_label.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        layout.addWidget(title_label)
        description_label = QLabel(description, card)
        description_label.setFont(theme.font(12))
        description_label.setWordWrap(True)
        description_label.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        layout.addWidget(description_label)
        button = QPushButton("Open →", card)
        button.setFixedSize(100, 30)
        button.setFont(theme.font(13, "bold"))
        button.setStyleSheet(
            theme.button_style(theme.ACCENT, theme.ACCENT_HOVER, theme.ON_ACCENT, 8)
        )
        button.clicked.connect(
            lambda _checked=False, name=key: self.app.show_screen(name)
        )
        layout.addWidget(button, 0, Qt.AlignLeft)
        return card

    def on_show(self, **kwargs) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        from app.core import audio_utils
        from app.core.voice_cloner import CloneState

        tts = self.app.tts
        if tts.mode == tts.MODE_QWEN:
            mode, color = "Qwen3-TTS (local)", theme.SUCCESS
        elif tts.mode == tts.MODE_PIPER:
            mode, color = "Piper (offline)", theme.SUCCESS
        else:
            mode, color = "System voices (offline)", theme.SUCCESS
        self._set("tts", f"{mode} · {len(tts.voices())} voices", color)

        mic = self.app.recorder.has_microphone or self.app.stt.has_microphone
        self._set(
            "mic",
            "Ready" if mic else "No device found",
            theme.SUCCESS if mic else theme.DANGER,
        )

        stt_label = {
            "vosk": "Vosk (offline)",
            "whisper": "Whisper (offline)",
        }.get(self.app.stt.engine, self.app.stt.engine)
        self._set("stt", stt_label, theme.SUCCESS)

        if not self.app.settings.get("clone", "enabled", True):
            self._set(
                "clone",
                "Off — Heavy / experimental",
                theme.SUBTEXT,
                "  ·  enable in Settings on a high-RAM machine",
            )
        else:
            engine = self.app.active_cloner
            engine_label = (
                "Qwen3-TTS (local)" if engine is self.app.qwen_cloner else "XTTS-v2"
            )
            state = engine.state
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
            self._set(
                "clone",
                f"{state_text} ({engine_label})",
                state_color,
                "  ·  falls back to a system voice"
                if state != CloneState.READY
                else "",
            )

        ffmpeg = audio_utils.ffmpeg_available()
        self._set(
            "ffmpeg",
            "Available — MP3 export enabled" if ffmpeg else "Missing — WAV only",
            theme.SUCCESS if ffmpeg else theme.WARNING,
        )

    def _set(self, key: str, text: str, color: str, suffix: str = "") -> None:
        label = self._status_vars.get(key)
        if label is not None:
            label.setText(text + suffix)
            label.setStyleSheet(theme.label_style(color, "left"))
