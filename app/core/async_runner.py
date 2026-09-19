"""A dedicated asyncio event loop running in a background thread.

The GUI runs on the main thread (tkinter/customtkinter) and must never block.
Latency-free async libraries such as ``edge-tts`` are driven on this loop so
screens can ``await`` their results without freezing the interface.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Awaitable, Optional


class AsyncRunner:
    """Owns one asyncio event loop in a daemon thread."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self._ensure_started()
        return self._loop  # type: ignore[return-value]

    def _ensure_started(self) -> None:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(loop,),
                name="async-runner",
                daemon=True,
            )
            self._thread.start()

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def run(self, coro: Awaitable) -> Future:
        """Schedule a coroutine on the background loop and return its Future."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future

    def stop(self) -> None:
        with self._lock:
            loop = self._loop
            if loop is None or not loop.is_running():
                return

            def _shutdown() -> None:
                for task in asyncio.all_tasks(loop):
                    task.cancel()
                loop.call_later(0.15, loop.stop)

            loop.call_soon_threadsafe(_shutdown)
