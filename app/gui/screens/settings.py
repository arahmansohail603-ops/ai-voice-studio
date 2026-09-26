from __future__ import annotations

import os
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import STT_LANGUAGES, language_display_name
from app.core import audio_utils
from app.core.errors import module_installed
from app.core.recorder import Recorder, list_input_devices
from app.core.stt_engine import STTEngine
from app.gui import theme
from app.gui.widgets import Screen
from app.services import file_service


class _Value:
    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value) -> None:
        self._value = str(value)


class SettingsScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self._lang_map = {}
        self._voice_map = {}
        self._device_map = {}
        self._fallback_map = {}
        self._card_layouts = {}
        self._backend_labels = {
            "system": "System voices (offline)",
            "piper": "Piper (offline)",
            "qwen3": "Qwen3-TTS (local — CPU may be slow)",
        }
        self._backend_map = {value: key for key, value in self._backend_labels.items()}
        self._clone_engine_labels = {
            "xtts": "Coqui XTTS-v2 (default)",
            "qwen": "Qwen3-TTS 1.7B Base",
        }
        self._clone_engine_map = {
            value: key for key, value in self._clone_engine_labels.items()
        }

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(theme.scroll_style(theme.APP_BG))
        self.content = QWidget()
        self.content.setStyleSheet("QWidget { background: transparent; }")
        self.content_layout = QGridLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setHorizontalSpacing(8)
        self.content_layout.setVerticalSpacing(8)
        self.content_layout.setColumnStretch(0, 1)
        self.content_layout.setColumnStretch(1, 1)
        self.content_layout.setRowStretch(0, 1)
        self.content_layout.setRowStretch(1, 1)
        self.content_layout.setRowStretch(2, 1)
        scroll.setWidget(self.content)
        root.addWidget(scroll)

        card = self._card("Speech defaults", 0, 0)
        self.lang_menu = self._menu(card, self._on_default_lang)
        self._field(card, "Default language", self.lang_menu, 170)
        self.voice_menu = self._menu(card, self._on_default_voice)
        self._field(card, "Default voice", self.voice_menu, 200)
        self.backend_menu = self._menu(card, self._on_tts_backend)
        self._field(card, "Speech engine", self.backend_menu, 190)
        self.qwen_gpu_lbl = QLabel("", card)
        self.qwen_gpu_lbl.setFont(theme.font(11))
        self.qwen_gpu_lbl.setWordWrap(True)
        self.qwen_gpu_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self._card_layouts[id(card)].addWidget(self.qwen_gpu_lbl, self._card_row, 0)
        self._card_row += 1
        self.qwen_ref_var = _Value()
        self.qwen_ref_entry = QLineEdit(card)
        self.qwen_ref_entry.setReadOnly(True)
        self.qwen_ref_entry.setStyleSheet(theme.input_style())
        qwen_browse = QPushButton("Browse…", card)
        self._style_button(qwen_browse, width=88, height=30)
        qwen_browse.clicked.connect(self._on_qwen_ref_browse)
        self._row_field(
            card,
            "Qwen3-TTS reference voice (optional)",
            self.qwen_ref_entry,
            qwen_browse,
        )
        self._fill_gap(card)

        card = self._card("Speech-to-text", 0, 1)
        self._stt_lang_map = {
            f"{language_display_name(language)} ({language})": language
            for language in STT_LANGUAGES
        }
        self.stt_engine_menu = self._menu(card, self._on_stt_engine)
        self._field(card, "Recognition engine", self.stt_engine_menu, 150)
        self.stt_lang_menu = self._menu(
            card, self._on_stt_lang, list(self._stt_lang_map)
        )
        self._field(card, "Recognition language", self.stt_lang_menu, 170)
        self._fill_gap(card)

        card = self._card("Microphone", 1, 0)
        self.device_menu = self._menu(card, self._on_device)
        self._field(card, "Input device", self.device_menu, 200)
        self.rate_menu = self._menu(
            card, self._on_rate, ["8000", "16000", "22050", "44100", "48000"]
        )
        self._field(card, "Sample rate (Hz)", self.rate_menu, 110)
        self._fill_gap(card)

        card = self._card("Output & storage", 1, 1)
        self.folder_var = _Value()
        self.folder_entry = QLineEdit(card)
        self.folder_entry.setReadOnly(True)
        self.folder_entry.setStyleSheet(theme.input_style())
        browse = QPushButton("Browse…", card)
        self._style_button(browse, width=90, height=32)
        browse.clicked.connect(self._choose_folder)
        self._row_field(card, "Output folder", self.folder_entry, browse)
        self.ffmpeg_lbl = QLabel("", card)
        self.ffmpeg_lbl.setFont(theme.font(11))
        self.ffmpeg_lbl.setWordWrap(True)
        self.ffmpeg_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        self._card_layouts[id(card)].addWidget(self.ffmpeg_lbl, self._card_row, 0)
        self._card_row += 1
        self._fill_gap(card)

        card = self._card("My Voice (voice cloning)", 2, 0)
        self.clone_engine_menu = self._menu(card, self._on_clone_engine)
        self._field(card, "Clone engine", self.clone_engine_menu, 200)
        self.fallback_voice_menu = self._menu(card, self._on_fallback_voice)
        self._field(
            card,
            "Fallback offline voice",
            self.fallback_voice_menu,
            200,
        )
        self.clone_switch = QCheckBox(
            "Enable voice cloning (heavy / experimental)", card
        )
        self.clone_switch.setFont(theme.font(12))
        self.clone_switch.setStyleSheet(theme.check_style())
        self.clone_switch.toggled.connect(self._on_clone_toggle)
        self._card_layouts[id(card)].addWidget(
            self.clone_switch, self._card_row, 0, Qt.AlignLeft
        )
        self._card_row += 1
        self._fill_gap(card)

        card = self._card("Data", 2, 1)
        data_row = QFrame(card)
        data_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        data_layout = QHBoxLayout(data_row)
        data_layout.setContentsMargins(0, 0, 0, 0)
        open_button = QPushButton("Open data folder", data_row)
        self._style_button(open_button, width=150, height=34)
        open_button.clicked.connect(self._open_data_folder)
        data_layout.addWidget(open_button)
        reset_button = QPushButton("Reset settings", data_row)
        self._style_button(reset_button, width=130, height=34, color=theme.WARNING)
        reset_button.clicked.connect(self._reset_settings)
        data_layout.addWidget(reset_button)
        data_layout.addStretch(1)
        self._card_layouts[id(card)].addWidget(data_row, self._card_row, 0)
        self._card_row += 1
        data_note = QLabel(
            "History and settings live in the data folder. "
            "Deleting history keeps audio files on disk.",
            card,
        )
        data_note.setFont(theme.font(11))
        data_note.setWordWrap(True)
        data_note.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        self._card_layouts[id(card)].addWidget(data_note, self._card_row, 0)
        self._card_row += 1
        self._fill_gap(card)

    def _card(self, title: str, row: int, column: int) -> QFrame:
        card = QFrame(self.content)
        card.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        self.content_layout.addWidget(card, row, column)
        layout = QGridLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(4)
        title_label = QLabel(title, card)
        title_label.setFont(theme.font(14, "bold"))
        title_label.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        layout.addWidget(title_label, 0, 0)
        self._card_layouts[id(card)] = layout
        self._card_row = 1
        return card

    def _fill_gap(self, card: QFrame) -> None:
        self._card_layouts[id(card)].setRowStretch(self._card_row, 1)

    def _menu(self, card, command, values=None) -> QComboBox:
        combo = QComboBox(card)
        combo.addItems(list(values or ["Loading…"]))
        combo.setFont(theme.font(13))
        combo.setStyleSheet(theme.combo_style())
        combo.currentTextChanged.connect(command)
        return combo

    def _field(self, card, text: str, widget, menu_width: int | None = None) -> None:
        if menu_width is not None:
            widget.setFixedWidth(menu_width)
        label = QLabel(text, card)
        label.setFont(theme.font(11))
        label.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        layout = self._card_layouts[id(card)]
        layout.addWidget(label, self._card_row, 0)
        self._card_row += 1
        layout.addWidget(widget, self._card_row, 0)
        self._card_row += 1

    def _row_field(self, card, text: str, entry, button) -> None:
        layout = self._card_layouts[id(card)]
        label = QLabel(text, card)
        label.setFont(theme.font(11))
        label.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        layout.addWidget(label, self._card_row, 0)
        self._card_row += 1
        row = QFrame(card)
        row.setStyleSheet("QFrame { background: transparent; border: none; }")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        entry.setParent(row)
        button.setParent(row)
        row_layout.addWidget(entry, 1)
        row_layout.addWidget(button, 0)
        layout.addWidget(row, self._card_row, 0)
        self._card_row += 1

    @staticmethod
    def _style_button(
        button: QPushButton, width: int, height: int, color: str = theme.TEXT
    ) -> None:
        button.setFixedSize(width, height)
        button.setFont(theme.font(12))
        button.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, color, 8, theme.BORDER, 1)
        )

    def on_show(self, **kwargs) -> None:
        self._populate_all()

    def _populate_all(self) -> None:
        settings = self.app.settings
        languages = self.app.tts.languages()
        self._lang_map = {
            f"{language_display_name(language)} ({language})": language
            for language in sorted(languages)
        }
        if self._lang_map:
            self._set_items(self.lang_menu, list(self._lang_map))
            saved_language = settings.get("tts", "language", "en")
            selected = next(
                (
                    key
                    for key, value in self._lang_map.items()
                    if value == saved_language
                ),
                None,
            )
            self.lang_menu.setCurrentText(selected or next(iter(self._lang_map)))
            self._populate_default_voices(saved_language)
        else:
            self._set_items(self.lang_menu, ["No languages"])
            self._set_items(self.voice_menu, ["No voices"])
        self._populate_fallback_voices()

        available_backends = self.app.tts.available_backends() or ["system"]
        backend_values = [
            self._backend_labels[key]
            for key in available_backends
            if key in self._backend_labels
        ] or [self._backend_labels["system"]]
        self._set_items(self.backend_menu, backend_values)
        current_backend = settings.get("tts", "backend", "system")
        self.backend_menu.setCurrentText(
            self._backend_labels.get(current_backend, backend_values[0])
        )
        self._update_qwen_gpu_status()

        # ``self.app.stt`` is a live instance, so the model-installed gate
        # applies here as it does on the Speech-to-Text screen.
        stt = self.app.stt
        engines = stt.available_engines() or ["vosk"]
        self._set_items(self.stt_engine_menu, engines)
        saved_engine = settings.get("stt", "engine", "vosk")
        self.stt_engine_menu.setCurrentText(
            saved_engine if saved_engine in engines else engines[0]
        )
        saved_stt = settings.get("stt", "language", "en-US")
        selected_stt = next(
            (key for key, value in self._stt_lang_map.items() if value == saved_stt),
            None,
        )
        self.stt_lang_menu.setCurrentText(
            selected_stt
            or (next(iter(self._stt_lang_map)) if self._stt_lang_map else "Unavailable")
        )
        self._populate_devices()
        self.rate_menu.setCurrentText(
            str(settings.get("recorder", "samplerate", 44100))
        )
        output_folder = str(
            settings.get(
                "output", "folder", str(file_service.category_dir("tts").parent)
            )
        )
        self.folder_var.set(output_folder)
        self.folder_entry.setText(output_folder)
        reference = str(settings.get("tts", "qwen_reference", "") or "")
        self.qwen_ref_var.set(reference)
        self.qwen_ref_entry.setText(reference)
        self.clone_switch.setChecked(bool(settings.get("clone", "enabled", True)))
        self._set_items(
            self.clone_engine_menu, list(self._clone_engine_labels.values())
        )
        saved_engine = settings.get("clone", "engine", "xtts")
        self.clone_engine_menu.setCurrentText(
            self._clone_engine_labels.get(saved_engine, "Coqui XTTS-v2 (default)")
        )
        ffmpeg = audio_utils.ffmpeg_available()
        self.ffmpeg_lbl.setText(
            "ffmpeg: "
            + (
                "Available — MP3 export enabled."
                if ffmpeg
                else "Not found — MP3 saving is disabled (WAV works fine)."
            )
        )
        self.ffmpeg_lbl.setStyleSheet(
            theme.label_style(theme.SUCCESS if ffmpeg else theme.WARNING, "left")
        )

    def _populate_default_voices(self, language: str) -> None:
        voices = self.app.tts.voices(language)
        if not voices:
            self._set_items(self.voice_menu, ["No voices"])
            return
        self._voice_map = {
            (
                f"{voice.get('friendly', voice['short_name'])[:50]} ({voice['locale']})"
            ): voice["short_name"]
            for voice in voices
        }
        self._set_items(self.voice_menu, list(self._voice_map))
        saved = self.app.settings.get("tts", "voice", "")
        selected = next(
            (label for label, short in self._voice_map.items() if short == saved),
            next(iter(self._voice_map)),
        )
        self.voice_menu.setCurrentText(selected)

    def _populate_fallback_voices(self) -> None:
        voices = self.app.tts.voices()
        if not voices:
            return
        self._fallback_map = {
            (
                f"{voice.get('friendly', voice['short_name'])[:50]} ({voice['locale']})"
            ): voice["short_name"]
            for voice in voices
        }
        values = list(self._fallback_map)
        self._set_items(self.fallback_voice_menu, values)
        saved = self.app.settings.get("clone", "fallback_voice", "")
        selected = next(
            (label for label, short in self._fallback_map.items() if short == saved),
            values[0],
        )
        self.fallback_voice_menu.setCurrentText(selected)

    def _populate_devices(self) -> None:
        try:
            devices = list_input_devices()
        except Exception:
            devices = []
        self._device_map = {device["name"]: device["index"] for device in devices}
        values = list(self._device_map) or ["No input devices found"]
        self._set_items(self.device_menu, values)
        saved = self.app.settings.get("recorder", "device")
        selected = next(
            (label for label, index in self._device_map.items() if index == saved),
            values[0],
        )
        self.device_menu.setCurrentText(selected)

    def _update_qwen_gpu_status(self) -> None:
        if not module_installed("qwen_tts") or self.app.tts.qwen_gpu_present:
            self.qwen_gpu_lbl.setText("")
            return
        self.qwen_gpu_lbl.setText(
            "ℹ No NVIDIA GPU detected — Qwen3-TTS will run on the CPU (slow). "
            "Install CUDA torch to use a GPU."
        )

    def _on_default_lang(self, value: str) -> None:
        code = self._lang_map.get(value, "en")
        self.app.settings.set("tts", "language", code)
        self._populate_default_voices(code)

    def _on_default_voice(self, value: str) -> None:
        short = self._voice_map.get(value, "")
        if short:
            self.app.settings.set("tts", "voice", short)

    def _on_clone_toggle(self, checked: bool | None = None) -> None:
        enabled = self.clone_switch.isChecked()
        self.app.settings.set("clone", "enabled", enabled)
        if enabled:
            self.toast(
                "Voice cloning enabled — it needs torch and roughly 8 GB RAM.",
                "warn",
            )

    def _on_clone_engine(self, value: str) -> None:
        engine = self._clone_engine_map.get(value, "xtts")
        self.app.settings.set("clone", "engine", engine)
        self.toast("Clone engine set — pick it again inside My Voice.", "ok")

    def _on_tts_backend(self, value: str) -> None:
        backend = self._backend_map.get(value, "system")
        self.app.settings.set("tts", "backend", backend)
        self.app.tts.set_backend(backend)
        self._populate_default_voices(self.app.settings.get("tts", "language", "en"))

    def _on_stt_engine(self, value: str) -> None:
        self.app.settings.set("stt", "engine", value)
        self.app.stt.engine = value

    def _on_stt_lang(self, value: str) -> None:
        code = self._stt_lang_map.get(value, value)
        self.app.settings.set("stt", "language", code)
        self.app.stt.language = code

    def _on_device(self, value: str) -> None:
        index = self._device_map.get(value)
        self.app.settings.set("recorder", "device", index)
        self._recreate_recorder()

    def _on_rate(self, value: str) -> None:
        self.app.settings.set("recorder", "samplerate", int(value))
        self._recreate_recorder()

    def _recreate_recorder(self) -> None:
        self.app.recorder = Recorder(
            samplerate=int(self.app.settings.get("recorder", "samplerate", 44100)),
            channels=int(self.app.settings.get("recorder", "channels", 1)),
            device=self.app.settings.get("recorder", "device"),
        )

    def _on_fallback_voice(self, value: str) -> None:
        short = self._fallback_map.get(value, "")
        self.app.settings.set("clone", "fallback_voice", short or value)

    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.folder_var.get()
        )
        if not chosen:
            return
        resolved = Path(chosen).resolve()
        file_service.set_output_root(resolved)
        self.app.settings.set("output", "folder", str(resolved))
        self.folder_var.set(str(resolved))
        self.folder_entry.setText(str(resolved))
        self.toast("Output folder updated.", "ok")

    def _on_qwen_ref_browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a voice profile (WAV)",
            "",
            "WAV audio (*.wav);;All files (*)",
        )
        if not chosen:
            return
        self.qwen_ref_var.set(chosen)
        self.qwen_ref_entry.setText(chosen)
        self.app.settings.set("tts", "qwen_reference", chosen)
        self.toast("Qwen3-TTS reference voice updated.", "ok")

    def _open_data_folder(self) -> None:
        try:
            path = file_service.category_dir("tts")
            if hasattr(os, "startfile"):
                os.startfile(str(path))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception:
            self.toast("Could not open folder.", "error")

    def _reset_settings(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset settings",
            "Restore all default settings?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.app.settings.reset()
        output = self.app.settings.get("output", "folder", "")
        if output:
            file_service.set_output_root(output)
        self._populate_all()
        self.toast("Settings reset.", "ok")

    @staticmethod
    def _set_items(combo: QComboBox, values: list[str]) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values)
        combo.blockSignals(False)
