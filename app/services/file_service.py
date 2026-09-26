"""Guaranteed folder layout and file naming helpers."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app import config


def ensure_dirs() -> None:
    """Create every folder the application needs at startup."""
    config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for path in config.MODEL_SUBDIRS.values():
        path.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in config.OUTPUT_SUBDIRS.values():
        path.mkdir(parents=True, exist_ok=True)


def set_output_root(new_root: str | Path) -> None:
    """Point all category folders at a new root (persisted by caller)."""
    root = Path(new_root).resolve()
    config.OUTPUT_DIR = root
    root.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_SUBDIRS = {
        "tts": root / "tts",
        "recordings": root / "recordings",
        "transcripts": root / "transcripts",
        "voices": root / "voices",
        "clones": root / "clones",
    }
    ensure_dirs()


def category_dir(category: str) -> Path:
    """Return the guaranteed-to-exist output folder for a category."""
    folder = config.OUTPUT_SUBDIRS.get(category, config.OUTPUT_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def unique_path(directory: Path, stem: str, extension: str) -> Path:
    """Return a non-colliding path inside ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    ext = extension if extension.startswith(".") else f".{extension}"
    candidate = directory / f"{stem}{ext}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{ext}"
        counter += 1
    return candidate


def timestamp_stem(prefix: str, fmt: str = "%Y%m%d_%H%M%S") -> str:
    """Stable timestamped filename stem, e.g. ``tts_20260919_141512``."""
    return f"{prefix}_{datetime.now().strftime(fmt)}"


def relative_to_output(path: Path) -> str:
    """Store output paths relative to the project to keep them portable."""
    try:
        return str(path.relative_to(config.BASE_DIR))
    except ValueError:
        return str(path)


def absolutize(path: str | Path) -> Path:
    """Expand a (possibly relative) stored path against the project root."""
    p = Path(path)
    if p.is_absolute():
        return p
    return (config.BASE_DIR / p).resolve()
