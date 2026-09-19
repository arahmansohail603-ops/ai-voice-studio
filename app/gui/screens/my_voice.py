"""My Voice screen: authorized voice sample -> cloned or fallback speech.

Voice cloning is clearly labelled: outputs marked 'Voice Clone' only when the
local XTTS model actually synthesised them; otherwise a 'Fallback Voice'
notice explains a neural voice was used. Consent is required before any
generation, and the reference recording must be something the user owns or has
permission to use.
"""
from __future__ import annotations

from pathlib import Path

import customtkinter as ctk

from app.config import language_display_name
from app.core import audio_utils
from app.core.voice_cloner import CloneState, VoiceCloner
from app.gui import theme
from app.gui.widgets import AudioPlayerBar, BusyButton, MethodBadge, Screen
from app.services import file_service


class MyVoiceScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)

        self.ref_path: Path | None = None
        self.ref_duration = 0.0
        self.profile_path: Path | None = None
        self._last_result = None
        self._ref_tracking = False
        self._ref_track_job = None
        self._generating = False
        self._fallback_populated = False
        self._fallback_map: dict = {}
        self._consent_var = ctk.BooleanVar(value=False)

        # ----------------------------------------------------------- top notice
        notice = ctk.CTkFrame(self, fg_color=theme.ACCENT_SOFT, corner_radius=12)
        notice.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            notice,
            text="Voice Cloning Notice",
            font=theme.font(14, "bold"), text_color=theme.ACCENT, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 2))
        ctk.CTkLabel(
            notice,
            text="Cloning synthesises speech that resembles a recorded reference voice. "
                 "Only use a recording you own or have explicit permission to use. "
                 "Generated 'Voice Clone' audio is clearly labelled in this app.",
            font=theme.font(12), text_color=theme.TEXT, wraplength=900, justify="left", anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        # ------------------------------------------------------- source panel
        source = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        source.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        source.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(source, text="1 · Provide an authorised voice sample",
                     font=theme.font(14, "bold"), text_color=theme.TEXT, anchor="w",
                     ).grid(row=0, column=0, columnspan=6, sticky="w", padx=16, pady=(12, 2))
        ctk.CTkLabel(source, text="Record 3–40s of clear speech, or upload an existing recording.",
                     font=theme.font(12), text_color=theme.SUBTEXT, anchor="w",
                     ).grid(row=1, column=0, columnspan=6, sticky="w", padx=16, pady=(0, 10))

        self.ref_record_btn = BusyButton(
            source, text="Record Sample", command=self._toggle_ref_record,
            width=130, height=34, font=theme.font(13, "bold"),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )
        self.ref_record_btn.grid(row=2, column=0, padx=(16, 6), pady=(0, 10))
        self.ref_stop_btn = ctk.CTkButton(
            source, text="Stop", command=self._stop_ref_record, width=70, height=34,
            state="disabled", font=theme.font(13), fg_color=theme.DANGER, hover_color="#c94343",
        )
        self.ref_stop_btn.grid(row=2, column=1, padx=6, pady=(0, 10))
        self.ref_timer = ctk.CTkLabel(source, text="00:00.0", font=theme.font(14, "bold"),
                                      text_color=theme.SUBTEXT)
        self.ref_timer.grid(row=2, column=2, padx=10, pady=(0, 10))

        self.upload_btn = ctk.CTkButton(
            source, text="Upload Sample…", command=self._upload_sample,
            width=130, height=34, font=theme.font(13),
            fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
        )
        self.upload_btn.grid(row=2, column=3, padx=(14, 6), pady=(0, 10))

        # ------------------------------------------------------ consent (SP)
        self.consent_lbl = ctk.CTkLabel(
            source, text="", font=theme.font(12), text_color=theme.SUBTEXT, anchor="w",
        )
        self.consent_lbl.grid(row=3, column=0, columnspan=6, sticky="w", padx=16, pady=(0, 10))

        # ------------------------------------------------------------- sample
        sample = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        sample.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        sample.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(sample, text="2 · Reference sample & profile",
                     font=theme.font(14, "bold"), text_color=theme.TEXT, anchor="w",
                     ).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 2))
        self.ref_lbl = ctk.CTkLabel(
            sample, text="No voice sample loaded yet.", font=theme.font(12),
            text_color=theme.WARNING, anchor="w",
        )
        self.ref_lbl.grid(row=1, column=0, columnspan=3, sticky="w", padx=16, pady=(4, 4))

        self.consent = ctk.CTkCheckBox(
            sample,
            text="I confirm I own or have permission to use this voice recording.",
            variable=self._consent_var, command=self._on_consent,
            font=theme.font(12), fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )
        self.consent.grid(row=2, column=0, columnspan=3, sticky="w", padx=16, pady=(4, 6))

        self.create_profile_btn = BusyButton(
            sample, text="Create Voice Profile", command=self._create_profile,
            height=36, font=theme.font(13, "bold"),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )
        self.create_profile_btn.grid(row=3, column=0, sticky="w", padx=16, pady=(2, 6))

        self.clone_state_badge = MethodBadge(sample, "clone")
        self.clone_state_badge.grid(row=3, column=1, sticky="w", padx=8, pady=(2, 6))

        self.clone_status_lbl = ctk.CTkLabel(
            sample, text="Model not loaded. 'Create Voice Profile' loads it on first use (~2 GB).",
            font=theme.font(12), text_color=theme.SUBTEXT, anchor="w",
        )
        self.clone_status_lbl.grid(row=4, column=0, columnspan=3, sticky="ew", padx=16, pady=(0, 12))

        # ------------------------------------------------------- generation
        gen_panel = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        gen_panel.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        gen_panel.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(gen_panel, text="3 · Generate speech with your voice",
                     font=theme.font(14, "bold"), text_color=theme.TEXT, anchor="w",
                     ).grid(row=0, column=0, columnspan=4, sticky="w", padx=16, pady=(12, 4))

        self.gen_text = ctk.CTkTextbox(
            gen_panel, height=110, wrap="word", corner_radius=10,
            fg_color=theme.INPUT_BG, text_color=theme.TEXT,
            border_width=1, border_color=theme.BORDER, font=theme.font(13),
        )
        self.gen_text.grid(row=1, column=0, columnspan=4, sticky="ew", padx=16, pady=(0, 10))
        self.gen_text.insert("1.0", "Speak the way I speak. This sentence is cloned from my voice.")

        ctk.CTkLabel(gen_panel, text="Language:", font=theme.font(13),
                     text_color=theme.SUBTEXT).grid(row=2, column=0, sticky="e", padx=(16, 4))
        self.clone_lang_menu = ctk.CTkOptionMenu(
            gen_panel, values=[f"{language_display_name(l)} ({l})" for l in VoiceCloner.supported_languages()],
            width=180, dynamic_resizing=False, font=theme.font(12),
            fg_color=theme.INPUT_BG, button_color=theme.ACCENT,
        )
        self.clone_lang_menu.set("English (en)")
        self.clone_lang_menu.grid(row=2, column=1, sticky="w", padx=4, pady=(0, 10))

        ctk.CTkLabel(gen_panel, text="Fallback voice:", font=theme.font(13),
                     text_color=theme.SUBTEXT).grid(row=2, column=2, sticky="e", padx=(12, 4))
        self.fallback_voice_menu = ctk.CTkOptionMenu(
            gen_panel, values=["Default"], width=240, dynamic_resizing=False,
            font=theme.font(12), fg_color=theme.INPUT_BG, button_color=theme.ACCENT,
        )
        self.fallback_voice_menu.grid(row=2, column=3, sticky="w", padx=4, pady=(0, 10))

        self.generate_btn = BusyButton(
            gen_panel, text="Generate Speech", command=self._generate,
            height=38, font=theme.font(14, "bold"),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )
        self.generate_btn.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 14))
        self.result_badge = MethodBadge(gen_panel, "clone")
        self.result_badge.grid(row=3, column=1, sticky="w", padx=4, pady=(0, 14))
        self.gen_status_lbl = ctk.CTkLabel(
            gen_panel, text="", font=theme.font(12), text_color=theme.SUBTEXT, anchor="w",
        )
        self.gen_status_lbl.grid(row=3, column=2, columnspan=2, sticky="ew", padx=12, pady=(0, 14))

        # ------------------------------------------------------ preview/save
        preview = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        preview.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        preview.grid_columnconfigure(0, weight=1)

        if app.player is not None:
            self.player_bar = AudioPlayerBar(preview, app.player)
            self.player_bar.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        else:
            self.player_bar = None

        save_row = ctk.CTkFrame(preview, fg_color="transparent")
        save_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        self.save_btn = BusyButton(
            save_row, text="Save Audio", command=self._save_result,
            width=130, height=34, font=theme.font(13, "bold"),
            fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
        )
        self.save_btn.pack(side="left")

    # ------------------------------------------------------------ lifecycle
    def on_show(self, **kwargs) -> None:
        self._consent_var.set(bool(self.app.settings.get("clone", "consent", False)))
        self._populate_fallback_voices()
        self._refresh_clone_state()

        if self.app.tts.voices_loaded and not self._fallback_populated:
            self._fallback_populated = True
            self._populate_fallback_voices()

    def on_hide(self) -> None:
        self._ref_tracking = False
        if self._ref_track_job:
            self.after_cancel(self._ref_track_job)
            self._ref_track_job = None
        if self.app.recorder.is_recording:
            self.app.recorder.stop()

    # ------------------------------------------------------------ fallback voices
    def _populate_fallback_voices(self) -> None:
        voices = self.app.tts.voices()
        if not voices:
            return
        self._fallback_map = {
            v["friendly"][:60] + "  ·  " + v["locale"]: v["short_name"] for v in voices
        }
        values = list(self._fallback_map)
        self.fallback_voice_menu.configure(values=values)
        saved = self.app.settings.get("clone", "fallback_voice", "")
        if values:
            for display, short in self._fallback_map.items():
                if short == saved:
                    self.fallback_voice_menu.set(display)
                    break
            else:
                self.fallback_voice_menu.set(values[0])

    def _fallback_voice(self) -> str:
        display = self.fallback_voice_menu.get()
        if self._fallback_map and display in self._fallback_map:
            return self._fallback_map[display]
        return self.app.settings.get("clone", "fallback_voice", "en-US-ChristopherNeural")

    # ---------------------------------------------------------- record sample
    def _toggle_ref_record(self) -> None:
        rec = self.app.recorder
        if rec.is_recording:
            self._stop_ref_record()
            return
        try:
            rec.start()
            self.ref_record_btn.set_busy(True, "Recording…")
            self.ref_stop_btn.configure(state="normal")
            self.ref_timer.configure(text_color=theme.SUCCESS)
            self.consent_lbl.configure(text="Recording reference sample… speak clearly.", text_color=theme.WARNING)
            self._ref_tracking = True
            self._ref_tick()
        except Exception as exc:
            self.toast(str(exc), "error")

    def _ref_tick(self) -> None:
        if not self._ref_tracking:
            return
        secs = int(self.app.recorder.elapsed() * 10)
        self.ref_timer.configure(text=f"{secs // 600:02d}:{(secs // 10) % 60:02d}.{secs % 10}")
        self._ref_track_job = self.after(100, self._ref_tick)

    def _stop_ref_record(self) -> None:
        rec = self.app.recorder
        if not rec.is_recording and rec.capture is None:
            return
        self._ref_tracking = False
        rec.stop()
        self.ref_record_btn.set_busy(False)
        self.ref_stop_btn.configure(state="disabled")

        if rec.capture is None or rec.capture.size == 0:
            self.toast("No audio captured.", "warn")
            return
        path = file_service.unique_path(
            file_service.category_dir("voices"),
            file_service.timestamp_stem("sample_draft"), "wav",
        )
        try:
            rec.save(path, "wav")
            self._accept_reference(path)
        except Exception as exc:
            self.toast(str(exc), "error")

    def _upload_sample(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="Select an authorised voice sample",
            filetypes=[("Audio", "*.wav *.mp3 *.ogg *.flac"), ("All files", "*.*")],
        )
        if not path:
            return
        self._accept_reference(Path(path))

    def _accept_reference(self, path: Path) -> None:
        try:
            self.app.cloner.validate_sample(path)
        except Exception as exc:
            self.toast(str(exc), "error")
            return
        self.ref_path = path
        self.ref_duration = audio_utils.audio_duration(path)
        self.ref_lbl.configure(
            text=f"Sample: {path.name}  ·  {self.ref_duration:.1f}s  (authorised use confirmed by you)",
            text_color=theme.SUCCESS,
        )
        self.toast("Voice sample accepted.", "ok")
        if self.player_bar is not None:
            try:
                self.player_bar.set_file(path)
            except Exception:
                pass

    def _on_consent(self) -> None:
        granted = bool(self._consent_var.get())
        self.app.settings.set("clone", "consent", granted)
        if granted:
            self.consent_lbl.configure(
                text="Consent recorded. You may now create a profile and generate speech.",
                text_color=theme.SUCCESS,
            )
        else:
            self.consent_lbl.configure(text="Consent required before generating.", text_color=theme.WARNING)

    # --------------------------------------------------------- create profile
    def _create_profile(self) -> None:
        if not self._consent_var.get():
            self.toast("Please confirm voice-use consent first.", "warn")
            return
        if self.ref_path is None:
            self.toast("Record or upload a voice sample first.", "warn")
            return

        self.create_profile_btn.set_busy(True, "Creating profile…")
        try:
            dest = file_service.unique_path(
                file_service.category_dir("voices"), "voice_profile", "wav"
            )
            if self.ref_path.suffix.lower() == ".wav":
                dest.write_bytes(self.ref_path.read_bytes())
            else:
                audio_utils.convert_format(self.ref_path, dest)
            self.profile_path = dest
            self.app.settings.set("clone", "profile", str(dest))
            self.ref_lbl.configure(
                text=f"Profile saved: {dest.name}  (reference: {self.ref_path.name})",
                text_color=theme.SUCCESS,
            )
            self._load_clone_model()
            self.toast("Voice profile created.", "ok")
        except Exception as exc:
            self.toast(str(exc), "error")
        finally:
            self.create_profile_btn.set_busy(False)

    def _load_clone_model(self) -> None:
        state = self.app.cloner.state
        if state in (CloneState.READY, CloneState.LOADING):
            self._refresh_clone_state()
            return

        def on_change(state, message):
            self.app.schedule(lambda: self._apply_clone_state(state, message))

        self.app.cloner.load_background(on_change)
        self._refresh_clone_state()

    def _refresh_clone_state(self) -> None:
        state = self.app.cloner.state
        message = self.app.cloner.error or {
            CloneState.NOT_LOADED: "Model not loaded yet.",
            CloneState.LOADING: "Loading voice-cloning model… first run downloads ~2 GB.",
            CloneState.READY: "Voice-cloning model ready.",
            CloneState.ERROR: "Voice cloning unavailable — a neural fallback voice will be used.",
        }.get(state, "")
        self._apply_clone_state(state, message)

    def _apply_clone_state(self, state, message: str) -> None:
        colors = {
            CloneState.NOT_LOADED: (theme.SUBTEXT, "edge-tts"),
            CloneState.LOADING: (theme.WARNING, "fallback-clone"),
            CloneState.READY: (theme.SUCCESS, "clone"),
            CloneState.ERROR: (theme.DANGER, "fallback-clone"),
        }
        color, badge = colors.get(state, (theme.SUBTEXT, "edge-tts"))
        self.clone_status_lbl.configure(text=message, text_color=color)
        self.clone_state_badge.destroy()
        self.clone_state_badge = MethodBadge(self.clone_state_badge.master, badge)
        self.clone_state_badge.grid(row=3, column=1, sticky="w", padx=8, pady=(2, 6))

    # ------------------------------------------------------------- generate
    def _generate(self) -> None:
        if self._generating:
            return
        if not self._consent_var.get():
            self.toast("Voice-use consent is required.", "warn")
            return

        text = self.gen_text.get("1.0", "end-1c").strip()
        if not text:
            self.toast("Enter text to generate.", "warn")
            return
        profile = self.profile_path
        if profile is None and self.ref_path is not None:
            profile = self.ref_path
        if profile is None or not Path(profile).exists():
            self.toast("Create a voice profile first.", "warn")
            return

        lang_code = self.clone_lang_menu.get().split("(")[-1].rstrip(")").strip()
        if not lang_code:
            lang_code = "en"
        self._generating = True
        self.generate_btn.set_busy(True, "Generating…")

        if self.app.cloner.state == CloneState.READY:
            dest = file_service.unique_path(
                file_service.category_dir("clones"),
                file_service.timestamp_stem("my_voice"), "wav",
            )
            self.gen_status_lbl.configure(
                text="Voice cloning in use — synthesising from your reference recording…",
                text_color=theme.ACCENT,
            )
            self.app.cloner.synthesize(
                text=text,
                reference_wav=profile,
                language=lang_code,
                output_path=dest,
                on_change=lambda m: self.app.schedule(lambda: self.gen_status_lbl.configure(text=m, text_color=theme.ACCENT)),
                on_done=lambda path, exc: self.app.schedule(
                    lambda: self._on_clone_done(dest, path, exc)
                ),
            )
            return
        self._fallback_generate(text, lang_code)

    def _fallback_generate(self, text: str, lang_code: str) -> None:
        voice = self._fallback_voice()
        self.gen_status_lbl.configure(
            text="⚠ Voice cloning unavailable — using a neural fallback voice instead.",
            text_color=theme.WARNING,
        )
        future = self.app.tts.synthesize_async(text, voice, 1.0, 0)
        self.run_between(future, on_success=self._on_fallback_done, on_error=self._on_generation_error)

    def _on_clone_done(self, dest: Path, path, exc) -> None:
        self._generating = False
        self.generate_btn.set_busy(False)
        if exc is not None or path is None:
            self.gen_status_lbl.configure(text=f"Cloning failed: {exc}", text_color=theme.DANGER)
            profile = self.profile_path or self.ref_path
            if profile is not None:
                text = self.gen_text.get("1.0", "end-1c").strip()
                self._fallback_generate(text, "en")
            return
        self._last_result = {"file": str(dest), "method": "clone"}
        self._result_badge("clone")
        self._history_entry("clone", text)
        self._preview(dest)
        self.gen_status_lbl.configure(text="Voice Clone generated from your reference voice.", text_color=theme.SUCCESS)
        self.toast("Voice Clone ready.", "ok")

    def _on_fallback_done(self, result: dict) -> None:
        self._generating = False
        self.generate_btn.set_busy(False)
        text = self.gen_text.get("1.0", "end-1c").strip()
        self._last_result = {"file": result["file"], "method": "fallback-clone"}
        self._result_badge("fallback-clone")
        self._history_entry("fallback-clone", text)
        self._preview(Path(result["file"]))
        self.gen_status_lbl.configure(
            text="Fallback voice used (clone model unavailable). Audio preview ready.",
            text_color=theme.WARNING,
        )
        self.toast("Generated with fallback voice.", "ok")

    def _on_generation_error(self, exc: Exception) -> None:
        self._generating = False
        self.generate_btn.set_busy(False)
        self.gen_status_lbl.configure(text=str(exc), text_color=theme.DANGER)
        self.toast("Generation failed.", "error")

    def _preview(self, path: Path) -> None:
        if self.player_bar is not None:
            try:
                self.player_bar.set_file(path)
            except Exception:
                pass

    def _result_badge(self, method: str) -> None:
        self.result_badge.destroy()
        self.result_badge = MethodBadge(self.result_badge.master, method)
        self.result_badge.grid(row=3, column=1, sticky="w", padx=4, pady=(0, 14))

    def _history_entry(self, method: str, text: str) -> None:
        self.app.history.create(
            type_="clone",
            method=method,
            title=text[:60],
            text=text,
            file=self._last_result["file"],
            params={"reference": str(self.profile_path or self.ref_path or ""),
                    "consent": bool(self._consent_var.get())},
        )

    def _save_result(self) -> None:
        if self._last_result is None:
            self.toast("Generate audio first.", "warn")
            return
        src = Path(self._last_result["file"])
        dest = file_service.unique_path(
            file_service.category_dir("clones"),
            file_service.timestamp_stem("my_voice_save"), "wav",
        )
        self.save_btn.set_busy(True, "Saving…")
        try:
            dest.write_bytes(src.read_bytes())
            self.toast(f"Saved: {dest.name}", "ok")
        except Exception as exc:
            self.toast(str(exc), "error")
        finally:
            self.save_btn.set_busy(False)
