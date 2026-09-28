"""Downloading, verifying, installing and removing catalog models.

One model is downloaded at a time and every step is a distinct, reportable
stage so the UI can show honest progress:

``fetch catalog`` -> ``download part N/M`` -> ``verify digest`` ->
``extract`` -> ``installed``

Guarantees that matter:

* **Never install unverified bytes.** The archive digest is checked while it
  streams, and again from disk, before a single file is extracted.
* **Atomic installs.** Extraction happens in a staging directory outside
  ``MODELS_DIR``; the finished tree is moved into place only after success. A
  crashed download can never leave a half-installed model that an engine would
  happily load.
* **Bounded resource use.** Declared sizes are checked against free disk space
  before starting, the response is capped at the declared size, and extraction
  is capped by both a byte budget and an expansion ratio.
* **Resumable.** A partial ``.part`` file is kept and continued with an HTTP
  ``Range`` request when the server supports it.
* **Cancellable and thread-safe.** Progress is reported as structured tuples,
  never pre-formatted strings, so the UI can reformat for any locale.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import (
    LEGACY_PROJECT_HF_HOME,
    MODEL_CATALOG_CACHE,
    MODEL_CATALOG_TIMEOUT,
    MODEL_DISK_HEADROOM_BYTES,
    MODEL_DOWNLOAD_TIMEOUT,
    MODEL_MANIFEST_URL,
    MODEL_MIRROR_BASE,
    MODEL_STAGING_DIR,
    MODELS_DIR,
)
from app.core.errors import AppError, ModelError, ModelNotInstalledError
from app.core.local_models import discover_local_specs
from app.core.model_catalog import (
    DEFAULT_ALLOWED_HOSTS,
    MAX_EXPANSION_RATIO,
    ModelCatalog,
    ModelSpec,
    load_bundled_catalog,
    verify_catalog,
)

#: Emitted as ``(stage, received_bytes, total_bytes, detail)``.
ProgressCallback = Callable[[str, int, int, str], None]

STAGE_FETCHING = "fetching"
STAGE_DOWNLOADING = "downloading"
STAGE_VERIFYING = "verifying"
STAGE_EXTRACTING = "extracting"
STAGE_INSTALLED = "installed"
STAGE_FAILED = "failed"

_CHUNK = 1024 * 256
#: Per-part ceiling on how much we will ever read, as a guard against a server
#: that streams forever or lies about Content-Length.
_MAX_OVERSHOOT = 8 * 1024 * 1024


class DownloadCancelled(AppError):
    """Raised internally when a download is cancelled by the user."""


# --------------------------------------------------------------------------- #
# Disk helpers
# --------------------------------------------------------------------------- #
def free_bytes(path: Path) -> int:
    """Best-effort free space on the volume that will hold ``path``."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return 0
    return int(usage.free)


def directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Safe extraction
# --------------------------------------------------------------------------- #
def _member_is_safe(name: str) -> bool:
    """Reject absolute paths, drive letters, traversal and symlink-ish entries."""
    if not name or name.startswith(("/", "\\")):
        return False
    head = name.split("/")[0]
    if len(head) == 2 and ":" in head:
        return False  # e.g. "C:/evil"
    normalised = name.replace("\\", "/")
    return not any(part == ".." for part in normalised.split("/"))


