"""Audio conversion, format detection and WAV writing (pydub + soundfile)."""
from __future__ import annotations

import shutil
import warnings
from functools import lru_cache
from pathlib import Path

warnings.filterwarnings("ignore", message=".*Couldn't find ffmpeg.*")

from app.core.errors import AppError, MissingDependencyError, soft_import

try:
    import numpy as np
except ImportError:

    class _MissingNumpy:
        def __getattr__(self, name):
            raise MissingDependencyError("numpy", "numpy")

    np = _MissingNumpy()  # type: ignore[assignment]


class ConversionError(AppError):
    """Raised when an audio conversion fails (e.g. MP3 needs ffmpeg)."""


@lru_cache(maxsize=1)
def _cached_ffmpeg_path() -> str | None:
    """Attempt to locate an ffmpeg binary several ways.

    1. On the PATH (pydub's ``which`` does the same search).
    2. Via the ``imageio-ffmpeg`` wheel which bundles a static binary.
    """
    pydub = soft_import("pydub")
    if pydub is not None:
        found = pydub.utils.which("ffmpeg")
        if found:
            return found
    imageio_ffmpeg = soft_import("imageio_ffmpeg")
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    """True when MP3 decoding/encoding (pydub->ffmpeg) is possible."""
    return _cached_ffmpeg_path() is not None


def configure_pydub() -> None:
    """Point pydub at the discovered ffmpeg if one exists."""
    import warnings

    pydub = soft_import("pydub")
    if pydub is None:
        return
    path = _cached_ffmpeg_path()
    warnings.simplefilter("ignore", RuntimeWarning)
    # Always assign so pydub never re-probes/warns; our own ffmpeg_available()
    # gate is what actually prevents broken MP3 operations.
    pydub.AudioSegment.converter = path or "ffmpeg"


def ensure_pydub():
    configure_pydub()
    module = soft_import("pydub")
    if module is None:
        raise MissingDependencyError("pydub", "pydub")
    return module


def audio_duration(path: str | Path) -> float:
    """Best-effort duration in seconds for WAV/MP3/OGG files."""
    p = Path(path)
    soundfile = soft_import("soundfile")
    if soundfile is not None:
        try:
            import soundfile as sf

            return float(sf.info(str(p)).duration)
        except Exception:
            pass
    try:
        segment = ensure_pydub().AudioSegment.from_file(str(p))
        return segment.duration_seconds
    except Exception as exc:
        raise AppError(f"Could not read duration of {p.name}. {exc}") from exc


def write_wav(samples: np.ndarray, samplerate: int, path: str | Path) -> Path:
    """Write a numpy array as a WAV file using soundfile."""
    sf_module = soft_import("soundfile")
    if sf_module is None:
        raise MissingDependencyError("soundfile", "soundfile")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sf_module.write(str(p), samples, samplerate)
    return p


def read_audio(path: str | Path) -> tuple[np.ndarray, int]:
    """Read audio file into a mono int16 float32 array plus samplerate."""
    sf_module = soft_import("soundfile")
    if sf_module is None:
        raise MissingDependencyError("soundfile", "soundfile")
    data, rate = sf_module.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    return data, int(rate)


def convert_format(src: str | Path, dst: str | Path) -> Path:
    """Convert between audio formats using pydub (needs ffmpeg for MP3)."""
    pydub = ensure_pydub()
    src_p, dst_p = Path(src), Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    try:
        segment = pydub.AudioSegment.from_file(str(src_p))
        segment.export(str(dst_p), format=dst_p.suffix.lstrip("."))
    except Exception as exc:
        fmt = dst_p.suffix.lstrip(".")
        if fmt in ("mp3",) and not ffmpeg_available():
            raise ConversionError(
                "MP3 encoding requires ffmpeg. Install it (e.g. `winget install "
                "ffmpeg`) or use WAV format."
            ) from exc
        raise ConversionError(f"Conversion failed: {exc}") from exc
    if not dst_p.exists():
        raise ConversionError("Conversion produced no output file.")
    return dst_p


def save_recording(
    samples: np.ndarray,
    samplerate: int,
    path: str | Path,
    fmt: str = "wav",
) -> Path:
    """Write a recording as WAV, or MP3 when possible; falls back to WAV."""
    p = Path(path)
    if fmt.lower() == "wav" or p.suffix.lower() != ".mp3":
        return write_wav(samples, samplerate, p)
    wav_tmp = p.with_suffix(".wav")
    write_wav(samples, samplerate, wav_tmp)
    try:
        convert_format(wav_tmp, p)
        wav_tmp.unlink(missing_ok=True)
        return p
    except AppError:
        return wav_tmp
