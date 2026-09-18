"""The Open Clank terminal shell.

This deliberately speaks only to ``/api/tui/v1/**``.  It does not embed the
engine, read provider configuration, or create a second session authority.
"""

from __future__ import annotations

import os
import sys
import textwrap
from collections.abc import Callable
from typing import Any

from src.openclank.tui_client import OpenClankTuiClient, TuiClientError


_CYAN = "\x1b[38;5;44m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def _ansi_enabled(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("NO_COLOR") is None


class OpenClankTui:
    """Small dependency-free terminal application shell for the control plane."""

    def __init__(
        self,
        client: OpenClankTuiClient,
        *,
        input_fn: Callable[[str], str] = input,
        output: Any = sys.stdout,
    ):
        self.client = client
        self.input = input_fn
        self.output = output
        self.color = _ansi_enabled(output)
        self.bootstrap: dict[str, Any] = {}
        self.sessions: list[dict[str, Any]] = []

    def _write(self, text: str = "") -> None:
        self.output.write(text + "\n")
        self.output.flush()

    def _brand(self) -> str:
        return f"{_CYAN}OPEN CLANK{_RESET}" if self.color else "OPEN CLANK"

    def _refresh(self) -> None:
        self.bootstrap = self.client.bootstrap()
        rows = self.bootstrap.get("sessions")
        self.sessions = rows if isinstance(rows, list) else []

    def _show_home(self) -> None:
        principal = self.bootstrap.get("principal") if isinstance(self.bootstrap, dict) else {}
        owner = principal.get("owner", "unknown") if isinstance(principal, dict) else "unknown"
        self._write(f"\n{self._brand()}  {self.client.profile.name}  {_DIM if self.color else ''}{owner}{_RESET if self.color else ''}")
        self._write("─" * 64)
        self._show_sessions()
        self._write(
            "\nCommands: open, send, sessions, providers, shares, tasks, "
            "diagnostics, refresh, help, quit"
        )

    def _show_sessions(self) -> None:
        if not self.sessions:
            self._write("No sessions yet.")
            return
        self._write("Sessions")
        for index, row in enumerate(self.sessions[:30], start=1):
            name = str(row.get("name") or "Untitled")
            model = str(row.get("model") or "automatic")
            marker = " [archived]" if row.get("archived") else ""
            self._write(f"  {index:>2}. {name[:42]:<42} {model[:16]}{marker}")

    def _resolve_session(self, value: str) -> dict[str, Any] | None:
        try:
            index = int(value)
        except ValueError:
            index = 0
        if 1 <= index <= len(self.sessions):
            return self.sessions[index - 1]
        return next((row for row in self.sessions if str(row.get("id")) == value), None)

    def _show_messages(self, target: str) -> None:
        session = self._resolve_session(target)
        if session is None:
            self._write(f"Unknown session: {target}")
            return
        data = self.client.messages(str(session["id"]), limit=100)
        self._write(f"\n{self._brand()} / {session.get('name') or 'Untitled'}")
        self._write("─" * 64)
        items = data.get("items") if isinstance(data, dict) else []
        for item in items if isinstance(items, list) else []:
            role = str(item.get("role") or "message").upper()
            content = str(item.get("content") or "")
            self._write(f"\n{role}")
            for line in textwrap.wrap(content, width=76, replace_whitespace=False) or [""]:
                self._write(line)
        self._write("\nUse `send <number|id> <message>` to start a reconnectable turn.")

    def _send_turn(self, argument: str) -> None:
        target, separator, message = argument.strip().partition(" ")
        if not separator or not message.strip():
            self._write("Usage: send <number|id> <message>")
            return
        session = self._resolve_session(target)
        if session is None:
            self._write(f"Unknown session: {target}")
            return
        self._write(f"\n{self._brand()} is working…")
        for event in self.client.stream_turn(str(session["id"]), message.strip()):
            if not isinstance(event, dict):
                continue
            delta = event.get("delta") or event.get("content")
            if delta:
                self.output.write(str(delta))
                self.output.flush()
        self._write()
        self._refresh()

    def _show_tasks(self) -> None:
        data = self.client.tasks(limit=100)
        rows = data.get("items") if isinstance(data, dict) else []
        self._write("\nTasks")
        if not rows:
            self._write("  No scheduled tasks.")
            return
        for row in rows:
            self._write(
                f"  {str(row.get('name') or row.get('id'))[:42]:<42} "
                f"{str(row.get('status') or 'unknown')[:16]}"
            )

    def _show_providers(self) -> None:
        data = self.client.providers()
        rows = data.get("connections") if isinstance(data, dict) else []
        self._write("\nProvider pools")
        if not rows:
            self._write("  No provider connections.")
            return
        for row in rows:
            connection_id = str(row.get("id") or "")
            label = str(row.get("label") or connection_id)
            lane = str(row.get("billing_lane") or "unknown")
            enabled = "enabled" if row.get("enabled") else "disabled"
            self._write(f"  {label}  [{lane}; {enabled}]")
            self._write(
                f"    {connection_id}  accounts "
                f"{row.get('healthy_count', 0)}/{row.get('account_count', 0)} available"
            )
            for account in row.get("accounts") or []:
                state = "enabled" if account.get("enabled") else "disabled"
                self._write(
                    f"      {str(account.get('label') or account.get('id'))[:38]:<38} "
                    f"{str(account.get('auth_class') or '')[:18]} {state}"
                )
            models = row.get("models") or []
            if models:
                names = ", ".join(str(item.get("display_name") or item.get("model_id")) for item in models[:4])
                suffix = f" +{len(models) - 4}" if len(models) > 4 else ""
                self._write(f"    Models: {names}{suffix}")

    def _use_next_account(self, connection_id: str) -> None:
        data = self.client.providers()
        rows = data.get("connections") if isinstance(data, dict) else []
        connection = next(
            (row for row in rows or [] if str(row.get("id")) == connection_id),
            None,
        )
        if connection is None:
            self._write(f"Unknown provider connection: {connection_id}")
            return
        result = self.client.use_next_provider_account(
            connection_id,
            expected_revision=int(connection.get("revision") or 0),
        )
        account = result.get("next_account") or {}
        self._write(
            "Next new operation will prefer "
            f"{account.get('label') or account.get('id') or 'the next eligible account'}."
        )

    def _show_shares(self) -> None:
        data = self.client.shares()
        owned = data.get("owned") if isinstance(data, dict) else []
        received = data.get("received") if isinstance(data, dict) else []
        self._write("\nProvider shares")
        self._write("  Owned")
        if not owned:
            self._write("    None")
        for row in owned or []:
            self._write(
                f"    {str(row.get('label') or row.get('id'))[:34]:<34} "
                f"to {str(row.get('recipient') or '')[:18]}  active"
            )
            self._write(f"      {row.get('id')}")
        self._write("  Received")
        if not received:
            self._write("    None")
        for row in received or []:
            self._write(
                f"    {str(row.get('label') or row.get('id'))[:34]:<34} "
                f"[{str(row.get('billing_lane') or '')}; active]"
            )
            self._write(f"      {row.get('id')}")

    def _show_diagnostics(self) -> None:
        data = self.client.diagnostics()
        engine = data.get("engine") or {}
        runtime = data.get("runtime") or {}
        providers = data.get("providers") or {}
        status = "ready" if data.get("ready") else "not ready"
        self._write(f"\nDiagnostics: {status}")
        self._write(
            f"  Engine: {'verified' if engine.get('verified') else 'unverified'}  "
            f"{engine.get('version') or 'unknown'}  {engine.get('target') or ''}"
        )
        self._write(
            f"  Runtime: {'ready' if runtime.get('ok') else 'not ready'}  "
            f"current owner {(runtime.get('current_owner') or {}).get('status', 'unknown')}"
        )
        self._write(
            f"  Providers: {providers.get('connection_count', 0)} connections, "
            f"{providers.get('account_count', 0)} accounts"
        )

    def run(self) -> int:
        try:
            self._refresh()
        except TuiClientError as exc:
            self._write(f"Open Clank could not bootstrap: {exc}")
            return 1
        self._show_home()
        while True:
            try:
                raw = self.input("openclank> ").strip()
            except (EOFError, KeyboardInterrupt):
                self._write()
                return 0
            if not raw:
                continue
            command, _, argument = raw.partition(" ")
            command = command.lower()
            try:
                if command in {"quit", "exit", "q"}:
                    return 0
                if command in {"sessions", "ls"}:
                    self._show_sessions()
                elif command == "open" and argument.strip():
                    self._show_messages(argument.strip())
                elif command == "send" and argument.strip():
                    self._send_turn(argument)
                elif command == "tasks":
                    self._show_tasks()
                elif command == "providers":
                    self._show_providers()
                elif command == "next" and argument.strip():
                    self._use_next_account(argument.strip())
                elif command == "shares":
                    self._show_shares()
                elif command in {"diagnostics", "doctor"}:
                    self._show_diagnostics()
                elif command == "refresh":
                    self._refresh()
                    self._show_home()
                elif command == "help":
                    self._write(
                        "open <number|id>  send <number|id> <message>  sessions  "
                        "providers  next <connection-id>  shares  tasks  "
                        "diagnostics  refresh  quit"
                    )
                else:
                    self._write("Unknown command. Type `help`.")
            except TuiClientError as exc:
                self._write(f"Request failed: {exc}")


__all__ = ["OpenClankTui"]
