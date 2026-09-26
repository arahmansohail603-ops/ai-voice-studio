"""The Models screen: browse, download, verify and remove AI models.

Every downloadable model comes from a signed catalog, so this screen never
guesses a URL or silently starts a multi-gigabyte transfer. Each row shows the
exact size, the install state and, while a download runs, real byte progress
with a cancel button.

A download runs on a worker :class:`QThread`; the thread never touches a widget
directly and instead emits signals, so a 4.5 GB transfer cannot freeze or crash
the interface.
"""
from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.errors import AppError
from app.core.model_catalog import ModelSpec
from app.core.model_manager import (
    STAGE_DOWNLOADING,
    STAGE_EXTRACTING,
    STAGE_FETCHING,
    STAGE_INSTALLED,
    STAGE_VERIFYING,
    ModelManager,
    get_manager,
    human_size,
)
from app.gui import theme
from app.gui.widgets import BusyButton, Screen

_KIND_LABELS = {
    "stt": "Speech recognition",
    "tts": "Text to speech",
    "translation": "Translation",
    "clone": "Voice cloning",
}
_KIND_ORDER = ("stt", "tts", "clone", "translation")

_STAGE_COLORS = {
    STAGE_DOWNLOADING: theme.ACCENT,
    STAGE_VERIFYING: theme.WARNING,
    STAGE_EXTRACTING: theme.WARNING,
    STAGE_INSTALLED: theme.SUCCESS,
}


