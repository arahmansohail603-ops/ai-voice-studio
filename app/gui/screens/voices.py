"""The Voices screen: download the offline neural voices Piper can speak with.

Translation and speaking have always been separate limits. Argos can translate
into 50 languages, but until a voice model is installed the app can only *speak*
English -- which is why "translate to Urdu, then convert" failed with "No usable
voice yet" even though the translation itself had succeeded.

This screen closes that gap. It reads the published Piper voice catalog, shows
every language with its voices, and installs one on request. Languages are
badged so it is obvious which ones can both be translated and spoken versus
spoken only. Each download runs on a worker :class:`QThread` that only emits
signals, so a 60 MB transfer cannot freeze the interface, and the bytes are
verified against the catalog's MD5 before being published into the models
folder.

The catalog is cached on first use, so this screen keeps working offline once
it has been loaded, and re-downloaded voices are refused rather than silently
fetched twice.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.errors import AppError
from app.core.model_manager import human_size
from app.core.piper_voices import (
    STAGE_DOWNLOADING,
    STAGE_INSTALLED,
    STAGE_VERIFYING,
    PiperVoiceLibrary,
    VoiceEntry,
)
from app.gui import theme
from app.gui.widgets import Screen


class _VoiceWorker(QThread):
    """Downloads one voice on a worker thread and reports progress as signals."""

    progressed = pyqtSignal(str, int, int, str, str)  # key, received, total, stage, detail
    succeeded = pyqtSignal(str, str)  # key, installed stem
    failed = pyqtSignal(str, str)  # key, message

    def __init__(self, library: PiperVoiceLibrary, entry: VoiceEntry) -> None:
        super().__init__(parent=None)
        self._library = library
        self._entry = entry

    def run(self) -> None:  # pragma: no cover - exercised via the screen
        try:
            path = self._library.install(self._entry, self._on_progress)
            self.succeeded.emit(self._entry.key, path.stem)
        except AppError as exc:
            self.failed.emit(self._entry.key, str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.failed.emit(self._entry.key, f"Unexpected error: {exc}")

    def _on_progress(self, stage: str, received: int, total: int, detail: str) -> None:
        self.progressed.emit(self._entry.key, received, total, stage, detail or stage)


class _CatalogWorker(QThread):
    """Fetches the voice catalog off the GUI thread.

    A cold fetch is a network round trip, and a dead network can hang for the
    full timeout, so it must not run on the GUI thread.
    """

    loaded = pyqtSignal(int, int)  # voice count, language count
    failed = pyqtSignal(str)

    def __init__(self, library: PiperVoiceLibrary, refresh: bool) -> None:
        super().__init__(parent=None)
        self._library = library
        self._refresh = refresh

    def run(self) -> None:  # pragma: no cover - exercised via the screen
        try:
            entries = self._library.catalog(refresh=self._refresh)
        except AppError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.loaded.emit(len(entries), len({e.language_family for e in entries}))


class _VoiceRow(QFrame):
    """One voice: name, quality, size, state and its action button."""

    download_clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)

    def __init__(self, master, entry: VoiceEntry, installed: bool) -> None:
        super().__init__(master)
        self.entry = entry
        self.installed = installed
        self.setStyleSheet(theme.frame_style(theme.PANEL_BG, 10))

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(10)

        text = QVBoxLayout()
        text.setSpacing(2)

        name = QLabel(entry.name.replace("_", " ").title(), self)
        name.setFont(theme.font(13, "bold"))
        name.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        text.addWidget(name)

        detail = QLabel(
            f"{entry.quality_label()}  ·  {human_size(entry.model_bytes())}"
            + (f"  ·  {entry.speakers} speakers" if entry.speakers > 1 else ""),
            self,
        )
        detail.setFont(theme.font(11))
        detail.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        text.addWidget(detail)
        root.addLayout(text, 1)

        self.state_lbl = QLabel("Installed" if installed else "", self)
        self.state_lbl.setFont(theme.font(11))
        self.state_lbl.setStyleSheet(
            theme.label_style(theme.SUCCESS if installed else theme.SUBTEXT, "right")
        )
        root.addWidget(self.state_lbl)

        self.action_btn = QPushButton("Delete" if installed else "Download", self)
        self.action_btn.setFixedSize(96, 30)
        self.action_btn.setFont(theme.font(12))
        self.action_btn.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 6, theme.BORDER, 1)
        )
        # clicked emits a checked bool, so it cannot be wired straight to a
        # str-carrying signal; the key is emitted explicitly instead.
        if installed:
            self.action_btn.clicked.connect(
                lambda _checked=False: self.delete_clicked.emit(entry.key)
            )
        else:
            self.action_btn.clicked.connect(
                lambda _checked=False: self.download_clicked.emit(entry.key)
            )
        root.addWidget(self.action_btn)

        self.bar = QProgressBar(self)
        self.bar.setFixedHeight(4)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 0)  # indeterminate until the first byte arrives
        self.bar.setVisible(False)
        root.addWidget(self.bar, 1)

    def set_progress(self, received: int, total: int) -> None:
        self.bar.setVisible(True)
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(received)

    def set_busy(self, busy: bool, stage: str = "") -> None:
        """Lock the row while a download owns it."""
        self.action_btn.setEnabled(not busy)
        if not busy:
            self.bar.setVisible(False)
            self.bar.setRange(0, 0)
        elif stage == STAGE_DOWNLOADING:
            self.bar.setVisible(True)
        else:
            # Verifying / installing: a determinate bar would sit at 100%.
            self.bar.setVisible(True)
            self.bar.setRange(0, 0)


class _LanguageGroup(QFrame):
    """One language, its badge, and the voices it offers."""

    download_clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)

    def __init__(self, master, row: dict, library: PiperVoiceLibrary, parent_screen) -> None:
        super().__init__(master)
        self.row = row
        self.library = library
        self.parent_screen = parent_screen
        self.voice_rows: dict[str, _VoiceRow] = {}
        self.setStyleSheet(theme.frame_style(theme.CARD_BG, 10))

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)

        title = QLabel(row["label"], self)
        title.setFont(theme.font(14, "bold"))
        title.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        top.addWidget(title)

        if row["native"] and row["native"] != row["label"]:
            native = QLabel(row["native"], self)
            native.setFont(theme.font(13))
            native.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
            top.addWidget(native)

        top.addStretch(1)

        count = QLabel(
            f"{row['installed']}/{row['count']} installed" if row["count"] > 1 else "",
            self,
        )
        count.setFont(theme.font(11))
        count.setStyleSheet(theme.label_style(theme.SUBTEXT, "right"))
        top.addWidget(count)
        root.addLayout(top)

        badge = QLabel(self)
        badge.setFont(theme.font(11))
        if row["translatable"]:
            badge.setText("Can be translated into, then spoken")
            badge.setStyleSheet(theme.label_style(theme.SUCCESS, "left"))
        else:
            badge.setText("Speak only — the app cannot translate into this language")
            badge.setStyleSheet(theme.label_style(theme.WARNING, "left"))
        root.addWidget(badge)

        for entry in row["voices"]:
            voice_row = _VoiceRow(self, entry, library.is_installed(entry))
            voice_row.download_clicked.connect(self.download_clicked)
            voice_row.delete_clicked.connect(self.delete_clicked)
            self.voice_rows[entry.key] = voice_row
            root.addWidget(voice_row)

    def set_busy(self, busy: bool, stage: str = "") -> None:
        for voice_row in self.voice_rows.values():
            voice_row.set_busy(busy, stage)


class VoicesScreen(Screen):
    """Browse and install the offline Piper voices."""

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self._library = PiperVoiceLibrary()
        self._entries: list[VoiceEntry] = []
        self._groups: dict[str, _LanguageGroup] = {}
        self._workers: dict[str, _VoiceWorker] = {}
        self._catalog_worker: _CatalogWorker | None = None
        self._filter = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QLabel("Voices", self)
        header.setFont(theme.font(20, "bold"))
        header.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        root.addWidget(header)

        blurb = QLabel(
            "Download the offline voices this app can speak with. Pick a language, "
            "install a voice once, and it works offline forever. Large downloads are "
            "checked against the published catalog before they are installed.",
            self,
        )
        blurb.setFont(theme.font(12))
        blurb.setWordWrap(True)
        blurb.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        root.addWidget(blurb)
        root.addSpacing(10)

        self.summary = QLabel("", self)
        self.summary.setFont(theme.font(12))
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        root.addWidget(self.summary)
        root.addSpacing(6)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Filter languages, e.g. Urdu or hi")
        self.search.setFont(theme.font(12))
        self.search.setFixedHeight(32)
        self.search.textChanged.connect(self._on_filter)
        controls.addWidget(self.search, 1)

        self.translatable_only = QCheckBox("Only languages I can translate into", self)
        self.translatable_only.setFont(theme.font(12))
        self.translatable_only.stateChanged.connect(self._on_filter)
        controls.addWidget(self.translatable_only)

        self.refresh_btn = QPushButton("Refresh list", self)
        self.refresh_btn.setFixedSize(120, 32)
        self.refresh_btn.setFont(theme.font(12))
        self.refresh_btn.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 6, theme.BORDER, 1)
        )
        self.refresh_btn.clicked.connect(lambda: self._load_catalog(refresh=True))
        controls.addWidget(self.refresh_btn)
        root.addLayout(controls)
        root.addSpacing(10)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        self._body = QVBoxLayout(holder)
        self._body.setContentsMargins(0, 0, 6, 0)
        self._body.setSpacing(8)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        self.empty = QLabel(
            "No voices to show yet.", holder
        )
        self.empty.setFont(theme.font(12))
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        self._body.addWidget(self.empty)
        self._body.addStretch(1)

    # ------------------------------------------------------------------ data
    @property
    def library(self) -> PiperVoiceLibrary:
        return self._library

    def on_show(self, **kwargs) -> None:
        # "Get a voice" from the Text to Speech screen arrives here, focused on
        # the language the user just translated into.
        wanted = kwargs.get("language")
        if wanted:
            from app.config import language_display_name

            label = language_display_name(wanted)
            self.search.blockSignals(True)
            self.search.setText(label)
            self.search.blockSignals(False)
            self._filter = label
        if not self._entries:
            self._load_catalog(refresh=False)
        else:
            self._rebuild()

    # --------------------------------------------------------------- loading
    def _load_catalog(self, refresh: bool) -> None:
        if self._catalog_worker is not None and self._catalog_worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        if refresh:
            self._show_message("Fetching the latest voice list…", theme.SUBTEXT)
        elif not self._library.index_path.is_file():
            self._show_message("Fetching the voice list for the first time…", theme.SUBTEXT)
        else:
            self._show_message("", theme.SUBTEXT)

        self._catalog_worker = _CatalogWorker(self._library, refresh)
        self._catalog_worker.loaded.connect(self._on_catalog_loaded)
        self._catalog_worker.failed.connect(self._on_catalog_failed)
        self._catalog_worker.finished.connect(self._on_catalog_finished)
        self._catalog_worker.start()

    def _on_catalog_loaded(self, voices: int, languages: int) -> None:
        self._entries = self._library.catalog_from_cache()
        self._rebuild()
        installed = len(self._library.installed())
        self._show_message(
            f"{voices} voices in {languages} languages  ·  "
            f"{installed} installed  ·  {human_size(self._library.disk_usage())} on disk",
            theme.SUBTEXT,
        )

    def _on_catalog_failed(self, message: str) -> None:
        # A cached catalog still works offline, so fall back rather than dead-end.
        cached = self._library.catalog_from_cache()
        if cached:
            self._entries = cached
            self._rebuild()
            self._show_message(
                f"Working offline from the saved voice list. ({message})",
                theme.WARNING,
            )
        else:
            self._show_message(message, theme.DANGER)

    def _on_catalog_finished(self) -> None:
        self.refresh_btn.setEnabled(True)
        self._catalog_worker = None

    # -------------------------------------------------------------- rendering
    def _clear_rows(self) -> None:
        for group in self._groups.values():
            group.setParent(None)
            group.deleteLater()
        self._groups.clear()

    def _matches(self, row: dict) -> bool:
        if self.translatable_only.isChecked() and not row["translatable"]:
            return False
        if not self._filter:
            return True
        needle = self._filter.strip().lower()
        haystack = " ".join(
            [row["label"], row["native"], row["family"], row["code"]]
        ).lower()
        return needle in haystack

    def _rebuild(self) -> None:
        if any(worker.isRunning() for worker in self._workers.values()):
            # Do not tear down rows out from under a running download.
            return

        while self._body.count() > 0:
            item = self._body.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.empty:
                widget.setParent(None)
                widget.deleteLater()

        self._clear_rows()

        rows = [r for r in self._library.languages(self._entries) if self._matches(r)]
        if not rows:
            self.empty.setText(self._empty_message(bool(self._entries)))
            self.empty.setVisible(True)
            self._body.addWidget(self.empty)
            self._body.addStretch(1)
            return

        self.empty.setVisible(False)
        for row in rows:
            group = _LanguageGroup(self, row, self._library, self)
            group.download_clicked.connect(self._on_download)
            group.delete_clicked.connect(self._on_delete)
            self._groups[row["family"]] = group
            self._body.addWidget(group)
        self._body.addStretch(1)

    def _on_filter(self, *_args) -> None:
        # The box is the source of truth for the filter; a language passed to
        # on_show writes into the box, so both routes stay in step.
        self._filter = self.search.text()
        self._rebuild()

    def _empty_message(self, have_entries: bool) -> str:
        """Explain *why* nothing matched.

        Arriving here from "Get a <language> voice" for a language Piper never
        published a voice for is a dead end by definition, so say that plainly
        instead of implying the filter simply found nothing.
        """
        if not have_entries:
            return "No voices to show yet."
        needle = self._filter.strip().lower()
        if needle and not self.translatable_only.isChecked():
            from app.config import language_display_name
            from app.core.translator import SUPPORTED_CODES

            for code in SUPPORTED_CODES:
                label = language_display_name(code).lower()
                if needle in (label, code, f"{label} ({code})"):
                    return (
                        f"No offline voice is published for "
                        f"{language_display_name(code)}. The app can translate "
                        f"into it, but no voice exists to read it aloud."
                    )
        if self.translatable_only.isChecked():
            return (
                "No languages match. Languages with no published voice are "
                "hidden by this filter."
            )
        return "No languages match that filter."

    def _show_message(self, text: str, color: str) -> None:
        self.summary.setText(text)
        self.summary.setStyleSheet(theme.label_style(color, "left"))

    def _group_for(self, key: str) -> _LanguageGroup | None:
        entry = next((e for e in self._entries if e.key == key), None)
        if entry is None:
            return None
        return self._groups.get(entry.language_family)

    def _row_for(self, key: str) -> _VoiceRow | None:
        group = self._group_for(key)
        return group.voice_rows.get(key) if group else None

    # ---------------------------------------------------------------- actions
    def _on_download(self, key: str) -> None:
        entry = next((e for e in self._entries if e.key == key), None)
        if entry is None:
            return
        if self._library.is_installed(entry):
            return
        row = self._row_for(key)
        if row is not None:
            row.set_progress(0, entry.total_bytes() or 1)
            row.set_busy(True, STAGE_DOWNLOADING)
        self._show_message(
            f"Downloading {entry.name} ({human_size(entry.total_bytes())})…",
            theme.SUBTEXT,
        )
        worker = _VoiceWorker(self._library, entry)
        worker.progressed.connect(self._on_progress)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda k=key: self._on_worker_done(k))
        self._workers[key] = worker
        worker.start()

    def _on_delete(self, key: str) -> None:
        entry = next((e for e in self._entries if e.key == key), None)
        if entry is None or not self._library.is_installed(entry):
            return
        confirm = QMessageBox.question(
            self,
            "Delete voice",
            f"Delete the {entry.name} voice? It is about "
            f"{human_size(entry.model_bytes())} and you can download it again later.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._library.remove(entry)
        except AppError as exc:
            self._show_message(str(exc), theme.DANGER)
            return
        self._show_message(f"Deleted {entry.name}.", theme.SUBTEXT)
        self._rebuild()
        # A voice may have been the one the Text to Speech screen was using.
        refresh = getattr(self.app, "tts", None)
        if refresh is not None and hasattr(refresh, "refresh_voices_async"):
            refresh.refresh_voices_async(lambda: None)

    def _on_progress(self, key: str, received: int, total: int, stage: str, detail: str) -> None:
        row = self._row_for(key)
        group = self._group_for(key)
        if group is not None:
            group.set_busy(True, stage)
        if stage == STAGE_DOWNLOADING and row is not None:
            row.set_progress(received, total)
        elif stage == STAGE_VERIFYING and row is not None:
            row.set_progress(total or received, total or received)
            self._show_message("Verifying the download…", theme.SUBTEXT)
        elif stage == STAGE_INSTALLED:
            self._show_message("Installed.", theme.SUBTEXT)

    def _on_succeeded(self, key: str, stem: str) -> None:
        self._show_message(f"Installed {stem}.", theme.SUCCESS)
        self._rebuild()
        refresh = getattr(self.app, "tts", None)
        if refresh is not None and hasattr(refresh, "refresh_voices_async"):
            refresh.refresh_voices_async(lambda: None)

    def _on_failed(self, key: str, message: str) -> None:
        self._show_message(message, theme.DANGER)
        row = self._row_for(key)
        if row is not None:
            row.set_busy(False)

    def _on_worker_done(self, key: str) -> None:
        self._workers.pop(key, None)
        self._rebuild()
