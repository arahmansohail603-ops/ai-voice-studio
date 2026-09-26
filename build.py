from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
MODEL_RESOURCES = ROOT / "app" / "resources"
ICON = ASSETS / "app.ico"
BUILD_LICENSE_MODULE = ROOT / "app" / "_build_license_config.py"
KEYRING_DISTRIBUTION = "keyring"
KEYRING_SMOKE_ACCOUNT = "license-store-key-v1"
KEYRING_MODULES = (
    "keyring",
    "keyring.backends",
    "keyring.backends.Windows",
    "keyring.backends.chainer",
    "keyring.backends.fail",
    "keyring.backends.kwallet",
    "keyring.backends.libsecret",
    "keyring.backends.macOS",
    "keyring.backends.null",
    "keyring.backends.SecretService",
    "win32ctypes",
    "win32ctypes.core",
    "win32ctypes.pywin32",
)
WIN32CTYPES_BACKENDS = ("win32ctypes.core.ctypes", "win32ctypes.core.cffi")
PYWIN32_MODULES = ("pywintypes", "win32api", "win32cred", "win32timezone")
PLATFORM_KEYRING_MODULES = {
    "win32": ("keyring.backends.Windows", "win32ctypes"),
    "darwin": ("keyring.backends.macOS",),
    "linux": ("keyring.backends.SecretService",),
}
DIST_SUFFIXES = (".pyd", ".so", ".dylib", ".pyc", ".py")


def _decode_public_key(material: object) -> None:
    if not isinstance(material, str) or not material.strip():
        raise SystemExit("Each built public key must be a 32-byte Ed25519 key")
    value = material.strip()
    if any(token in value.lower() for token in ("<", ">", "private", "placeholder")):
        raise SystemExit("Replace placeholder values before building")
    candidates: list[bytes] = []
    try:
        candidates.append(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (binascii.Error, ValueError):
        pass
    try:
        candidates.append(
            base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
        )
    except (binascii.Error, ValueError):
        pass
    try:
        if len(value) % 2 == 0:
            candidates.append(bytes.fromhex(value))
    except ValueError:
        pass
    if not any(len(candidate) == 32 for candidate in candidates):
        raise SystemExit("Each built public key must decode to 32 Ed25519 bytes")


def _license_values() -> tuple[str, str]:
    url = os.environ.get("AI_VOICE_STUDIO_LICENSE_URL", "").strip().rstrip("/")
    public_keys = os.environ.get("AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS", "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SystemExit(
            "Set a valid AI_VOICE_STUDIO_LICENSE_URL before building"
        ) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("Set AI_VOICE_STUDIO_LICENSE_URL before building")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise SystemExit(
            "The built license server URL must not contain credentials or query data"
        )
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise SystemExit("The built license server URL has an invalid host") from exc
    if parsed.scheme != "https" and hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise SystemExit("The built license server URL must use HTTPS")
    try:
        parsed_keys = json.loads(public_keys)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS must be valid JSON"
        ) from exc
    if not isinstance(parsed_keys, (dict, list)) or not parsed_keys:
        raise SystemExit("The built public keyring must contain at least one key")
    if isinstance(parsed_keys, dict):
        items = parsed_keys.items()
    else:
        items = (
            (item.get("key_id"), item.get("public_key"))
            for item in parsed_keys
            if isinstance(item, dict)
        )
    valid_items = [
        (key_id, material)
        for key_id, material in items
        if isinstance(key_id, str) and key_id.strip() and material is not None
    ]
    if not valid_items or len(valid_items) != len(parsed_keys):
        raise SystemExit("The built public keyring has an invalid key entry")
    for _key_id, material in valid_items:
        _decode_public_key(material)
    return url, public_keys


def _write_license_module(url: str, public_keys: str) -> None:
    if BUILD_LICENSE_MODULE.exists():
        raise SystemExit(f"Refusing to overwrite existing {BUILD_LICENSE_MODULE.name}")
    BUILD_LICENSE_MODULE.write_text(
        f"BUILD_LICENSE_CONFIGURED = True\n"
        f"LICENSE_SERVER_URL = {url!r}\n"
        f"LICENSE_PUBLIC_KEYS = {public_keys!r}\n",
        encoding="utf-8",
    )


