"""JSON-backed history of every generated speech, voice note and transcription."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from app import config
from app.services import file_service


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class HistoryEntry:
    """Lightweight data object for a single history item."""

    def __init__(self, **data) -> None:
        self.id: str = data.get("id", uuid.uuid4().hex)
        self.ts: str = data.get("ts", _now())
        self.type: str = data.get("type", "tts")            # tts|note|transcript|clone
        self.method: str = data.get("method", "unknown")    # edge-tts|pyttsx3|record|stt|clone
        self.title: str = data.get("title", "Untitled")
        self.text: str = data.get("text", "")
        self.file: str = data.get("file", "")
        self.params: dict = data.get("params", {})
        self.duration: float = data.get("duration", 0.0)
        self.note: str = data.get("note", "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "type": self.type,
            "method": self.method,
            "title": self.title,
            "text": self.text,
            "file": self.file,
            "params": self.params,
            "duration": self.duration,
            "note": self.note,
        }


class HistoryService:
    """Persistent history list (newest first) with filtering helpers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: List[HistoryEntry] = []
        self._path = config.history_path()

    def load(self) -> "HistoryService":
        with self._lock:
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    self._entries = [HistoryEntry(**e) for e in raw]
                except (json.JSONDecodeError, OSError, TypeError):
                    self._entries = []
            else:
                self._entries = []
            self._save_locked()
        return self

    def _save_locked(self) -> None:
        file_service.ensure_dirs()
        self._path.write_text(
            json.dumps([e.to_dict() for e in self._entries], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def add(self, entry: HistoryEntry) -> HistoryEntry:
        with self._lock:
            self._entries.insert(0, entry)
            self._save_locked()
        return entry

    def create(
        self,
        type_: str,
        method: str,
        title: str,
        text: str = "",
        file: str = "",
        params: Optional[dict] = None,
        duration: float = 0.0,
        note: str = "",
    ) -> HistoryEntry:
        entry = HistoryEntry(
            type=type_,
            method=method,
            title=title,
            text=text,
            file=file,
            params=params or {},
            duration=duration,
            note=note,
        )
        return self.add(entry)

    def all(self) -> List[HistoryEntry]:
        with self._lock:
            return list(self._entries)

    def filter(self, entry_type: Optional[str] = None) -> List[HistoryEntry]:
        if not entry_type or entry_type == "all":
            return self.all()
        return [e for e in self.all() if e.type == entry_type]

    def get(self, entry_id: str) -> Optional[HistoryEntry]:
        for e in self.all():
            if e.id == entry_id:
                return e
        return None

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.id != entry_id]
            removed = len(self._entries) != before
            if removed:
                self._save_locked()
            return removed

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save_locked()
