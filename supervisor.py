"""
Process supervisor: one async wrapper around any long-running CLI radio tool
(ubertooth-specan, ubertooth-btle, ...).

Efficiency module #2 — write the process lifecycle ONCE, reuse everywhere.
Handles: non-blocking line-by-line stdout streaming, clean SIGTERM-then-SIGKILL
shutdown, and state tracking. Every subprocess-based data source is built on
this instead of re-implementing subprocess plumbing.
"""
from __future__ import annotations

import asyncio
import signal
from enum import Enum
from typing import Callable, Optional, Sequence


class State(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    ERROR = "error"


class Process:
    def __init__(self, argv: Sequence[str], on_line: Callable[[str], None]) -> None:
        self.argv = list(argv)
        self.on_line = on_line
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.state: State = State.STOPPED
        self.error: str = ""
        self._pump_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.state is State.RUNNING:
            return
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            self.state = State.ERROR
            self.error = f"binary not found: {self.argv[0]}"
            raise
        self.state = State.RUNNING
        self.error = ""
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    self.on_line(line)
        except asyncio.CancelledError:
            raise
        finally:
            rc = await self.proc.wait() if self.proc else None
            if self.state is State.RUNNING and rc not in (0, None, -signal.SIGTERM):
                self.state = State.ERROR
                self.error = f"exited rc={rc}"

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._pump_task:
            self._pump_task.cancel()
        self.state = State.STOPPED
