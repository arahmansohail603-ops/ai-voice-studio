from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import language_display_name
from app.core import audio_utils
from app.core.piper_voices import PiperVoiceLibrary
from app.core.qwen_cloner import QwenVoiceCloner
from app.core.voice_cloner import (
    MAX_SAMPLE_SECONDS,
    MIN_SAMPLE_SECONDS,
    CloneState,
    VoiceCloner,
)
from app.gui import theme
from app.gui.widgets import (
    AudioPlayerBar,
    BusyButton,
    MethodBadge,
    MicLevelMeter,
    Screen,
    TextEdit,
)
from app.services import file_service

_ENGINE_LABELS = {
    "xtts": "Coqui XTTS-v2",
    "qwen": "Qwen3-TTS 1.7B Base",
}
_ENGINE_KEYS = {value: key for key, value in _ENGINE_LABELS.items()}
_ALL_CLONE_LANGS = sorted(
    set(VoiceCloner.supported_languages()) | set(QwenVoiceCloner.supported_languages())
)


class _ConsentValue:
    def __init__(self, checkbox: QCheckBox):
        self.checkbox = checkbox

    def get(self) -> bool:
        return self.checkbox.isChecked()

    def set(self, value) -> None:
        self.checkbox.setChecked(bool(value))


class MyVoiceScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.ref_path: Path | None = None
        self.ref_duration = 0.0
        self.profile_path: Path | None = None
        self._last_result = None
        self._ref_tracking = False
        # True once the reference recording has actually seen sound, so the
        # screen can tell a working microphone from a silent one mid-take.
        self._ref_peak_seen = False
        self._ref_track_timer = QTimer(self)
        self._ref_track_timer.setInterval(100)
        self._ref_track_timer.timeout.connect(self._ref_tick)
        self._generating = False
        self._fallback_populated = False
        self._fallback_map: dict[str, str] = {}
        self._gen_lang = "en"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(theme.scroll_style(theme.APP_BG))
        content = QWidget()
        content.setStyleSheet("QWidget { background: transparent; }")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)
        scroll.setWidget(content)
        root.addWidget(scroll)

        notice = QFrame(content)
        notice.setStyleSheet(theme.frame_style(theme.ACCENT_SOFT, 12))
        notice_layout = QVBoxLayout(notice)
        notice_layout.setContentsMargins(16, 12, 16, 12)
        notice_title = QLabel("Voice Cloning Notice", notice)
        notice_title.setFont(theme.font(14, "bold"))
        notice_title.setStyleSheet(theme.label_style(theme.ACCENT, "left"))
        notice_layout.addWidget(notice_title)
        notice_text = QLabel(
            "Cloning synthesises speech that resembles a recorded reference voice. "
            "Only use a recording you own or have explicit permission to use. "
            "Generated Voice Clone audio is clearly labelled in this app.",
            notice,
        )
        notice_text.setFont(theme.font(12))
        notice_text.setWordWrap(True)
        notice_text.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        notice_layout.addWidget(notice_text)
        engine_row = QFrame(notice)
        engine_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        engine_layout = QHBoxLayout(engine_row)
        engine_layout.setContentsMargins(0, 0, 0, 0)
        engine_label = QLabel("Clone engine:", engine_row)
        engine_label.setFont(theme.font(12, "bold"))
        engine_label.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        engine_layout.addWidget(engine_label)
        self.engine_menu = self._combo(engine_row, list(_ENGINE_LABELS.values()), 200)
        self.engine_menu.currentTextChanged.connect(self._on_engine)
        engine_layout.addWidget(self.engine_menu)
        engine_layout.addStretch(1)
        notice_layout.addWidget(engine_row)
        content_layout.addWidget(notice)

        source = self._panel(content, "1 · Provide an authorised voice sample")
        source_text = QLabel(
            "Record 3–40s of clear speech, or upload an existing recording.", source
        )
        source_text.setFont(theme.font(12))
        source_text.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        source_layout = source.layout()
        source_layout.addWidget(source_text)
        source_buttons = QFrame(source)
        source_buttons.setStyleSheet(
            "QFrame { background: transparent; border: none; }"
        )
        source_button_layout = QHBoxLayout(source_buttons)
        source_button_layout.setContentsMargins(0, 0, 0, 0)
        # One button does the whole job: press it, talk, and the take stops and
        # loads itself at MAX_SAMPLE_SECONDS. A separate Stop button meant two
        # clicks plus a timer the user had to watch and stop by hand.
        self.ref_record_btn = BusyButton(
            source_buttons,
            text="Record & Use",
            command=self._toggle_ref_record,
            width=160,
            height=34,
            font=theme.font(13, "bold"),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
        )
        source_button_layout.addWidget(self.ref_record_btn)
        self.ref_timer = QLabel("00:00.0", source_buttons)
        self.ref_timer.setFont(theme.font(14, "bold"))
        self.ref_timer.setStyleSheet(theme.label_style(theme.SUBTEXT))
        source_button_layout.addWidget(self.ref_timer)
        # Without a level meter a reference recording gives no feedback at all:
        # the timer runs either way, so a microphone that is picking up nothing
        # looks exactly like a working one until the sample is rejected. This
        # laptop's mic measures around -31 dBFS in a quiet room, which is well
        # inside the range where a quiet voice fails the silence check.
        self.ref_meter = MicLevelMeter(source_buttons, width=130)
        source_button_layout.addWidget(self.ref_meter)
        self.upload_btn = QPushButton("Upload Sample…", source_buttons)
        self._style_button(self.upload_btn, 130, 34)
        self.upload_btn.clicked.connect(self._upload_sample)
        source_button_layout.addWidget(self.upload_btn)
        source_button_layout.addStretch(1)
        source_layout.addWidget(source_buttons)
        self.consent_lbl = QLabel("", source)
        self.consent_lbl.setFont(theme.font(12))
        self.consent_lbl.setWordWrap(True)
        self.consent_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        source_layout.addWidget(self.consent_lbl)
        content_layout.addWidget(source)

        sample = self._panel(content, "2 · Reference sample & profile")
        sample_layout = sample.layout()
        self.ref_lbl = QLabel("No voice sample loaded yet.", sample)
        self.ref_lbl.setFont(theme.font(12))
        self.ref_lbl.setWordWrap(True)
        self.ref_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        sample_layout.addWidget(self.ref_lbl)
        transcript_label = QLabel(
            "Reference transcript (optional — spoken words in the sample):", sample
        )
        transcript_label.setFont(theme.font(11))
        transcript_label.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        sample_layout.addWidget(transcript_label)
        self.transcript_entry = QLineEdit(sample)
        self.transcript_entry.setStyleSheet(theme.input_style())
        sample_layout.addWidget(self.transcript_entry)
        transcript_note = QLabel(
            "Improves Qwen3-TTS clone quality; Coqui XTTS-v2 ignores it.", sample
        )
        transcript_note.setFont(theme.font(11))
        transcript_note.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        sample_layout.addWidget(transcript_note)
        self.consent = QCheckBox(
            "I confirm I own or have permission to use this voice recording.", sample
        )
        self.consent.setFont(theme.font(12))
        self.consent.setStyleSheet(theme.check_style())
        self.consent.toggled.connect(self._on_consent)
        self._consent_var = _ConsentValue(self.consent)
        sample_layout.addWidget(self.consent)
        profile_row = QFrame(sample)
        profile_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        profile_layout = QHBoxLayout(profile_row)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        self.create_profile_btn = BusyButton(
            profile_row,
            text="Create Voice Profile",
            command=self._create_profile,
            height=36,
            font=theme.font(13, "bold"),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
        )
        profile_layout.addWidget(self.create_profile_btn)
        self.clone_state_badge = MethodBadge(profile_row, "clone")
        profile_layout.addWidget(self.clone_state_badge)
        profile_layout.addStretch(1)
        sample_layout.addWidget(profile_row)
        self.clone_status_lbl = QLabel(
            "Model not loaded. Create Voice Profile loads it on first use (~2 GB).",
            sample,
        )
        self.clone_status_lbl.setFont(theme.font(12))
        self.clone_status_lbl.setWordWrap(True)
        self.clone_status_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        sample_layout.addWidget(self.clone_status_lbl)
        content_layout.addWidget(sample)

        gen_panel = self._panel(content, "3 · Generate speech with your voice")
        gen_layout = gen_panel.layout()
        self.gen_text = TextEdit(gen_panel)
        self.gen_text.setFixedHeight(110)
        self.gen_text.setAcceptRichText(False)
        self.gen_text.setStyleSheet(theme.input_style())
        self.gen_text.setPlainText(
            "Speak the way I speak. This sentence is cloned from my voice."
        )
        gen_layout.addWidget(self.gen_text)
        language_row = QFrame(gen_panel)
        language_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        language_layout = QHBoxLayout(language_row)
        language_layout.setContentsMargins(0, 0, 0, 0)
        language_label = QLabel("Language:", language_row)
        language_label.setFont(theme.font(13))
        language_label.setStyleSheet(theme.label_style(theme.SUBTEXT))
        language_layout.addWidget(language_label)
        self.clone_lang_menu = self._combo(
            language_row,
            [f"{language_display_name(code)} ({code})" for code in _ALL_CLONE_LANGS],
            180,
        )
        self.clone_lang_menu.setCurrentText("English (en)")
        language_layout.addWidget(self.clone_lang_menu)
        fallback_label = QLabel("Fallback voice:", language_row)
        fallback_label.setFont(theme.font(13))
        fallback_label.setStyleSheet(theme.label_style(theme.SUBTEXT))
        language_layout.addWidget(fallback_label)
        self.fallback_voice_menu = self._combo(language_row, ["Default"], 240)
        language_layout.addWidget(self.fallback_voice_menu)
        language_layout.addStretch(1)
        gen_layout.addWidget(language_row)
        action_row = QFrame(gen_panel)
        action_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        self.generate_btn = BusyButton(
            action_row,
            text="Generate Speech",
            command=self._generate,
            height=38,
            font=theme.font(14, "bold"),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
        )
        action_layout.addWidget(self.generate_btn)
        self.result_badge = MethodBadge(action_row, "clone")
        action_layout.addWidget(self.result_badge)
        self.gen_status_lbl = QLabel("", action_row)
        self.gen_status_lbl.setFont(theme.font(12))
        self.gen_status_lbl.setWordWrap(True)
        self.gen_status_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        action_layout.addWidget(self.gen_status_lbl, 1)
        # Shown when the requested language has no installed voice, which
        # silently produced English audio from non-English text before.
        self.get_voice_btn = QPushButton("Get a voice", action_row)
        self.get_voice_btn.setFont(theme.font(12))
        self.get_voice_btn.setStyleSheet(
            theme.button_style(
                theme.ACCENT, theme.ACCENT_HOVER, theme.ON_ACCENT, 8, theme.ACCENT, 1
            )
        )
        self.get_voice_btn.clicked.connect(self._on_get_voice)
        self.get_voice_btn.setVisible(False)
        action_layout.addWidget(self.get_voice_btn)
        gen_layout.addWidget(action_row)
        content_layout.addWidget(gen_panel)

        preview = self._panel(content, "Preview & save")
        preview_layout = preview.layout()
        if app.player is not None:
            self.player_bar = AudioPlayerBar(preview, app.player)
            preview_layout.addWidget(self.player_bar)
        else:
            self.player_bar = None
            unavailable = QLabel(
                "Playback unavailable (no audio output device). "
                "Audio files are still saved on disk.",
                preview,
            )
            unavailable.setFont(theme.font(13))
            unavailable.setWordWrap(True)
            unavailable.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            preview_layout.addWidget(unavailable)
        save_row = QFrame(preview)
        save_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        save_layout = QHBoxLayout(save_row)
        save_layout.setContentsMargins(0, 0, 0, 0)
        self.save_btn = BusyButton(
            save_row,
            text="Save Audio",
            command=self._save_result,
            width=130,
            height=34,
            font=theme.font(13, "bold"),
            fg_color=theme.INPUT_BG,
            hover_color=theme.CARD_BG,
            border_color=theme.BORDER,
            border_width=1,
        )
        save_layout.addWidget(self.save_btn)
        save_layout.addStretch(1)
        preview_layout.addWidget(save_row)
        content_layout.addWidget(preview)
        content_layout.addStretch(1)

    @staticmethod
    def _panel(parent: QWidget, title: str) -> QFrame:
        panel = QFrame(parent)
        panel.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)
        title_label = QLabel(title, panel)
        title_label.setFont(theme.font(14, "bold"))
        title_label.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        layout.addWidget(title_label)
        return panel

    @staticmethod
    def _combo(parent: QWidget, values: list[str], width: int) -> QComboBox:
        combo = QComboBox(parent)
        combo.addItems(values)
        combo.setFixedWidth(width)
        combo.setFont(theme.font(12))
        combo.setStyleSheet(theme.combo_style())
        return combo

    @staticmethod
    def _style_button(
        button: QPushButton, width: int, height: int, color: str = theme.TEXT
    ) -> None:
        button.setFixedSize(width, height)
        button.setFont(theme.font(13))
        button.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, color, 8, theme.BORDER, 1)
        )

    def on_show(self, **kwargs) -> None:
        self._consent_var.set(bool(self.app.settings.get("clone", "consent", False)))
        saved_engine = self.app.settings.get("clone", "engine", "xtts")
        self.engine_menu.setCurrentText(
            _ENGINE_LABELS.get(saved_engine, "Coqui XTTS-v2")
        )
        self._populate_fallback_voices()
        self._refresh_clone_state()
        self._populate_clone_languages()
        if self.app.tts.voices_loaded and not self._fallback_populated:
            self._fallback_populated = True
            self._populate_fallback_voices()
        saved_profile = self.app.settings.get("clone", "profile", "")
        if self.profile_path is None and saved_profile and Path(saved_profile).exists():
            self.profile_path = Path(saved_profile)

    def on_hide(self) -> None:
        self._ref_tracking = False
        self._ref_track_timer.stop()
        if self.app.recorder.is_recording:
            self.app.recorder.stop()

    def _populate_fallback_voices(self) -> None:
        voices = self.app.tts.voices()
        if not voices:
            return
        self._fallback_populated = True
        self._fallback_map = {
            voice.get("friendly", voice["short_name"])[:60]
            + "  ·  "
            + voice["locale"]: voice["short_name"]
            for voice in voices
        }
        values = list(self._fallback_map)
        self._set_items(self.fallback_voice_menu, values)
        saved = self.app.settings.get("clone", "fallback_voice", "")
        # A saved voice whose engine is gone (Piper uninstalled, model deleted)
        # must not stay selected -- generating would fail every time.
        if saved and not self.app.tts.voice_usable(saved):
            saved = self._resolve_usable_voice("")
            if saved:
                self.app.settings.set("clone", "fallback_voice", saved)
        selected = next(
            (label for label, short in self._fallback_map.items() if short == saved),
            values[0],
        )
        self.fallback_voice_menu.setCurrentText(selected)

    def _resolve_usable_voice(self, lang_code: str) -> str:
        """A fallback voice this machine can really speak, or ""."""
        tts = self.app.tts
        pool = tts.voices(lang_code) if lang_code else []
        for voice in pool or tts.voices():
            if voice.get("gender", "").lower() in ("female", "feminine") and tts.voice_usable(
                voice["short_name"]
            ):
                return voice["short_name"]
        for voice in pool or tts.voices():
            if tts.voice_usable(voice["short_name"]):
                return voice["short_name"]
        return tts._system_fallback_voice(lang_code) or ""

    def _fallback_voice(self, lang_code: str = "") -> str:
        tts = self.app.tts
        saved = self.app.settings.get("clone", "fallback_voice", "")
        if saved and tts.voice_usable(saved):
            return saved
        resolved = self._resolve_usable_voice(lang_code)
        if resolved:
            return resolved
        display = self.fallback_voice_menu.currentText()
        return self._fallback_map.get(display, saved)

    def _populate_clone_languages(self) -> None:
        languages = sorted(self.app.active_cloner.supported_languages())
        values = [f"{language_display_name(code)} ({code})" for code in languages]
        self._set_items(self.clone_lang_menu, values)
        if values and self.clone_lang_menu.currentText() not in values:
            self.clone_lang_menu.setCurrentText(values[0])

    def _on_engine(self, label: str) -> None:
        engine = _ENGINE_KEYS.get(label, "xtts")
        self.app.settings.set("clone", "engine", engine)
        self._populate_clone_languages()
        self._refresh_clone_state()
        if engine == "qwen" and not self.app.qwen_cloner._cuda_available():
            self.toast(
                "Qwen3-TTS: no NVIDIA GPU found. Cloning will run on the CPU "
                "and take several minutes per clip — XTTS is much faster here.",
                "warn",
            )
        else:
            self.toast(f"Clone engine: {label}", "info")

    def _toggle_ref_record(self) -> None:
        recorder = self.app.recorder
        if recorder.is_recording:
            self._stop_ref_record()
            return
        try:
            recorder.start()
        except Exception as exc:
            self.toast(str(exc), "error")
            return
        # The button stays clickable so a short take can be ended by hand, but
        # the take also ends by itself at MAX_SAMPLE_SECONDS.
        self.ref_record_btn.set_idle_text("Stop & Use")
        self.ref_timer.setStyleSheet(theme.label_style(theme.SUCCESS))
        self.consent_lbl.setText(
            "Recording… speak clearly. It stops and loads itself at "
            f"{MAX_SAMPLE_SECONDS:.0f}s."
        )
        self.consent_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self._ref_tracking = True
        self._ref_peak_seen = False
        self._ref_track_timer.start()
        self._ref_tick()

    def _ref_tick(self) -> None:
        if not self._ref_tracking:
            return
        recorder = self.app.recorder
        elapsed = recorder.elapsed()
        tenths = int(elapsed * 10)
        self.ref_timer.setText(
            f"{tenths // 600:02d}:{(tenths // 10) % 60:02d}.{tenths % 10}"
        )
        remaining = MAX_SAMPLE_SECONDS - elapsed
        if remaining <= 0.0:
            # End the take by itself: the user only has to talk, never to stop.
            self._stop_ref_record(auto=True)
            return
        countdown = int(remaining) + 1
        if countdown <= 4:
            if self._ref_peak_seen:
                self.consent_lbl.setText(f"{countdown}s left…")
                self.consent_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
            else:
                self.consent_lbl.setText(
                    f"No sound yet — {countdown}s left. Move closer to the mic."
                )
                self.consent_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
        # One read only: the queue holds a single level, so a second get_nowait()
        # would always come back empty.
        try:
            level = recorder.level_queue.get_nowait()
        except Exception:
            level = None
        if level is None:
            return
        self.ref_meter.push(level)
        if level > 0.0 and not self._ref_peak_seen:
            # Warn while recording rather than after, when the only feedback is
            # that the sample was refused.
            self._ref_peak_seen = True
            self.consent_lbl.setText("Microphone is picking up sound… keep going.")
            self.consent_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))

    def _stop_ref_record(self, auto: bool = False) -> None:
        recorder = self.app.recorder
        if not recorder.is_recording and recorder.capture is None:
            return
        self._ref_tracking = False
        self._ref_track_timer.stop()
        recorder.stop()
        self.ref_record_btn.set_idle_text("Record & Use")
        self.ref_meter.setValue(0)
        if not self._ref_peak_seen:
            # The take produced no signal at all. Say so plainly instead of
            # letting it fail later as an unexplained "silent" rejection.
            self.toast(
                "The microphone picked up no sound. Check the input device in "
                "Settings > Microphone, and speak closer to the mic.",
                "error",
            )
            self._ref_peak_seen = False
            return
        if recorder.capture is None or recorder.capture.size == 0:
            self.toast("No audio captured.", "warn")
            return
        # Count real samples, not wall-clock time, so this matches exactly what
        # validate_reference_sample will measure. Refusing here avoids writing a
        # file that would be rejected a moment later.
        duration = 0.0
        if recorder.samplerate:
            duration = float(recorder.capture.shape[0]) / float(recorder.samplerate)
        if duration < MIN_SAMPLE_SECONDS:
            self.toast(
                f"Too short ({duration:.1f}s). Speak for at least "
                f"{MIN_SAMPLE_SECONDS:.0f}s, then press Record & Use again.",
                "warn",
            )
            return
        path = file_service.unique_path(
            file_service.category_dir("voices"),
            file_service.timestamp_stem("sample_draft"),
            "wav",
        )
        try:
            recorder.save(path, "wav")
            if auto:
                self.toast(
                    f"Reached {MAX_SAMPLE_SECONDS:.0f}s — stopped and loaded "
                    "automatically.",
                    "ok",
                )
            self._accept_reference(path)
        except Exception as exc:
            self.toast(str(exc), "error")

    def _upload_sample(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select an authorised voice sample",
            "",
            "Audio (*.wav *.mp3 *.ogg *.flac);;All files (*)",
        )
        if path:
            self._accept_reference(Path(path))

    def _accept_reference(self, path: Path) -> None:
        try:
            self.app.active_cloner.validate_sample(path)
        except Exception as exc:
            self.toast(str(exc), "error")
            return
        self.ref_path = path
        self.ref_duration = audio_utils.audio_duration(path)
        self.ref_lbl.setText(
            f"Sample: {path.name}  ·  {self.ref_duration:.1f}s  "
            "(authorised use confirmed by you)"
        )
        self.ref_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        self.toast("Voice sample accepted.", "ok")
        if self.player_bar is not None:
            try:
                self.player_bar.set_file(path)
            except Exception:
                pass

    def _on_consent(self) -> None:
        granted = self._consent_var.get()
        self.app.settings.set("clone", "consent", granted)
        if granted:
            self.consent_lbl.setText(
                "Consent recorded. You may now create a profile and generate speech."
            )
            self.consent_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        else:
            self.consent_lbl.setText("Consent required before generating.")
            self.consent_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))

    def _create_profile(self) -> None:
        if self._clone_disabled():
            # Checked before anything is written: a profile created with cloning
            # off looks successful but is never used for anything.
            self._apply_clone_disabled_notice()
            self.toast(
                "Voice cloning is switched off — turn it on in Settings first.",
                "warn",
            )
            return
        if not self._consent_var.get():
            self.toast("Please confirm voice-use consent first.", "warn")
            return
        if self.ref_path is None:
            self.toast("Press Record & Use to take a voice sample first.", "warn")
            return
        self.create_profile_btn.set_busy(True, "Creating profile…")
        try:
            destination = file_service.unique_path(
                file_service.category_dir("voices"), "voice_profile", "wav"
            )
            if self.ref_path.suffix.lower() == ".wav":
                destination.write_bytes(self.ref_path.read_bytes())
            else:
                audio_utils.convert_format(self.ref_path, destination)
            self.profile_path = destination
            self.app.settings.set("clone", "profile", str(destination))
            self.ref_lbl.setText(
                f"Profile saved: {destination.name}  (reference: {self.ref_path.name})"
            )
            self.ref_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
            self._load_clone_model()
            self.toast("Voice profile created.", "ok")
        except Exception as exc:
            self.toast(str(exc), "error")
        finally:
            self.create_profile_btn.set_busy(False)

    def _clone_disabled(self) -> bool:
        return not self.app.settings.get("clone", "enabled", True)

    def _apply_clone_disabled_notice(self) -> None:
        """State plainly, in a persistent line, that cloning is switched off.

        This used to be a toast only. A toast disappears after a few seconds, so
        the sample would load, the profile would be written, and then nothing
        would happen for no visible reason — which looked exactly like a
        rejected sample. A permanent label cannot be missed.
        """
        self.clone_status_lbl.setText(
            "⚠ Voice cloning is switched OFF in Settings, so nothing will be "
            "cloned. Turn it on in Settings → Voice Cloning. The model also "
            "still needs downloading once (~2 GB)."
        )
        self.clone_status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))

    def _load_clone_model(self) -> None:
        if self._clone_disabled():
            self._apply_clone_disabled_notice()
            self.toast(
                "Voice cloning is switched off in Settings — nothing will be "
                "cloned until you enable it.",
                "warn",
            )
            return
        engine = self.app.active_cloner
        if engine.state in (CloneState.READY, CloneState.LOADING):
            self._refresh_clone_state()
            return

        def on_change(state, message):
            self.app.schedule(self._apply_clone_state, state, message)

        engine.load_background(on_change)
        self._refresh_clone_state()

    def _refresh_clone_state(self) -> None:
        if self._clone_disabled():
            self._apply_clone_disabled_notice()
            return
        engine = self.app.active_cloner
        state = engine.state
        message = engine.error or {
            CloneState.NOT_LOADED: "Model not loaded yet.",
            CloneState.LOADING: (
                "Loading voice-cloning model… first run downloads the weights."
            ),
            CloneState.READY: "Voice-cloning model ready.",
            CloneState.ERROR: (
                "Voice cloning unavailable — a neural fallback voice will be used."
            ),
        }.get(state, "")
        if self._qwen_without_gpu():
            # Keep this in the persistent status line, not just a toast: a toast
            # disappears after a few seconds and the user is then left clicking
            # Clone wondering why it takes minutes.
            message = (
                f"{message}  ⚠  No NVIDIA GPU detected — Qwen3-TTS 1.7B will "
                "run on the CPU and can take several minutes per clip. "
                "Use the XTTS engine for faster cloning."
            ).strip()
            self._apply_clone_state(state, message)
            self.clone_status_lbl.setStyleSheet(
                theme.label_style(theme.WARNING, "left")
            )
            return
        self._apply_clone_state(state, message)

    def _qwen_without_gpu(self) -> bool:
        """True when Qwen3-TTS is the active clone engine and CUDA is absent."""
        settings_engine = self.app.settings.get("clone", "engine", "xtts")
        cloner = self.app.qwen_cloner
        if cloner is None or not hasattr(cloner, "_cuda_available"):
            return False
        return (
            settings_engine == "qwen" and not cloner._cuda_available()
        )

    def _apply_clone_state(self, state, message: str) -> None:
        colors = {
            CloneState.NOT_LOADED: (theme.SUBTEXT, "pyttsx3"),
            CloneState.LOADING: (theme.WARNING, "fallback-clone"),
            CloneState.READY: (theme.SUCCESS, "clone"),
            CloneState.ERROR: (theme.DANGER, "fallback-clone"),
        }
        color, badge_method = colors.get(state, (theme.SUBTEXT, "pyttsx3"))
        self.clone_status_lbl.setText(message)
        self.clone_status_lbl.setStyleSheet(theme.label_style(color, "left"))
        old = self.clone_state_badge
        parent = old.parentWidget()
        old.hide()
        old.deleteLater()
        badge = MethodBadge(parent, badge_method)
        parent_layout = parent.layout() if parent is not None else None
        if parent_layout is not None:
            parent_layout.addWidget(badge)
        self.clone_state_badge = badge

    def _generate(self) -> None:
        if self._generating:
            return
        if self._clone_disabled():
            self._apply_clone_disabled_notice()
            self.toast(
                "Voice cloning is switched off in Settings — you will get an "
                "offline system voice instead of your own cloned voice.",
                "warn",
            )
        if not self._consent_var.get():
            self.toast("Voice-use consent is required.", "warn")
            return
        text = self.gen_text.get().strip()
        if not text:
            self.toast("Enter text to generate.", "warn")
            return
        profile = self.profile_path or self.ref_path
        if profile is None or not Path(profile).exists():
            self.toast("Create a voice profile first.", "warn")
            return
        lang_code = (
            self.clone_lang_menu.currentText().split("(")[-1].rstrip(")").strip()
            or "en"
        )
        self._gen_lang = lang_code
        self._generating = True
        self.generate_btn.set_busy(True, "Generating…")
        engine = self.app.active_cloner
        if (
            engine.state == CloneState.READY
            and lang_code in engine.supported_languages()
        ):
            destination = file_service.unique_path(
                file_service.category_dir("clones"),
                file_service.timestamp_stem("my_voice"),
                "wav",
            )
            self.gen_status_lbl.setText(
                "Voice cloning in use — synthesising from your reference recording…"
            )
            self.gen_status_lbl.setStyleSheet(theme.label_style(theme.ACCENT, "left"))
            try:
                engine.synthesize(
                    text=text,
                    reference_wav=profile,
                    language=lang_code,
                    output_path=destination,
                    transcript=self.transcript_entry.text(),
                    on_change=lambda message: self.app.schedule(
                        self._set_generation_status, message, theme.ACCENT
                    ),
                    on_done=lambda path, exc: self.app.schedule(
                        self._on_clone_done, destination, path, exc, text
                    ),
                )
            except Exception as exc:
                self._on_clone_done(destination, None, exc, text)
            return
        self._fallback_generate(text, lang_code)

    def _set_generation_status(self, message: str, color: str) -> None:
        self.gen_status_lbl.setText(message)
        self.gen_status_lbl.setStyleSheet(theme.label_style(color, "left"))

    def _fallback_generate(self, text: str, lang_code: str) -> None:
        if lang_code and not self._has_voice_for(lang_code):
            # A fallback voice for some *other* language would read the text in
            # the wrong accent, so name the gap instead of generating nonsense.
            self._generating = False
            self.generate_btn.set_busy(False)
            label = language_display_name(lang_code)
            if PiperVoiceLibrary().can_never_speak(lang_code):
                # No download could ever fix this, so do not offer one.
                self._set_get_voice_button(label, lang_code, visible=False)
                self.gen_status_lbl.setText(
                    f"No offline {label} voice is published, so {label} text "
                    f"cannot be spoken aloud."
                )
                self.toast(f"No {label} voice exists.", "warn")
            else:
                self.gen_status_lbl.setText(
                    f"No {label} voice is installed, so this text cannot be "
                    f"spoken correctly. Download one (~60 MB) to hear it."
                )
                self._set_get_voice_button(label, lang_code, visible=True)
                self.toast(f"No {label} voice installed.", "warn")
            self.gen_status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            return
        self._set_get_voice_button(language_display_name(lang_code or "en"), lang_code or "en", visible=False)
        self._generating = True
        self.generate_btn.set_busy(True, "Generating fallback…")
        voice = self._fallback_voice(lang_code)
        self.gen_status_lbl.setText(
            "Voice cloning unavailable — using a neural fallback voice instead."
        )
        self.gen_status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        try:
            future = self.app.tts.synthesize_async(text, voice, 1.0, 0)
        except Exception as exc:
            self._on_generation_error(exc)
            return
        self.run_between(future, self._on_fallback_done, self._on_generation_error)

    def _has_voice_for(self, code: str) -> bool:
        """True when some installed voice can speak this language."""
        try:
            return bool(self.app.tts.voices(code))
        except Exception:  # noqa: BLE001 - a refresh failure must not crash
            return False

    def _set_get_voice_button(self, label: str, code: str, visible: bool) -> None:
        if not hasattr(self, "get_voice_btn"):
            return
        self.get_voice_btn.setVisible(visible)
        article = "an" if label[:1].lower() in "aeiou" else "a"
        self.get_voice_btn.setText(f"Get {article} {label} voice")
        self.get_voice_btn.setProperty("language", code)
        self.get_voice_btn.style().unpolish(self.get_voice_btn)
        self.get_voice_btn.style().polish(self.get_voice_btn)

    def _on_get_voice(self) -> None:
        code = self.get_voice_btn.property("language") or self._gen_lang or "en"
        self._set_get_voice_button(language_display_name(code), code, visible=False)
        self.app.show_screen("voices", language=code)

    def _on_clone_done(self, destination: Path, path, exc, text: str) -> None:
        self._generating = False
        self.generate_btn.set_busy(False)
        if exc is not None or path is None:
            self.gen_status_lbl.setText(f"Cloning failed: {exc}")
            self.gen_status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
            profile = self.profile_path or self.ref_path
            if profile is not None:
                self._fallback_generate(text, self._gen_lang or "en")
            return
        self._last_result = {"file": str(destination), "method": self._clone_method()}
        self._result_badge(self._clone_method())
        self._history_entry(self._clone_method(), text)
        self._preview(destination)
        self.gen_status_lbl.setText("Voice Clone generated from your reference voice.")
        self.gen_status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        self.toast("Voice Clone ready.", "ok")

    def _on_fallback_done(self, result: dict) -> None:
        self._generating = False
        self.generate_btn.set_busy(False)
        text = self.gen_text.get().strip()
        self._last_result = {"file": result["file"], "method": "fallback-clone"}
        self._result_badge("fallback-clone")
        self._history_entry("fallback-clone", text)
        self._preview(Path(result["file"]))
        self.gen_status_lbl.setText(
            "Fallback voice used (clone model unavailable). Audio preview ready."
        )
        self.gen_status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        self.toast("Generated with fallback voice.", "ok")

    def _on_generation_error(self, exc: Exception) -> None:
        self._generating = False
        self.generate_btn.set_busy(False)
        self.gen_status_lbl.setText(str(exc))
        self.gen_status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))
        self.toast("Generation failed.", "error")

    def _preview(self, path: Path) -> None:
        if self.player_bar is not None:
            try:
                self.player_bar.set_file(path)
            except Exception:
                pass

    def _clone_method(self) -> str:
        return (
            "qwen-clone"
            if self.app.settings.get("clone", "engine", "xtts") == "qwen"
            else "clone"
        )

    def _result_badge(self, method: str) -> None:
        old = self.result_badge
        parent = old.parentWidget()
        old.hide()
        old.deleteLater()
        badge = MethodBadge(parent, method)
        parent_layout = parent.layout() if parent is not None else None
        if parent_layout is not None:
            parent_layout.addWidget(badge)
        self.result_badge = badge

    def _history_entry(self, method: str, text: str) -> None:
        self.app.history.create(
            type_="clone",
            method=method,
            title=text[:60],
            text=text,
            file=self._last_result["file"],
            params={
                "reference": str(self.profile_path or self.ref_path or ""),
                "consent": bool(self._consent_var.get()),
            },
        )

    def _save_result(self) -> None:
        if self._last_result is None:
            self.toast("Generate audio first.", "warn")
            return
        source = Path(self._last_result["file"])
        destination = file_service.unique_path(
            file_service.category_dir("clones"),
            file_service.timestamp_stem("my_voice_save"),
            "wav",
        )
        self.save_btn.set_busy(True, "Saving…")
        try:
            destination.write_bytes(source.read_bytes())
            self.toast(f"Saved: {destination.name}", "ok")
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
