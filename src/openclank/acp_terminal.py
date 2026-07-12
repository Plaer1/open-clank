"""Bounded owner/session-scoped ACP terminal callbacks."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)\s*[:=]\s*\S+"
)
_ENV_ALLOW = {"COLORTERM", "LANG", "LC_ALL", "NO_COLOR", "TERM"}


@dataclass
class _Terminal:
    terminal_id: str
    session_id: str
    process: asyncio.subprocess.Process
    byte_limit: int
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    reader: asyncio.Task | None = None
    reaper: asyncio.Task | None = None


class ACPTerminalManager:
    def __init__(self, context_resolver: Callable[[str], dict]) -> None:
        self._context_resolver = context_resolver
        self._terminals: dict[str, _Terminal] = {}
        self._lock = asyncio.Lock()

    def _context(self, session_id: str) -> tuple[dict, Path]:
        context = self._context_resolver(session_id) or {}
        owner = str(context.get("owner") or "")
        if context.get("incognito") or (owner and not context.get("is_admin")):
            raise PermissionError("ACP terminal is forbidden by the active policy")
        workspace = str(context.get("workspace") or "")
        if not workspace:
            raise PermissionError("ACP terminal requires an active workspace")
        root = Path(workspace).expanduser().resolve()
        from src.tool_execution import vet_workspace

        if vet_workspace(str(root)) is None:
            raise PermissionError("ACP terminal workspace is not permitted")
        return context, root

    @staticmethod
    def _cwd(raw: Any, root: Path) -> Path:
        cwd = Path(str(raw or root)).expanduser().resolve()
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise PermissionError("ACP terminal cwd escapes the active workspace") from exc
        if not cwd.is_dir():
            raise ValueError("ACP terminal cwd does not exist")
        return cwd

    async def create(self, params: dict) -> dict:
        session_id = str(params.get("sessionId") or "")
        _, root = self._context(session_id)
        command = str(params.get("command") or "").strip()
        args = params.get("args") or []
        if not command or not isinstance(args, list) or len(args) > 256:
            raise ValueError("ACP terminal command and bounded args are required")
        argv = [command, *(str(value)[:16_384] for value in args)]
        cwd = self._cwd(params.get("cwd"), root)
        requested_env = params.get("env") or []
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _ENV_ALLOW or key == "PATH"
        }
        if isinstance(requested_env, list):
            for item in requested_env[:64]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if name in _ENV_ALLOW:
                    env[name] = str(item.get("value") or "")[:8_192]
        byte_limit = max(1_024, min(int(params.get("outputByteLimit") or 262_144), 1_048_576))

        async with self._lock:
            owned = sum(1 for terminal in self._terminals.values() if terminal.session_id == session_id)
            if owned >= 4 or len(self._terminals) >= 32:
                raise RuntimeError("ACP terminal concurrency limit reached")
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            terminal_id = f"term_{uuid.uuid4().hex}"
            terminal = _Terminal(terminal_id, session_id, process, byte_limit)
            self._terminals[terminal_id] = terminal
            terminal.reader = asyncio.create_task(self._read(terminal))
            terminal.reaper = asyncio.create_task(self._timeout(terminal, 300.0))
        return {"terminalId": terminal_id}

    async def _read(self, terminal: _Terminal) -> None:
        assert terminal.process.stdout is not None
        try:
            while chunk := await terminal.process.stdout.read(8_192):
                terminal.output.extend(chunk)
                if len(terminal.output) > terminal.byte_limit:
                    del terminal.output[: len(terminal.output) - terminal.byte_limit]
                    terminal.truncated = True
        except asyncio.CancelledError:
            raise

    async def _timeout(self, terminal: _Terminal, timeout: float) -> None:
        try:
            await asyncio.wait_for(terminal.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._terminate(terminal)
        except asyncio.CancelledError:
            raise

    def _get(self, params: dict) -> _Terminal:
        session_id = str(params.get("sessionId") or "")
        self._context(session_id)
        terminal = self._terminals.get(str(params.get("terminalId") or ""))
        if terminal is None or terminal.session_id != session_id:
            raise KeyError("ACP terminal not found")
        return terminal

    @staticmethod
    def _status(terminal: _Terminal) -> dict | None:
        code = terminal.process.returncode
        if code is None:
            return None
        return {
            "exitCode": code if code >= 0 else None,
            "signal": signal.Signals(-code).name if code < 0 else None,
        }

    async def output(self, params: dict) -> dict:
        terminal = self._get(params)
        text = terminal.output.decode("utf-8", errors="replace")
        return {
            "output": _SECRET.sub(r"\1=[redacted]", text),
            "truncated": terminal.truncated,
            "exitStatus": self._status(terminal),
        }

    async def wait_for_exit(self, params: dict) -> dict:
        terminal = self._get(params)
        await terminal.process.wait()
        if terminal.reader:
            await terminal.reader
        return self._status(terminal) or {"exitCode": None, "signal": None}

    async def _terminate(self, terminal: _Terminal) -> None:
        if terminal.process.returncode is not None:
            return
        try:
            os.killpg(terminal.process.pid, signal.SIGTERM)
            await asyncio.wait_for(terminal.process.wait(), timeout=2.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            if terminal.process.returncode is None:
                try:
                    os.killpg(terminal.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await terminal.process.wait()

    async def kill(self, params: dict) -> dict:
        await self._terminate(self._get(params))
        return {}

    async def release(self, params: dict) -> dict:
        terminal = self._get(params)
        await self._terminate(terminal)
        if terminal.reader:
            await terminal.reader
        if terminal.reaper:
            terminal.reaper.cancel()
        self._terminals.pop(terminal.terminal_id, None)
        return {}

    async def cleanup_session(self, session_id: str) -> None:
        for terminal in [
            item for item in self._terminals.values() if item.session_id == session_id
        ]:
            await self.release({"sessionId": session_id, "terminalId": terminal.terminal_id})

    async def close(self) -> None:
        for session_id in {item.session_id for item in self._terminals.values()}:
            await self.cleanup_session(session_id)
