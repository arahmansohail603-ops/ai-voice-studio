"""Signed model catalog: the single source of truth for downloadable models.

Every downloadable model is described by a ``ModelSpec`` inside a signed
manifest. The manifest uses the same Ed25519 envelope as the licensing system
(see ``app/licensing/crypto.py``), so the desktop app only ever trusts a
catalog that was signed by a key whose public half is embedded in the build.

Design rules:

* **Fail closed.** Anything unexpected in the manifest (unknown fields, a
  non-HTTPS asset URL, a host that is not allow-listed, a malformed digest, an
  absolute or traversing ``install_dir``) is rejected rather than tolerated.
* **One spec per artifact.** A spec pins the exact version, size and digest of
  every asset, so a download can always be verified byte for byte.
* **Large models are chunked.** A spec may list several ordered ``assets``
  (GitHub caps a single Release asset at 2 GB). The parts are concatenated in
  order and the *joined* stream must match ``archive.sha256``.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import BUNDLED_MODEL_CATALOG, MODEL_PUBLIC_KEYS
from app.core.errors import ModelError
from app.licensing.crypto import b64url_decode, canonical_json, parse_public_keys

try:  # pragma: no cover - exercised implicitly by the test suite
    from cryptography.exceptions import InvalidSignature
except ImportError:  # pragma: no cover
    InvalidSignature = Exception


#: Version of the manifest envelope this build understands.
CATALOG_VERSION = 1

#: A model is one of these logical roles.
MODEL_KINDS = frozenset({"stt", "tts", "translation", "clone"})

#: Only these download hosts are accepted unless a mirror is configured.
DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: Hard ceiling on how large a single decompressed archive may be (16 GiB).
MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024
#: Refuse archives that expand beyond this multiple of their compressed size.
MAX_EXPANSION_RATIO = 200
#: Guard against a manifest that tries to make the app open thousands of sockets.
MAX_ASSETS_PER_SPEC = 64
MAX_MODELS = 256

_ALLOWED_SPEC_FIELDS = frozenset(
    {
        "id",
        "kind",
        "name",
        "version",
        "description",
        "languages",
        "engine",
        "install_dir",
        "size_bytes",
        "requires",
        "marker_files",
        "archive",
        "assets",
        "optional",
    }
)
_ALLOWED_ASSET_FIELDS = frozenset({"name", "url", "size_bytes", "sha256", "part"})
_ALLOWED_ARCHIVE_FIELDS = frozenset(
    {"format", "sha256", "strip_single_root", "max_extracted_bytes"}
)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"Model catalog {label} must be an object.")
    return value


def _reject_unknown(mapping: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ModelError(
            f"Model catalog {label} has unsupported field(s): {', '.join(unknown)}."
        )


def _require_str(mapping: dict[str, Any], field_name: str, label: str) -> str:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"Model catalog {label} is missing '{field_name}'.")
    return value.strip()


def _require_slug(mapping: dict[str, Any], field_name: str, label: str) -> str:
    value = _require_str(mapping, field_name, label)
    if not _SLUG_RE.match(value):
        raise ModelError(
            f"Model catalog {label} has an invalid '{field_name}': {value!r}."
        )
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ModelError(f"Model catalog {label} digest must be a string.")
    digest = value.strip().lower()
    if not _SHA256_RE.match(digest):
        raise ModelError(f"Model catalog {label} has an invalid SHA-256 digest.")
    return digest


def _require_positive_int(value: Any, label: str, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ModelError(f"Model catalog {label} has an invalid '{field_name}'.")
    return value


def _require_str_list(value: Any, label: str, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ModelError(
            f"Model catalog {label} has an invalid '{field_name}' list."
        )
    return tuple(item.strip() for item in value)


def _validate_relative_dir(value: str, label: str) -> str:
    """Accept only a safe, relative, POSIX-style directory path."""
    if not value or value != value.strip():
        raise ModelError(f"Model catalog {label} has an empty 'install_dir'.")
    if "\\" in value:
        raise ModelError(f"Model catalog {label} 'install_dir' must use '/' separators.")
    if value.startswith("/") or ":" in value:
        raise ModelError(f"Model catalog {label} 'install_dir' must be relative.")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ModelError(f"Model catalog {label} 'install_dir' must not traverse upwards.")
    return "/".join(parts)


def _validate_relative_name(value: str, label: str) -> str:
    """Accept only a safe relative file name (used for asset names)."""
    if not value or "/" in value or "\\" in value or value in (".", ".."):
        raise ModelError(f"Model catalog {label} has an unsafe asset name.")
    return value


def _validate_url(url: str, label: str, allowed_hosts: frozenset[str]) -> str:
    if not url:
        raise ModelError(f"Model catalog {label} is missing an asset URL.")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ModelError(f"Model catalog {label} asset URL must use HTTPS.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ModelError(f"Model catalog {label} asset URL has no host.")
    if allowed_hosts and host not in allowed_hosts:
        raise ModelError(
            f"Model catalog {label} asset host '{host}' is not on the allow-list."
        )
    return url


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelAsset:
    """One downloadable file. Multi-part models list several in order."""

    name: str
    url: str
    size_bytes: int
    sha256: str
    part: int = 0

    @classmethod
    def parse(
        cls, raw: Any, label: str, allowed_hosts: frozenset[str]
    ) -> ModelAsset:
        data = _require_mapping(raw, label)
        _reject_unknown(data, _ALLOWED_ASSET_FIELDS, label)
        part = data.get("part", 0)
        if type(part) is not int or part < 0:
            raise ModelError(f"Model catalog {label} has an invalid 'part' index.")
        return cls(
            name=_validate_relative_name(_require_str(data, "name", label), label),
            url=_validate_url(_require_str(data, "url", label), label, allowed_hosts),
            size_bytes=_require_positive_int(
                data.get("size_bytes"), label, "size_bytes"
            ),
            sha256=_require_sha256(data.get("sha256"), label),
            part=part,
        )


@dataclass(frozen=True)
class ModelSpec:
    """Everything the app needs to install and verify one model."""

    id: str
    kind: str
    name: str
    version: str
    install_dir: str
    assets: tuple[ModelAsset, ...] = ()
    archive_sha256: str = ""
    engine: str = ""
    description: str = ""
    languages: tuple[str, ...] = ()
    size_bytes: int = 0
    requires: tuple[str, ...] = ()
    marker_files: tuple[str, ...] = ()
    archive_format: str = "zip"
    strip_single_root: bool = True
    max_extracted_bytes: int = MAX_EXTRACTED_BYTES
    optional: bool = False
    local: bool = False

    @property
    def is_downloadable(self) -> bool:
        """True when a catalog entry can fetch this model from a Release.

        ``parse`` still requires a non-empty asset list, so a signed catalog can
        never publish an entry that only pretends to be installed.
        """
        return bool(self.assets)

    # ----------------------------------------------------------------- paths
    def install_path(self, models_root: Path) -> Path:
        # Catalog entries are validated to be relative and traversal-free, so a
        # signed catalog can never point outside the models folder. Locally
        # discovered folders are the one exception: a Hugging Face cache may
        # legitimately live outside it, so an absolute ``install_dir`` is
        # honoured as-is.
        path = Path(self.install_dir)
        return path if path.is_absolute() else Path(models_root) / path

    def download_bytes(self) -> int:
        return sum(asset.size_bytes for asset in self.assets)

    @property
    def is_chunked(self) -> bool:
        return len(self.assets) > 1

    def is_installed(self, models_root: Path) -> bool:
        """A model counts as installed when its directory and markers exist."""
        target = self.install_path(models_root)
        if not target.is_dir():
            return False
        if not self.marker_files:
            return True
        return all((target / marker).exists() for marker in self.marker_files)

    def install_marker(self, models_root: Path) -> Path:
        """Path of the sentinel file written after a successful install."""
        return self.install_path(models_root) / ".installed.json"

    def read_install_marker(self, models_root: Path) -> dict[str, Any]:
        """Return the recorded install metadata, or an empty dict."""
        marker = self.install_marker(models_root)
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def installed_version(self, models_root: Path) -> str:
        """The version actually on disk, or '' when it was never recorded."""
        recorded = str(self.read_install_marker(models_root).get("version", "") or "")
        if recorded:
            return recorded
        return "local" if self.local else ""

    def needs_update(self, models_root: Path) -> bool:
        """True when the catalog publishes something other than what is on disk.

        An install that predates the marker file, or whose recorded version
        differs from the catalog, counts as outdated: a newer catalog entry is
        the only way a model ever changes, so treating it as up to date would
        leave users permanently stuck on the first version they downloaded.

        Locally discovered folders are never outdated -- the catalog says
        nothing about them, so it has no opinion on their version either.
        """
        if not self.is_installed(models_root):
            return False
        if self.local:
            return False
        installed = self.installed_version(models_root)
        if not installed:
            return True
        return installed != self.version

    def update_state(self, models_root: Path) -> str:
        """'missing' | 'current' | 'update' -- the tri-state the UI renders."""
        if not self.is_installed(models_root):
            return "missing"
        return "update" if self.needs_update(models_root) else "current"

    # --------------------------------------------------------------- parsing
    @classmethod
    def parse(
        cls, raw: Any, label: str, allowed_hosts: frozenset[str]
    ) -> ModelSpec:
        data = _require_mapping(raw, label)
        _reject_unknown(data, _ALLOWED_SPEC_FIELDS, label)

        spec_id = _require_slug(data, "id", label)
        label = f"model '{spec_id}'"

        kind = _require_str(data, "kind", label).lower()
        if kind not in MODEL_KINDS:
            raise ModelError(
                f"Model catalog {label} has an unknown kind '{kind}'. "
                f"Expected one of: {', '.join(sorted(MODEL_KINDS))}."
            )

        raw_assets = data.get("assets")
        if not isinstance(raw_assets, list) or not raw_assets:
            raise ModelError(f"Model catalog {label} lists no downloadable assets.")
        if len(raw_assets) > MAX_ASSETS_PER_SPEC:
            raise ModelError(f"Model catalog {label} lists too many assets.")
        assets = tuple(
            ModelAsset.parse(item, f"{label} asset {index}", allowed_hosts)
            for index, item in enumerate(raw_assets)
        )

        if len({asset.name for asset in assets}) != len(assets):
            raise ModelError(f"Model catalog {label} repeats an asset name.")
        if len(assets) > 1:
            ordered = sorted(assets, key=lambda item: item.part)
            expected = list(range(len(assets)))
            if [asset.part for asset in ordered] != expected:
                raise ModelError(
                    f"Model catalog {label} parts must be numbered 0..{len(assets) - 1} "
                    "with no gaps or duplicates."
                )
            assets = ordered
        elif assets[0].part != 0:
            raise ModelError(f"Model catalog {label} single asset must use part 0.")

        archive = _require_mapping(data.get("archive"), f"{label} archive")
        _reject_unknown(archive, _ALLOWED_ARCHIVE_FIELDS, f"{label} archive")
        archive_format = archive.get("format", "zip")
        if archive_format != "zip":
            raise ModelError(
                f"Model catalog {label} uses unsupported archive format "
                f"{archive_format!r}."
            )

        max_extracted = archive.get("max_extracted_bytes", MAX_EXTRACTED_BYTES)
        if type(max_extracted) is not int or not 0 < max_extracted <= MAX_EXTRACTED_BYTES:
            raise ModelError(f"Model catalog {label} has an invalid extraction cap.")

        strip_root = archive.get("strip_single_root", True)
        if not isinstance(strip_root, bool):
            raise ModelError(f"Model catalog {label} has an invalid 'strip_single_root'.")

        size_bytes = data.get("size_bytes", 0)
        if type(size_bytes) is not int or size_bytes < 0:
            raise ModelError(f"Model catalog {label} has an invalid 'size_bytes'.")

        requires = _require_str_list(data.get("requires"), label, "requires")
        markers = _require_str_list(data.get("marker_files"), label, "marker_files")
        for marker in markers:
            _validate_relative_dir(marker, label)

        return cls(
            id=spec_id,
            kind=kind,
            name=_require_str(data, "name", label),
            version=_require_str(data, "version", label),
            install_dir=_validate_relative_dir(
                _require_str(data, "install_dir", label), label
            ),
            assets=assets,
            archive_sha256=_require_sha256(archive.get("sha256"), f"{label} archive"),
            engine=_require_str(data, "engine", label) if data.get("engine") else "",
            description=(
                data.get("description", "")
                if isinstance(data.get("description"), str)
                else ""
            ),
            languages=_require_str_list(data.get("languages"), label, "languages"),
            size_bytes=size_bytes,
            requires=requires,
            marker_files=markers,
            archive_format=archive_format,
            strip_single_root=strip_root,
            max_extracted_bytes=max_extracted,
            optional=bool(data.get("optional", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Round-trip back to manifest form (used when writing install markers)."""
        archive: dict[str, Any] = {
            "format": self.archive_format,
            "sha256": self.archive_sha256,
            "strip_single_root": self.strip_single_root,
            "max_extracted_bytes": self.max_extracted_bytes,
        }
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "install_dir": self.install_dir,
            "size_bytes": self.size_bytes,
            "archive": archive,
            "assets": [
                {
                    "name": asset.name,
                    "url": asset.url,
                    "size_bytes": asset.size_bytes,
                    "sha256": asset.sha256,
                    "part": asset.part,
                }
                for asset in self.assets
            ],
        }
        if self.engine:
            payload["engine"] = self.engine
        if self.description:
            payload["description"] = self.description
        if self.languages:
            payload["languages"] = list(self.languages)
        if self.requires:
            payload["requires"] = list(self.requires)
        if self.marker_files:
            payload["marker_files"] = list(self.marker_files)
        if self.optional:
            payload["optional"] = True
        return payload


