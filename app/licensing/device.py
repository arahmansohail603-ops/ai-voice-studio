from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import uuid
from pathlib import Path

_NAMESPACE = "ai-voice-studio-device-v1"


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _windows_machine_id() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip()
    except (OSError, ImportError):
        return ""


def _mac_machine_id() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        for line in result.stdout.splitlines():
            if "IOPlatformUUID" in line and "=" in line:
                return line.split("=", 1)[1].strip().strip('"')
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _linux_machine_id() -> str:
    if not sys.platform.startswith("linux"):
        return ""
    return _read_text("/etc/machine-id") or _read_text("/var/lib/dbus/machine-id")


def _fallback_identity() -> str:
    node = platform.node().strip()
    mac = uuid.getnode()
    return f"{node}|{mac:012x}"


def _fallback_path() -> Path:
    override = os.environ.get("AI_VOICE_STUDIO_DEVICE_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    root = os.environ.get("AI_VOICE_STUDIO_DATA", "").strip()
    base = Path(root).expanduser() if root else Path.home() / "AI Voice Studio Data"
    return base / ".device-id"


def _persisted_fallback() -> str:
    path = _fallback_path()
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        value = ""
    if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
        return value
    source = _fallback_identity() or uuid.uuid4().hex
    value = hashlib.sha256(f"{_NAMESPACE}|{source}".encode()).hexdigest()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="ascii")
        if os.name != "nt":
            path.chmod(0o600)
    except OSError:
        pass
    return value


def get_device_id() -> str:
    source = (_windows_machine_id() or _mac_machine_id() or _linux_machine_id()).strip()
    if not source:
        return _persisted_fallback()
    return hashlib.sha256(f"{_NAMESPACE}|{source}".encode()).hexdigest()
