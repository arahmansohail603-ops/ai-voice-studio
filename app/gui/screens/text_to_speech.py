from __future__ import annotations

import re
import threading
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from app.config import language_display_name
from app.core import audio_utils
from app.core.errors import AppError, TranslationModelConsentRequired
from app.core.translator import Translator
from app.core.piper_voices import PiperVoiceLibrary, is_speakable
from app.gui import theme
from app.gui.widgets import (
    AudioPlayerBar,
    BusyButton,
    MethodBadge,
    Screen,
    TextEdit,
)

_SPEED_STEPS = 30
_ALL_VOICES = "🌐 All languages"
_PITCH_STEPS = 40
TRANSLATE_OFF = "Off (no translation)"
#: Marks a language the app can translate into but never speak, because Piper
#: publishes no voice for it. Without this the user only discovers it after
#: waiting for a translation and pressing a download that can never work.
TEXT_ONLY_SUFFIX = " — text only"


def _build_translate_options() -> dict[str, str]:
    """Label -> code for every translatable language.

    Read at import time from the *cached* voice catalog only; on a first run
    there is no cache yet, so nothing is annotated and the convert-time check
    does the work instead. Never blocks startup on the network.
    """
    options: dict[str, str] = {}
    for code in Translator.supported_codes():
        label = f"{language_display_name(code)} ({code})"
        if not is_speakable(code):
            label += TEXT_ONLY_SUFFIX
        options[label] = code
    return options


TRANS_OPTIONS = _build_translate_options()
_SCRIPT_LANGS = {
    "Urdu/Arabic": "ur",
    "Hindi/Devanagari": "hi",
    "Japanese/Chinese": "ja",
    "English/Latin": "en",
}


class _Value:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value

    def set(self, value) -> None:
        self._value = value


class TextToSpeechScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self._lang_map = {}
        self._voice_map = {}
        self._all_voices_mode = True
        self._last_result = None
        self._generating = False
        self._prefill_lang_hint = None
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._auto_match_voice)
        self._gen_text = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        text_panel = QFrame(self)
        text_panel.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        text_layout = QVBoxLayout(text_panel)
        text_layout.setContentsMargins(16, 12, 16, 12)
        text_layout.setSpacing(4)
        text_label = QLabel("Your text", text_panel)
        text_label.setFont(theme.font(14, "bold"))
        text_label.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        text_layout.addWidget(text_label)
        self.textbox = TextEdit(text_panel)
        self.textbox.setFixedHeight(170)
        self.textbox.setAcceptRichText(False)
        self.textbox.setStyleSheet(theme.input_style())
        self.textbox.setPlainText(
            "Welcome to AI Voice Studio. Type or paste anything here and press "
            "Convert to Speech to hear it as natural human-like audio."
        )
        self.textbox.textChanged.connect(self._on_text_modified)
        text_layout.addWidget(self.textbox, 1)
        root.addWidget(text_panel, 1)

        controls = QFrame(self)
        controls.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        controls_layout = QGridLayout(controls)
        controls_layout.setContentsMargins(16, 12, 16, 12)
        controls_layout.setHorizontalSpacing(8)
        controls_layout.setVerticalSpacing(2)
        controls_layout.setColumnStretch(4, 1)

        self._control_label(controls_layout, "Language", 0, 0)
        self.lang_menu = self._combo(controls, ["Loading…"], 190)
        self.lang_menu.currentTextChanged.connect(self._on_language)
        controls_layout.addWidget(self.lang_menu, 1, 0)

        self._control_label(controls_layout, "Voice", 0, 1)
        self.voice_menu = self._combo(controls, ["Loading…"], 300)
        self.voice_menu.currentTextChanged.connect(self._on_voice)
        controls_layout.addWidget(self.voice_menu, 1, 1)

        self.refresh_btn = QPushButton("↻ Voices", controls)
        self.refresh_btn.setFixedWidth(90)
        self.refresh_btn.setFont(theme.font(12))
        self.refresh_btn.setStyleSheet(
            theme.button_style(
                theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 8, theme.BORDER, 1
            )
        )
        self.refresh_btn.clicked.connect(self._refresh_voices)
        controls_layout.addWidget(self.refresh_btn, 1, 2)

        self._control_label(controls_layout, "Speed", 0, 3)
        speed_row = QFrame(controls)
        speed_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        speed_layout = QHBoxLayout(speed_row)
        speed_layout.setContentsMargins(0, 0, 0, 0)
        self.speed_var = _Value(1.0)
        self.speed_slider = QSlider(Qt.Horizontal, speed_row)
        self.speed_slider.setRange(0, _SPEED_STEPS)
        self.speed_slider.setValue(_SPEED_STEPS)
        self.speed_slider.valueChanged.connect(self._on_speed)
        speed_layout.addWidget(self.speed_slider, 1)
        self.speed_lbl = QLabel("1.00x", speed_row)
        self.speed_lbl.setFixedWidth(52)
        self.speed_lbl.setFont(theme.font(12))
        self.speed_lbl.setStyleSheet(theme.label_style(theme.TEXT))
        speed_layout.addWidget(self.speed_lbl)
        controls_layout.addWidget(speed_row, 1, 3)

        self._control_label(controls_layout, "Pitch", 0, 4)
        pitch_row = QFrame(controls)
        pitch_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        pitch_layout = QHBoxLayout(pitch_row)
        pitch_layout.setContentsMargins(0, 0, 0, 0)
        self.pitch_var = _Value(0)
        self.pitch_slider = QSlider(Qt.Horizontal, pitch_row)
        self.pitch_slider.setRange(-20, 20)
        self.pitch_slider.setValue(0)
        self.pitch_slider.valueChanged.connect(self._on_pitch)
        pitch_layout.addWidget(self.pitch_slider, 1)
        self.pitch_lbl = QLabel("0 Hz", pitch_row)
        self.pitch_lbl.setFixedWidth(52)
        self.pitch_lbl.setFont(theme.font(12))
        self.pitch_lbl.setStyleSheet(theme.label_style(theme.TEXT))
        pitch_layout.addWidget(self.pitch_lbl)
        controls_layout.addWidget(pitch_row, 1, 4)

        self._control_label(controls_layout, "Translate to:", 2, 0)
        self.translate_menu = self._combo(
            controls, [TRANSLATE_OFF] + list(TRANS_OPTIONS), 190
        )
        self.translate_menu.setCurrentText(TRANSLATE_OFF)
        controls_layout.addWidget(self.translate_menu, 3, 1)
        note = QLabel(
            "Your text is translated first, then spoken in that language.",
            controls,
        )
        note.setFont(theme.font(11))
        note.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        controls_layout.addWidget(note, 3, 2, 1, 3)
        root.addWidget(controls)
        root.addSpacing(14)

        action = QFrame(self)
        action.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        action_layout = QGridLayout(action)
        action_layout.setContentsMargins(16, 14, 16, 12)
        action_layout.setColumnStretch(1, 1)
        self.convert_btn = BusyButton(
            action,
            text="Convert to Speech",
            command=self._convert,
            height=40,
            width=210,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
            font=theme.font(14, "bold"),
            corner_radius=10,
        )
        action_layout.addWidget(self.convert_btn, 0, 0)
        self.badge = MethodBadge(action, "pyttsx3")
        action_layout.addWidget(self.badge, 0, 1, Qt.AlignLeft | Qt.AlignVCenter)
        # Shown only when a translation lands in a language with no installed
        # voice, which is the one failure refreshing cannot fix.
        self.get_voice_btn = QPushButton("Get a voice", action)
        self.get_voice_btn.setFont(theme.font(12))
        self.get_voice_btn.setStyleSheet(
            theme.button_style(theme.ACCENT, theme.ACCENT_HOVER, theme.ON_ACCENT, 8, theme.ACCENT, 1)
        )
        self.get_voice_btn.clicked.connect(self._on_get_voice)
        self.get_voice_btn.setVisible(False)
        action_layout.addWidget(self.get_voice_btn, 0, 2, Qt.AlignRight | Qt.AlignVCenter)
        self.status_lbl = QLabel("Ready.", action)
        self.status_lbl.setFont(theme.font(12))
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        action_layout.addWidget(self.status_lbl, 1, 0, 1, 2)
        root.addWidget(action)
        root.addSpacing(14)

        player_panel = QFrame(self)
        player_panel.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        player_layout = QVBoxLayout(player_panel)
        player_layout.setContentsMargins(14, 10, 14, 12)
        player_layout.setSpacing(8)
        if app.player is not None:
            self.player_bar = AudioPlayerBar(player_panel, app.player)
            player_layout.addWidget(self.player_bar)
        else:
            self.player_bar = None
            unavailable = QLabel(
                "Playback unavailable (no audio output device). "
                "Audio files are still saved on disk.",
                player_panel,
            )
            unavailable.setFont(theme.font(13))
            unavailable.setWordWrap(True)
            unavailable.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            player_layout.addWidget(unavailable)
        save_row = QFrame(player_panel)
        save_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        save_layout = QHBoxLayout(save_row)
        save_layout.setContentsMargins(0, 0, 0, 0)
        save_label = QLabel("Save as:", save_row)
        save_label.setFont(theme.font(13))
        save_label.setStyleSheet(theme.label_style(theme.SUBTEXT))
        save_layout.addWidget(save_label)
        self.format_menu = self._combo(save_row, ["mp3", "wav"], 80)
        self.format_menu.setCurrentText("mp3")
        save_layout.addWidget(self.format_menu)
        self.save_btn = BusyButton(
            save_row,
            text="Save Audio",
            command=self._save_audio,
            width=120,
            height=32,
            font=theme.font(13, "bold"),
            fg_color=theme.INPUT_BG,
            hover_color=theme.CARD_BG,
            border_color=theme.BORDER,
            border_width=1,
        )
        save_layout.addWidget(self.save_btn)
        format_note = QLabel(
            "MP3 export requires ffmpeg; otherwise WAV is used.", save_row
        )
        format_note.setFont(theme.font(12))
        format_note.setStyleSheet(theme.label_style(theme.SUBTEXT))
        save_layout.addWidget(format_note)
        save_layout.addStretch(1)
        player_layout.addWidget(save_row)
        root.addWidget(player_panel)
        root.addStretch(0)

    @staticmethod
    def _combo(parent, values, width: int) -> QComboBox:
        combo = QComboBox(parent)
        combo.addItems(list(values))
        combo.setFixedWidth(width)
        combo.setFont(theme.font(13))
        combo.setStyleSheet(theme.combo_style())
        return combo

    @staticmethod
    def _control_label(layout, text: str, row: int, column: int) -> None:
        label = QLabel(text)
        label.setFont(theme.font(12, "bold"))
        label.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        layout.addWidget(label, row, column)

    def on_show(self, **kwargs) -> None:
        prefill = kwargs.get("prefill")
        if prefill:
            self.textbox.setPlainText(str(prefill))
            self.textbox.setFocus()
        self._prefill_lang_hint = kwargs.get("prefill_lang")
        if self.app.tts.voices_loaded:
            self._populate()
        else:
            self._refresh_voices()

    def _refresh_voices(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Loading…")
        self.status_lbl.setText("Loading offline voices…")
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))

        def done() -> None:
            self.app.schedule(self._populate)

        try:
            self.app.tts.refresh_voices_async(on_done=done)
        except Exception as exc:
            self.app.schedule(lambda error=exc: self._voice_refresh_failed(error))

    def _voice_refresh_failed(self, exc: Exception) -> None:
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("↻ Voices")
        self.status_lbl.setText(f"Could not load voices: {exc}")
        self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))

    def _populate(self) -> None:
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("↻ Voices")
        tts = self.app.tts
        langs = tts.languages()
        if not langs:
            self.status_lbl.setText(
                "No offline voices found. Install pyttsx3, or add a Piper voice "
                "model and refresh."
            )
            self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
            self._set_items(self.lang_menu, ["Unavailable"])
            self._set_items(self.voice_menu, ["Unavailable"])
            return
        mode = getattr(tts, "mode", "")
        if mode == getattr(tts, "MODE_QWEN", ""):
            status = (
                "Qwen3-TTS local neural voices — fully offline. "
                "CPU synthesis can be slow for long text."
            )
            color = theme.SUCCESS
        elif mode == getattr(tts, "MODE_PIPER", ""):
            status = "Piper offline neural voices ready."
            color = theme.SUCCESS
        else:
            status = (
                "Offline system voices — no internet required, pitch not supported."
            )
            color = theme.SUCCESS
        self.status_lbl.setText(status)
        self.status_lbl.setStyleSheet(theme.label_style(color, "left"))
        self.pitch_lbl.setText("n/a")

        self._lang_map = {
            f"{language_display_name(code)} ({code})": code for code in sorted(langs)
        }
        self._set_items(self.lang_menu, [_ALL_VOICES] + list(self._lang_map))
        hint_label = (
            self._label_for_lang(self._prefill_lang_hint)
            if self._prefill_lang_hint
            else None
        )
        if hint_label:
            self.lang_menu.setCurrentText(hint_label)
            self._all_voices_mode = False
        else:
            self.lang_menu.setCurrentText(_ALL_VOICES)
            self._all_voices_mode = True
        self._populate_voices()

    def _label_for_lang(self, code: str) -> str | None:
        for label, language in self._lang_map.items():
            if language == code:
                return label
        return None

    def _on_text_modified(self) -> None:
        self._auto_timer.start(350)

    def _auto_match_voice(self, force: bool = False) -> None:
        if not self._lang_map:
            return
        code = self._lang_for_script(self.textbox.get())
        if not code or code not in self._lang_map.values():
            return
        current_label = self.lang_menu.currentText()
        current_code = (
            self._lang_map.get(current_label, "") if not self._all_voices_mode else ""
        )
        if current_code == code:
            return
        if self._all_voices_mode and not force:
            return
        self._all_voices_mode = False
        label = self._label_for_lang(code)
        if label:
            self.lang_menu.setCurrentText(label)
        self._populate_voices(persist=False)

    def _on_language(self, label: str) -> None:
        if label == _ALL_VOICES:
            self._all_voices_mode = True
            code = ""
        else:
            self._all_voices_mode = False
            code = self._lang_map.get(label, "")
        if code:
            self.app.settings.set("tts", "language", code)
        self._populate_voices()

    def _populate_voices(self, persist: bool = True) -> None:
        tts = self.app.tts
        if self._all_voices_mode:
            voices = list(tts.voices())
        else:
            code = self._lang_map.get(self.lang_menu.currentText(), "")
            voices = tts.voices(code)
        if not voices:
            self._set_items(self.voice_menu, ["No voices available"])
            self.voice_menu.setCurrentText("No voices available")
            return
        self._voice_map = {
            self._voice_display(voice): {
                "short_name": voice["short_name"],
                "language": voice.get("language", ""),
            }
            for voice in voices
        }
        self._set_items(self.voice_menu, list(self._voice_map))
        saved_voice = self.app.settings.get("tts", "voice", "")
        display = next(
            (
                label
                for label, meta in self._voice_map.items()
                if meta["short_name"] == saved_voice
            ),
            self._default_voice_display(voices),
        )
        self.voice_menu.setCurrentText(display)
        if display not in self._voice_map:
            return
        if persist:
            self.app.settings.set(
                "tts", "voice", self._voice_map[display]["short_name"]
            )
            if not self._all_voices_mode:
                code = self._lang_map.get(self.lang_menu.currentText(), "")
                if code:
                    self.app.settings.set("tts", "language", code)

    def _on_voice(self, label: str) -> None:
        meta = self._voice_map.get(label)
        if not meta:
            return
        self.app.settings.set("tts", "voice", meta["short_name"])
        if meta["language"]:
            self.app.settings.set("tts", "language", meta["language"])
            self._sync_lang_menu(meta["language"])

    def _sync_lang_menu(self, language: str) -> None:
        label = self._label_for_lang(language)
        if label:
            self.lang_menu.setCurrentText(label)

    @staticmethod
    def _voice_display(voice: dict) -> str:
        friendly = voice.get("friendly", "") or voice["short_name"]
        return f"{friendly}  ·  {voice['locale']}"

    @classmethod
    def _lang_for_script(cls, text: str) -> str | None:
        return _SCRIPT_LANGS.get(cls._detect_script(text) or "", None)

    @classmethod
    def _detect_script(cls, text: str) -> str | None:
        letters = re.sub(r"[\s\d\W_]+", "", text)
        if not letters:
            return None
        if re.search(r"[\u0900-\u097F]", letters):
            return "Hindi/Devanagari"
        if re.search(r"[\u0600-\u06FF]", letters):
            return "Urdu/Arabic"
        if re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", letters):
            return "Japanese/Chinese"
        if re.search(r"[A-Za-z]", letters):
            return "English/Latin"
        return None

    def _language_of_voice(self, short_name: str) -> str | None:
        for voice in self.app.tts.voices():
            if voice.get("short_name") == short_name:
                language = voice.get("language", "")
                if language:
                    return language_display_name(language)
        return None

    def _default_voice_display(self, voices: list[dict]) -> str:
        for voice in voices:
            if voice.get("gender", "").lower() in ("female", "feminine"):
                return self._voice_display(voice)
        return self._voice_display(voices[0])

    def _on_speed(self, value: int) -> None:
        speed = 0.5 + (float(value) / _SPEED_STEPS) * 1.5
        self.speed_var.set(speed)
        self.speed_lbl.setText(f"{speed:.2f}x")

    def _on_pitch(self, value: int) -> None:
        self.pitch_var.set(int(value))
        self.pitch_lbl.setText(f"{int(value)} Hz")

    def _translate_code(self) -> str:
        return TRANS_OPTIONS.get(self.translate_menu.currentText(), "")

    def _convert(self) -> None:
        if self._generating:
            return
        text = self.textbox.get().strip()
        if not text:
            self.toast("Enter some text first.", "warn")
            return
        target = self._translate_code()
        if not target:
            self._do_convert(text)
            return
        self._generating = True
        self.convert_btn.set_busy(True, "Translating…")
        self.status_lbl.setText(f"Translating to {language_display_name(target)}…")
        self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self._run_translation(text, target, allow_download=False)

    def _run_translation(
        self, text: str, target: str, allow_download: bool
    ) -> None:
        def worker() -> None:
            try:
                translated = self.app.translator.translate(
                    text, target=target, allow_download=allow_download
                )
                error = None
            except Exception as exc:
                translated, error = None, exc
            self.app.schedule(
                lambda: self._on_text_translated(text, translated, target, error)
            )

        threading.Thread(
            target=worker,
            name="tts-translate" + ("-dl" if allow_download else ""),
            daemon=True,
        ).start()

    def _ask_translation_download(self, text: str, target: str, needed: AppError) -> None:
        """Ask before fetching a language pair, then retry the translation."""
        answer = QMessageBox.question(
            self,
            "Download translation model?",
            f"{needed}\n\nIt downloads once, then that language pair works "
            "with no internet.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self._generating = False
            self.convert_btn.set_busy(False)
            self.status_lbl.setText("Translation cancelled — model not downloaded.")
            self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            return
        self.convert_btn.set_busy(True, "Downloading…")
        self.status_lbl.setText(
            f"Downloading the offline {language_display_name(target)} model…"
        )
        self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self._run_translation(text, target, allow_download=True)

    def _on_text_translated(
        self,
        original: str,
        translated: str | None,
        target: str,
        error: Exception | None,
    ) -> None:
        if isinstance(error, TranslationModelConsentRequired):
            self._ask_translation_download(original, target, error)
            return
        if error or not translated:
            self._generating = False
            self.convert_btn.set_busy(False)
            self.status_lbl.setText(f"Translation failed: {error}")
            self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
            self.toast("Translation failed.", "error")
            return
        self._force_language(target)
        self.status_lbl.setText(
            f"Translated → {language_display_name(target)}: “{translated[:60]}…”"
        )
        self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        if not self._has_voice_for(target):
            # The translation worked; speaking is what is missing. Say so
            # precisely instead of letting _do_convert report a vague
            # "no usable voice", and offer the one action that fixes it.
            self._prompt_missing_voice(target, translated)
            return
        self._do_convert(translated)

    def _has_voice_for(self, code: str) -> bool:
        """True when some installed voice can speak this language."""
        try:
            return bool(self.app.tts.voices(code))
        except Exception:  # noqa: BLE001 - a refresh failure must not crash
            return False

    @staticmethod
    def _same_language(voice_language: str, code: str) -> bool:
        """Compare language codes by family, so ``ur_PK`` matches ``ur``."""
        left = (voice_language or "").replace("-", "_").split("_")[0].lower()
        right = (code or "").replace("-", "_").split("_")[0].lower()
        return bool(left and right and left == right)

    def _voice_speaks(self, meta: dict, code: str) -> bool:
        """True when this voice entry really speaks the given language."""
        return self._same_language(meta.get("language", ""), code)

    def _voice_for_language(self, code: str) -> dict | None:
        """Any installed voice that speaks this language, else ``None``."""
        try:
            voices = self.app.tts.voices(code)
        except Exception:  # noqa: BLE001 - a refresh failure must not crash
            return None
        for voice in voices:
            if not self.app.tts.voice_usable(voice["short_name"]):
                continue
            if self._same_language(voice.get("language", ""), code):
                return {
                    "short_name": voice["short_name"],
                    "language": voice.get("language", ""),
                }
        return None

    def _prompt_missing_voice(self, code: str, translated: str) -> None:
        """Translated, but the target language cannot be spoken.

        Two different problems share this point and must not be conflated: a
        voice that exists but is not downloaded (fixable in one click), and a
        language Piper publishes no voice for (not fixable at all). Offering a
        download button in the second case sends the user in circles.
        """
        self._generating = False
        self.convert_btn.set_busy(False)
        self._gen_text = translated
        label = language_display_name(code)
        if PiperVoiceLibrary().can_never_speak(code):
            self._set_get_voice_button(label, code, visible=False)
            self.status_lbl.setText(
                f"Translated to {label}, but no offline {label} voice is "
                f"published, so this can be read as text but not spoken aloud."
            )
            self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            self.toast(f"No {label} voice exists.", "warn")
            return
        self.status_lbl.setText(
            f"Translated to {label}, but no {label} voice is installed. "
            f"Download one (~60 MB) to hear it — it works offline afterwards."
        )
        self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self._set_get_voice_button(label, code, visible=True)
        self.toast(f"No {label} voice installed.", "warn")

    def _set_get_voice_button(
        self, label: str, code: str, visible: bool
    ) -> None:
        """Show or hide the inline 'Get a voice' action."""
        if not hasattr(self, "get_voice_btn"):
            return
        self.get_voice_btn.setVisible(visible)
        # "a Urdu voice" reads wrong; pick the article from the language name.
        article = "an" if label[:1].lower() in "aeiou" else "a"
        self.get_voice_btn.setText(f"Get {article} {label} voice")
        self.get_voice_btn.setProperty("language", code)
        # Qt caches geometry on a hidden widget, so re-polish after a show.
        self.get_voice_btn.style().unpolish(self.get_voice_btn)
        self.get_voice_btn.style().polish(self.get_voice_btn)

    def _on_get_voice(self) -> None:
        code = self.get_voice_btn.property("language") or self._translate_code() or ""
        self._set_get_voice_button(language_display_name(code), code, visible=False)
        self.app.show_screen("voices", language=code)

    def _force_language(self, code: str) -> None:
        label = self._label_for_lang(code)
        if label:
            self.lang_menu.setCurrentText(label)
            self._all_voices_mode = False
            self._populate_voices()

    def _do_convert(self, text: str) -> None:
        self._gen_text = text
        target = self._translate_code()
        if not target:
            self._auto_match_voice(force=True)
        voice_display = self.voice_menu.currentText()
        meta = self._voice_map.get(voice_display)
        if target and (meta is None or not self._voice_speaks(meta, target)):
            # Never read translated text aloud with a voice for another
            # language. When the language menu failed to follow the
            # translation -- an unrecognised label, voices still loading --
            # this quietly produced English audio from Urdu text, which sounds
            # broken rather than wrong. Resolve a matching voice, or say why
            # there is none.
            meta = self._voice_for_language(target)
        if meta is None:
            self._generating = False
            self.convert_btn.set_busy(False)
            # Name the language when we know it; "check voices and refresh"
            # told the user nothing about what was actually missing.
            code = target or self._lang_map.get(self.lang_menu.currentText(), "")
            if code:
                self._prompt_missing_voice(code, text)
            else:
                self.toast("No usable voice yet. Check voices and refresh.", "warn")
            self._refresh_voices()
            return
        self._set_get_voice_button(
            language_display_name(target or "en"), target or "en", visible=False
        )
        self._generating = True
        self.convert_btn.set_busy(True, "Generating speech…")
        if self.app.tts.mode == getattr(self.app.tts, "MODE_QWEN", ""):
            note = "Qwen3-TTS on CPU — generating… may take a while."
        else:
            note = "Generating… this can take a few seconds."
        self.status_lbl.setText(note)
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        try:
            future = self.app.tts.synthesize_async(
                text, meta["short_name"], self.speed_var.get(), self.pitch_var.get()
            )
        except Exception as exc:
            self._on_generation_error(exc)
            return
        self.run_between(future, self._on_generated, self._on_generation_error)

    def _on_generated(self, result: dict) -> None:
        self._generating = False
        self.convert_btn.set_busy(False)
        self._last_result = result
        method = result.get("mode", "qwen3-tts")
        badge_method = "pyttsx3" if result.get("fallback") else method
        self._replace_badge(badge_method)
        text = self._gen_text or self.textbox.get().strip()
        self.app.settings.set("tts", "voice", result.get("voice", ""))
        self.app.history.create(
            type_="tts",
            method=method,
            title=text[:60],
            text=text,
            file=result["file"],
            params={
                "voice": result.get("voice", ""),
                "speed": float(self.speed_var.get()),
                "pitch": int(self.pitch_var.get()),
            },
        )
        if self.player_bar is not None:
            try:
                self.player_bar.set_file(result["file"])
            except Exception as exc:
                self.toast(f"Playback failed: {exc}", "error")
        self.status_lbl.setText(f"Done — {method} voice. Audio ready to play and save.")
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        notes = []
        if result.get("voice_changed"):
            used_voice = result.get("voice", "")
            new_display = next(
                (
                    label
                    for label, meta in self._voice_map.items()
                    if meta["short_name"] == used_voice
                ),
                None,
            )
            if new_display:
                self.voice_menu.setCurrentText(new_display)
                self._sync_lang_menu(self._voice_map[new_display]["language"])
            changed_from = result.get("voice_changed_from", "?")
            notes.append(f"Voice '{changed_from}' unavailable — used '{used_voice}'")
        script_name = self._detect_script(text)
        voice_lang = self._language_of_voice(result.get("voice", ""))
        if script_name and voice_lang and script_name != voice_lang:
            notes.append(f"Text: {script_name} · Voice: {voice_lang}")
        if notes:
            self.status_lbl.setText(
                ". ".join(notes) + ". Audio ready to play and save."
            )
            self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        if result.get("fallback"):
            reason = result.get("fallback_reason", "")
            self.status_lbl.setText(
                "Qwen3-TTS was unavailable — used the offline system voice instead. "
                f"({reason}) Seamless: audio is ready."
            )
            self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            self.toast("Qwen3-TTS unavailable — offline voice used.", "warn")
        else:
            self.toast("Speech generated.", "ok")

    def _on_generation_error(self, exc: Exception) -> None:
        self._generating = False
        self.convert_btn.set_busy(False)
        self.status_lbl.setText(str(exc))
        self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
        self.toast("Speech generation failed.", "error")

    def _replace_badge(self, method: str) -> MethodBadge:
        old = self.badge
        parent = old.parentWidget()
        old.hide()
        old.deleteLater()
        badge = MethodBadge(parent, method)
        layout = parent.layout() if parent is not None else None
        if isinstance(layout, QGridLayout):
            layout.addWidget(badge, 0, 1, Qt.AlignLeft | Qt.AlignVCenter)
        elif layout is not None:
            layout.addWidget(badge)
        self.badge = badge
        return badge

    def _save_audio(self) -> None:
        if self._last_result is None:
            self.toast("Generate speech first.", "warn")
            return
        from app.services import file_service

        source = Path(self._last_result["file"])
        output_format = self.format_menu.currentText()
        if (
            not audio_utils.ffmpeg_available()
            and output_format == "mp3"
            and source.suffix.lower() != ".mp3"
        ):
            self.toast("MP3 needs ffmpeg — saving as WAV.", "warn")
            output_format = "wav"
        destination = file_service.unique_path(
            file_service.category_dir("tts"),
            file_service.timestamp_stem("tts"),
            output_format,
        )
        self.save_btn.set_busy(True, "Saving…")
        try:
            if source.suffix.lower() == f".{output_format}":
                destination.write_bytes(source.read_bytes())
            else:
                audio_utils.convert_format(source, destination)
            self.toast(f"Saved: {destination.name}", "ok")
            self.status_lbl.setText(str(destination))
            self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        except Exception as exc:
            self.toast(str(exc), "error")
        finally:
            self.save_btn.set_busy(False)

    @staticmethod
    def _set_items(combo: QComboBox, values: list[str]) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values)
        combo.blockSignals(False)
