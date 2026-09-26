from __future__ import annotations

import threading

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.config import STT_LANGUAGES, language_display_name, locale_language_code
from app.core.errors import (
    MicPermissionError,
    MissingDependencyError,
    NetworkError,
    TranslationModelConsentRequired,
)
from app.core.recorder import default_input_device
from app.core.translator import Translator
from app.gui import theme
from app.gui.widgets import BusyButton, MicLevelMeter, Screen, TextEdit

STT_OPTIONS = {
    f"{language_display_name(language)} ({language})": language
    for language in STT_LANGUAGES
}
TRANSLATE_OFF = "Off (no translation)"
TRANS_OPTIONS = {
    f"{language_display_name(code)} ({code})": code
    for code in Translator.supported_codes()
}


class SpeechToTextScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self._listening = False
        self._session_entries = 0
        self._translations: list[str] = []
        self._last_tr_language = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        controls = QFrame(self)
        controls.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        controls_layout = QGridLayout(controls)
        controls_layout.setContentsMargins(16, 12, 16, 12)
        controls_layout.setHorizontalSpacing(8)
        controls_layout.setVerticalSpacing(6)
        controls_layout.setColumnStretch(5, 1)

        self.mic_status = QLabel("Idle.", controls)
        self.mic_status.setFont(theme.font(13, "bold"))
        self.mic_status.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        controls_layout.addWidget(self.mic_status, 0, 0, 1, 6)

        self.listen_btn = BusyButton(
            controls,
            text="Start Listening",
            command=self._toggle,
            width=160,
            height=38,
            font=theme.font(14, "bold"),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
        )
        controls_layout.addWidget(self.listen_btn, 1, 0)

        self._label(controls_layout, "Language:", 1, 1)
        self.lang_menu = self._combo(
            controls, list(STT_OPTIONS) or ["Unavailable"], 148
        )
        saved = self.app.stt.language
        selected = next(
            (key for key, value in STT_OPTIONS.items() if value == saved), None
        )
        self.lang_menu.setCurrentText(
            selected or (list(STT_OPTIONS)[0] if STT_OPTIONS else "Unavailable")
        )
        self.lang_menu.currentTextChanged.connect(self._on_language)
        controls_layout.addWidget(self.lang_menu, 1, 2)

        self._label(controls_layout, "Engine:", 1, 3)
        engines = self.app.stt.available_engines() or ["vosk"]
        self.engine_menu = self._combo(controls, engines, 110)
        # Fall back to an engine the combo actually offers, not a hardcoded
        # "vosk": a saved Vosk/Urdu pairing drops Vosk from available_engines
        # (no model is published for Urdu), and selecting a name the list does
        # not contain leaves the menu showing one engine while stt.engine keeps
        # trying another. Syncing the engine too keeps the two in agreement.
        selected = self.app.stt.engine if self.app.stt.engine in engines else engines[0]
        self.engine_menu.setCurrentText(selected)
        if selected != self.app.stt.engine:
            self.app.stt.engine = selected
        self.engine_menu.currentTextChanged.connect(self._on_engine)
        controls_layout.addWidget(self.engine_menu, 1, 4)

        self.level_meter = MicLevelMeter(controls, width=130)
        controls_layout.addWidget(
            self.level_meter, 1, 5, Qt.AlignLeft | Qt.AlignVCenter
        )

        self.copy_btn = QPushButton("⧉ Copy", controls)
        self.copy_btn.setFixedSize(90, 38)
        self.copy_btn.setFont(theme.font(13))
        self.copy_btn.setStyleSheet(
            theme.button_style(
                theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 8, theme.BORDER, 1
            )
        )
        self.copy_btn.clicked.connect(self._copy)
        controls_layout.addWidget(self.copy_btn, 2, 0)

        self.send_btn = QPushButton("Send to Text-to-Speech", controls)
        self.send_btn.setFixedSize(190, 38)
        self.send_btn.setFont(theme.font(13, "bold"))
        self.send_btn.setStyleSheet(
            theme.button_style(
                theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 8, theme.BORDER, 1
            )
        )
        self.send_btn.clicked.connect(self._send_to_tts)
        controls_layout.addWidget(self.send_btn, 2, 1, 1, 2)

        self.test_btn = BusyButton(
            controls,
            text="Test Mic",
            command=self._test_mic,
            width=110,
            height=38,
            font=theme.font(13, "bold"),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
        )
        controls_layout.addWidget(self.test_btn, 2, 5, Qt.AlignRight | Qt.AlignVCenter)

        self._label(controls_layout, "Translate to:", 3, 0)
        self.translate_menu = self._combo(
            controls, [TRANSLATE_OFF] + list(TRANS_OPTIONS), 190
        )
        self.translate_menu.setCurrentText(TRANSLATE_OFF)
        controls_layout.addWidget(self.translate_menu, 3, 1)
        root.addWidget(controls)
        root.addSpacing(14)

        # Shown only when the selected engine has no model installed, so the
        # user knows the engine is gated rather than broken.
        self.engine_hint = QLabel("", self)
        self.engine_hint.setFont(theme.font(12))
        self.engine_hint.setWordWrap(True)
        self.engine_hint.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self.engine_hint.setVisible(False)
        root.addWidget(self.engine_hint)

        transcript = QFrame(self)
        transcript.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        transcript_layout = QVBoxLayout(transcript)
        transcript_layout.setContentsMargins(16, 12, 16, 16)
        transcript_layout.setSpacing(4)
        title = QLabel("Live transcription", transcript)
        title.setFont(theme.font(14, "bold"))
        title.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        transcript_layout.addWidget(title)
        self.output = TextEdit(transcript)
        self.output.setAcceptRichText(False)
        self.output.setStyleSheet(theme.input_style())
        transcript_layout.addWidget(self.output, 1)
        root.addWidget(transcript, 1)

        self.status_lbl = QLabel(
            "Pick the language you will speak in before starting. "
            "Recognition runs fully offline with Vosk — no internet needed.",
            self,
        )
        self.status_lbl.setFont(theme.font(12))
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        root.addWidget(self.status_lbl)
        root.addSpacing(10)

    @staticmethod
    def _combo(parent, values, width: int) -> QComboBox:
        combo = QComboBox(parent)
        combo.addItems(list(values))
        combo.setFixedWidth(width)
        combo.setFont(theme.font(13))
        combo.setStyleSheet(theme.combo_style())
        return combo

    @staticmethod
    def _label(layout, text: str, row: int, column: int) -> None:
        label = QLabel(text)
        label.setFont(theme.font(13))
        label.setStyleSheet(theme.label_style(theme.SUBTEXT))
        layout.addWidget(label, row, column)

    def on_show(self, **kwargs) -> None:
        saved = self.app.stt.language
        selected = next(
            (key for key, value in STT_OPTIONS.items() if value == saved), None
        )
        if selected:
            self.lang_menu.setCurrentText(selected)
        self._refresh_engines()
        self._update_mic_indicator()

    def on_hide(self) -> None:
        if self._listening:
            self.app.stt.stop_listening()
            self._listening = False
            self.listen_btn.set_busy(False, "Start Listening")
            self.mic_status.setText("Stopped.")
            self.mic_status.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
            self.level_meter.set(0)

    def _update_mic_indicator(self) -> None:
        if self.app.stt.has_microphone:
            self.mic_status.setText("Microphone detected.")
            self.mic_status.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        else:
            self.mic_status.setText(
                "No microphone found — enable access in Windows privacy settings."
            )
            self.mic_status.setStyleSheet(theme.label_style(theme.DANGER, "left"))

    def _on_language(self, value: str) -> None:
        code = STT_OPTIONS.get(value, value)
        self.app.stt.language = code
        self.app.settings.set("stt", "language", code)
        # Vosk needs a model per language, so the usable engines change here.
        self._refresh_engines()

    def _on_engine(self, value: str) -> None:
        self.app.stt.engine = value
        self.app.settings.set("stt", "engine", value)
        self._refresh_engine_hint()

    def _refresh_engines(self) -> None:
        """Rebuild the engine list and update the 'needs a model' hint."""
        engines = self.app.stt.available_engines() or ["vosk"]
        self._set_items(self.engine_menu, engines)
        if self.app.stt.engine in engines:
            self.engine_menu.setCurrentText(self.app.stt.engine)
        else:
            self.app.stt.engine = "vosk"
            self.engine_menu.setCurrentText("vosk")
        self._refresh_engine_hint()

    def _refresh_engine_hint(self) -> None:
        """Explain a gated engine, or clear the hint when all is ready."""
        if self.engine_hint is None:
            return
        engine = self.app.stt.engine
        ready = self.app.stt.is_engine_ready(engine)
        if ready:
            self.engine_hint.setText("")
            self.engine_hint.setVisible(False)
            return
        self.engine_hint.setText(self.app.stt.engine_status(engine))
        self.engine_hint.setVisible(True)

    def _toggle(self) -> None:
        if self._listening:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        # Gated engine: explain what to install instead of failing mid-session.
        if not self.app.stt.is_engine_ready(self.app.stt.engine):
            reason = self.app.stt.engine_status(self.app.stt.engine)
            self.mic_status.setText(reason)
            self.mic_status.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            self._refresh_engine_hint()
            return

        self.listen_btn.set_busy(True, "Listening…")
        self._session_entries = 0
        self._translations = []
        self._last_tr_language = ""
        self.level_meter.set(0)

        def on_text(text):
            self.app.schedule(self._append, text)

        def on_status(status):
            self.app.schedule(self._set_busy_status, status)

        def on_error(exc):
            self.app.schedule(self._on_error, exc)

        def on_level(level):
            self.app.schedule(self.level_meter.push, level)

        started = self.app.stt.start_listening(on_text, on_status, on_error, on_level)
        if started:
            self._listening = True
            self.mic_status.setText("Listening…")
            self.mic_status.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        else:
            self.listen_btn.set_busy(False)
            self.mic_status.setText("Could not start listening.")
            self.mic_status.setStyleSheet(theme.label_style(theme.DANGER, "left"))

    def _stop(self) -> None:
        self.app.stt.stop_listening()
        self._listening = False
        self.listen_btn.set_busy(False, "Start Listening")
        self.mic_status.setText("Stopped.")
        self.mic_status.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        self.level_meter.set(0)
        self.status_lbl.setText(
            f"{self._session_entries} phrase(s) transcribed this session."
        )
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))

    def _set_busy_status(self, status: str) -> None:
        self.status_lbl.setText(status)
        self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        if "Recognising" in status:
            self.listen_btn.set_busy(True, "Recognising…")

    def _append(self, text: str) -> None:
        self._session_entries += 1
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.output.setTextCursor(cursor)
        self.output.insertPlainText(text + "\n")
        self.output.ensureCursorVisible()
        self.listen_btn.set_busy(True, "Listening…")
        self.mic_status.setText("Listening…")
        self.mic_status.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        self.status_lbl.setText(
            f"Recognised: {self._session_entries} phrase(s) so far."
        )
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        target = self._translate_code()
        if target and text:
            self._translate_text(text, target)

    def _translate_code(self) -> str:
        return TRANS_OPTIONS.get(self.translate_menu.currentText(), "")

    def _translate_text(self, text: str, target: str) -> None:
        self.status_lbl.setText("Translating…")
        self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self._run_translation(text, target, allow_download=False)

    def _run_translation(
        self, text: str, target: str, allow_download: bool
    ) -> None:
        def worker() -> None:
            try:
                result = self.app.translator.translate(
                    text, target=target, allow_download=allow_download
                )
                error = None
            except Exception as exc:
                result, error = None, exc
            self.app.schedule(self._on_translated, text, result, target, error)

        threading.Thread(
            target=worker,
            name="stt-translate" + ("-dl" if allow_download else ""),
            daemon=True,
        ).start()

    def _ask_translation_download(self, text: str, target: str, needed: Exception) -> None:
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
            self.status_lbl.setText("Translation skipped — model not downloaded.")
            self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            return
        self.status_lbl.setText(
            f"Downloading the offline {language_display_name(target)} model…"
        )
        self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self._run_translation(text, target, allow_download=True)

    def _on_translated(
        self, original: str, result: str | None, target: str, error: Exception | None
    ) -> None:
        if isinstance(error, TranslationModelConsentRequired):
            self._ask_translation_download(original, target, error)
            return
        if not result:
            self.status_lbl.setText(f"Translation unavailable: {error}")
            self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
            return
        self._translations.append(result)
        self._last_tr_language = target
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.output.setTextCursor(cursor)
        self.output.insertPlainText(f"→ {result}\n")
        self.output.ensureCursorVisible()
        self.status_lbl.setText(
            f"Translated to {language_display_name(target)}: {result}"
        )
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))

    def _on_error(self, exc: Exception) -> None:
        if isinstance(exc, NetworkError):
            self.status_lbl.setText(str(exc))
            self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
            if not self._listening:
                self.mic_status.setText("Mic error — try Start Listening again.")
                self.mic_status.setStyleSheet(theme.label_style(theme.DANGER, "left"))
            return
        self._listening = False
        self.listen_btn.set_busy(False, "Start Listening")
        if isinstance(exc, MicPermissionError):
            self.mic_status.setText(str(exc).split("\n")[0])
            self.mic_status.setStyleSheet(theme.label_style(theme.DANGER, "left"))
        elif isinstance(exc, MissingDependencyError):
            # Keep the "pip install ..." line -- the first line alone is what
            # made this look like an unexplained failure.
            self.status_lbl.setText(str(exc))
            self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
            self.mic_status.setText("Engine unavailable — see the message above.")
            self.mic_status.setStyleSheet(theme.label_style(theme.DANGER, "left"))
        else:
            self.status_lbl.setText(f"Recognition issue: {exc}")
            self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))

    def _test_mic(self) -> None:
        import numpy as np
        import sounddevice as sd

        if self._listening:
            self.toast("Stop listening first.", "warn")
            return
        if not self.app.stt.has_microphone:
            self.toast("No microphone found.", "warn")
            return
        self.test_btn.set_busy(True, "Testing…")
        self.status_lbl.setText("Testing microphone… speak now!")
        self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))

        def worker() -> None:
            try:
                device = default_input_device()
                rate = 16000
                blocksize = int(rate * 0.1)
                turns = int(rate * 3)
                with sd.InputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="int16",
                    device=device,
                    blocksize=blocksize,
                ) as stream:
                    chunks = [
                        stream.read(blocksize)[0] for _ in range(turns // blocksize)
                    ]
                data = np.concatenate(chunks)
                rms = float(np.sqrt((data.astype(np.float32) ** 2).mean()))
                from speech_recognition import AudioData as SRAudioData

                audio = SRAudioData(data.tobytes(), rate, 2)
                text = (
                    self.app.stt._recognize(audio, lambda _status: None) or ""
                ).strip()

                def done() -> None:
                    self.test_btn.set_busy(False, "Test Mic")
                    self.level_meter.set(0)
                    percent = round(min(rms / 32767.0, 1.0) * 100)
                    model = self.app.stt.model_info()
                    if text:
                        self.toast(f"Level {percent}% — heard: “{text[:50]}”", "ok")
                        self.status_lbl.setText(f"Heard: “{text}” · {model}")
                        self.status_lbl.setStyleSheet(
                            theme.label_style(theme.SUCCESS, "left")
                        )
                    else:
                        self.toast(f"Level {percent}% — no speech detected.", "warn")
                        self.status_lbl.setText(
                            f"Mic level {percent}% but no speech detected — {model}. "
                            "Bolo zara aloud / mic gain karo."
                        )
                        self.status_lbl.setStyleSheet(
                            theme.label_style(theme.WARNING, "left")
                        )

                self.app.schedule(done)
            except Exception as exc:

                def fail(error=exc) -> None:
                    self.test_btn.set_busy(False, "Test Mic")
                    self._on_error(error)

                self.app.schedule(fail)

        threading.Thread(target=worker, name="stt-test-mic", daemon=True).start()

    def _current_text(self) -> str:
        return self.output.get().strip()

    def _copy(self) -> None:
        text = self._current_text()
        if not text:
            self.toast("Nothing to copy yet.", "warn")
            return
        QApplication.clipboard().setText(text)
        self.toast("Copied to clipboard.", "ok")

    def _send_to_tts(self) -> None:
        text = self._current_text()
        if not text:
            self.toast("Transcribe something first.", "warn")
            return
        if self._listening:
            self._stop()
        if self._translations:
            payload = "\n".join(self._translations)
            language = self._last_tr_language or locale_language_code(
                self.app.stt.language
            )
            self.app.send_text_to_tts(payload, language=language)
            self.toast("Translated text sent to Text-to-Speech.", "ok")
        else:
            self.app.send_text_to_tts(
                text, language=locale_language_code(self.app.stt.language)
            )
            self.toast("Text sent to Text-to-Speech.", "ok")

    @staticmethod
    def _set_items(combo: QComboBox, values: list[str]) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values)
        combo.blockSignals(False)