# --------------------------------------------------------------------------- #
# Manifest envelope
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelCatalog:
    """A verified, immutable set of model specs."""

    models: tuple[ModelSpec, ...]
    generated_at: str = ""
    key_id: str = ""
    source: str = "bundled"
    _by_id: dict[str, ModelSpec] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self._by_id:
            object.__setattr__(
                self, "_by_id", {spec.id: spec for spec in self.models}
            )

    def get(self, spec_id: str) -> ModelSpec | None:
        return self._by_id.get(spec_id)

    def require(self, spec_id: str) -> ModelSpec:
        spec = self.get(spec_id)
        if spec is None:
            raise ModelError(f"Model '{spec_id}' is not in the catalog.")
        return spec

    def by_kind(self, kind: str) -> tuple[ModelSpec, ...]:
        wanted = kind.strip().lower()
        return tuple(spec for spec in self.models if spec.kind == wanted)

    def find(
        self,
        *,
        kind: str = "",
        engine: str = "",
        language: str = "",
    ) -> ModelSpec | None:
        """Best-effort lookup: exact language match first, then any match."""
        candidates = self.models
        if kind:
            candidates = tuple(s for s in candidates if s.kind == kind.strip().lower())
        if engine:
            wanted = engine.strip().lower()
            candidates = tuple(s for s in candidates if s.engine.lower() == wanted)
        if not candidates:
            return None
        if language:
            primary = language.split("-")[0].lower()
            for spec in candidates:
                for tag in spec.languages:
                    tag_primary = tag.split("-")[0].lower()
                    if tag.lower() == language.lower() or tag_primary == primary:
                        return spec
        return candidates[0]

    def installed(self, models_root: Path) -> tuple[ModelSpec, ...]:
        return tuple(spec for spec in self.models if spec.is_installed(models_root))

    def total_download_bytes(self, models_root: Path) -> int:
        """Bytes still to fetch: missing models plus outdated installs."""
        return sum(
            spec.download_bytes()
            for spec in self.models
            if not spec.optional and spec.update_state(models_root) != "current"
        )

    def outdated(self, models_root: Path) -> tuple[ModelSpec, ...]:
        """Models installed at a version the catalog has moved past."""
        return tuple(
            spec for spec in self.models if spec.update_state(models_root) == "update"
        )

    def _index(self) -> ModelCatalog:
        """Force the id index to be rebuilt (kept for symmetry with construction)."""
        return ModelCatalog(
            models=self.models,
            generated_at=self.generated_at,
            key_id=self.key_id,
            source=self.source,
        )


