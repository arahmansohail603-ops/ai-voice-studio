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

from PyQt5.QtCore import QThread, pyqtSignal
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
    STAGE_RETRYING,
    STAGE_VERIFYING,
    BulkInstallResult,
    PiperVoiceLibrary,
    VoiceEntry,
)
from app.gui import theme
from app.gui.widgets import PROGRESS_SCALE, Screen, progress_value, ui_slot


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


class _BulkVoiceWorker(QThread):
    """Fetches every published voice, resuming anything already half-downloaded.

    The whole catalog is around 11 GB served at roughly 1 MB/s, so this runs for
    hours. It stops between chunks, and every voice it finishes stays finished,
    so closing the app costs nothing and re-running continues where it left off.
    """

    progressed = pyqtSignal(int, int, str, str)  # received, total, stage, label
    done = pyqtSignal(object)  # BulkInstallResult

    def __init__(self, library: PiperVoiceLibrary, entries: list[VoiceEntry]) -> None:
        super().__init__(parent=None)
        self._library = library
        self._entries = entries
        self._stop = False

    def stop(self) -> None:
        self._stop = True
        self.requestInterruption()

    def _should_cancel(self) -> bool:
        return self._stop or self.isInterruptionRequested()

    def run(self) -> None:  # pragma: no cover - exercised via the screen
        try:
            result = self._library.install_all(
                self._entries,
                on_progress=self._on_progress,
                should_cancel=self._should_cancel,
            )
        except AppError as exc:
            self.done.emit(BulkInstallResult(failed=[("(prefetch)", str(exc))]))
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.done.emit(
                BulkInstallResult(failed=[("(prefetch)", f"Unexpected error: {exc}")])
            )
            return
        self.done.emit(result)

    def _on_progress(self, stage: str, received: int, total: int, label: str) -> None:
        self.progressed.emit(received, total, stage, label)


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
        self.bar.setRange(0, PROGRESS_SCALE)
        self.bar.setValue(progress_value(received, total))

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
        self._bulk_worker: _BulkVoiceWorker | None = None
        self._prefetch_requested = False
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
        root.addSpacing(8)

        bulk = QHBoxLayout()
        bulk.setSpacing(8)

        self.download_all_btn = QPushButton("Download all voices", self)
        self.download_all_btn.setFixedHeight(32)
        self.download_all_btn.setFont(theme.font(12))
        self.download_all_btn.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 6, theme.BORDER, 1)
        )
        self.download_all_btn.clicked.connect(self._on_download_all)
        self.download_all_btn.setEnabled(False)
        bulk.addWidget(self.download_all_btn)

        self.stop_all_btn = QPushButton("Stop", self)
        self.stop_all_btn.setFixedSize(90, 32)
        self.stop_all_btn.setFont(theme.font(12))
        self.stop_all_btn.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 6, theme.BORDER, 1)
        )
        self.stop_all_btn.clicked.connect(self._on_stop_all)
        self.stop_all_btn.setVisible(False)
        bulk.addWidget(self.stop_all_btn)

        self.bulk_bar = QProgressBar(self)
        self.bulk_bar.setFixedHeight(18)
        self.bulk_bar.setTextVisible(False)
        self.bulk_bar.setVisible(False)
        bulk.addWidget(self.bulk_bar, 1)

        self.bulk_status = QLabel("", self)
        self.bulk_status.setFont(theme.font(11))
        self.bulk_status.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        bulk.addWidget(self.bulk_status)
        root.addLayout(bulk)
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
            self._update_bulk_button()
            self._rebuild()
        if kwargs.get("prefetch"):
            # Arrived from the first-run offer, so start without asking again.
            self.request_prefetch()

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

    @ui_slot
    def _on_catalog_loaded(self, voices: int, languages: int) -> None:
        self._entries = self._library.catalog_from_cache()
        self._update_bulk_button()
        self._rebuild()
        if self._prefetch_requested:
            self.start_prefetch()
        installed = len(self._library.installed())
        self._show_message(
            f"{voices} voices in {languages} languages  ·  "
            f"{installed} installed  ·  {human_size(self._library.disk_usage())} on disk",
            theme.SUBTEXT,
        )

    @ui_slot
    def _on_catalog_failed(self, message: str) -> None:
        # A cached catalog still works offline, so fall back rather than dead-end.
        cached = self._library.catalog_from_cache()
        if cached:
            self._entries = cached
            self._update_bulk_button()
            self._rebuild()
            if self._prefetch_requested:
                self.start_prefetch()
            self._show_message(
                f"Working offline from the saved voice list. ({message})",
                theme.WARNING,
            )
        else:
            self._show_message(message, theme.DANGER)

    @ui_slot
    def _on_catalog_finished(self) -> None:
        self.refresh_btn.setEnabled(True)
        self._catalog_worker = None

    # ------------------------------------------------------------ bulk prefetch
    @property
    def prefetching(self) -> bool:
        return self._bulk_worker is not None and self._bulk_worker.isRunning()

    def _update_bulk_button(self) -> None:
        """Label the button with the work genuinely left, not the whole catalog."""
        if self.prefetching:
            return
        if not self._entries:
            self.download_all_btn.setEnabled(False)
            self.download_all_btn.setText("Download all voices")
            return
        count, total = self._library.remaining(self._entries)
        if count <= 0:
            self.download_all_btn.setEnabled(False)
            self.download_all_btn.setText("Every voice is installed")
            return
        self.download_all_btn.setEnabled(True)
        self.download_all_btn.setText(
            f"Download all ({count} left  ·  {human_size(total)})"
        )

    @ui_slot
    def _on_download_all(self) -> None:
        if self.prefetching or not self._entries:
            return
        count, total = self._library.remaining(self._entries)
        if count <= 0:
            self._update_bulk_button()
            return
        confirm = QMessageBox.question(
            self,
            "Download every voice",
            f"Download the {count} voices that are still missing "
            f"({human_size(total)})?\n\n"
            "The download runs in the background, keeps its place if the "
            "connection drops, and can be stopped at any time. Voices you "
            "already have are not downloaded again.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        self.start_prefetch()

    def request_prefetch(self) -> None:
        """Start the bulk download, waiting for the catalog if it is still loading."""
        self._prefetch_requested = True
        if self._entries and not self.prefetching:
            self.start_prefetch()

    def start_prefetch(self) -> None:
        if self.prefetching or not self._entries:
            return
        self._prefetch_requested = False
        _, total = self._library.remaining(self._entries)
        self.download_all_btn.setEnabled(False)
        self.stop_all_btn.setVisible(True)
        self.bulk_bar.setVisible(True)
        self.bulk_bar.setRange(0, PROGRESS_SCALE)
        self.bulk_bar.setValue(0)
        for group in self._groups.values():
            group.set_busy(True, STAGE_DOWNLOADING)
        # Written once here rather than on every progress tick, which would
        # rewrite the shared header thousands of times over the whole catalog.
        self._show_message(
            f"Downloading every voice ({human_size(total)}). This can take "
            "hours; it keeps its place if the connection drops, and you can "
            "stop it at any time.",
            theme.SUBTEXT,
        )

        worker = _BulkVoiceWorker(self._library, list(self._entries))
        worker.progressed.connect(self._on_bulk_progress)
        worker.done.connect(self._on_bulk_done)
        worker.finished.connect(self._on_bulk_finished)
        self._bulk_worker = worker
        worker.start()

    @ui_slot
    def _on_stop_all(self) -> None:
        worker = self._bulk_worker
        if worker is None:
            return
        self.stop_all_btn.setEnabled(False)
        self.bulk_status.setText("Stopping after this voice…")
        worker.stop()

    @ui_slot
    def _on_bulk_progress(
        self, received: int, total: int, stage: str, label: str
    ) -> None:
        self.bulk_bar.setRange(0, PROGRESS_SCALE)
        self.bulk_bar.setValue(progress_value(received, total))
        done = human_size(received)
        whole = human_size(total)
        # Say so while a flaky connection is being retried. Over an hours-long
        # transfer a stalled bar is indistinguishable from a hung app, and the
        # user has no way to tell whether to wait or to give up.
        note = "  retrying connection..." if stage == STAGE_RETRYING else ""
        self.bulk_status.setText(f"{done} of {whole}  ·  {label}{note}")
        # Deliberately no _show_message() here. The header is shared with the
        # voice list and every other tab, and this slot runs once per chunk, so
        # an 11 GB download would rewrite it thousands of times. The running
        # totals belong to the status line under the progress bar, and the
        # explanation is written once when the run starts.

    @ui_slot
    def _on_bulk_done(self, result: BulkInstallResult) -> None:
        message = result.summary()
        if result.cancelled:
            self._show_message(message, theme.WARNING)
        elif result.failed:
            self._show_message(message, theme.DANGER)
        else:
            self._show_message(message, theme.SUCCESS)
        self.bulk_status.setText(message)
        refresh = getattr(self.app, "tts", None)
        if refresh is not None and hasattr(refresh, "refresh_voices_async"):
            refresh.refresh_voices_async(lambda: None)

    def shutdown(self) -> None:
        # Stop the bulk download and let the thread unwind. Leaving it running
        # would destroy a running QThread with the window and abort the process.
        # Everything already fetched stays on disk, so the next launch resumes.
        worker = self._bulk_worker
        if worker is None:
            return
        worker.stop()
        worker.wait(5000)

    @ui_slot
    def _on_bulk_finished(self) -> None:
        self._bulk_worker = None
        self.stop_all_btn.setEnabled(True)
        self.stop_all_btn.setVisible(False)
        self.bulk_bar.setVisible(False)
        self.bulk_status.setText("")
        for group in self._groups.values():
            group.set_busy(False)
        self._update_bulk_button()
        self._rebuild()

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
        if self.prefetching or any(
            worker.isRunning() for worker in self._workers.values()
        ):
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

    @ui_slot
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
    @ui_slot
    def _on_download(self, key: str) -> None:
        # A failure re-enables the row's button from _on_failed, which runs
        # before this worker's queued `finished` has been delivered. Without
        # this guard a second click started a second install for the same voice
        # writing the same directory concurrently, and overwrote the entry in
        # _workers -- so the still-running QThread lost its last Python
        # reference and aborted the process. The Models screen already guards
        # the same way.
        if self.prefetching:
            # The bulk run is already fetching this voice's files; a second
            # writer on the same directory would corrupt the partial file.
            return
        if key in self._workers:
            return
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

    @ui_slot
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

    @ui_slot
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

    @ui_slot
    def _on_succeeded(self, key: str, stem: str) -> None:
        self._show_message(f"Installed {stem}.", theme.SUCCESS)
        self._rebuild()
        refresh = getattr(self.app, "tts", None)
        if refresh is not None and hasattr(refresh, "refresh_voices_async"):
            refresh.refresh_voices_async(lambda: None)

    @ui_slot
    def _on_failed(self, key: str, message: str) -> None:
        self._show_message(message, theme.DANGER)
        row = self._row_for(key)
        if row is not None:
            row.set_busy(False)

    def _on_worker_done(self, key: str) -> None:
        self._workers.pop(key, None)
        self._rebuild()


def prefetch_already_offered() -> bool:
    """True once the one-time offer has been made, so it is never asked twice."""
    from app.config import PIPER_PREFETCH_MARKER

    try:
        return PIPER_PREFETCH_MARKER.exists()
    except OSError:
        return True


def offer_bulk_prefetch(app) -> None:
    """Offer the one-time "fetch every voice" download, once, on first run.

    Must run on the UI thread: it shows a modal question and navigates. It reads
    the cached catalog, so the caller warms the cache in the background first --
    otherwise a fresh install, which is the only install this is for, would have
    nothing cached and would never ask. It also asks rather than just starting,
    because the full set is around 11 GB and silently spending that on a metered
    connection is not a reasonable default.
    """
    from app.config import PIPER_PREFETCH_MARKER

    if prefetch_already_offered():
        return

    library = PiperVoiceLibrary()
    try:
        entries = library.catalog_from_cache()
    except OSError:
        return
    if not entries:
        return

    try:
        count, total = library.remaining(entries)
    except OSError:
        return
    try:
        PIPER_PREFETCH_MARKER.parent.mkdir(parents=True, exist_ok=True)
        PIPER_PREFETCH_MARKER.write_text("offered\n", encoding="utf-8")
    except OSError:
        # A read-only data folder just means we may ask again next time.
        pass

    if count <= 0:
        return

    answer = QMessageBox.question(
        None,
        "Download every voice now?",
        f"{count} Piper voices are not installed yet "
        f"({human_size(total)} in total).\n\n"
        "Downloading them all now means every language the app can speak "
        "works offline straight away, with no per-voice download later. "
        "It runs in the background, keeps its place if the connection drops, "
        "and you can stop it at any time.",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    if answer == QMessageBox.Yes:
        app.show_screen("voices", prefetch=True)
