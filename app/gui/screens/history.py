"""History screen: filterable list of generated speeches and voice notes."""
from __future__ import annotations

from pathlib import Path

import customtkinter as ctk

from app.gui import theme
from app.gui.widgets import MethodBadge, Screen
from app.services import file_service
from app.services.history_service import HistoryEntry

TYPE_LABELS = {
    "all": "All",
    "tts": "Text to Speech",
    "note": "Voice Notes",
    "transcript": "Transcripts",
    "clone": "Voice Clone",
}


class HistoryScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ---------------------------------------------------------- toolbar
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        bar.grid_columnconfigure(2, weight=1)

        self.filter = ctk.CTkSegmentedButton(
            bar,
            values=[v for _, v in TYPE_LABELS.items()],
            command=self._on_filter,
            font=theme.font(12),
            selected_color=theme.ACCENT, selected_hover_color=theme.ACCENT_HOVER,
            fg_color=theme.PANEL_BG,
        )
        self.filter.set("All")
        self.filter.grid(row=0, column=0, sticky="w")

        self.playing_lbl = ctk.CTkLabel(
            bar, text="", font=theme.font(12), text_color=theme.SUBTEXT, anchor="w",
        )
        self.playing_lbl.grid(row=0, column=1, sticky="w", padx=(12, 0))

        self.stop_all_btn = ctk.CTkButton(
            bar, text="Stop", command=self._stop_all, width=70, height=28,
            font=theme.font(12), fg_color=theme.INPUT_BG,
            border_width=1, border_color=theme.BORDER,
        )
        self.stop_all_btn.grid(row=0, column=2, sticky="e")

        self.clear_btn = ctk.CTkButton(
            bar, text="Clear all", command=self._clear_all, width=90, height=28,
            font=theme.font(12), fg_color=theme.INPUT_BG, text_color=theme.DANGER,
            border_width=1, border_color=theme.BORDER,
        )
        self.clear_btn.grid(row=0, column=3, sticky="e", padx=(8, 0))

        # ------------------------------------------------------------ list
        self.list_frame = ctk.CTkScrollableFrame(
            self, fg_color=theme.PANEL_BG, corner_radius=12, label_text=""
        )
        self.list_frame.grid(row=2, column=0, sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)

        self.empty_lbl = ctk.CTkLabel(
            self, text="", font=theme.font(14), text_color=theme.SUBTEXT,
        )
        self.empty_lbl.grid(row=3, column=0, pady=20)

        self._filter_key = "all"

    # ------------------------------------------------------------ lifecycle
    def on_show(self, **kwargs) -> None:
        self.rebuild(self._filter_key)

    def on_hide(self) -> None:
        if self.app.player is not None:
            self.app.player.stop()

    # ------------------------------------------------------------ filtering
    def _on_filter(self, value: str) -> None:
        key = {"All": "all", "Text to Speech": "tts", "Voice Notes": "note",
               "Transcripts": "transcript", "Voice Clone": "clone"}.get(value, "all")
        self.rebuild(key)

    def rebuild(self, entry_type: str = "all") -> None:
        self._filter_key = entry_type
        self._row_counter = 0
        for child in self.list_frame.winfo_children():
            child.destroy()

        entries = self.app.history.filter(entry_type)
        self.empty_lbl.configure(text="")
        if not entries:
            self.empty_lbl.configure(
                text="Nothing here yet. Generate speech, record a note or transcribe later."
            )
            return
        for entry in entries:
            self._add_card(entry)

    # --------------------------------------------------------------- cards
    def _add_card(self, entry: HistoryEntry) -> None:
        card = ctk.CTkFrame(self.list_frame, fg_color=theme.CARD_BG, corner_radius=10)
        card.grid(row=self._row_counter, column=0, sticky="ew", padx=8, pady=4)
        self._row_counter += 1
        card.grid_columnconfigure(1, weight=1)

        MethodBadge(card, entry.method).grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=10)

        title = entry.title or entry.type
        ctk.CTkLabel(
            card, text=title[:70], font=theme.font(14, "bold"),
            text_color=theme.TEXT, anchor="w",
        ).grid(row=0, column=1, sticky="w", pady=(8, 0))

        timestamp = entry.ts.replace("T", "  ")
        exists = self._file_exists(entry.file)
        extra = f"  ·  {entry.duration:.1f}s" if entry.duration else ""
        file_line = (Path(entry.file).name if entry.file else "") or "in-app only"
        state = "" if exists else "  ·  file missing"
        ctk.CTkLabel(
            card, text=f"{timestamp}  ·  {file_line}{extra}{state}",
            font=theme.font(12), text_color=theme.DANGER if not exists else theme.SUBTEXT, anchor="w",
        ).grid(row=1, column=1, sticky="w", pady=(0, 8))

        if exists and entry.file:
            play_btn = ctk.CTkButton(
                card, text="Play", width=70, height=28, font=theme.font(12),
                command=lambda e=entry: self._play(e),
                fg_color=theme.INPUT_BG, border_width=1, border_color=theme.BORDER,
            )
        else:
            play_btn = ctk.CTkButton(
                card, text="Play", width=70, height=28, font=theme.font(12), state="disabled",
                fg_color=theme.INPUT_BG,
            )
        play_btn.grid(row=0, column=2, rowspan=2, padx=(8, 6), pady=10)

        del_btn = ctk.CTkButton(
            card, text="Delete", width=70, height=28, font=theme.font(12),
            text_color=theme.DANGER, fg_color=theme.INPUT_BG,
            border_width=1, border_color=theme.BORDER,
            command=lambda e=entry: self._delete(e),
        )
        del_btn.grid(row=0, column=3, rowspan=2, padx=(0, 12), pady=10)

    @staticmethod
    def _file_exists(file_ref: str) -> bool:
        if not file_ref:
            return True
        try:
            return file_service.absolutize(file_ref).exists()
        except Exception:
            return False

    # ------------------------------------------------------------- actions
    def _play(self, entry: HistoryEntry) -> None:
        if self.app.player is None:
            self.toast("Playback unavailable (no audio output device).", "warn")
            return
        path = file_service.absolutize(entry.file)
        try:
            self.app.player.load(path)
            self.app.player.play()
            self.playing_lbl.configure(
                text=f"Playing: {path.name}   [stop on another screen]",
                text_color=theme.SUCCESS,
            )
        except Exception as exc:
            self.toast(str(exc), "error")

    def _stop_all(self) -> None:
        if self.app.player is not None:
            self.app.player.stop()
        self.playing_lbl.configure(text="", text_color=theme.SUBTEXT)

    def _delete(self, entry: HistoryEntry) -> None:
        self.app.history.delete(entry.id)
        self.rebuild(self._filter_key)
        self.toast("Entry removed from history.", "info")

    def _clear_all(self) -> None:
        from tkinter import messagebox

        if not messagebox.askyesno(
            "Clear history", "Remove all history entries? (Files on disk are kept.)"
        ):
            return
        self.app.history.clear()
        self.rebuild(self._filter_key)
        self.toast("History cleared.", "ok")