def build_target() -> str:
    return "AI Voice Studio.exe" if os.name == "nt" else "AI Voice Studio"


def _module_spec(name: str):
    try:
        return importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        return None


def _module_available(name: str) -> bool:
    return _module_spec(name) is not None


def _available_modules(names: tuple[str, ...]) -> list[str]:
    return [name for name in names if _module_available(name)]


def _distribution_available(name: str) -> bool:
    try:
        from importlib import metadata
    except ImportError:
        return False
    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return False
    return True


def _keyring_modules() -> list[str]:
    if not _module_available(KEYRING_DISTRIBUTION):
        raise SystemExit(
            "keyring is required to build the license store; "
            "install requirements-build.txt in the build environment"
        )
    modules = _available_modules(KEYRING_MODULES)
    if os.name == "nt":
        if _module_available("win32ctypes"):
            if _module_available("cffi"):
                modules.extend(("win32ctypes.core.cffi", "cffi"))
            else:
                modules.append("win32ctypes.core.ctypes")
        else:
            modules.extend(_available_modules(PYWIN32_MODULES))
        modules.extend(_available_modules(("win32timezone",)))
    return modules


def _keyring_options(modules: list[str]) -> list[str]:
    options = []
    for name in modules:
        spec = _module_spec(name)
        kind = (
            "package"
            if spec is not None and spec.submodule_search_locations
            else "module"
        )
        options.append(f"--include-{kind}={name}")
    if _distribution_available(KEYRING_DISTRIBUTION):
        options.append(f"--include-distribution-metadata={KEYRING_DISTRIBUTION}")
    return options


def _smoke_keyring() -> str:
    try:
        import keyring
        import keyring.backend
        from keyring.backends import fail, null
    except ImportError as exc:
        raise SystemExit(f"keyring cannot be imported: {exc}") from exc
    try:
        selected = keyring.get_keyring()
    except Exception as exc:
        raise SystemExit(f"keyring cannot initialise a backend: {exc}") from exc
    if isinstance(selected, (fail.Keyring, null.Keyring)):
        detected = ", ".join(
            sorted(
                f"{type(ring).__module__}.{type(ring).__name__}"
                for ring in keyring.backend.get_all_keyring()
            )
        )
        raise SystemExit(
            "keyring has no operating-system backend"
            f" (detected: {detected or 'none'}); the executable could not "
            "protect the local license store"
        )
    service = f"AI Voice Studio build check {secrets.token_hex(8)}"
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    try:
        keyring.set_password(service, KEYRING_SMOKE_ACCOUNT, secret)
        stored = keyring.get_password(service, KEYRING_SMOKE_ACCOUNT)
    except Exception as exc:
        raise SystemExit(
            f"the {type(selected).__name__} keyring backend rejected "
            f"the build check: {exc}"
        ) from exc
    if stored != secret:
        raise SystemExit(
            f"the {type(selected).__name__} keyring backend did not keep "
            "the build check secret"
        )
    with contextlib.suppress(Exception):
        keyring.delete_password(service, KEYRING_SMOKE_ACCOUNT)
    return f"{type(selected).__module__}.{type(selected).__name__}"


def _dist_module_present(dist: Path, module: str) -> bool:
    parts = module.split(".")
    candidates = [Path(*parts[:-1], f"{parts[-1]}{suffix}") for suffix in DIST_SUFFIXES]
    candidates.extend(Path(f"{module}{suffix}") for suffix in DIST_SUFFIXES)
    candidates.append(Path(*parts[:-1], parts[-1]))
    candidates.append(Path(module))
    return any((dist / candidate).exists() for candidate in candidates)


def _dist_metadata_present(dist: Path, name: str) -> bool:
    return any(dist.glob(f"**/{name}-*.dist-info"))


