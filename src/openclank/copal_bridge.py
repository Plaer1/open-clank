"""Owned stdio bridge to Copal's Redb source of truth."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_CRATE = _ROOT / "packages" / "Copal" / "rust" / "copal-db"
_DEFAULT_COMMAND = _CRATE / "target" / "release" / "copal-bridge"
_DEFAULT_DATA = _ROOT / "packages" / "Copal" / "db"
_BRIDGE_STREAM_LIMIT = 32 * 1024 * 1024


class CopalBridgeError(RuntimeError):
    pass


class CopalBridge:
    def __init__(self, command: str | Path | None = None, data_dir: str | Path | None = None):
        self.command = Path(command or os.environ.get("COPAL_BRIDGE_COMMAND", _DEFAULT_COMMAND)).expanduser()
        self.data_dir = Path(data_dir or os.environ.get("COPAL_DATA_DIR", _DEFAULT_DATA)).expanduser()
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._request_id = 0
        self._stderr_task: asyncio.Task | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self.is_alive() else None

    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _build(self) -> None:
        if self.command.is_file():
            return

        def build() -> None:
            result = subprocess.run(
                ["cargo", "build", "--release", "--bin", "copal-bridge"],
                cwd=_CRATE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0:
                raise CopalBridgeError(f"Copal bridge build failed: {result.stdout[-2000:]}")

        await asyncio.to_thread(build)
        if not self.command.is_file():
            raise CopalBridgeError(f"Copal bridge binary missing after build: {self.command}")

    async def start(self) -> None:
        async with self._start_lock:
            if self.is_alive():
                return
            await self._build()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["COPAL_DATA_DIR"] = str(self.data_dir)
            # Wiki store lives beside the notes store in the same data dir.
            env["COPAL_WIKI_DATA_DIR"] = str(self.data_dir)
            self._process = await asyncio.create_subprocess_exec(
                str(self.command),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=_BRIDGE_STREAM_LIMIT,
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            await self.call("status", timeout=10, _ensure_started=False)
            logger.info("Copal Redb bridge started pid=%s", self.pid)

    async def _drain_stderr(self) -> None:
        process = self._process
        if not process or not process.stderr:
            return
        while line := await process.stderr.readline():
            logger.debug("[copal-bridge] %s", line.decode(errors="replace").rstrip())

    async def call(
        self,
        operation: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = 20,
        _ensure_started: bool = True,
    ) -> Any:
        while True:
            if _ensure_started:
                await self.start()
            async with self._lock:
                process = self._process
                if not process or not process.stdin or not process.stdout or process.returncode is not None:
                    if _ensure_started:
                        continue
                    raise CopalBridgeError("Copal bridge is not running")
                self._request_id += 1
                request_id = self._request_id
                payload = json.dumps(
                    {"id": request_id, "op": operation, "args": args or {}},
                    separators=(",", ":"),
                ).encode() + b"\n"
                try:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
                    if not line:
                        raise CopalBridgeError("Copal bridge closed its output")
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise CopalBridgeError("Copal bridge returned invalid JSON") from exc
                    if response.get("id") != request_id:
                        raise CopalBridgeError("Copal bridge response was out of sequence")
                except BaseException:
                    # Once a request may have reached the child, a timeout or caller
                    # cancellation leaves its eventual line ambiguous. Retire that
                    # process before another call so responses can never be paired
                    # with the wrong request. Redb rolls back an interrupted write;
                    # a just-committed import is safe to retry because it is idempotent.
                    await self._retire_process(process, graceful=False)
                    raise
                if not response.get("ok"):
                    raise CopalBridgeError(str(response.get("error") or "Copal operation failed"))
                return response.get("result")

    async def _retire_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        graceful: bool,
    ) -> None:
        if self._process is process:
            self._process = None
        if process and process.returncode is None:
            if graceful and process.stdin:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    process.stdin.close()
            elif not graceful:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self._stderr_task:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def stop(self) -> None:
        process = self._process
        if process:
            await self._retire_process(process, graceful=True)
        logger.info("Copal Redb bridge stopped")