def parse_catalog(
    payload: Any,
    *,
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
    key_id: str = "",
    generated_at: str = "",
    source: str = "bundled",
) -> ModelCatalog:
    """Validate a *manifest payload* (already signature-checked)."""
    data = _require_mapping(payload, "payload")
    if data.get("type") != "model_catalog":
        raise ModelError("The model catalog has an unexpected type.")
    if data.get("version") != CATALOG_VERSION:
        raise ModelError(
            f"Unsupported model catalog version {data.get('version')!r}; "
            f"this build understands version {CATALOG_VERSION}."
        )
    raw_models = data.get("models")
    if not isinstance(raw_models, list):
        raise ModelError("The model catalog does not contain a 'models' list.")
    if len(raw_models) > MAX_MODELS:
        raise ModelError(f"The model catalog is too large ({len(raw_models)} models).")

    models: list[ModelSpec] = []
    for index, raw in enumerate(raw_models):
        spec = ModelSpec.parse(raw, f"model #{index}", allowed_hosts)
        if spec.id in {existing.id for existing in models}:
            raise ModelError(f"The model catalog repeats the id '{spec.id}'.")
        models.append(spec)

    return ModelCatalog(
        models=tuple(models),
        generated_at=generated_at if isinstance(generated_at, str) else "",
        key_id=key_id,
        source=source,
    )._index()


