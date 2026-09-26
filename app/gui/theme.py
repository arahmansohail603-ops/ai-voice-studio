from __future__ import annotations

from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import QApplication

APP_BG = "#0a0a0c"
SIDEBAR_BG = "#0f0f12"
PANEL_BG = "#151519"
CARD_BG = "#1c1c23"
INPUT_BG = "#0f0f13"
BORDER = "#26262f"
TEXT = "#f4f5f7"
SUBTEXT = "#9b9ba9"
ACCENT = "#e8a33d"
ACCENT_HOVER = "#cc8c2d"
ACCENT_SOFT = "#2b2416"
ON_ACCENT = "#12100a"
SUCCESS = "#3ecf8e"
WARNING = "#f0a63c"
DANGER = "#ef5757"


def apply_theme(application: QApplication | None = None) -> None:
    app = application or QApplication.instance()
    if app is None:
        return
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(APP_BG))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor(INPUT_BG))
    palette.setColor(QPalette.AlternateBase, QColor(PANEL_BG))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(CARD_BG))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor(ON_ACCENT))
    palette.setColor(QPalette.ToolTipBase, QColor(CARD_BG))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    app.setPalette(palette)


def font(size: int = 14, weight: str = "normal") -> QFont:
    result = QFont("Segoe UI", int(size))
    weights = {
        "normal": QFont.Normal,
        "bold": QFont.Bold,
        "light": QFont.Light,
        "demibold": QFont.DemiBold,
    }
    result.setWeight(weights.get(str(weight).lower(), QFont.Normal))
    return result


def panel_fg() -> str:
    return PANEL_BG


def card_fg() -> str:
    return CARD_BG


def frame_style(
    background: str = PANEL_BG,
    radius: int = 12,
    border: str | None = None,
    border_width: int = 0,
) -> str:
    border_rule = ""
    if border and border_width:
        border_rule = f"border: {border_width}px solid {border};"
    return (
        f"QFrame {{ background-color: {background}; border-radius: {radius}px; "
        f"{border_rule} }}"
    )


def label_style(color: str = TEXT, align: str | None = None) -> str:
    alignment = f"text-align: {align};" if align else ""
    return f"QLabel {{ color: {color}; background: transparent; {alignment} }}"


def button_style(
    background: str = CARD_BG,
    hover: str | None = None,
    color: str = TEXT,
    radius: int = 8,
    border: str | None = None,
    border_width: int = 0,
) -> str:
    hover_color = hover or background
    border_rule = (
        f"border: {border_width}px solid {border};" if border and border_width else ""
    )
    return (
        f"QPushButton {{ background-color: {background}; color: {color}; "
        f"border-radius: {radius}px; border: none; {border_rule} padding: 6px 12px; }}"
        f"QPushButton:hover {{ background-color: {hover_color}; }}"
        f"QPushButton:pressed {{ background-color: {hover_color}; }}"
        f"QPushButton:disabled {{ color: {SUBTEXT}; background-color: {PANEL_BG}; }}"
    )


def input_style(
    background: str = INPUT_BG, border: str = BORDER, color: str = TEXT, radius: int = 8
) -> str:
    return (
        f"QLineEdit, QTextEdit, QPlainTextEdit {{ background-color: {background}; "
        f"color: {color}; border: 1px solid {border}; border-radius: {radius}px; "
        f"padding: 6px; selection-background-color: {ACCENT}; "
        f"selection-color: {ON_ACCENT}; }}"
    )


def combo_style(
    background: str = INPUT_BG, border: str = BORDER, color: str = TEXT, radius: int = 8
) -> str:
    return (
        f"QComboBox {{ background-color: {background}; color: {color}; "
        f"border: 1px solid {border}; border-radius: {radius}px; padding: 6px 10px; }}"
        f"QComboBox:hover {{ border-color: {ACCENT}; }}"
        f"QComboBox::drop-down {{ border: none; width: 24px; }}"
        f"QComboBox QAbstractItemView {{ background-color: {CARD_BG}; color: {color}; "
        f"selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; "
        f"border: 1px solid {border}; }}"
    )


def check_style(color: str = TEXT) -> str:
    return (
        f"QCheckBox {{ color: {color}; spacing: 8px; }}"
        f"QCheckBox::indicator {{ width: 17px; height: 17px; border-radius: 4px; "
        f"border: 1px solid {BORDER}; background: {INPUT_BG}; }}"
        f"QCheckBox::indicator:checked {{ background: {ACCENT}; "
        f"border-color: {ACCENT}; }}"
    )


def progress_style(
    background: str = INPUT_BG, accent: str = ACCENT, radius: int = 5
) -> str:
    return (
        f"QProgressBar {{ background: {background}; border: none; "
        f"border-radius: {radius}px; }}"
        f"QProgressBar::chunk {{ background: {accent}; border-radius: {radius}px; }}"
    )


def scroll_style(background: str = PANEL_BG) -> str:
    return (
        f"QScrollArea {{ background: {background}; border: none; }}"
        f"QScrollBar:vertical {{ background: {background}; width: 10px; margin: 0; }}"
        f"QScrollBar::handle:vertical {{ background: {BORDER}; "
        f"min-height: 24px; border-radius: 5px; }}"
        f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
        f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical "
        f"{{ background: transparent; }}"
    )
