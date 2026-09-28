from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from .crypto import canonical_json, constant_time_equal, digest_bytes
from .errors import LicenseConfigurationError, LicenseStoreError

_MAGIC = b"AVSL1\0"
_NONCE_SIZE = 12
_DIGEST_SIZE = hashlib.sha256().digest_size
_MAX_STORE_SIZE = 4 * 1024 * 1024


def license_store_path() -> Path:
    override = os.environ.get("AI_VOICE_STUDIO_LICENSE_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "AI Voice Studio" / "license.dat"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "AI Voice Studio"
            / "license.dat"
        )
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "ai-voice-studio" / "license.dat"


def _decode_key_material(material: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(material + "=" * (-len(material) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("The license keystore returned invalid key material") from exc
    if len(key) != 32:
        raise ValueError("The license keystore returned invalid key material")
    return key


def _default_key_provider() -> bytes:
    try:
        import keyring
    except ImportError as exc:
        raise LicenseConfigurationError(
            "keyring is required to protect the local license store"
        ) from exc
    service = os.environ.get("AI_VOICE_STUDIO_KEYRING_SERVICE", "AI Voice Studio")
    account = os.environ.get("AI_VOICE_STUDIO_KEYRING_ACCOUNT", "license-store-key-v1")
    try:
        material = keyring.get_password(service, account)
        if material:
            return _decode_key_material(material)
        key = os.urandom(32)
        stored = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
        keyring.set_password(service, account, stored)
        confirmed = keyring.get_password(service, account)
        if not confirmed:
            raise ValueError("The operating-system keystore did not persist the key")
        return _decode_key_material(confirmed)
    except LicenseConfigurationError:
        raise
    except Exception as exc:
        raise LicenseConfigurationError(
            "The operating-system keystore is unavailable; "
            "license storage cannot be protected"
        ) from exc


class EncryptedLicenseStore:
    def __init__(
        self,
        path: str | Path | None = None,
        key_provider: Callable[[], bytes] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else license_store_path()
        self._key_provider = key_provider or _default_key_provider

    def exists(self) -> bool:
        return self.path.is_file()

    def _key(self) -> bytes:
        try:
            key = self._key_provider()
        except LicenseConfigurationError:
            raise
        except Exception as exc:
            # A keystore that cannot be reached is a configuration problem, not
            # a damaged store, and load() and save() have to say the same thing
            # about it. Normalised here so both callers report it identically
            # and neither leaks a bare provider error to the activation dialog.
            raise LicenseConfigurationError(
                "The operating-system keystore is unavailable; "
                "license storage cannot be protected"
            ) from exc
        if not isinstance(key, bytes) or len(key) != 32:
            raise LicenseConfigurationError(
                "The license keystore returned an invalid key"
            )
        return key

    def load(self) -> dict | None:
        if not self.exists():
            return None
        try:
            if self.path.stat().st_size > _MAX_STORE_SIZE:
                raise ValueError("file is too large")
            raw = self.path.read_bytes()
            if len(raw) > _MAX_STORE_SIZE:
                raise ValueError("file is too large")
            if len(raw) < len(_MAGIC) + _NONCE_SIZE + _DIGEST_SIZE + 16:
                raise ValueError("file is truncated")
            if raw[: len(_MAGIC)] != _MAGIC:
                raise ValueError("file header is invalid")
            digest_offset = len(_MAGIC)
            nonce_offset = digest_offset + _DIGEST_SIZE
            cipher_offset = nonce_offset + _NONCE_SIZE
            digest = raw[digest_offset:nonce_offset]
            body = raw[:digest_offset] + raw[nonce_offset:]
            if not constant_time_equal(digest, digest_bytes(body)):
                raise ValueError("file hash does not match")
            nonce = raw[nonce_offset:cipher_offset]
            ciphertext = raw[cipher_offset:]
            # Resolved before the decrypt handler on purpose. A keystore that is
            # unavailable, or that returned a key of the wrong length, raises
            # LicenseConfigurationError("...keystore..."), which is actionable.
            # Inside the handler below it was swallowed by the broad except and
            # rewritten as "the store is missing or has been modified" -- which
            # points the user at deleting license.dat, the exact wrong remedy,
            # and contradicts save(), which reports the real cause for the same
            # broken keystore.
            key = self._key()
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                plaintext = AESGCM(key).decrypt(nonce, ciphertext, _MAGIC)
            except ImportError as exc:
                raise LicenseConfigurationError(
                    "cryptography is required to read the local license store"
                ) from exc
            except Exception as exc:
                raise ValueError("license store authentication failed") from exc
            value = json.loads(plaintext.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("license store payload is invalid")
            return value
        except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
            raise LicenseStoreError(
                "The local license store is missing or has been modified"
            ) from exc

    def save(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise LicenseStoreError("License state is invalid")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            nonce = os.urandom(_NONCE_SIZE)
            plaintext = canonical_json(state).encode("utf-8")
            ciphertext = AESGCM(self._key()).encrypt(nonce, plaintext, _MAGIC)
            body = (
                _MAGIC + digest_bytes(_MAGIC + nonce + ciphertext) + nonce + ciphertext
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix="license-", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except LicenseStoreError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise LicenseStoreError(
                "Could not write the protected license store"
            ) from exc

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise LicenseStoreError("Could not remove the local license store") from exc