def verify_catalog(
    envelope: Any,
    public_keys: Any,
    *,
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
    source: str = "remote",
) -> ModelCatalog:
    """Verify an Ed25519-signed manifest envelope and parse its payload.

    Mirrors ``app.licensing.crypto.verify_lease``: only the canonical JSON of the
    payload is signed, and the signing key must be one we already trust.
    """
    data = _require_mapping(envelope, "envelope")
    if (
        data.get("algorithm") != "Ed25519"
        or data.get("encoding") != "canonical-json"
    ):
        raise ModelError("Unsupported model catalog envelope format.")
    key_id = data.get("key_id")
    signature = data.get("signature")
    payload = data.get("payload")
    if (
        not isinstance(key_id, str)
        or not key_id
        or not isinstance(signature, str)
        or not signature
        or not isinstance(payload, dict)
    ):
        raise ModelError("The model catalog envelope is incomplete.")

    keys = parse_public_keys(public_keys)
    public_key = keys.get(key_id)
    if public_key is None:
        raise ModelError("The model catalog was signed by an unknown key.")
    try:
        public_key.verify(
            b64url_decode(signature), canonical_json(payload).encode("utf-8")
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ModelError("The model catalog signature is invalid.") from exc

    return parse_catalog(
        payload,
        allowed_hosts=allowed_hosts,
        key_id=key_id,
        generated_at=str(payload.get("generated_at", "")),
        source=source,
    )


def sign_catalog(payload: dict[str, Any], private_key: Any, key_id: str) -> dict[str, Any]:
    """Sign a catalog payload, returning a complete envelope.

    Used by the offline publishing tooling (``tools/sign_manifest.py``); the
    desktop app only ever verifies.
    """
    if not isinstance(payload, dict):
        raise ModelError("The model catalog payload must be an object.")
    if not isinstance(key_id, str) or not key_id.strip():
        raise ModelError("A signing key id is required.")
    if private_key is None:
        raise ModelError("A private signing key is required.")
    signature = private_key.sign(canonical_json(payload).encode("utf-8"))
    return {
        "algorithm": "Ed25519",
        "encoding": "canonical-json",
        "key_id": key_id.strip(),
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        "payload": payload,
    }


def load_bundled_catalog(path: Path | None = None) -> ModelCatalog:
    """Load the catalog shipped inside the app.

    A signed bundled catalog is still verified against the configured trust
    keys. When no model public key is configured the bundled copy cannot be
    verified, so it is treated as untrusted-but-valid: schema validation only,
    and callers must not serve it to users who have a configured key.
    """
    target = Path(path) if path is not None else BUNDLED_MODEL_CATALOG
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ModelCatalog((), source="bundled")
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelError(f"The bundled model catalog is unreadable: {exc}") from exc

    if isinstance(raw, dict) and raw.get("payload") is not None:
        if not MODEL_PUBLIC_KEYS:
            payload = raw.get("payload")
            if isinstance(payload, dict):
                return parse_catalog(payload, source="bundled")
            raise ModelError("The bundled model catalog envelope is incomplete.")
        return verify_catalog(
            raw, public_keys=MODEL_PUBLIC_KEYS, source="bundled"
        )
    return parse_catalog(raw, source="bundled")