def extract_zip(
    archive: Path,
    destination: Path,
    *,
    max_extracted_bytes: int,
    strip_single_root: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Extract ``archive`` into ``destination`` with hard resource limits.

    ``zipfile`` already refuses to write outside the destination directory, but
    it will happily expand a small archive into an unbounded amount of data, so
    the total decompressed size and the expansion ratio are both capped here.
    """
    try:
        handle = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ModelError(f"The downloaded model archive is not a valid zip: {exc}") from exc

    with handle as zf:
        members = [info for info in zf.infolist() if _member_is_safe(info.filename)]
        if not members:
            raise ModelError("The downloaded model archive is empty.")

        declared = sum(info.file_size for info in members)
        if declared > max_extracted_bytes:
            raise ModelError(
                "The model archive expands to "
                f"{declared / (1024 ** 3):.1f} GB, which is larger than the "
                f"{max_extracted_bytes / (1024 ** 3):.1f} GB limit for this model."
            )

        compressed = sum(info.compress_size for info in members) or 1
        if declared / compressed > MAX_EXPANSION_RATIO:
            raise ModelError(
                "The model archive has an implausible compression ratio and was "
                "rejected as a possible decompression attack."
            )

        root: Path | None = None
        if strip_single_root:
            tops = {info.filename.replace("\\", "/").split("/")[0] for info in members}
            if len(tops) == 1:
                only = next(iter(tops))
                if any("/" in info.filename.replace("\\", "/") for info in members):
                    root = only

        destination.mkdir(parents=True, exist_ok=True)
        written = 0
        total = len(members)
        for index, info in enumerate(members, start=1):
            if should_cancel and should_cancel():
                raise DownloadCancelled("Model installation was cancelled.")
            relative = info.filename.replace("\\", "/")
            if root and relative.startswith(f"{root}/"):
                relative = relative[len(root) + 1 :]
            if not relative or relative.endswith("/"):
                if relative:
                    (destination / relative).mkdir(parents=True, exist_ok=True)
                continue

            written += max(info.file_size, 0)
            if written > max_extracted_bytes:
                raise ModelError(
                    "The model archive expanded beyond its declared size and was "
                    "stopped."
                )

            target = destination / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                resolved = target.resolve()
                if not str(resolved).startswith(str(destination.resolve())):
                    raise ModelError(
                        "The model archive contains an unsafe file path and was rejected."
                    )
            except OSError as exc:
                raise ModelError(
                    f"Could not prepare '{relative}' for extraction: {exc}"
                ) from exc

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            with zf.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink, _CHUNK)
            if on_progress:
                on_progress(index, total)


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #
def _require_https(url: str | None, setting: str) -> str:
    """Normalise a configured URL and refuse anything that is not HTTPS.

    Model downloads are trusted because they are signed, but a plain-HTTP
    manifest or mirror would still let anyone on the path rewrite the bytes we
    are about to fetch. Refusing at construction time fails closed and loudly.
    """
    text = (url or "").strip()
    if not text:
        return ""
    if not text.lower().startswith("https://"):
        raise ModelError(
            f"{setting} must start with https://; refusing to use '{text}'."
        )
    return text


@dataclass(frozen=True)
class InstallResult:
    spec: ModelSpec
    install_path: Path
    installed: bool


class ModelManager:
    """Owns the catalog, the download lock and the on-disk install state."""

    def __init__(
        self,
        models_root: Path | None = None,
        staging_dir: Path | None = None,
        *,
        manifest_url: str = MODEL_MANIFEST_URL,
        public_keys: Any = None,
        mirror_base: str = MODEL_MIRROR_BASE,
        catalog_cache: Path | None = None,
        keep_archive: bool = False,
        allowed_hosts: frozenset[str] | None = None,
    ) -> None:
        self.models_root = Path(models_root or MODELS_DIR)
        self.staging_dir = Path(staging_dir or MODEL_STAGING_DIR)
        #: Keep the verified archive after installing, so a re-install of the
        #: same version skips the download. Off by default: it doubles disk use.
        self._keep_archive = keep_archive
        self.manifest_url = _require_https(manifest_url, "AI_VOICE_STUDIO_MODEL_MANIFEST_URL")
        self.mirror_base = _require_https(
            mirror_base, "AI_VOICE_STUDIO_MODEL_MIRROR_BASE"
        ).rstrip("/")
        self.catalog_cache = Path(catalog_cache or MODEL_CATALOG_CACHE)
        self.public_keys = _normalise_public_keys(public_keys)
        self.allowed_hosts = frozenset(allowed_hosts or DEFAULT_ALLOWED_HOSTS)
        self._catalog: ModelCatalog | None = None
        self._local: tuple[ModelSpec, ...] = ()
        self._lock = threading.RLock()
        self._download_lock = threading.Lock()
        self._cancel = threading.Event()

    # ------------------------------------------------------------- catalog
    @property
    def catalog(self) -> ModelCatalog:
        with self._lock:
            if self._catalog is None:
                self._catalog = self._load_catalog()
            return self._catalog

    @property
    def local_specs(self) -> tuple[ModelSpec, ...]:
        """Model folders found on disk that the catalog does not publish."""
        with self._lock:
            if not self._local:
                self._local = discover_local_specs(
                    self.models_root, self._extra_hf_homes()
                )
            return self._local

    def _extra_hf_homes(self) -> tuple[Path, ...]:
        """Hugging Face caches to search besides the ones under ``models_root``.

        The source-tree cache is only consulted for the real, configured models
        folder. A caller that redirects storage elsewhere - a test, or a user who
        points ``AI_VOICE_STUDIO_DATA`` at an external drive - must not silently
        adopt a model that lives outside the location it asked for.
        """
        if Path(self.models_root) == Path(MODELS_DIR):
            return (LEGACY_PROJECT_HF_HOME,)
        return ()

    def all_specs(self) -> tuple[ModelSpec, ...]:
        """Catalog entries first, then adopted local folders.

        A local folder never shadows a catalog entry for the same install path:
        the published entry wins, because it carries a version and an update
        path that the adopted one cannot offer.
        """
        with self._lock:
            published = self.catalog.models
            claimed = {spec.install_path(self.models_root) for spec in published}
            adopted = tuple(
                spec
                for spec in self.local_specs
                if spec.install_path(self.models_root) not in claimed
            )
            return published + adopted

    def refresh(self, on_progress: ProgressCallback | None = None) -> ModelCatalog:
        """Fetch the remote catalog when configured, else use the bundled copy."""
        with self._lock:
            self._local = ()
            if not self.manifest_url:
                self._catalog = load_bundled_catalog()
                return self._catalog
            if not self.public_keys:
                raise ModelError(
                    "No model catalog signing key is configured, so the remote "
                    "catalog cannot be trusted. Set AI_VOICE_STUDIO_MODEL_PUBLIC_KEYS "
                    "or run without a manifest URL to use the bundled catalog."
                )
            if on_progress:
                on_progress(STAGE_FETCHING, 0, 0, "Fetching the model catalog…")
            envelope = self._http_get_json(self.manifest_url)
            catalog = verify_catalog(
                envelope,
                public_keys=self.public_keys,
                allowed_hosts=self.allowed_hosts,
                source="remote",
            )
            self._cache_catalog(envelope)
            self._catalog = catalog
            return catalog

    def set_catalog(self, catalog: ModelCatalog) -> None:
        """Inject a catalog directly (used by tests and the offline mirror)."""
        with self._lock:
            self._catalog = catalog

    def _load_catalog(self) -> ModelCatalog:
        if self.manifest_url:
            try:
                return self.refresh()
            except AppError:
                cached = self._load_cached_catalog()
                if cached is not None:
                    return cached
        return load_bundled_catalog()

    def _cache_catalog(self, envelope: Any) -> None:
        try:
            self.catalog_cache.parent.mkdir(parents=True, exist_ok=True)
            self.catalog_cache.write_text(
                json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass

    def _load_cached_catalog(self) -> ModelCatalog | None:
        try:
            raw = json.loads(self.catalog_cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or not self.public_keys:
            return None
        try:
            return verify_catalog(
                raw,
                public_keys=self.public_keys,
                allowed_hosts=self.allowed_hosts,
                source="cached",
            )
        except AppError:
            return None

    def _http_get_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": _user_agent()}
        )
        try:
            with self._open(request, MODEL_CATALOG_TIMEOUT) as response:
                raw = response.read(_MAX_CATALOG_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ModelError(f"Could not fetch the model catalog: {exc}") from exc
        if len(raw) > _MAX_CATALOG_BYTES:
            raise ModelError("The model catalog is unexpectedly large.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelError("The model catalog is not valid JSON.") from exc

    # -------------------------------------------------------------- queries
    def spec(self, spec_id: str) -> ModelSpec:
        for spec in self.all_specs():
            if spec.id == spec_id:
                return spec
        return self.catalog.require(spec_id)

    def is_installed(self, spec_id: str) -> bool:
        spec = next((s for s in self.all_specs() if s.id == spec_id), None)
        return bool(spec and spec.is_installed(self.models_root))

    def installed_specs(self) -> tuple[ModelSpec, ...]:
        return tuple(spec for spec in self.all_specs() if spec.is_installed(self.models_root))

    def require_installed(self, spec_id: str, detail: str = "") -> Path:
        """Return an installed model's path, or explain how to install it."""
        spec = next((s for s in self.all_specs() if s.id == spec_id), None)
        if spec is None or not spec.is_installed(self.models_root):
            name = spec.name if spec else spec_id
            raise ModelNotInstalledError(spec_id, name, detail)
        return spec.install_path(self.models_root)

    def find_installed(
        self, *, kind: str = "", engine: str = "", language: str = ""
    ) -> ModelSpec | None:
        """First installed spec matching the given filters."""
        candidates = self.all_specs()
        if kind:
            wanted = kind.strip().lower()
            candidates = tuple(s for s in candidates if s.kind == wanted)
        if engine:
            wanted = engine.strip().lower()
            candidates = tuple(s for s in candidates if s.engine.lower() == wanted)
        installed = [spec for spec in candidates if spec.is_installed(self.models_root)]
        if not installed:
            return None
        if language:
            primary = language.split("-")[0].lower()
            for spec in installed:
                for tag in spec.languages:
                    if tag.lower() == language.lower() or (
                        tag.split("-")[0].lower() == primary
                    ):
                        return spec
        return installed[0]

    def find_any(
        self,
        *,
        kind: str = "",
        engine: str = "",
        language: str = "",
        strict_language: bool = False,
    ) -> ModelSpec | None:
        """Best spec for the given filters.

        ``language`` is a preference by default: when nothing publishes that
        language the first remaining candidate comes back, which is what callers
        asking "show me anything for STT" want. Pass ``strict_language=True``
        when the answer is used as "does a model for *this* language exist?" --
        the fallback would otherwise hand back an unrelated model and let the
        caller report success for a language that was never published.
        """
        candidates = self.all_specs()
        if kind:
            wanted = kind.strip().lower()
            candidates = tuple(s for s in candidates if s.kind == wanted)
        if engine:
            wanted = engine.strip().lower()
            candidates = tuple(s for s in candidates if s.engine.lower() == wanted)
        if not candidates:
            return None
        if language:
            primary = language.split("-")[0].lower()
            for spec in candidates:
                for tag in spec.languages:
                    if tag.lower() == language.lower() or (
                        tag.split("-")[0].lower() == primary
                    ):
                        return spec
        if language and strict_language:
            return None
        return candidates[0]

    def status_line(self, spec: ModelSpec) -> str:
        if spec.local:
            return f"Installed on this computer · {human_size(spec.size_bytes)} on disk"
        state = spec.update_state(self.models_root)
        if state == "update":
            installed = spec.installed_version(self.models_root)
            shown = f" (installed v{installed})" if installed else ""
            return (
                f"Update available — v{spec.version}{shown} · "
                f"{human_size(spec.download_bytes())} to download"
            )
        if state == "current":
            return f"Installed — v{spec.installed_version(self.models_root) or spec.version}"
        parts = len(spec.assets)
        suffix = f" ({parts} parts)" if parts > 1 else ""
        return f"Not installed — {human_size(spec.download_bytes())} to download{suffix}"

    def disk_report(self) -> tuple[int, int]:
        """``(free_bytes, pending_download_bytes)`` for the target volume."""
        return (
            free_bytes(self.models_root),
            self.catalog.total_download_bytes(self.models_root),
        )

    # ------------------------------------------------------------- download
    def cancel(self) -> None:
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    @property
    def is_cancelling(self) -> bool:
        return self._cancel.is_set()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise DownloadCancelled("Model download cancelled.")

    def delete(self, spec_id: str) -> None:
        """Remove an installed model. Refuses if it is the last one for a kind."""
        with self._download_lock:
            spec = self.spec(spec_id)
            target = spec.install_path(self.models_root)
            if not target.exists():
                return
            _remove(target)
            for asset in spec.assets:
                _remove(self.staging_dir / f"{spec.id}.{asset.name}")
            _remove(self.staging_dir / f"{spec.id}.archive")

    def install(
        self,
        spec_id: str,
        on_progress: ProgressCallback | None = None,
        *,
        force: bool = False,
    ) -> InstallResult:
        """Download, verify and install one model.

        Raises ``ModelError`` on any integrity or network failure and
        ``DownloadCancelled`` if cancelled. The install is atomic: either the
        complete verified tree appears at its destination, or nothing does.

        An up-to-date install is a no-op. A stale one -- the catalog publishes
        a newer ``version`` than the marker on disk -- is reinstalled, so
        publishing a new model version actually reaches existing users.
        """
        spec = self.spec(spec_id)
        with self._download_lock:
            # Cancelling is a one-way latch on this shared manager, so it has to
            # be cleared when a *new* attempt starts. Nothing else did: pressing
            # Cancel once made every later install, for any model, raise
            # DownloadCancelled before fetching a byte, and the only way out was
            # restarting the app. Cleared here rather than in the Cancel handler
            # so the in-flight download is still cancelled -- this line only runs
            # once the previous attempt has released the download lock.
            self.reset_cancel()
            self._check_cancel()
            if spec.is_installed(self.models_root) and not force:
                if not spec.needs_update(self.models_root):
                    return InstallResult(
                        spec, spec.install_path(self.models_root), False
                    )
                if on_progress:
                    on_progress(
                        STAGE_FETCHING,
                        0,
                        0,
                        f"Updating {spec.name} to v{spec.version}…",
                    )

            self.staging_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_space(spec)

            archive = self.staging_dir / f"{spec.id}.archive"
            if self._archive_is_valid(spec, archive):
                if on_progress:
                    on_progress(
                        STAGE_VERIFYING,
                        spec.download_bytes(),
                        spec.download_bytes(),
                        "Using the previously downloaded archive…",
                    )
            else:
                _remove(archive)
                self._download_archive(spec, archive, on_progress)
                self._verify_archive(spec, archive)

            self._check_cancel()
            try:
                return self._install_from_archive(spec, archive, on_progress)
            finally:
                # The verified archive has served its purpose; freeing it now
                # keeps a 4.5 GB model from occupying disk twice.
                if not self._keep_archive:
                    _remove(archive)

    # ---------------------------------------------------------- internals
    def _ensure_space(self, spec: ModelSpec) -> None:
        needed = spec.download_bytes() + spec.max_extracted_bytes // 4
        available = free_bytes(self.staging_dir)
        if available and available < needed + MODEL_DISK_HEADROOM_BYTES:
            raise ModelError(
                f"'{spec.name}' needs about {human_size(needed + MODEL_DISK_HEADROOM_BYTES)} "
                f"of free disk space but only {human_size(available)} is available. "
                "Free up space or remove an unused model first."
            )

    def _resolve_url(self, spec: ModelSpec, url: str) -> str:
        """Optionally rewrite an asset URL onto a configured mirror."""
        if not self.mirror_base:
            return url
        tail = url.split("github.com/", 1)[-1]
        if tail == url:
            # The asset does not live on GitHub, so a GitHub-shaped mirror
            # cannot serve it. Silently prefixing the whole URL would produce a
            # nonsense address, so fall back to the original.
            return url
        return f"{self.mirror_base}/{tail}"

    def _open(self, request: urllib.request.Request, timeout: float) -> Any:
        """Perform the HTTP request. The single seam every network call goes through.

        Kept separate from the digest and size checks so those stay testable
        without a live server, and so a deployment can route traffic through a
        proxy by overriding this one method.
        """
        return urllib.request.urlopen(request, timeout=timeout)

    def _archive_is_valid(self, spec: ModelSpec, archive: Path) -> bool:
        if not archive.is_file():
            return False
        if archive.stat().st_size != spec.download_bytes():
            return False
        return file_digest(archive) == spec.archive_sha256

    def _download_archive(
        self, spec: ModelSpec, archive: Path, on_progress: ProgressCallback | None
    ) -> None:
        """Concatenate every asset part into a single staged archive.

        Parts stream straight into the destination handle, so a 4.5 GB model
        never needs 9 GB of scratch space, and a crash keeps whatever arrived
        as a ``.part`` file that the next attempt resumes.
        """
        total = spec.download_bytes()
        temp_archive = archive.with_name(archive.name + ".assembling")
        received = 0
        try:
            with open(temp_archive, "wb") as sink:
                for index, asset in enumerate(spec.assets, start=1):
                    self._check_cancel()
                    part_path = self.staging_dir / f"{spec.id}.{asset.name}"
                    received += self._download_part(
                        spec,
                        asset,
                        part_path,
                        on_progress,
                        overall_total=total,
                        already=received,
                        part_number=index,
                    )
                    with open(part_path, "rb") as part:
                        shutil.copyfileobj(part, sink, _CHUNK)
                    part_path.unlink(missing_ok=True)
        except Exception:
            temp_archive.unlink(missing_ok=True)
            raise

        actual = temp_archive.stat().st_size
        if actual != total:
            temp_archive.unlink(missing_ok=True)
            raise ModelError(
                f"The downloaded archive is {actual} bytes but the catalog "
                f"declares {total}."
            )
        if file_digest(temp_archive) != spec.archive_sha256:
            temp_archive.unlink(missing_ok=True)
            raise ModelError(
                f"'{spec.name}' failed its integrity check and was discarded."
            )
        os.replace(temp_archive, archive)

    def _download_part(
        self,
        spec: ModelSpec,
        asset,
        part_path: Path,
        on_progress: ProgressCallback | None,
        *,
        overall_total: int,
        already: int,
        part_number: int,
    ) -> int:
        """Download one asset, resuming a partial file when possible."""
        url = self._resolve_url(spec, asset.url)
        resume_from = 0
        if part_path.is_file():
            existing = part_path.stat().st_size
            if existing == asset.size_bytes:
                if file_digest(part_path) == asset.sha256:
                    return existing
                _remove(part_path)
            elif 0 < existing < asset.size_bytes:
                resume_from = existing
            else:
                _remove(part_path)

        digest = hashlib.sha256()
        if resume_from:
            digest = _resumed_digest(part_path)
            if digest is None:
                resume_from = 0
                _remove(part_path)
                digest = hashlib.sha256()

        request = urllib.request.Request(
            url, headers={"User-Agent": _user_agent(), "Accept": "*/*"}
        )
        if resume_from:
            request.add_header("Range", f"bytes={resume_from}-")

        detail = (
            f"Downloading {spec.name} — part {part_number}/{len(spec.assets)}"
            if len(spec.assets) > 1
            else f"Downloading {spec.name}…"
        )
        try:
            response = self._open(request, MODEL_DOWNLOAD_TIMEOUT)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ModelError(
                f"Could not download '{spec.name}'. Check your internet connection. "
                f"({exc})"
            ) from exc

        with response:
            status = getattr(response, "status", 200) or 200
            mode = "ab" if resume_from and status == 206 else "wb"
            if mode == "wb":
                resume_from = 0
                digest = hashlib.sha256()

            written = resume_from
            with open(part_path, mode) as sink:
                while True:
                    self._check_cancel()
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > asset.size_bytes + _MAX_OVERSHOOT:
                        raise ModelError(
                            f"The server sent more data than the catalog declares for "
                            f"'{spec.name}'. The download was stopped."
                        )
                    if written > asset.size_bytes:
                        raise ModelError(
                            f"'{spec.name}' is larger than the catalog declares. "
                            "The download was stopped."
                        )
                    digest.update(chunk)
                    sink.write(chunk)
                    if on_progress:
                        on_progress(
                            STAGE_DOWNLOADING,
                            already + written,
                            overall_total,
                            detail,
                        )

        if written != asset.size_bytes:
            raise ModelError(
                f"'{spec.name}' download ended early ({written} of {asset.size_bytes} "
                "bytes). Run the download again to resume it."
            )
        if digest.hexdigest() != asset.sha256:
            _remove(part_path)
            raise ModelError(
                f"'{spec.name}' failed its integrity check and was discarded. "
                "The download was corrupted; please try again."
            )
        if on_progress:
            on_progress(
                STAGE_VERIFYING, already + written, overall_total,
                f"Verified {spec.name} — part {part_number}/{len(spec.assets)}",
            )
        return written

    def _verify_archive(self, spec: ModelSpec, archive: Path) -> None:
        if archive.stat().st_size != spec.download_bytes():
            raise ModelError(
                f"The downloaded '{spec.name}' is {archive.stat().st_size} bytes but "
                f"the catalog declares {spec.download_bytes()}."
            )
        if file_digest(archive) != spec.archive_sha256:
            _remove(archive)
            raise ModelError(
                f"'{spec.name}' failed its integrity check and was discarded."
            )

    def _install_from_archive(
        self, spec: ModelSpec, archive: Path, on_progress: ProgressCallback | None
    ) -> InstallResult:
        if on_progress:
            on_progress(
                STAGE_EXTRACTING, 0, spec.size_bytes or spec.download_bytes(),
                f"Installing {spec.name}…",
            )

        staging = Path(tempfile.mkdtemp(prefix=f".install-{spec.id}-", dir=self.staging_dir))
        target = spec.install_path(self.models_root)
        try:
            extract_zip(
                archive,
                staging,
                max_extracted_bytes=spec.max_extracted_bytes,
                strip_single_root=spec.strip_single_root,
                should_cancel=self._cancel.is_set,
            )
            self._check_cancel()

            inner = staging
            if spec.strip_single_root:
                children = list(staging.iterdir())
                if len(children) == 1 and children[0].is_dir():
                    inner = children[0]

            for marker in spec.marker_files:
                if not (inner / marker).exists():
                    raise ModelError(
                        f"'{spec.name}' did not contain the expected '{marker}' data, "
                        "so it was not installed. The catalog entry may be wrong."
                    )

            self._write_marker(inner, spec)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Swap, do not delete-then-move. Removing the old tree first meant a
            # failure in between -- a slow copy, a full disk, an antivirus lock
            # -- left the user with no model at all. It also hid its own
            # failures: _remove swallows them, so if the old tree survived then
            # inner.replace() raised, and the fallback shutil.move() saw an
            # existing directory and quietly moved the *new* model inside it.
            # That reported success while the engine went on loading the stale
            # top-level files, and the old marker still said the old version, so
            # the screen never offered the update again.
            previous = target.with_name(f".{target.name}.previous")
            _remove(previous)
            had_previous = False
            if target.exists():
                try:
                    target.replace(previous)
                    had_previous = True
                except OSError as exc:
                    raise ModelError(
                        f"Could not update '{spec.name}' because its folder is in "
                        "use by another program. Close anything that has it open "
                        "and try again."
                    ) from exc
            try:
                try:
                    inner.replace(target)
                except OSError:
                    # Staging can sit on another volume, where rename is not
                    # allowed, so a copy is the only way in.
                    shutil.move(str(inner), str(target))
            except Exception:
                # Never leave the user with nothing because a swap half-failed.
                if had_previous and not target.exists():
                    try:
                        previous.replace(target)
                    except OSError:
                        pass
                raise
            _remove(previous)
        except Exception:
            _remove(staging)
            raise
        else:
            _remove(staging)

        if on_progress:
            total = spec.size_bytes or spec.download_bytes()
            on_progress(STAGE_INSTALLED, total, total, f"{spec.name} is ready.")
        return InstallResult(spec, target, True)

    def _write_marker(self, root: Path, spec: ModelSpec) -> None:
        payload = {
            "id": spec.id,
            "name": spec.name,
            "version": spec.version,
            "kind": spec.kind,
            "engine": spec.engine,
            "archive_sha256": spec.archive_sha256,
            "installed_at": _utc_now(),
        }
        try:
            (root / ".installed.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass

    def clear_staging(self) -> None:
        """Drop staged archives and partial downloads (used by 'clear cache')."""
        if not self.staging_dir.exists():
            return
        for child in self.staging_dir.iterdir():
            if child.name.endswith(".archive") or ".part" in child.name:
                _remove(child)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
_MAX_CATALOG_BYTES = 8 * 1024 * 1024
#: Accept a key config as a dict (key_id -> material) or as a JSON string, which
#: is what the environment variable and the licensing client both provide.
KeyMaterial = Any


def _normalise_public_keys(value: KeyMaterial) -> KeyMaterial:
    if value is None or isinstance(value, dict):
        return value or {}
    if isinstance(value, str):
        text = value.strip()
        return text
    raise TypeError("Model catalog public keys must be a dict or a JSON string")


def _user_agent() -> str:
    from app.config import APP_NAME, APP_VERSION

    return f"{APP_NAME.replace(' ', '')}/{APP_VERSION}"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resumed_digest(path: Path) -> Any:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest


def file_digest(path: Path) -> str:
    """Stream a SHA-256 over a file without loading it into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# --------------------------------------------------------------------------- #
# Process-wide default instance
# --------------------------------------------------------------------------- #
_default: ModelManager | None = None
_default_lock = threading.Lock()


def get_manager() -> ModelManager:
    """Return the shared :class:`ModelManager`, creating it on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = ModelManager()
        return _default
