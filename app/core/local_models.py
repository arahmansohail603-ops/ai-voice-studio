"""Adopt model folders that are on disk but absent from the signed catalog.

The signed catalog is the source of truth for *downloadable* models, and it can
legitimately be empty: a development tree before anything was published, a
build shipped ahead of its first Release, or a user who copied a model folder
across by hand. Those models work perfectly well - they simply have no catalog
entry - so without this module the app would hide a model the user can already
see on their own disk.

Discovery is deliberately read-only and conservative: a folder is only adopted
when its known marker files are present, and adopted models carry ``local=True``
so they are never reported as outdated (the catalog has no opinion about a
version it never published) and never offer a download.
"""
from __future__ import annotations

from pathlib import Path

from app.config import QWEN_MODEL_NAME
from app.core.model_catalog import ModelSpec

# Vosk names a few languages differently than BCP-47 primary subtags.
_VOSK_NAME_MAP = {"en": "en-us", "zh": "cn"}
_VOSK_MARKERS = ("am", "graph")


def _folder_bytes(path: Path) -> int:
    """Total size of a model folder, or 0 when it cannot be read."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return 0
    return total


def _language_from_vosk_dir(name: str) -> str:
    """'small_en-us' -> 'en-US', 'small_hi' -> 'hi'."""
    base = name.removeprefix("small_")
    parts = [p for p in base.split("_") if p]
    if not parts:
        return ""
    return "-".join([parts[0].lower(), *(p.upper() for p in parts[1:])])


def _discover_vosk(models_root: Path) -> list[ModelSpec]:
    root = models_root / "vosk-models"
    if not root.is_dir():
        return []
    found: list[ModelSpec] = []
    try:
        candidates = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    for folder in candidates:
        if not all((folder / marker).is_dir() for marker in _VOSK_MARKERS):
            continue
        language = _language_from_vosk_dir(folder.name)
        name = f"Vosk {folder.name.removeprefix('small_')}"
        found.append(
            ModelSpec(
                id=f"vosk-{folder.name.replace('_', '-')}".lower(),
                kind="stt",
                engine="vosk",
                name=name,
                version="local",
                description="Found in your models folder (not published in a catalog).",
                languages=(language,) if language else (),
                install_dir=f"vosk-models/{folder.name}",
                marker_files=_VOSK_MARKERS,
                size_bytes=_folder_bytes(folder),
                local=True,
            )
        )
    return found


def _qwen_snapshot(hf_home: Path) -> Path | None:
    """Locate a complete Qwen3-TTS snapshot inside one Hugging Face cache."""
    folder = hf_home / "hub" / ("models--" + QWEN_MODEL_NAME.replace("/", "--"))
    snapshots = folder / "snapshots"
    if not snapshots.is_dir():
        return None
    try:
        candidates = sorted(p for p in snapshots.iterdir() if p.is_dir())
    except OSError:
        return None
    for snapshot in candidates:
        if (snapshot / "config.json").is_file() and (
            snapshot / "model.safetensors"
        ).is_file():
            return snapshot
    return None


def _discover_qwen(models_root: Path, hf_homes: tuple[Path, ...]) -> list[ModelSpec]:
    """Qwen3-TTS ships through the Hugging Face cache, not a zip on the Models screen."""
    default_home = models_root / "huggingface"
    homes = (default_home, *(h for h in hf_homes if h != default_home))
    for home in homes:
        snapshot = _qwen_snapshot(home)
        if snapshot is None:
            continue
        return [
            ModelSpec(
                id="qwen3-tts-1.7b-base",
                kind="tts",
                engine="qwen3",
                name="Qwen3-TTS 12Hz 1.7B Base",
                version="local",
                description="Found in your Hugging Face cache (not published in a catalog).",
                install_dir=str(snapshot),
                marker_files=("config.json", "model.safetensors"),
                size_bytes=_folder_bytes(snapshot),
                local=True,
            )
        ]
    return []


def _discover_piper(models_root: Path) -> list[ModelSpec]:
    """Piper voices sit flat in ``models/piper-voices/`` -- see ``tts_engine``."""
    root = models_root / "piper-voices"
    if not root.is_dir():
        return []
    try:
        candidates = sorted(p for p in root.glob("*.onnx") if p.is_file())
    except OSError:
        return []
    found: list[ModelSpec] = []
    for model in candidates:
        config = model.with_suffix(".onnx.json")
        if not config.is_file():
            continue
        found.append(
            ModelSpec(
                id=f"piper-{model.stem}".lower(),
                kind="tts",
                engine="piper",
                name=model.stem,
                version="local",
                description="Found in your models folder (not published in a catalog).",
                install_dir="piper-voices",
                marker_files=(model.name, config.name),
                size_bytes=model.stat().st_size,
                local=True,
            )
        )
    return found


def discover_local_specs(
    models_root: Path, hf_homes: tuple[Path, ...] = ()
) -> tuple[ModelSpec, ...]:
    """Every usable model folder that no catalog entry accounts for."""
    root = Path(models_root)
    specs: list[ModelSpec] = []
    specs += _discover_vosk(root)
    specs += _discover_piper(root)
    specs += _discover_qwen(root, hf_homes)
    return tuple(specs)
