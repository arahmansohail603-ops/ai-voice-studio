"""Text-to-Speech screen: text box, language/voice selectors, controls, player."""
from __future__ import annotations

from pathlib import Path

import customtkinter as ctk

from app.config import language_display_name
from app.core import audio_utils
from app.gui import theme
from app.gui.widgets import AudioPlayerBar, BusyButton, MethodBadge, Screen

_SPEED_STEPS = 30  # 0.5 .. 2.0 in 0.05 steps
_PITCH_STEPS = 40  # -20 .. +20 Hz


class TextToSpeechScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._lang_map = {}
        self._voice_map = {}
        self._last_result = None
        self._generating = False

        # ----------------------------------------------------------- text box
        text_panel = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        text_panel.grid(row=0, column=0, sticky="nsew")
        text_panel.grid_rowconfigure(1, weight=1)
        text_panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            text_panel, text="Your text", font=theme.font(14, "bold"),
            text_color=theme.TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 4))

        self.textbox = ctk.CTkTextbox(
            text_panel, height=170, wrap="word", corner_radius=10,
            fg_color=theme.INPUT_BG, text_color=theme.TEXT,
            border_width=1, border_color=theme.BORDER, font=theme.font(14),
        )
        self.textbox.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 12))
        self.textbox.insert(
            "1.0",
            "Welcome to AI Voice Studio. Type or paste anything here and press "
            "Convert to Speech to hear it as natural human-like audio.",
        )

        # ---------------------------------------------------------- controls
        controls = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        controls.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        controls.grid_columnconfigure(0, weight=1)
        controls.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(controls, text="Language", font=theme.font(12, "bold"),
                     text_color=theme.SUBTEXT, anchor="w").grid(row=0, column=0, sticky="w", padx=(16, 8), pady=(14, 2))
        self.lang_menu = ctk.CTkOptionMenu(
            controls, values=["Loading…"], width=190, dynamic_resizing=False,
            command=self._on_language, fg_color=theme.INPUT_BG, button_color=theme.ACCENT,
            button_hover_color=theme.ACCENT_HOVER, font=theme.font(13),
        )
        self.lang_menu.grid(row=1, column=0, sticky="ew", padx=(16, 8), pady=(0, 12))

        ctk.CTkLabel(controls, text="Voice", font=theme.font(12, "bold"),
                     text_color=theme.SUBTEXT, anchor="w").grid(row=0, column=1, sticky="w", padx=8, pady=(14, 2))
        self.voice_menu = ctk.CTkOptionMenu(
            controls, values=["Loading…"], width=300, dynamic_resizing=False,
            fg_color=theme.INPUT_BG, button_color=theme.ACCENT,
            button_hover_color=theme.ACCENT_HOVER, font=theme.font(13),
        )
        self.voice_menu.grid(row=1, column=1, sticky="ew", padx=8, pady=(0, 12))

        self.refresh_btn = ctk.CTkButton(
            controls, text="↻ Voices", width=90,
            command=self._refresh_voices, font=theme.font(12),
            fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
        )
        self.refresh_btn.grid(row=1, column=2, padx=(8, 16), pady=(0, 12))

        # sliders
        self.speed_var = ctk.DoubleVar(value=1.0)
        ctk.CTkLabel(controls, text="Speed", font=theme.font(12, "bold"),
                     text_color=theme.SUBTEXT).grid(row=0, column=3, padx=8, pady=(14, 2))
        speed_row = ctk.CTkFrame(controls, fg_color="transparent")
        speed_row.grid(row=1, column=3, sticky="ew", padx=8, pady=(0, 12))
        ctk.CTkSlider(
            speed_row, from_=0.5, to=2.0, number_of_steps=_SPEED_STEPS,
            variable=self.speed_var, command=self._on_speed, width=150,
        ).pack(side="left", fill="x", expand=True)
        self.speed_lbl = ctk.CTkLabel(speed_row, text="1.00x", width=52,
                                      font=theme.font(12), text_color=theme.TEXT)
        self.speed_lbl.pack(side="left", padx=(8, 0))

        self.pitch_var = ctk.DoubleVar(value=0)
        ctk.CTkLabel(controls, text="Pitch", font=theme.font(12, "bold"),
                     text_color=theme.SUBTEXT).grid(row=0, column=4, padx=8, pady=(14, 2))
        pitch_row = ctk.CTkFrame(controls, fg_color="transparent")
        pitch_row.grid(row=1, column=4, sticky="ew", padx=8, pady=(0, 12))
        ctk.CTkSlider(
            pitch_row, from_=-20, to=20, number_of_steps=_PITCH_STEPS,
            variable=self.pitch_var, command=self._on_pitch, width=150,
        ).pack(side="left", fill="x", expand=True)
        self.pitch_lbl = ctk.CTkLabel(pitch_row, text="0 Hz", width=52,
                                      font=theme.font(12), text_color=theme.TEXT)
        self.pitch_lbl.pack(side="left", padx=(8, 0))

        # ---------------------------------------------------------- action bar
        action = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        action.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        action.grid_columnconfigure(1, weight=1)

        self.convert_btn = BusyButton(
            action, text="Convert to Speech",
            command=self._convert, height=40, corner_radius=10,
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            font=theme.font(14, "bold"), width=210,
        )
        self.convert_btn.grid(row=0, column=0, padx=(16, 12), pady=14)

        self.badge = MethodBadge(action, "edge-tts")
        self.badge.grid(row=0, column=1, sticky="w", pady=14)

        self.status_lbl = ctk.CTkLabel(
            action, text="Ready.", font=theme.font(12), text_color=theme.SUBTEXT, anchor="w",
        )
        self.status_lbl.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 12))

        # ------------------------------------------------------------- player
        player_panel = ctk.CTkFrame(self, fg_color=theme.PANEL_BG, corner_radius=12)
        player_panel.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        player_panel.grid_columnconfigure(0, weight=1)

        if app.player is not None:
            self.player_bar = AudioPlayerBar(player_panel, app.player)
            self.player_bar.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        else:
            self.player_bar = None
            ctk.CTkLabel(
                player_panel, text="Playback unavailable (no audio output device). "
                "Audio files are still saved on disk.",
                font=theme.font(13), text_color=theme.WARNING, anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=16, pady=10)
        save_row = ctk.CTkFrame(player_panel, fg_color="transparent")
        save_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        ctk.CTkLabel(save_row, text="Save as:", font=theme.font(13),
                     text_color=theme.SUBTEXT).pack(side="left")
        self.format_menu = ctk.CTkOptionMenu(
            save_row, values=["mp3", "wav"], width=80, dynamic_resizing=False,
            font=theme.font(13), fg_color=theme.INPUT_BG, button_color=theme.ACCENT,
        )
        self.format_menu.set("mp3")
        self.format_menu.pack(side="left", padx=(8, 12))
        self.save_btn = BusyButton(
            save_row, text="Save Audio", command=self._save_audio,
            width=120, height=32, font=theme.font(13, "bold"),
            fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
        )
        self.save_btn.pack(side="left")

        ctk.CTkLabel(
            save_row, text="MP3 export requires ffmpeg; otherwise WAV is used.",
            font=theme.font(12), text_color=theme.SUBTEXT,
        ).pack(side="left", padx=14)

        if app.player is None:
            self.player_bar = None
            ctk.CTkLabel(
                player_panel, text="Playback unavailable (no audio output device). "
                "Audio files are still saved on disk.",
                font=theme.font(13), text_color=theme.WARNING, anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=16, pady=10)

    # ------------------------------------------------------------ population
    def on_show(self, **kwargs) -> None:
        prefill = kwargs.get("prefill")
        if prefill:
            self.textbox.delete("1.0", "end")
            self.textbox.insert("1.0", prefill)
            self.textbox.focus_set()
        if not self.app.tts.voices_loaded:
            self._refresh_voices()

    def _refresh_voices(self) -> None:
        self.refresh_btn.configure(state="disabled", text="Loading…")
        self.status_lbl.configure(
            text="Fetching neural voices… (requires internet)",
            text_color=theme.WARNING,
        )

        def done():
            self.app.schedule(self._populate)

        self.app.tts.refresh_voices_async(on_done=done)

    def _populate(self) -> None:
        self.refresh_btn.configure(state="normal", text="↻ Voices")
        tts = self.app.tts
        langs = tts.languages()
        if not langs:
            self.status_lbl.configure(
                text="No voices found. Install edge-tts and check your internet.",
                text_color=theme.DANGER,
            )
            self.lang_menu.configure(values=["Unavailable"])
            self.voice_menu.configure(values=["Unavailable"])
            return
        if tts.mode == tts.MODE_PYTTSSX3:
            self.status_lbl.configure(
                text="Offline mode (pyttsx3) — voices limited, pitch not supported.",
                text_color=theme.WARNING,
            )
            self.pitch_lbl.configure(text="n/a")
        else:
            self.status_lbl.configure(text="Neural voices ready.", text_color=theme.SUCCESS)
            self.pitch_lbl.configure(text=f"{int(self.pitch_var.get())} Hz")

        self._lang_map = {
            f"{language_display_name(l)} ({l})": l for l in sorted(langs)
        }
        self.lang_menu.configure(values=list(self._lang_map))

        saved_lang = self.app.settings.get("tts", "language", "en")
        label = self._label_for_lang(saved_lang)
        self.lang_menu.set(label if label else list(self._lang_map)[0])
        self._populate_voices()

    def _label_for_lang(self, code: str) -> str | None:
        for label, l in self._lang_map.items():
            if l == code:
                return label
        return None

    def _on_language(self, label: str) -> None:
        code = self._lang_map.get(label, "en")
        self.app.settings.set("tts", "language", code)
        self._populate_voices()

    def _populate_voices(self) -> None:
        label = self.lang_menu.get()
        code = self._lang_map.get(label, "en")
        voices = self.app.tts.voices(code)
        if not voices:
            self.voice_menu.configure(values=["No voices for this language"])
            return
        self._voice_map = {self._voice_display(v): v["short_name"] for v in voices}
        self.voice_menu.configure(values=list(self._voice_map))

        saved_voice = self.app.settings.get("tts", "voice", "")
        if saved_voice in self._voice_map.values():
            display = next(d for d, s in self._voice_map.items() if s == saved_voice)
        else:
            display = self._default_voice_display(voices)
        self.voice_menu.set(display)

    @staticmethod
    def _voice_display(v: dict) -> str:
        friendly = v.get("friendly", "") or v["short_name"]
        return f"{friendly}  ·  {v['locale']}"

    def _default_voice_display(self, voices: list[dict]) -> str:
        for v in voices:
            if v["gender"].lower() in ("female", "feminine"):
                return self._voice_display(v)
        return self._voice_display(voices[0])

    # -------------------------------------------------------------- actions
    def _on_speed(self, value) -> None:
        self.speed_lbl.configure(text=f"{float(value):.2f}x")

    def _on_pitch(self, value) -> None:
        self.pitch_lbl.configure(text=f"{int(value)} Hz")

    def _convert(self) -> None:
        if self._generating:
            return
        text = self.textbox.get("1.0", "end-1c").strip()
        if not text:
            self.toast("Enter some text first.", "warn")
            return

        voice_display = self.voice_menu.get()
        if voice_display == "Unavailable" or voice_display not in self._voice_map:
            self.toast("No usable voice yet. Check your connection and refresh voices.", "warn")
            self._refresh_voices()
            return

        voice_short = self._voice_map[voice_display]
        speed = float(self.speed_var.get())
        pitch = int(self.pitch_var.get())

        self._generating = True
        self.convert_btn.set_busy(True, "Generating speech…")
        self.status_lbl.configure(text="Generating… this can take a few seconds.",
                                  text_color=theme.SUBTEXT)

        future = self.app.tts.synthesize_async(text, voice_short, speed, pitch)
        self.run_between(
            future,
            on_success=self._on_generated,
            on_error=self._on_generation_error,
        )

    def _on_generated(self, result: dict) -> None:
        self._generating = False
        self.convert_btn.set_busy(False)
        self._last_result = result
        method = result.get("mode", "edge-tts")
        self.badge = self._replace_badge(method)

        text = self.textbox.get("1.0", "end-1c").strip()
        self.app.settings.set("tts", "voice", result.get("voice", ""))
        self.app.history.create(
            type_="tts",
            method=method,
            title=text[:60],
            text=text,
            file=result["file"],
            params={"voice": result.get("voice", ""), "speed": float(self.speed_var.get()),
                    "pitch": int(self.pitch_var.get())},
        )

        if self.player_bar is not None:
            try:
                self.player_bar.set_file(result["file"])
            except Exception as exc:
                self.toast(f"Playback failed: {exc}", "error")

        self.status_lbl.configure(
            text=f"Done — {method} voice. Audio ready to play and save.",
            text_color=theme.SUCCESS,
        )
        self.toast("Speech generated.", "ok")

    def _on_generation_error(self, exc: Exception) -> None:
        self._generating = False
        self.convert_btn.set_busy(False)
        self.status_lbl.configure(text=str(exc), text_color=theme.DANGER)
        self.toast("Speech generation failed.", "error")

    def _replace_badge(self, method: str):
        self.badge.destroy()
        badge = MethodBadge(self.badge.master, method)
        badge.grid(row=0, column=1, sticky="w", pady=14)
        self.badge = badge
        return badge

    # ---------------------------------------------------------------- saving
    def _save_audio(self) -> None:
        if self._last_result is None:
            self.toast("Generate speech first.", "warn")
            return
        from app.services import file_service

        src = Path(self._last_result["file"])
        fmt = self.format_menu.get()
        if not audio_utils.ffmpeg_available() and fmt == "mp3" and src.suffix.lower() != ".mp3":
            self.toast("MP3 needs ffmpeg — saving as WAV.", "warn")
            fmt = "wav"

        dest = file_service.unique_path(
            file_service.category_dir("tts"), file_service.timestamp_stem("tts"), fmt
        )
        self.save_btn.set_busy(True, "Saving…")
        try:
            if src.suffix.lower() == f".{fmt}":
                dest.write_bytes(src.read_bytes())
            else:
                audio_utils.convert_format(src, dest)
            self.toast(f"Saved: {dest.name}", "ok")
            self.status_lbl.configure(text=str(dest), text_color=theme.SUCCESS)
        except Exception as exc:
            self.toast(str(exc), "error")
        finally:
            self.save_btn.set_busy(False)
