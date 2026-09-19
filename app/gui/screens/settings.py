"""Settings screen: defaults, devices, storage and data management."""
from __future__ import annotations

import os
from pathlib import Path

import customtkinter as ctk

from app.config import language_display_name
from app.core import audio_utils
from app.core.recorder import Recorder, list_input_devices
from app.core.stt_engine import STTEngine
from app.gui import theme
from app.gui.widgets import Screen
from app.services import file_service


class SettingsScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.grid(row=1, column=0, sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)

        self._row = -1
        self._cols: dict = {}
        self._lang_map = {}
        self._voice_map = {}
        self._device_map = {}
        self._fallback_map = {}

        # ------------------------------------------------------- speech
        self._section("Speech defaults")
        r = self._row_frame()
        self.lang_menu = self._menu(r, 170, self._on_default_lang)
        self._field_label(r, "Default language")
        self.voice_menu = self._menu(r, 270, self._on_default_voice)
        self._field_label(r, "Default voice")

        # ---------------------------------------------------------- stt
        self._section("Speech-to-text")
        r = self._row_frame()
        self.stt_engine_menu = self._menu(r, 150, self._on_stt_engine)
        self._field_label(r, "Recognition engine")
        self.stt_lang_menu = self._menu(
            r, 150, self._on_stt_lang,
            ["en-US", "en-GB", "es-ES", "fr-FR", "de-DE", "it-IT", "pt-BR",
             "hi-IN", "ja-JP", "ko-KR", "zh-CN", "ar-SA", "ru-RU", "nl-NL"],
        )
        self._field_label(r, "Recognition language")

        # ------------------------------------------------------- recorder
        self._section("Microphone")
        r = self._row_frame()
        self.device_menu = self._menu(r, 320, self._on_device)
        self._field_label(r, "Input device")
        self.rate_menu = self._menu(
            r, 110, self._on_rate,
            ["8000", "16000", "22050", "44100", "48000"],
        )
        self._field_label(r, "Sample rate (Hz)")

        # ------------------------------------------------------- output
        self._section("Output & storage")
        r = self._row_frame()
        self.folder_var = ctk.StringVar()
        self.folder_entry = ctk.CTkEntry(
            r, textvariable=self.folder_var, width=400, state="readonly",
            fg_color=theme.INPUT_BG, border_color=theme.BORDER, font=theme.font(13),
        )
        self.folder_entry.grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkButton(
            r, text="Browse…", command=self._choose_folder, width=90, height=32,
            font=theme.font(12), fg_color=theme.INPUT_BG,
            border_width=1, border_color=theme.BORDER,
        ).grid(row=0, column=1, sticky="w", padx=(4, 12), pady=6)
        self.ffmpeg_lbl = ctk.CTkLabel(
            r, text="", font=theme.font(11), text_color=theme.SUBTEXT, anchor="w",
        )
        self.ffmpeg_lbl.grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 8))

        # ------------------------------------------------------- my voice
        self._section("My Voice (voice cloning)")
        r = self._row_frame()
        self.fallback_voice_menu = self._menu(r, 320, self._on_fallback_voice)
        self._field_label(r, "Fallback neural voice")

        # --------------------------------------------------------- data
        self._section("Data")
        r = self._row_frame()
        ctk.CTkButton(
            r, text="Open data folder", command=self._open_data_folder, width=150, height=34,
            font=theme.font(13), fg_color=theme.INPUT_BG,
            border_width=1, border_color=theme.BORDER,
        ).grid(row=0, column=0, sticky="w", padx=8, pady=8)
        ctk.CTkButton(
            r, text="Reset settings", command=self._reset_settings, width=130, height=34,
            font=theme.font(13), text_color=theme.WARNING, fg_color=theme.INPUT_BG,
            border_width=1, border_color=theme.BORDER,
        ).grid(row=0, column=1, sticky="w", padx=8, pady=8)
        ctk.CTkLabel(
            r, text="History and settings live in the data folder. Deleting history "
                    "keeps audio files on disk.",
            font=theme.font(11), text_color=theme.SUBTEXT, anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 8))

    # -------------------------------------------------------- layout helpers
    def _section(self, text: str) -> None:
        self._row += 1
        ctk.CTkLabel(
            self.scroll, text=text, font=theme.font(15, "bold"),
            text_color=theme.TEXT, anchor="w",
        ).grid(row=self._row, column=0, sticky="w", pady=(16, 2), padx=8)

    def _row_frame(self):
        self._row += 1
        frame = ctk.CTkFrame(self.scroll, fg_color=theme.PANEL_BG, corner_radius=10)
        frame.grid(row=self._row, column=0, sticky="ew", pady=2)
        frame.grid_columnconfigure(4, weight=1)
        return frame

    def _field_label(self, row, text: str) -> None:
        col = max(0, self._cols.get(id(row), 0) - 1)
        ctk.CTkLabel(
            row, text=text, font=theme.font(11), text_color=theme.SUBTEXT,
        ).grid(row=1, column=col, sticky="w", padx=(8, 2), pady=(0, 6))

    def _menu(self, row, width, command, values=None):
        menu = ctk.CTkOptionMenu(
            row, values=values or ["Loading…"], width=width, dynamic_resizing=False,
            command=command, fg_color=theme.INPUT_BG, button_color=theme.ACCENT,
            button_hover_color=theme.ACCENT_HOVER, font=theme.font(13),
        )
        col = self._cols[id(row)] = self._cols.get(id(row), 0)
        menu.grid(row=0, column=col, sticky="w", padx=8, pady=6)
        self._cols[id(row)] = col + 1
        return menu

    # ------------------------------------------------------------ lifecycle
    def on_show(self, **kwargs) -> None:
        self._cols = {}
        self._populate_all()

    def _populate_all(self) -> None:
        s = self.app.settings
        langs = self.app.tts.languages()
        self._lang_map = {
            f"{language_display_name(l)} ({l})": l for l in sorted(langs)
        } if langs else {}
        if self._lang_map:
            self.lang_menu.configure(values=list(self._lang_map))
            saved_lang = s.get("tts", "language", "en")
            sel = next((k for k, v in self._lang_map.items() if v == saved_lang), None)
            self.lang_menu.set(sel if sel else next(iter(self._lang_map)))
            self._populate_default_voices(saved_lang)
        else:
            self.lang_menu.configure(values=["No languages"])
            self.voice_menu.configure(values=["No voices"])

        self._populate_fallback_voices()
        self.stt_engine_menu.configure(values=STTEngine.available_engines())
        self.stt_engine_menu.set(s.get("stt", "engine", "google"))
        self.stt_lang_menu.set(s.get("stt", "language", "en-US"))
        self._populate_devices()
        self.rate_menu.set(str(s.get("recorder", "samplerate", 44100)))
        self.folder_var.set(s.get("output", "folder", str(file_service.category_dir("tts").parent)))

        ff = audio_utils.ffmpeg_available()
        self.ffmpeg_lbl.configure(
            text="ffmpeg: " + ("Available — MP3 export enabled." if ff else
                               "Not found — MP3 saving is disabled (WAV works fine)."),
            text_color=theme.SUCCESS if ff else theme.WARNING,
        )

    def _populate_default_voices(self, lang: str) -> None:
        voices = self.app.tts.voices(lang)
        if not voices:
            self.voice_menu.configure(values=["No voices"])
            return
        self._voice_map = {
            f"{v.get('friendly', v['short_name'])[:50]} ({v['locale']})": v["short_name"]
            for v in voices
        }
        self.voice_menu.configure(values=list(self._voice_map))
        saved = self.app.settings.get("tts", "voice", "")
        for display, short in self._voice_map.items():
            if short == saved:
                self.voice_menu.set(display)
                return
        if self._voice_map:
            self.voice_menu.set(next(iter(self._voice_map)))

    def _populate_fallback_voices(self) -> None:
        voices = self.app.tts.voices()
        if not voices:
            return
        self._fallback_map = {
            f"{v.get('friendly', v['short_name'])[:50]} ({v['locale']})": v["short_name"]
            for v in voices
        }
        values = list(self._fallback_map)
        self.fallback_voice_menu.configure(values=values)
        saved = self.app.settings.get("clone", "fallback_voice", "")
        for display, short in self._fallback_map.items():
            if short == saved:
                self.fallback_voice_menu.set(display)
                return
        if values:
            self.fallback_voice_menu.set(values[0])

    def _populate_devices(self) -> None:
        try:
            devices = list_input_devices()
        except Exception:
            devices = []
        self._device_map = {d["name"]: d["index"] for d in devices}
        values = list(self._device_map) or ["No input devices found"]
        self.device_menu.configure(values=values)
        saved = self.app.settings.get("recorder", "device")
        def_disp = next((k for k, i in self._device_map.items() if i == saved), None)
        if def_disp:
            self.device_menu.set(def_disp)
        elif values:
            self.device_menu.set(values[0])

    # ------------------------------------------------------------- handlers
    def _on_default_lang(self, value: str) -> None:
        code = self._lang_map.get(value, "en")
        self.app.settings.set("tts", "language", code)
        self._populate_default_voices(code)

    def _on_default_voice(self, value: str) -> None:
        short = self._voice_map.get(value, "")
        if short:
            self.app.settings.set("tts", "voice", short)

    def _on_stt_engine(self, value: str) -> None:
        self.app.settings.set("stt", "engine", value)
        self.app.stt.engine = value

    def _on_stt_lang(self, value: str) -> None:
        self.app.settings.set("stt", "language", value)
        self.app.stt.language = value

    def _on_device(self, value: str) -> None:
        idx = self._device_map.get(value)
        self.app.settings.set("recorder", "device", idx)
        self._recreate_recorder()

    def _on_rate(self, value: str) -> None:
        self.app.settings.set("recorder", "samplerate", int(value))
        self._recreate_recorder()

    def _recreate_recorder(self) -> None:
        self.app.recorder = Recorder(
            samplerate=int(self.app.settings.get("recorder", "samplerate", 44100)),
            channels=1,
            device=self.app.settings.get("recorder", "device"),
        )

    def _on_fallback_voice(self, value: str) -> None:
        short = self._fallback_map.get(value, "")
        self.app.settings.set("clone", "fallback_voice", short or value)

    def _choose_folder(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="Choose output folder")
        if not chosen:
            return
        resolved = Path(chosen).resolve()
        file_service.set_output_root(resolved)
        self.app.settings.set("output", "folder", str(resolved))
        self.folder_var.set(str(resolved))
        self.toast("Output folder updated.", "ok")

    def _open_data_folder(self) -> None:
        try:
            os.startfile(str(file_service.category_dir("tts")))  # type: ignore[attr-defined]
        except Exception:
            self.toast("Could not open folder.", "error")

    def _reset_settings(self) -> None:
        from tkinter import messagebox

        if not messagebox.askyesno("Reset settings", "Restore all default settings?"):
            return
        self.app.settings.reset()
        file_service.set_output_root(self.app.settings.get("output", "folder", ""))
        self._populate_all()
        self.toast("Settings reset.", "ok")
