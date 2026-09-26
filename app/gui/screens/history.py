from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

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
        self._filter_key = "all"
        self._row_counter = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        bar = QFrame(self)
        bar.setStyleSheet("QFrame { background: transparent; border: none; }")
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        controls = QWidget(bar)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        self.filter = QComboBox(controls)
        self.filter.addItems([value for _, value in TYPE_LABELS.items()])
        self.filter.setFont(theme.font(12))
        self.filter.setFixedWidth(170)
        self.filter.setStyleSheet(theme.combo_style())
        self.filter.currentTextChanged.connect(self._on_filter)
        controls_layout.addWidget(self.filter)
        self.playing_lbl = QLabel("", controls)
        self.playing_lbl.setFont(theme.font(12))
        self.playing_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        controls_layout.addWidget(self.playing_lbl, 1)
        self.stop_all_btn = QPushButton("Stop", controls)
        self.stop_all_btn.setFixedSize(70, 28)
        self.stop_all_btn.setFont(theme.font(12))
        self.stop_all_btn.setStyleSheet(
            theme.button_style(
                theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 8, theme.BORDER, 1
            )
        )
        self.stop_all_btn.clicked.connect(self._stop_all)
        controls_layout.addWidget(self.stop_all_btn)
        self.clear_btn = QPushButton("Clear all", controls)
        self.clear_btn.setFixedSize(90, 28)
        self.clear_btn.setFont(theme.font(12))
        self.clear_btn.setStyleSheet(
            theme.button_style(
                theme.INPUT_BG, theme.CARD_BG, theme.DANGER, 8, theme.BORDER, 1
            )
        )
        self.clear_btn.clicked.connect(self._clear_all)
        controls_layout.addWidget(self.clear_btn)
        bar_layout.addWidget(controls)
        root.addWidget(bar)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(theme.scroll_style(theme.PANEL_BG))
        self.list_frame = QWidget()
        self.list_frame.setStyleSheet(theme.frame_style(theme.PANEL_BG, 12))
        self.list_layout = QVBoxLayout(self.list_frame)
        self.list_layout.setContentsMargins(8, 8, 8, 8)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_frame)
        root.addWidget(self.scroll, 1)

        self.empty_lbl = QLabel("", self)
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setFont(theme.font(14))
        self.empty_lbl.setWordWrap(True)
        self.empty_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "center"))
        root.addWidget(self.empty_lbl)

    def on_show(self, **kwargs) -> None:
        self.rebuild(self._filter_key)

    def on_hide(self) -> None:
        if self.app.player is not None:
            self.app.player.stop()

    def _on_filter(self, value: str) -> None:
        key = {
            "All": "all",
            "Text to Speech": "tts",
            "Voice Notes": "note",
            "Transcripts": "transcript",
            "Voice Clone": "clone",
        }.get(value, "all")
        self.rebuild(key)

    def rebuild(self, entry_type: str = "all") -> None:
        self._filter_key = entry_type
        self._row_counter = 0
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        entries = self.app.history.filter(entry_type)
        self.empty_lbl.setText("")
        if not entries:
            self.empty_lbl.setText(
                "Nothing here yet. Generate speech, record a note or transcribe later."
            )
            self.list_layout.addStretch(1)
            return
        for entry in entries:
            self._add_card(entry)

    def _add_card(self, entry: HistoryEntry) -> None:
        card = QFrame(self.list_frame)
        card.setStyleSheet(theme.frame_style(theme.CARD_BG, 10))
        card_layout = QVBoxLayout(card)
        self.list_layout.addWidget(card)
        card_layout.setContentsMargins(12, 8, 12, 8)
        card_layout.setSpacing(2)
        badge = MethodBadge(card, entry.method)
        top = QWidget(card)
        top.setStyleSheet("QWidget { background: transparent; border: none; }")
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(2)
        title = QLabel((entry.title or entry.type)[:70], top)
        title.setFont(theme.font(14, "bold"))
        title.setWordWrap(True)
        title.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        top_layout.addWidget(badge, 0, Qt.AlignLeft)
        top_layout.addWidget(title)
        timestamp = entry.ts.replace("T", "  ")
        exists = self._file_exists(entry.file)
        extra = f"  ·  {entry.duration:.1f}s" if entry.duration else ""
        file_line = (Path(entry.file).name if entry.file else "") or "in-app only"
        state = "" if exists else "  ·  file missing"
        metadata = QLabel(f"{timestamp}  ·  {file_line}{extra}{state}", top)
        metadata.setFont(theme.font(12))
        metadata.setStyleSheet(
            theme.label_style(theme.SUBTEXT if exists else theme.DANGER, "left")
        )
        top_layout.addWidget(metadata)
        card_layout.addWidget(top)

        actions = QWidget(card)
        actions.setStyleSheet("QWidget { background: transparent; border: none; }")
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        if exists and entry.file:
            play_btn = QPushButton("Play", actions)
            play_btn.setFixedSize(70, 28)
            play_btn.setFont(theme.font(12))
            play_btn.setStyleSheet(
                theme.button_style(
                    theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 8, theme.BORDER, 1
                )
            )
            play_btn.clicked.connect(
                lambda _checked=False, item=entry: self._play(item)
            )
        else:
            play_btn = QPushButton("Play", actions)
            play_btn.setFixedSize(70, 28)
            play_btn.setEnabled(False)
            play_btn.setFont(theme.font(12))
            play_btn.setStyleSheet(
                theme.button_style(
                    theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 8, theme.BORDER, 1
                )
            )
        actions_layout.addWidget(play_btn, 0, Qt.AlignRight)
        delete_btn = QPushButton("Delete", actions)
        delete_btn.setFixedSize(70, 28)
        delete_btn.setFont(theme.font(12))
        delete_btn.setStyleSheet(
            theme.button_style(
                theme.INPUT_BG, theme.CARD_BG, theme.DANGER, 8, theme.BORDER, 1
            )
        )
        delete_btn.clicked.connect(
            lambda _checked=False, item=entry: self._delete(item)
        )
        actions_layout.addWidget(delete_btn, 0, Qt.AlignRight)
        card_layout.addWidget(actions)

    @staticmethod
    def _file_exists(file_ref: str) -> bool:
        if not file_ref:
            return True
        try:
            return file_service.absolutize(file_ref).exists()
        except Exception:
            return False

    def _play(self, entry: HistoryEntry) -> None:
        if self.app.player is None:
            self.toast("Playback unavailable (no audio output device).", "warn")
            return
        path = file_service.absolutize(entry.file)
        try:
            self.app.player.load(path)
            self.app.player.play()
            self.playing_lbl.setText(f"Playing: {path.name}   [stop on another screen]")
            self.playing_lbl.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        except Exception as exc:
            self.toast(str(exc), "error")

    def _stop_all(self) -> None:
        if self.app.player is not None:
            self.app.player.stop()
        self.playing_lbl.setText("")
        self.playing_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))

    def _delete(self, entry: HistoryEntry) -> None:
        self.app.history.delete(entry.id)
        self.rebuild(self._filter_key)
        self.toast("Entry removed from history.", "info")

    def _clear_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear history",
            "Remove all history entries? (Files on disk are kept.)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.app.history.clear()
        self.rebuild(self._filter_key)
        self.toast("History cleared.", "ok")