def _standalone_dist(output_dir: Path) -> Path | None:
    candidates = [path for path in output_dir.glob("*.dist") if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _verify_keyring_packaging(output_dir: Path, onefile: bool) -> None:
    if onefile:
        print(
            "Keyring packaging check skipped: --onefile packs the standalone folder "
            "into the executable"
        )
        return
    dist = _standalone_dist(output_dir)
    if dist is None:
        raise SystemExit(f"no standalone build folder was found in {output_dir}")
    required = [KEYRING_DISTRIBUTION, "keyring.backends"]
    required.extend(PLATFORM_KEYRING_MODULES.get(sys.platform, ()))
    missing = [name for name in required if not _dist_module_present(dist, name)]
    if sys.platform == "win32" and not any(
        _dist_module_present(dist, name) for name in WIN32CTYPES_BACKENDS
    ):
        missing.append("win32ctypes.core")
    if missing:
        raise SystemExit(
            f"the standalone build in {dist} is missing keyring modules: "
            + ", ".join(missing)
        )
    if not _dist_metadata_present(dist, KEYRING_DISTRIBUTION):
        print(
            "Warning: the standalone folder has no keyring distribution metadata; "
            "the keyring backends may not be discovered at runtime"
        )


def _nuitka_command(
    output_dir: Path,
    include_optional: bool,
    keyring_options: list[str],
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--remove-output",
        "--no-pyi-file",
        "--include-package=app",
        "--include-module=app._build_license_config",
        # Model catalog + downloader are always compiled in; they are pure
        # stdlib, so they add no dependency weight.
        "--include-module=app.core.model_catalog",
        "--include-module=app.core.model_manager",
        "--include-qt-plugins",
        f"--output-dir={output_dir}",
        f"--output-filename={build_target()}",
        "--company-name=AI Voice Studio",
        "--product-name=AI Voice Studio",
        "--file-description=AI Voice Studio",
    ]
    command.extend(keyring_options)
    if include_optional:
        for module in (
            "argostranslate",
            "piper",
            "qwen_tts",
            "torch",
            "TTS",
            "vosk",
            "whisper",
        ):
            if _module_available(module):
                command.append(f"--include-package={module}")
    if ASSETS.is_dir():
        command.append(f"--include-data-dir={ASSETS}=assets")
    # The bundled (development) model catalog is read at runtime via
    # app.config.BUNDLED_MODEL_CATALOG, so it must ship inside app/resources/.
    if MODEL_RESOURCES.is_dir():
        command.append(
            f"--include-data-dir={MODEL_RESOURCES}=app/resources"
        )
    return command


def _run_nuitka(command: list[str]) -> None:
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Nuitka build failed with exit code {exc.returncode}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the protected desktop application with Nuitka"
    )
    parser.add_argument(
        "--onefile", action="store_true", help="Create a single-file executable"
    )
    parser.add_argument(
        "--output-dir", default="dist", help="Directory receiving the build"
    )
    parser.add_argument(
        "--include-optional",
        action="store_true",
        help="Include installed local AI, ASR, and translation packages",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run the keyring checks against an existing build without compiling",
    )
    parser.add_argument(
        "--skip-keyring-check",
        action="store_true",
        help="Skip the post-build keyring backend and packaging checks",
    )
    args = parser.parse_args()

    keyring_modules = _keyring_modules()
    output_dir = (ROOT / args.output_dir).resolve()
    if args.check_only:
        print(f"Checking the existing build in {output_dir}")
    else:
        license_url, public_keys = _license_values()
        output_dir.mkdir(parents=True, exist_ok=True)
        command = _nuitka_command(
            output_dir, args.include_optional, _keyring_options(keyring_modules)
        )
        if args.onefile:
            command.append("--onefile")
        if os.name == "nt" and ICON.is_file():
            command.append(f"--windows-icon-from-ico={ICON}")
        if os.name == "nt":
            command.append("--windows-console-mode=disable")
        command.append(str(ROOT / "main.py"))
        _write_license_module(license_url, public_keys)
        try:
            _run_nuitka(command)
        finally:
            BUILD_LICENSE_MODULE.unlink(missing_ok=True)
    if not args.skip_keyring_check:
        print(f"Keyring backend check passed: {_smoke_keyring()}")
        _verify_keyring_packaging(output_dir, args.onefile)
    print(f"Build output: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