class _DownloadWorker(QThread):
    """Runs one install on a worker thread and reports progress as signals."""

    progressed = pyqtSignal(str, int, int, str, str)  # id, received, total, stage, detail
    succeeded = pyqtSignal(str, bool)  # spec_id, was_fresh_install
    failed = pyqtSignal(str, str)  # spec_id, message

    def __init__(self, manager: ModelManager, spec_id: str) -> None:
        super().__init__(parent=None)
        self._manager = manager
        self._spec_id = spec_id

    def run(self) -> None:  # pragma: no cover - exercised via the screen
        try:
            result = self._manager.install(
                self._spec_id, self._on_progress, force=True
            )
            self.succeeded.emit(self._spec_id, result.installed)
        except AppError as exc:
            self.failed.emit(self._spec_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.failed.emit(self._spec_id, f"Unexpected error: {exc}")

    def _on_progress(
        self, stage: str, received: int, total: int, detail: str
    ) -> None:
        # Forward the stage itself: the screen must not have to guess it back
        # out of a human-readable sentence.
        self.progressed.emit(self._spec_id, received, total, stage, detail or stage)


class _RefreshWorker(QThread):
    """Fetches the catalog off the GUI thread.

    A refresh is a network round trip that can take many seconds on a bad
    connection, so doing it inline would freeze every window in the app.
    """

    refreshed = pyqtSignal(int, str)  # model_count, source
    failed = pyqtSignal(str)

    def __init__(self, manager: ModelManager) -> None:
        super().__init__(parent=None)
        self._manager = manager

    def run(self) -> None:  # pragma: no cover - exercised via the screen
        try:
            catalog = self._manager.refresh()
        except AppError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.refreshed.emit(len(catalog.models), catalog.source)


class _Row(QFrame):
    """One model: name, description, size, state and its action buttons."""

    download_clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)
    cancel_clicked = pyqtSignal(str)

    def __init__(
        self,
        master,
        spec: ModelSpec,
        status: str,
        installed: bool,
        *,
        updatable: bool | None = None,
        downloadable: bool = True,
    ):
        super().__init__(master)
        self.spec = spec
        self.installed = installed
        self.updatable = installed if updatable is None else updatable
        self.downloadable = downloadable
        self.setStyleSheet(theme.frame_style(theme.PANEL_BG, 10))

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        name = QLabel(spec.name, self)
        name.setFont(theme.font(13, "bold"))
        name.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        top.addWidget(name)

        version = QLabel(f"v{spec.version}" if spec.version else "local", self)
        version.setFont(theme.font(11))
        version.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        top.addWidget(version)
        top.addStretch(1)

        self.state_lbl = QLabel(status, self)
        self.state_lbl.setFont(theme.font(11))
        self.state_lbl.setStyleSheet(
            theme.label_style(theme.SUCCESS if installed else theme.SUBTEXT, "right")
        )
        top.addWidget(self.state_lbl)
        root.addLayout(top)

        details = []
        if spec.description:
            details.append(spec.description)
        languages = [code for code in spec.languages if code]
        if languages:
            shown = ", ".join(languages[:6])
            extra = len(languages) - 6
            details.append(f"Languages: {shown}{f' +{extra} more' if extra > 0 else ''}")
        size = human_size(spec.size_bytes or spec.download_bytes())
        if spec.is_downloadable:
            parts = f" · {len(spec.assets)} parts" if spec.is_chunked else ""
            details.append(f"Download: {size}{parts}")
        else:
            details.append(f"On disk: {size}")
        if spec.requires:
            details.append(f"Requires: {', '.join(spec.requires)}")

        subtitle = QLabel(" · ".join(details), self)
        subtitle.setFont(theme.font(11))
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        root.addWidget(subtitle)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)

        self.delete_btn = QPushButton("Remove", self)
        self.delete_btn.setFixedSize(96, 32)
        self.delete_btn.setFont(theme.font(12))
        self.delete_btn.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 6, theme.BORDER, 1)
        )
        self.delete_btn.clicked.connect(
            lambda: self.delete_clicked.emit(self.spec.id)
        )
        self.delete_btn.setVisible(installed and self.downloadable)
        buttons.addWidget(self.delete_btn)

        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.setFixedSize(84, 32)
        self.cancel_btn.setFont(theme.font(12))
        self.cancel_btn.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 6, theme.BORDER, 1)
        )
        self.cancel_btn.clicked.connect(
            lambda: self.cancel_clicked.emit(self.spec.id)
        )
        self.cancel_btn.setVisible(False)
        buttons.addWidget(self.cancel_btn)

        self.action_btn = BusyButton(
            self,
            text=self._action_text(),
            command=lambda: self.download_clicked.emit(self.spec.id),
            width=124,
            height=32,
            font=theme.font(12, "bold"),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
        )
        self.action_btn.setVisible(self.downloadable)
        buttons.addWidget(self.action_btn)
        root.addLayout(buttons)

    def _action_text(self) -> str:
        if not self.installed:
            return "Download"
        return "Update" if self.updatable else "Re-download"

    # ------------------------------------------------------------------ state
    def set_installed(self, status: str, installed: bool, *, updatable: bool | None = None) -> None:
        self.installed = installed
        if updatable is not None:
            self.updatable = updatable
        self.state_lbl.setText(status)
        self.state_lbl.setStyleSheet(
            theme.label_style(theme.SUCCESS if installed else theme.SUBTEXT, "right")
        )
        self.delete_btn.setVisible(installed)
        if not self.action_btn.busy:
            self.action_btn.setText(self._action_text())

    def show_idle(self) -> None:
        self.progress.setVisible(False)
        self.progress.setValue(0)
        self.action_btn.set_busy(False)
        self.action_btn.setVisible(True)
        self.cancel_btn.setVisible(False)
        self.delete_btn.setEnabled(True)

    def show_progress(self, stage: str, received: int, total: int, detail: str) -> None:
        self.progress.setVisible(True)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(received)
            percent = int(received * 100 / total)
            self.progress.setFormat(f"{percent}%")
        else:
            # Indeterminate: the total is not known yet (catalog fetch).
            self.progress.setRange(0, 0)
            self.progress.setFormat("")
        self.progress.setStyleSheet(
            theme.progress_style(
                theme.ACCENT, _STAGE_COLORS.get(stage, theme.ACCENT)
            )
        )
        self.state_lbl.setText(detail)
        self.state_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "right"))
        self.action_btn.set_busy(True, "Downloading…")
        self.action_btn.setVisible(False)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)
        self.delete_btn.setEnabled(False)

    def show_failure(self, message: str) -> None:
        self.progress.setVisible(False)
        self.progress.setValue(0)
        self.state_lbl.setText(message)
        self.state_lbl.setStyleSheet(theme.label_style(theme.DANGER, "right"))
        self.action_btn.set_busy(False)
        self.action_btn.setVisible(True)
        self.cancel_btn.setVisible(False)
        self.delete_btn.setEnabled(True)


class ModelsScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self._manager: ModelManager | None = None
        self._rows: dict[str, _Row] = {}
        self._workers: dict[str, _DownloadWorker] = {}
        self._refresh_worker: _RefreshWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QLabel("Models", self)
        header.setFont(theme.font(20, "bold"))
        header.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        root.addWidget(header)

        blurb = QLabel(
            "Download the AI models this app uses. Everything runs locally — no "
            "cloud service is contacted. Each download is verified against a "
            "signed catalog before it is installed.",
            self,
        )
        blurb.setFont(theme.font(12))
        blurb.setWordWrap(True)
        blurb.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        root.addWidget(blurb)
        root.addSpacing(10)

        self.summary = QLabel("", self)
        self.summary.setFont(theme.font(12))
        self.summary.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        root.addWidget(self.summary)
        root.addSpacing(6)

        self.refresh_btn = QPushButton("Refresh catalog", self)
        self.refresh_btn.setFixedSize(150, 32)
        self.refresh_btn.setFont(theme.font(12))
        self.refresh_btn.setStyleSheet(
            theme.button_style(theme.INPUT_BG, theme.CARD_BG, theme.TEXT, 6, theme.BORDER, 1)
        )
        self.refresh_btn.clicked.connect(self._on_refresh)
        root.addWidget(self.refresh_btn)
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
            "No models are published for this build yet. Open Settings to point the "
            "app at a model catalog, or use the bundled catalog.",
            holder,
        )
        self.empty.setFont(theme.font(12))
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        self._body.addWidget(self.empty)
        self._body.addStretch(1)
    # ------------------------------------------------------------------ data
    @property
    def manager(self) -> ModelManager:
        if self._manager is None:
            self._manager = get_manager()
        return self._manager

    def on_show(self, **kwargs) -> None:
        self._rebuild()

    # --------------------------------------------------------------- building
    def _clear_rows(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._sections.clear()

    def _rebuild(self) -> None:
        """Rebuild every row from the current catalog and install state."""
        if any(worker.isRunning() for worker in self._workers.values()):
            # Do not tear down rows out from under a running download.
            return

        while self._body.count() > 0:
            item = self._body.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.empty:
                widget.setParent(None)
                widget.deleteLater()

        self._rows.clear()
        try:
            specs = self.manager.all_specs()
        except AppError as exc:
            self._show_message(str(exc), theme.DANGER)
            self._update_summary()
            return

        by_kind: dict[str, list[ModelSpec]] = {}
        for spec in specs:
            by_kind.setdefault(spec.kind, []).append(spec)

        ordered = [kind for kind in _KIND_ORDER if by_kind.get(kind)]
        ordered += sorted(k for k in by_kind if k not in _KIND_ORDER)

        if not specs:
            self.empty.setVisible(True)
            self._body.addWidget(self.empty)
        else:
            self.empty.setVisible(False)
            for kind in ordered:
                rows = sorted(by_kind[kind], key=lambda s: (s.optional, s.name.lower()))
                self._body.addWidget(self._section_title(kind))
                for spec in rows:
                    state = spec.update_state(self.manager.models_root)
                    row = _Row(
                        self,
                        spec,
                        self.manager.status_line(spec),
                        state != "missing",
                        updatable=state == "update",
                        downloadable=spec.is_downloadable,
                    )
                    row.download_clicked.connect(self._on_download)
                    row.delete_clicked.connect(self._on_delete)
                    row.cancel_clicked.connect(self._on_cancel)
                    self._rows[spec.id] = row
                    self._body.addWidget(row)
                self._body.addSpacing(8)

        self._body.addStretch(1)
        self._update_summary()

    def _section_title(self, kind: str) -> QLabel:
        title = QLabel(_KIND_LABELS.get(kind, kind.title()), self)
        title.setFont(theme.font(14, "bold"))
        title.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        return title

    def _update_summary(self) -> None:
        try:
            free, pending = self.manager.disk_report()
        except AppError:
            return
        installed = len(self._installed_ids())
        outdated = len(self._outdated_ids())
        parts = [f"{installed} model(s) installed."]
        if outdated:
            parts.append(f"{outdated} update(s) available.")
        if pending:
            parts.append(f"{human_size(pending)} still to download.")
        if free:
            parts.append(f"{human_size(free)} free on the models drive.")
        self.summary.setText("  ".join(parts))

    def _installed_ids(self) -> set[str]:
        try:
            return {spec.id for spec in self.manager.installed_specs()}
        except AppError:
            return set()

    def _outdated_ids(self) -> set[str]:
        try:
            return {spec.id for spec in self.manager.catalog.outdated(self.manager.models_root)}
        except AppError:
            return set()

    def _show_message(self, text: str, color: str) -> None:
        self.summary.setText(text)
        self.summary.setStyleSheet(theme.label_style(color, "left"))

    # --------------------------------------------------------------- actions
    def _on_refresh(self) -> None:
        if self._refresh_worker is not None and self._refresh_worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self._show_message("Fetching the model catalog…", theme.SUBTEXT)

        worker = _RefreshWorker(self.manager)
        worker.refreshed.connect(self._on_refreshed, Qt.QueuedConnection)
        worker.failed.connect(self._on_refresh_failed, Qt.QueuedConnection)
        self._refresh_worker = worker
        worker.start()

    def _on_refreshed(self, count: int, source: str) -> None:
        self._release_refresh_worker()
        self._rebuild()
        label = "GitHub" if source in ("remote", "cached") else "bundled"
        self._show_message(
            f"Catalog updated from the {label} source — {count} model(s).",
            theme.SUCCESS,
        )

    def _on_refresh_failed(self, message: str) -> None:
        self._release_refresh_worker()
        self._show_message(message, theme.DANGER)

    def _release_refresh_worker(self) -> None:
        worker = self._refresh_worker
        self._refresh_worker = None
        self.refresh_btn.setEnabled(True)
        if worker is not None:
            worker.deleteLater()

    def _on_download(self, spec_id: str) -> None:
        if spec_id in self._workers:
            return
        try:
            spec = self.manager.spec(spec_id)
        except AppError as exc:
            self._show_message(str(exc), theme.DANGER)
            return

        row = self._rows.get(spec_id)
        state = spec.update_state(self.manager.models_root)

        if state == "missing":
            # A first install: nothing on disk to protect, so just start it.
            pass
        elif state == "update":
            confirmed = QMessageBox.question(
                self,
                "Update model?",
                f"'{spec.name}' v{spec.version} is available (you have v"
                f"{spec.installed_version(self.manager.models_root) or 'unknown'}).\n\n"
                f"Updating downloads {human_size(spec.download_bytes())} and replaces "
                "the installed files.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmed != QMessageBox.Yes:
                return
        else:
            # Up to date. Offer a repair-style re-download, but do not push it.
            confirmed = QMessageBox.question(
                self,
                "Re-download model?",
                f"'{spec.name}' v{spec.version} is already installed.\n\n"
                "Download it again to replace the installed files?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmed != QMessageBox.Yes:
                if row is not None:
                    row.show_idle()
                self._update_summary()
                self._show_message(
                    f"'{spec.name}' is already up to date (v{spec.version}).", theme.SUBTEXT
                )
                return

        worker = _DownloadWorker(self.manager, spec_id)
        worker.progressed.connect(self._on_progress, Qt.QueuedConnection)
        worker.succeeded.connect(self._on_succeeded, Qt.QueuedConnection)
        worker.failed.connect(self._on_failed, Qt.QueuedConnection)
        self._workers[spec_id] = worker
        if row is not None:
            row.show_progress(STAGE_FETCHING, 0, 0, "Starting…")
        worker.start()

    def _on_delete(self, spec_id: str) -> None:
        row = self._rows.get(spec_id)
        spec = row.spec if row is not None else None
        name = spec.name if spec is not None else spec_id
        confirmed = QMessageBox.question(
            self,
            "Remove model?",
            f"Delete '{name}' from this computer?\n\n"
            "Any engine that needs it will be unavailable until you download it again.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return
        try:
            self.manager.delete(spec_id)
        except AppError as exc:
            self._show_message(str(exc), theme.DANGER)
            return
        if row is not None:
            row.set_installed(
                self.manager.status_line(row.spec), False, updatable=False
            )
        self._update_summary()
        self._show_message(f"Removed '{name}'.", theme.SUBTEXT)

    def _on_cancel(self, spec_id: str) -> None:
        self.manager.cancel()

    # -------------------------------------------------------------- progress
    def _on_progress(
        self, spec_id: str, received: int, total: int, stage: str, detail: str
    ) -> None:
        row = self._rows.get(spec_id)
        if row is None:
            return
        row.show_progress(stage, received, total, detail)

    def _on_succeeded(self, spec_id: str, was_fresh: bool) -> None:
        worker = self._workers.pop(spec_id, None)
        if worker is not None:
            worker.deleteLater()
        row = self._rows.get(spec_id)
        if row is not None:
            row.show_idle()
            row.set_installed(
                self.manager.status_line(row.spec), True, updatable=False
            )
        self._update_summary()
        self._show_message(f"{row.spec.name} is ready." if row else "Model is ready.", theme.SUCCESS)

    def _on_failed(self, spec_id: str, message: str) -> None:
        worker = self._workers.pop(spec_id, None)
        if worker is not None:
            worker.deleteLater()
        row = self._rows.get(spec_id)
        if row is not None:
            row.show_failure(message)
        self._update_summary()

    # ------------------------------------------------------------------ misc
    def on_hide(self) -> None:
        # Downloads keep running in the background; the rows catch up on return.
        return None

    def models_dir(self) -> Path:
        return self.manager.models_root
