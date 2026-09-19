"""AI Voice Studio — application entry point.

Run with:  python main.py
"""
from __future__ import annotations

import sys

from app import config


def _preflight() -> bool:
    """Friendly error when the very first (GUI) dependency is missing."""
    try:
        import customtkinter  # noqa: F401
        return True
    except ImportError:
        print(
            f"{config.APP_NAME} needs 'customtkinter' to start.\n\n"
            "Install the dependencies first:\n\n"
            "    pip install -r requirements.txt\n\n"
            "Then run:\n\n"
            "    python main.py\n",
            file=sys.stderr,
        )
        return False


def main() -> None:
    if not _preflight():
        sys.exit(1)

    from app.services.file_service import ensure_dirs

    ensure_dirs()

    from app.gui.app import run

    try:
        run()
    except Exception as exc:  # noqa: BLE001
        from app.core.errors import AppError, MissingDependencyError

        if isinstance(exc, MissingDependencyError):
            print(str(exc), file=sys.stderr)
        elif isinstance(exc, AppError):
            print(f"{config.APP_NAME} error: {exc}", file=sys.stderr)
        else:
            import traceback

            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
