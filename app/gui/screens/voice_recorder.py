from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.core import audio_utils
from app.core.errors import DeviceError
from app.gui import theme
from app.gui.widgets import AudioPlayerBar, BusyButton, MicLevelMeter, Screen
from app.services import file_service


class VoiceRecorderScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self._tracking = False
        self._draft: Path | None = None
        self._recorded_seconds = 0.0
        self._track_timer = QTimer(self)
        self._track_timer.setInterval(100)
        self._track_timer.timeout.connect(self._tick)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        panel = QFrame(self)
        panel.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        panel_layout = QGridLayout(panel)
        panel_layout.setContentsMargins(16, 12, 16, 12)
        panel_layout.setHorizontalSpacing(8)
        panel_layout.setVerticalSpacing(4)
        panel_layout.setColumnStretch(4, 1)

        self.mic_status = QLabel("Checking microphone…", panel)
        self.mic_status.setFont(theme.font(13))
        self.mic_status.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        panel_layout.addWidget(self.mic_status, 0, 0, 1, 5)

        self.timer_lbl = QLabel("00:00.0", panel)
        self.timer_lbl.setFont(theme.font(40, "bold"))
        self.timer_lbl.setStyleSheet(theme.label_style(theme.TEXT))
        panel_layout.addWidget(self.timer_lbl, 1, 0)

        self.meter = MicLevelMeter(panel, width=220)
        panel_layout.addWidget(self.meter, 1, 1)
        level_label = QLabel("input level", panel)
        level_label.setFont(theme.font(11))
        level_label.setStyleSheet(theme.label_style(theme.SUBTEXT))
        panel_layout.addWidget(level_label, 2, 1)

        button_row = QFrame(panel)
        button_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.record_btn = BusyButton(
            button_row,
            text="● Record",
            command=self._record,
            width=110,
            height=38,
            font=theme.font(14, "bold"),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
        )
        button_layout.addWidget(self.record_btn)
        self.pause_btn = QPushButton("⏸ Pause", button_row)
        self.pause_btn.setFixedSize(96, 38)
        self.pause_btn.setFont(theme.font(13))
        self.pause_btn.setEnabled(False)
        self.pause_btn.setStyleSheet(
            theme.button_style(
                theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 8, theme.BORDER, 1
            )
        )
        self.pause_btn.clicked.connect(self._pause)
        button_layout.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("⏹ Stop", button_row)
        self.stop_btn.setFixedSize(88, 38)
        self.stop_btn.setFont(theme.font(13, "bold"))
        self.stop_btn.setStyleSheet(
            theme.button_style(theme.DANGER, "#c94343", theme.TEXT, 8)
        )
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        button_layout.addWidget(self.stop_btn)
        button_layout.addStretch(1)
        panel_layout.addWidget(button_row, 1, 2, 1, 3)
        root.addWidget(panel)
        root.addSpacing(14)

        play_panel = QFrame(self)
        play_panel.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        play_layout = QVBoxLayout(play_panel)
        play_layout.setContentsMargins(14, 10, 14, 12)
        play_layout.setSpacing(8)
        if app.player is not None:
            self.player_bar = AudioPlayerBar(play_panel, app.player)
            play_layout.addWidget(self.player_bar)
        else:
            self.player_bar = None
            unavailable = QLabel(
                "Playback unavailable (no audio output device). "
                "Audio files are still saved on disk.",
                play_panel,
            )
            unavailable.setWordWrap(True)
            unavailable.setFont(theme.font(13))
            unavailable.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            play_layout.addWidget(unavailable)

        save_row = QFrame(play_panel)
        save_row.setStyleSheet("QFrame { background: transparent; border: none; }")
        save_layout = QHBoxLayout(save_row)
        save_layout.setContentsMargins(0, 0, 0, 0)
        name_label = QLabel("Name:", save_row)
        name_label.setFont(theme.font(13))
        name_label.setStyleSheet(theme.label_style(theme.SUBTEXT))
        save_layout.addWidget(name_label)
        self.name_entry = QLineEdit(save_row)
        self.name_entry.setFixedWidth(280)
        self.name_entry.setPlaceholderText("voice note name")
        self.name_entry.setStyleSheet(theme.input_style())
        save_layout.addWidget(self.name_entry)
        format_label = QLabel("Format:", save_row)
        format_label.setFont(theme.font(13))
        format_label.setStyleSheet(theme.label_style(theme.SUBTEXT))
        save_layout.addWidget(format_label)
        self.format_menu = QComboBox(save_row)
        self.format_menu.addItems(["wav", "mp3"])
        self.format_menu.setFixedWidth(80)
        self.format_menu.setFont(theme.font(13))
        self.format_menu.setStyleSheet(theme.combo_style())
        save_layout.addWidget(self.format_menu)
        self.save_btn = BusyButton(
            save_row,
            text="Save Voice Note",
            command=self._save_note,
            width=140,
            height=34,
            font=theme.font(13, "bold"),
            fg_color=theme.INPUT_BG,
            hover_color=theme.CARD_BG,
            border_color=theme.BORDER,
            border_width=1,
        )
        save_layout.addWidget(self.save_btn)
        if not audio_utils.ffmpeg_available():
            warning = QLabel("ffmpeg not found — MP3 disabled.", save_row)
            warning.setFont(theme.font(11))
            warning.setStyleSheet(theme.label_style(theme.WARNING))
            save_layout.addWidget(warning)
        save_layout.addStretch(1)
        play_layout.addWidget(save_row)
        play_layout.addStretch(1)
        root.addWidget(play_panel, 1)

        self.status_lbl = QLabel("", self)
        self.status_lbl.setFont(theme.font(12))
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        root.addWidget(self.status_lbl)
        root.addSpacing(10)

    def on_show(self, **kwargs) -> None:
        self._update_mic_status()
        if not self._tracking:
            self._start_tracking()

    def on_hide(self) -> None:
        self._tracking = False
        self._track_timer.stop()

    def _update_mic_status(self) -> None:
        try:
            self.app.recorder.check_microphone(raise_error=True)
            self.mic_status.setText("Microphone ready — click Record to start.")
            self.mic_status.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        except DeviceError as exc:
            self.mic_status.setText(str(exc))
            self.mic_status.setStyleSheet(theme.label_style(theme.DANGER, "left"))

    def _start_tracking(self) -> None:
        self._tracking = True
        self._track_timer.start()
        self._tick()

    def _tick(self) -> None:
        if not self._tracking:
            return
        recorder = self.app.recorder
        elapsed = recorder.elapsed()
        tenths = int(elapsed * 10)
        self.timer_lbl.setText(
            f"{tenths // 600:02d}:{(tenths // 10) % 60:02d}.{tenths % 10}"
        )
        try:
            self.meter.push(recorder.level_queue.get_nowait())
        except Exception:
            pass

    def _record(self) -> None:
        if self.app.recorder.is_recording:
            return
        try:
            self.app.recorder.start()
        except DeviceError as exc:
            self.mic_status.setText(str(exc))
            self.mic_status.setStyleSheet(theme.label_style(theme.DANGER, "left"))
            self.toast("Could not start recording.", "error")
            return
        self._draft = None
        self.record_btn.set_busy(True, "● Recording…")
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("⏸ Pause")
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Recording…")
        self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))

    def _pause(self) -> None:
        recorder = self.app.recorder
        if recorder.is_paused:
            recorder.resume()
            self.pause_btn.setText("⏸ Pause")
            self.status_lbl.setText("Recording…")
            self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        else:
            recorder.pause()
            self.pause_btn.setText("▶ Resume")
            self.status_lbl.setText("Paused.")
            self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))

    def _stop(self) -> None:
        recorder = self.app.recorder
        if not recorder.is_recording and recorder.capture is None:
            self.toast("Nothing to stop.", "warn")
            return
        self._recorded_seconds = recorder.elapsed()
        recorder.stop()
        self.record_btn.set_busy(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("⏸ Pause")
        self.stop_btn.setEnabled(False)
        if recorder.capture is None or recorder.capture.size == 0:
            self.status_lbl.setText("No audio captured.")
            self.status_lbl.setStyleSheet(theme.label_style(theme.WARNING, "left"))
            return
        self._draft = file_service.unique_path(
            file_service.category_dir("recordings"),
            file_service.timestamp_stem("recording_draft"),
            "wav",
        )
        try:
            recorder.save(self._draft, "wav")
            self.status_lbl.setText(
                f"Captured {self._recorded_seconds:.1f}s — playback ready."
            )
            self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
            self.toast("Recording captured.", "ok")
            if self.player_bar is not None:
                try:
                    self.player_bar.set_file(self._draft)
                except Exception:
                    pass
        except Exception as exc:
            self.status_lbl.setText(str(exc))
            self.status_lbl.setStyleSheet(theme.label_style(theme.DANGER, "left"))

    def _save_note(self) -> None:
        if self._draft is None or not self._draft.exists():
            self.toast("Record something first.", "warn")
            return
        name = self.name_entry.text().strip().replace(" ", "_") or "voice_note"
        output_format = self.format_menu.currentText()
        if output_format == "mp3" and not audio_utils.ffmpeg_available():
            self.toast("MP3 needs ffmpeg — using WAV.", "warn")
            output_format = "wav"
        destination = file_service.unique_path(
            file_service.category_dir("recordings"),
            file_service.timestamp_stem(name),
            output_format,
        )
        self.save_btn.set_busy(True, "Saving…")
        try:
            if destination.suffix == self._draft.suffix:
                destination.write_bytes(self._draft.read_bytes())
                self._draft.unlink(missing_ok=True)
            else:
                audio_utils.convert_format(self._draft, destination)
                self._draft.unlink(missing_ok=True)
            self._draft = None
            self.app.history.create(
                type_="note",
                method="record",
                title=name,
                text="Voice note recorded in-app.",
                file=str(destination),
                duration=self._recorded_seconds,
            )
            self.toast(f"Voice note saved: {destination.name}", "ok")
            self.status_lbl.setText(str(destination))
            self.status_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        except Exception as exc:
            self.toast(f"Save failed: {exc}", "error")
        finally:
            self.save_btn.set_busy(False)
