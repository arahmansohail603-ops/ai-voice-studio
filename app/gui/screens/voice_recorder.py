"""Voice Recorder screen: record, pause, playback, save voice note, timer."""
from __future__ import annotations

from pathlib import Path

import customtkinter as ctk

from app.core import audio_utils
from app.core.errors import DeviceError
from app.gui import theme
from app.gui.widgets import AudioPlayerBar, BusyButton, MicLevelMeter, Screen
from app.services import file_service


class VoiceRecorderScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)

        self._tracking = False
        self._track_job = None
        self._draft: Path | None = None
        self._recorded_seconds = 0.0

        # ----------------------------------------------------------- mic-only
        panel = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        panel.grid(row=0, column=0, sticky="ew")
        panel.grid_columnconfigure(4, weight=1)

        self.mic_status = ctk.CTkLabel(
            panel, text="Checking microphone…", font=theme.font(13),
            text_color=theme.SUBTEXT, anchor="w",
        )
        self.mic_status.grid(row=0, column=0, columnspan=5, sticky="ew", padx=16, pady=(12, 2))

        self.timer_lbl = ctk.CTkLabel(
            panel, text="00:00.0", font=theme.font(40, "bold"), text_color=theme.TEXT,
        )
        self.timer_lbl.grid(row=1, column=0, padx=(16, 12), pady=(6, 6))

        self.meter = MicLevelMeter(panel, width=220)
        self.meter.grid(row=1, column=1, padx=8, pady=6)
        ctk.CTkLabel(
            panel, text="input level", font=theme.font(11), text_color=theme.SUBTEXT,
        ).grid(row=2, column=1, padx=8, pady=(0, 10))

        btn_row = ctk.CTkFrame(panel, fg_color="transparent")
        btn_row.grid(row=1, column=2, columnspan=3, rowspan=2, sticky="e", padx=16)

        self.record_btn = BusyButton(
            btn_row, text="● Record", command=self._record, width=110, height=38,
            font=theme.font(14, "bold"), fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
        )
        self.record_btn.pack(side="left", padx=4)

        self.pause_btn = ctk.CTkButton(
            btn_row, text="⏸ Pause", command=self._pause, width=96, height=38,
            font=theme.font(13), state="disabled",
            fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
        )
        self.pause_btn.pack(side="left", padx=4)

        self.stop_btn = ctk.CTkButton(
            btn_row, text="⏹ Stop", command=self._stop, width=88, height=38,
            font=theme.font(13, "bold"), state="disabled",
            fg_color=theme.DANGER, hover_color="#c94343",
        )
        self.stop_btn.pack(side="left", padx=4)

        # -------------------------------------------------------- playback row
        play_panel = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        play_panel.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        play_panel.grid_columnconfigure(0, weight=1)

        if app.player is not None:
            self.player_bar = AudioPlayerBar(play_panel, app.player)
            self.player_bar.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        else:
            self.player_bar = None

        save_row = ctk.CTkFrame(play_panel, fg_color="transparent")
        save_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        ctk.CTkLabel(save_row, text="Name:", font=theme.font(13),
                     text_color=theme.SUBTEXT).pack(side="left")
        self.name_entry = ctk.CTkEntry(
            save_row, width=280, placeholder_text="voice note name",
            fg_color=theme.INPUT_BG, border_color=theme.BORDER, font=theme.font(13),
        )
        self.name_entry.pack(side="left", padx=(8, 12))
        ctk.CTkLabel(save_row, text="Format:", font=theme.font(13),
                     text_color=theme.SUBTEXT).pack(side="left")
        self.format_menu = ctk.CTkOptionMenu(
            save_row, values=["wav", "mp3"], width=80, dynamic_resizing=False,
            font=theme.font(13), fg_color=theme.INPUT_BG, button_color=theme.ACCENT,
        )
        self.format_menu.pack(side="left", padx=(8, 12))
        self.save_btn = BusyButton(
            save_row, text="Save Voice Note", command=self._save_note,
            width=140, height=34, font=theme.font(13, "bold"),
            fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
        )
        self.save_btn.pack(side="left")
        if not audio_utils.ffmpeg_available():
            ctk.CTkLabel(
                save_row, text="ffmpeg not found — MP3 disabled.",
                font=theme.font(11), text_color=theme.WARNING,
            ).pack(side="left", padx=10)

        self.status_lbl = ctk.CTkLabel(
            self, text="", font=theme.font(12), text_color=theme.SUBTEXT, anchor="w",
        )
        self.status_lbl.grid(row=2, column=0, sticky="ew", pady=(10, 0))

    # ------------------------------------------------------------ lifecycle
    def on_show(self, **kwargs) -> None:
        self._update_mic_status()
        if not self._tracking:
            self._start_tracking()

    def on_hide(self) -> None:
        self._tracking = False
        if self._track_job:
            self.after_cancel(self._track_job)
            self._track_job = None

    def _update_mic_status(self) -> None:
        try:
            self.app.recorder.check_microphone(raise_error=True)
            self.mic_status.configure(
                text="Microphone ready — click Record to start.",
                text_color=theme.SUCCESS,
            )
        except DeviceError as exc:
            self.mic_status.configure(text=str(exc), text_color=theme.DANGER)

    # ------------------------------------------------------------- tracking
    def _start_tracking(self) -> None:
        self._tracking = True
        self._tick()

    def _tick(self) -> None:
        if not self._tracking:
            return
        rec = self.app.recorder
        elapsed = rec.elapsed()
        tenths = int(elapsed * 10)
        self.timer_lbl.configure(
            text=f"{tenths // 600:02d}:{(tenths // 10) % 60:02d}.{tenths % 10}"
        )
        try:
            level = rec.level_queue.get_nowait()
            self.meter.push(level)
        except Exception:
            pass
        self._track_job = self.after(100, self._tick)

    # ------------------------------------------------------------- actions
    def _record(self) -> None:
        if self.app.recorder.is_recording:
            return
        try:
            self.app.recorder.start()
        except DeviceError as exc:
            self.mic_status.configure(text=str(exc), text_color=theme.DANGER)
            self.toast("Could not start recording.", "error")
            return
        self._draft = None
        self.record_btn.set_busy(True, "● Recording…")
        self.pause_btn.configure(state="normal", text="⏸ Pause")
        self.stop_btn.configure(state="normal")
        self.status_lbl.configure(text="Recording…", text_color=theme.SUCCESS)

    def _pause(self) -> None:
        rec = self.app.recorder
        if rec.is_paused:
            rec.resume()
            self.pause_btn.configure(text="⏸ Pause")
            self.status_lbl.configure(text="Recording…", text_color=theme.SUCCESS)
        else:
            rec.pause()
            self.pause_btn.configure(text="▶ Resume")
            self.status_lbl.configure(text="Paused.", text_color=theme.WARNING)

    def _stop(self) -> None:
        rec = self.app.recorder
        if not rec.is_recording and rec.capture is None:
            self.toast("Nothing to stop.", "warn")
            return
        self._recorded_seconds = rec.elapsed()
        rec.stop()
        self.record_btn.set_busy(False)
        self.pause_btn.configure(state="disabled", text="⏸ Pause")
        self.stop_btn.configure(state="disabled")

        if rec.capture is None or rec.capture.size == 0:
            self.status_lbl.configure(text="No audio captured.", text_color=theme.WARNING)
            return

        self._draft = file_service.unique_path(
            file_service.category_dir("recordings"),
            file_service.timestamp_stem("recording_draft"),
            "wav",
        )
        try:
            rec.save(self._draft, "wav")
            self.status_lbl.configure(
                text=f"Captured {self._recorded_seconds:.1f}s — playback ready.",
                text_color=theme.SUCCESS,
            )
            self.toast("Recording captured.", "ok")
            if self.player_bar is not None:
                try:
                    self.player_bar.set_file(self._draft)
                except Exception:
                    pass
        except Exception as exc:
            self.status_lbl.configure(text=str(exc), text_color=theme.DANGER)

    def _save_note(self) -> None:
        if self._draft is None or not self._draft.exists():
            self.toast("Record something first.", "warn")
            return
        name = (self.name_entry.get() or "").strip().replace(" ", "_") or "voice_note"
        fmt = self.format_menu.get()
        if fmt == "mp3" and not audio_utils.ffmpeg_available():
            self.toast("MP3 needs ffmpeg — using WAV.", "warn")
            fmt = "wav"

        dest = file_service.unique_path(
            file_service.category_dir("recordings"),
            file_service.timestamp_stem(name),
            fmt,
        )
        self.save_btn.set_busy(True, "Saving…")
        try:
            if dest.suffix == self._draft.suffix:
                dest.write_bytes(self._draft.read_bytes())
                self._draft.unlink(missing_ok=True)
            else:
                audio_utils.convert_format(self._draft, dest)
                self._draft.unlink(missing_ok=True)
            self._draft = None
            self.app.history.create(
                type_="note", method="record", title=name,
                text="Voice note recorded in-app.",
                file=str(dest), duration=self._recorded_seconds,
            )
            self.toast(f"Voice note saved: {dest.name}", "ok")
            self.status_lbl.configure(text=str(dest), text_color=theme.SUCCESS)
        except Exception as exc:
            self.toast(f"Save failed: {exc}", "error")
        finally:
            self.save_btn.set_busy(False)
