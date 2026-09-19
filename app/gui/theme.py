"""Dark theme palette and customtkinter setup."""
from __future__ import annotations

import customtkinter as ctk

APP_BG = "#0e1116"
SIDEBAR_BG = "#151a21"
PANEL_BG = "#1a212b"
CARD_BG = "#212a36"
INPUT_BG = "#161c25"
BORDER = "#2a3542"
TEXT = "#e8edf2"
SUBTEXT = "#9aa7b6"
ACCENT = "#3d8bff"
ACCENT_HOVER = "#2f74dd"
ACCENT_SOFT = "#24344d"
SUCCESS = "#34c784"
WARNING = "#f0a63c"
DANGER = "#ef5b5b"


def apply_theme() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")


def font(size: int = 14, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


def panel_fg() -> str:
    return PANEL_BG


def card_fg() -> str:
    return CARD_BG
