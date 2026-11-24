"""The Open Clank terminal shell.

This deliberately speaks only to ``/api/tui/v1/**``.  It does not embed the
engine, read provider configuration, or create a second session authority.
"""

from __future__ import annotations

import os
import json
import uuid
import re
import shlex
import sys
import textwrap
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from src.openclank.client_profiles import normalize_server_url
from src.openclank.tui_client import OpenClankTuiClient, TuiClientError
from src.openclank.cli_output import clean_text


_CYAN = "\x1b[38;5;44m"
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
        self.runs: dict[str, dict[str, Any]] = {}
        self.shells: dict[str, list[dict[str, Any]]] = {}

    def _write(self, text: str = "") -> None:
        text = clean_text(str(text))
        lines = []
        for line in text.split("\n"):
            lines.extend(textwrap.wrap(line, width=self._width(), replace_whitespace=False, drop_whitespace=False) or [""])
        if self.color:
            lines = [(_CYAN + "OPEN CLANK" + _RESET + line[len("OPEN CLANK"):]) if line.startswith("OPEN CLANK") else line for line in lines]
        self.output.write("\n".join(lines) + "\n")
        self.output.flush()

    def _brand(self) -> str:
        return "OPEN CLANK"

    def _width(self) -> int:
        try:
            return max(1, min(120, os.get_terminal_size(self.output.fileno()).columns))
        except (AttributeError, OSError, ValueError):
            return 80

    def _refresh(self) -> None:
        previous = self.bootstrap.get("principal")
        old_owner = previous.get("owner") if isinstance(previous, dict) else None
        self.bootstrap = self.client.bootstrap()
        principal = self.bootstrap.get("principal") if isinstance(self.bootstrap, dict) else None
        new_owner = principal.get("owner") if isinstance(principal, dict) else None
        if old_owner and old_owner != new_owner:
            self.runs.clear()
            self.shells.clear()
        rows = self.bootstrap.get("sessions")
        self.sessions = rows if isinstance(rows, list) else []

    def _show_home(self) -> None:
        principal = self.bootstrap.get("principal") if isinstance(self.bootstrap, dict) else {}
        owner = principal.get("owner", "unknown") if isinstance(principal, dict) else "unknown"
        self._write(f"\n{self._brand()}  {self.client.profile.name}  {owner}")
        self._write("─" * min(64, self._width()))
        self._show_sessions()
        self._write(
            "\nCommands: open, browser, send, active, resume, stop, actors, sessions, providers, shares, tasks, "
            "capabilities, shells, diagnostics, refresh, help, quit"
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
            if self._width() < 70:
                self._write(f"  {index:>2}. {name[:max(1, self._width() - 6)]}{marker}")
                self._write(f"      Model: {model[:max(1, self._width() - 13)]}")
            else:
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
        self._write("─" * min(64, self._width()))
        items = data.get("items") if isinstance(data, dict) else []
        for item in items if isinstance(items, list) else []:
            role = str(item.get("role") or "message").upper()
            content = str(item.get("content") or "")
            self._write(f"\n{role}")
            for line in textwrap.wrap(clean_text(content), width=max(1, self._width() - 2), replace_whitespace=False) or [""]:
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
        attachments: list[str] = []
        allow_bash = allow_web_search = False
        # Options require an explicit sentinel so existing messages are unchanged.
        if message.startswith("--options "):
            options, marker, message = message[len("--options "):].partition(" -- ")
            if not marker or not message.strip():
                self._write("Usage: send <session> --options [--bash] [--web-search] [--attachment ID]... -- <message>")
                return
            try:
                tokens = shlex.split(options)
                while tokens:
                    flag = tokens.pop(0)
                    if flag == "--bash" and not allow_bash:
                        allow_bash = True
                    elif flag == "--web-search" and not allow_web_search:
                        allow_web_search = True
                    elif flag == "--attachment" and tokens:
                        attachments.append(tokens.pop(0))
                    else:
                        raise ValueError("Unsupported or repeated turn option")
                if len(attachments) > 64 or len(set(attachments)) != len(attachments) or any(not item for item in attachments):
                    raise ValueError("Invalid attachment IDs")
            except ValueError as exc:
                self._write(f"Turn options unavailable: {exc}")
                return
            capabilities = self.client.session_capabilities(str(session["id"]))
            operations = capabilities.get("operations", {}) if isinstance(capabilities, dict) else {}
            requested = (["attachments"] if attachments else []) + (["bash_option"] if allow_bash else []) + (["web_search_option"] if allow_web_search else [])
            for name in requested:
                capability = operations.get(name, {}) if isinstance(operations, dict) else {}
                if not isinstance(capability, dict) or capability.get("status") not in {"available", "conditional"}:
                    self._write(f"Turn option {name} unavailable: {capability.get('reason') or 'capability not established'}" if isinstance(capability, dict) else f"Turn option {name} unavailable")
                    return
            if requested:
                self._write("Requested options are subject to current Host policy and admission.")
        self._write(f"\n{self._brand()} is working…")
        import uuid
        operation_key = uuid.uuid4().hex
        self._write(f"Original submission key: {operation_key}")
        events = self.client.stream_turn(str(session["id"]), message.strip(), idempotency_key=operation_key,
            attachments=attachments, allow_bash=allow_bash, allow_web_search=allow_web_search)
        pending = False
        try:
            for event in events:
                if not isinstance(event, dict):
                    continue
                if event.get("type") in {"permission_request", "ask_user"}:
                    pending = True
                    self._write("\nHuman action required. Use interactions <session> if this device has the required scope.")
                    break
                delta = event.get("delta") or event.get("content")
                if delta:
                    self.output.write(clean_text(str(delta)))
                    self.output.flush()
        finally:
            close = getattr(events, "close", None)
            if close:
                close()
        if not pending:
            self._write("\nTurn stream ended; final status is not confirmed here.")
        self._refresh()

    def _open_browser(self, target: str) -> None:
        """Navigate to an existing authenticated host chat without starting a turn."""
        session = self._resolve_session(target)
        if session is None:
            self._write(f"Unknown session: {target}")
            return
        session_id = session.get("id")
        if (not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", session_id)
                or re.match(r"^(document|note|image|email|event|task|skill|research)-", session_id)):
            self._write("Browser handoff is unavailable for this session.")
            return
        result = self.client.existing_chat_handoff(session_id)
        principal = self.bootstrap.get("principal")
        owner = principal.get("owner") if isinstance(principal, dict) else None
        if (not isinstance(result, dict) or not isinstance(owner, str) or not owner
                or result.get("owner") != owner or result.get("host_chat_id") != session_id
                or result.get("actor_id") != "main" or not isinstance(result.get("account_id"), str)
                or not result["account_id"].strip() or not isinstance(result.get("engine_session_id"), str)
                or not result["engine_session_id"]
                or any(type(result.get(k)) is not int or result[k] < 0 for k in ("map_revision", "binding_revision"))):
            self._write("Browser handoff is unavailable or changed.")
            return
        try:
            base, _ = normalize_server_url(self.client.profile.url)
            url = base.rstrip("/") + "/#" + quote(session_id, safe="")
            opened = webbrowser.open(url)
        except Exception:
            opened = False
            try:
                base, _ = normalize_server_url(self.client.profile.url)
                url = base.rstrip("/") + "/#" + quote(session_id, safe="")
            except Exception:
                self._write("Browser handoff is unavailable.")
                return
        if not opened:
            self._write("Open this existing chat in your browser:")
            self.output.write(clean_text(url) + "\n")
            self.output.flush()

    def _selected_session_id(self, target: str) -> str | None:
        session = self._resolve_session(target)
        if session is None:
            self._write(f"Unknown session: {target}")
            return None
        return str(session["id"])

    def _show_original_turn(self, argument: str) -> None:
        target, separator, key = argument.strip().partition(' ')
        if not separator or not key.strip():
            self._write('Usage: status <number|id> <original submission key>')
            return
        session_id = self._selected_session_id(target)
        if session_id is None: return
        result = self.client.original_turn_status(session_id, key.strip())
        self._write('Original submission: ' + clean_text(str(result.get('state', 'unknown'))))
        if result.get('run_id'):
            self._write('Original run: ' + clean_text(str(result['run_id'])))

    def _interactions(self, target: str) -> None:
        session_id = self._selected_session_id(target)
        if session_id is None: return
        page = self.client.interactions(session_id)
        items = page.get('items', [])
        if not items:
            self._write('No current supported blocking interaction was observed.')
            return
        for index, item in enumerate(items, 1):
            self._write(f"{index}. {clean_text(str(item.get('kind', 'interaction')))}")
        if page.get('complete') is not True: self._write('Additional interactions may be unavailable.')
        selected = self.input('Select a number, or Enter to leave pending: ').strip()
        if not selected.isdecimal() or not 1 <= int(selected) <= len(items): return
        item = items[int(selected)-1]
        request = item.get('request') or {}
        self._write(clean_text(json.dumps(request, ensure_ascii=False, indent=2)))
        if item.get('kind') == 'question' and item.get('tool') is None:
            self._write('Source tool identity unavailable.')
        reply = {'kind':item.get('kind'), 'request_id':request.get('request_id'),
                 'turn_id':request.get('turn_id'), 'revision':request.get('revision')}
        if reply['kind'] in ('approval', 'shell_approval', 'file_approval'):
            choice = self.input('Enter once or reject, or Enter to leave pending: ').strip()
            if choice not in request.get('options', []): return
            reply['option'] = choice
        else:
            choice = self.input('Enter JSON array of answer arrays, reject, or Enter to leave pending: ').strip()
            if not choice: return
            if choice == 'reject': reply['rejected'] = True
            else:
                try: reply['answers'] = json.loads(choice)
                except ValueError:
                    self._write('Invalid JSON; question remains pending.')
                    return
        result = self.client.reply_interaction(session_id, reply)
        self._write('Interaction: ' + clean_text(str(result.get('state', 'unknown'))))

    def _optional_questions(self, argument: str) -> None:
        target, separator, actor_id = argument.strip().partition(' ')
        if not separator or not actor_id.strip():
            self._write('Usage: questions <number|id> <actor-id>')
            return
        session_id = self._selected_session_id(target)
        if session_id is None: return
        handoff = self.client.existing_chat_handoff(session_id)
        revision = handoff.get('map_revision')
        if type(revision) is not int or revision < 0:
            self._write('Current chat mapping is unavailable.')
            return
        actor_id = actor_id.strip()
        page = self.client.optional_questions(session_id, actor_id, revision)
        items = page.get('items', [])
        if not items:
            self._write('No optional questions in this page.')
            return
        for index, item in enumerate(items, 1):
            self._write(f"{index}. {clean_text(str(item.get('questionRef', 'question')))} ({clean_text(str(item.get('state', 'unknown')))})")
        if page.get('nextCursor'): self._write('Additional questions exist beyond this page.')
        selected = self.input('Select a number, or Enter to leave pending: ').strip()
        if not selected.isdecimal() or not 1 <= int(selected) <= len(items): return
        item = items[int(selected)-1]
        self._write('Source: ' + clean_text(json.dumps({key:item.get(key) for key in
            ('requestID','questionRevision','originTurnID','actorID','sessionID','tool')}, ensure_ascii=False)))
        self._write(clean_text(json.dumps(item.get('questions', []), ensure_ascii=False, indent=2)))
        self._write('Submitting uses chat mode, no Bash, web or research; existing memory policy may apply.')
        choice = self.input('Enter JSON array of answer arrays, dismiss, or Enter to leave pending: ').strip()
        if not choice: return
        answer = {'requestID':item.get('requestID'), 'questionRevision':item.get('questionRevision'),
                  'disposition':'dismissed' if choice == 'dismiss' else 'answered'}
        if choice != 'dismiss':
            try: answer['answers'] = json.loads(choice)
            except ValueError:
                self._write('Invalid JSON; question remains pending.')
                return
        key, handoff_id = uuid.uuid4().hex, uuid.uuid4().hex
        payload = {'requestID':key, 'handoffID':handoff_id, 'text':'', 'attachmentRefs':[], 'uploadIDs':[],
                   'questionRefs':[item.get('questionRef')], 'questionAnswers':[answer], 'mode':'after-turn',
                   'actorSessionId':page.get('actorSessionId'), 'turnPolicy':{'version':1,'mode':'chat',
                   'allowBash':False,'allowWebSearch':False,'useWeb':False,'useResearch':False,
                   'compareMode':False,'incognito':False,'noMemory':False}}
        self._write('Original answer receipt: ' + key)
        result = self.client.answer_optional_question(session_id, actor_id, revision, payload)
        self._write('Answer delivery: ' + clean_text(str(result.get('deliveryState') or result.get('delivery') or 'unknown')))

    def _show_active_turn(self, target: str) -> None:
        session_id = self._selected_session_id(target)
        if session_id is None:
            return
        state = self.client.active_turn(session_id)
        if state.get("session_id") != session_id:
            self._write("Run identity unavailable; no run was selected.")
            return
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
            self._write("No retained run is currently available; previous selection is unchanged.")
            return
        prior = self.runs.get(session_id)
        if not prior or prior["run_id"] != run_id:
            self.runs[session_id] = {"run_id": run_id, "cursor": 0}
        self._write(f"Selected run {run_id}: {state.get('status') or 'unknown'}; cursor {self.runs[session_id]['cursor']}.")
        self._write("Use resume to attach, or stop to explicitly request cancellation of this run.")

    def _resume_turn(self, target: str) -> None:
        session_id = self._selected_session_id(target)
        if session_id is None:
            return
        run = self.runs.get(session_id)
        if not run:
            self._write("No run selected. Use active <number|id> first.")
            return
        events = None
        terminal = False
        try:
            events = self.client.stream_active_turn(session_id, run_id=run["run_id"], after=run["cursor"])
            for event in events:
                cursor = event.get("id") if isinstance(event, dict) else None
                if type(cursor) is not int or cursor < 0:
                    self._write("Stream event has no usable cursor; detached with outcome unknown.")
                    return
                if cursor <= run["cursor"]:
                    continue
                if cursor != run["cursor"] + 1:
                    self._write("Stream history has a gap or was evicted; outcome unknown. Detached without advancing the cursor.")
                    return
                value = event.get("data")
                if event.get("event") == "done":
                    terminal = True
                    self._write("\nRun stream ended; check active for its current status.")
                    run["cursor"] = cursor
                    break
                if event.get("event") == "error":
                    self._write("\nRun reported an error; inspect the existing chat for details.")
                if isinstance(value, dict) and value.get("type") in {"permission_request", "ask_user"}:
                    terminal = True
                    self._write("\nHuman action required. Use interactions <session> if this device has the required scope.")
                    run["cursor"] = cursor
                    break
                if isinstance(value, dict) and isinstance(value.get("delta"), str) and not value.get("thinking"):
                    self.output.write(clean_text(value["delta"]))
                    self.output.flush()
                run["cursor"] = cursor
            if not terminal:
                self._write("\nStream ended without a terminal event; outcome unknown.")
        except KeyboardInterrupt:
            self._write("\nDetached from the selected run; it was not cancelled.")
        except TuiClientError:
            self._write("Selected run stream unavailable or changed; outcome unknown. No replacement was attached.")
        finally:
            if events is not None:
                close = getattr(events, "close", None)
                if close:
                    close()

    def _stop_turn(self, target: str) -> None:
        session_id = self._selected_session_id(target)
        if session_id is None:
            return
        run = self.runs.get(session_id)
        if not run:
            self._write("No run selected. Use active <number|id> first.")
            return
        try:
            result = self.client.stop_active_turn(session_id, run_id=run["run_id"])
        except TuiClientError:
            self._write("Cancellation outcome unknown; no replacement run was selected.")
            return
        if result.get("session_id") != session_id or type(result.get("stopped")) is not bool:
            self._write("Cancellation response unavailable; outcome unknown.")
        elif result.get("stopped") is True:
            self._write("Cancellation requested for the selected run; final outcome is not confirmed.")
        else:
            self._write("Selected run is no longer active; no cancellation was requested.")

    def _discover_background_shells(self, target: str) -> None:
        session_id = self._selected_session_id(target)
        if session_id is None: return
        result = self.client.background_shells(session_id)
        rows = result.get('items') if isinstance(result, dict) else None
        if not isinstance(rows, list):
            self.shells.pop(session_id, None)
            self._write('Background shell discovery unavailable.')
            return
        self.shells[session_id] = rows
        self._write('Current background shells (read only)')
        for number, item in enumerate(rows, 1):
            self._write(f"  {number}. actor {item.get('actorID', 'unknown')} · {item.get('status', 'unknown')}")
        if not rows: self._write('  No current actor-bound background shell was found.')
        if result.get('complete') is not True: self._write('  Some jobs may be unavailable or omitted.')
        self._write('Use shell <session> <number> [cursor]. Foreground Bash and Human kill are unavailable.')

    def _show_background_shell(self, argument: str) -> None:
        parts = argument.split()
        if len(parts) in (2, 3):
            session_id = self._selected_session_id(parts[0])
            if session_id is None: return
            try:
                number = int(parts[1]); cursor = int(parts[2]) if len(parts) == 3 else 0
                rows = self.shells.get(session_id, [])
                if number < 1 or number > len(rows) or cursor < 0: raise ValueError()
                selected = rows[number - 1]
                parts = [parts[0], selected['jobID'], selected['actorID'], selected['actorSessionID'],
                         str(selected['executionRevision']), str(selected['mapRevision']), str(cursor)]
            except (ValueError, KeyError, TypeError):
                self._write('Shell selection unavailable. Use shells <session> to refresh.')
                return
        if len(parts) not in (6, 7):
            self._write('Usage: shells <session>  shell <session> <number> [cursor]')
            return
        session_id = self._selected_session_id(parts[0])
        if session_id is None: return
        try:
            execution_revision, map_revision = int(parts[4]), int(parts[5])
            cursor = int(parts[6]) if len(parts) == 7 else 0
            if min(execution_revision, map_revision, cursor) < 0: raise ValueError()
            result = self.client.background_shell(session_id, parts[1], parts[2], parts[3], execution_revision, map_revision, cursor=cursor)
        except ValueError:
            self._write('Shell revisions and cursor must be nonnegative integers.')
            return
        frame = result.get('frame') if isinstance(result, dict) else None
        if not isinstance(frame, dict) or not isinstance(frame.get('output'), str):
            self._write('Background shell unavailable.')
            return
        self._write(frame['output'])
        self._write(f"Status: {result.get('status', 'unknown')} · next cursor: {frame.get('nextCursor', 'unknown')} · exit: {result.get('settledExit', {}).get('exitCode') if isinstance(result.get('settledExit'), dict) else 'unknown'}")
        self._write('Read only. Human kill and foreground Bash viewing are unavailable. Use browser <session>, then select a current file independently in Files/Editor.')

    def _show_capabilities(self, target: str) -> None:
        session_id = self._selected_session_id(target)
        if session_id is None:
            return
        payload = self.client.session_capabilities(session_id)
        operations = payload.get("operations") if isinstance(payload, dict) else None
        if not isinstance(operations, dict) or not operations:
            self._write("Capabilities are unavailable for this chat.")
            return
        self._write("Operations for the selected chat")
        for name, operation in list(operations.items())[:64]:
            if isinstance(operation, dict):
                status = operation.get("status") or "unknown"
                reason = operation.get("reason")
                label = name.replace("_", " ")
                self._write(f"  {label} · {status}" + (f" · {reason}" if reason else ""))
        if payload.get("note"):
            self._write(payload["note"])

    def _show_actors(self, target: str) -> None:
        session_id = self._selected_session_id(target)
        if session_id is None:
            return
        data = self.client.actors(session_id)
        rows = data.get("items") if isinstance(data, dict) else None
        self._write("Persisted actor history")
        if not isinstance(rows, list) or not rows:
            self._write("  No persisted actors available.")
            return
        for row in rows[:100]:
            if isinstance(row, dict):
                actor = row.get("actor_id") or "actor unknown"
                actor_session = row.get("actor_session_id")
                identity = f"{actor} in {actor_session}" if actor_session else f"{actor} · session unavailable"
                self._write(f"  {identity} · {row.get('agent') or 'unknown'} · "
                            f"{row.get('status') or 'unknown'} · {row.get('outcome') or 'outcome unknown'}")
                if parent := row.get("parent_actor_id"):
                    parent_session = row.get("parent_actor_session_id")
                    self._write(f"    Parent: {parent} in {parent_session}" if parent_session else
                                "    Parent session unavailable.")
        self._write("Live actor control is unavailable in this view.")
        if len(rows) > 100:
            self._write(f"  {len(rows) - 100} additional persisted actors omitted.")

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
                elif command == "browser" and argument.strip():
                    self._open_browser(argument.strip())
                elif command == "send" and argument.strip():
                    self._send_turn(argument)
                elif command == "status" and argument.strip():
                    self._show_original_turn(argument)
                elif command == "interactions" and argument.strip():
                    self._interactions(argument.strip())
                elif command == "questions" and argument.strip():
                    self._optional_questions(argument)
                elif command == "active" and argument.strip():
                    self._show_active_turn(argument.strip())
                elif command == "resume" and argument.strip():
                    self._resume_turn(argument.strip())
                elif command == "stop" and argument.strip():
                    self._stop_turn(argument.strip())
                elif command == "shells" and argument.strip():
                    self._discover_background_shells(argument.strip())
                elif command == "shell" and argument.strip():
                    self._show_background_shell(argument.strip())
                elif command == "capabilities" and argument.strip():
                    self._show_capabilities(argument.strip())
                elif command == "actors" and argument.strip():
                    self._show_actors(argument.strip())
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
                        "open/browser <number|id>  send <number|id> <message> (or --options ... -- <message>)  status <number|id> <key>  interactions <number|id>  questions <number|id> <actor-id>  active/resume/stop/actors <number|id>  capabilities <session>  shells <session>  shell <session> <number> [cursor]  sessions  "
                        "providers  next <connection-id>  shares  tasks  "
                        "diagnostics  refresh  quit"
                    )
                else:
                    self._write("Unknown command. Type `help`.")
            except TuiClientError as exc:
                self._write(f"Request failed: {exc}")


__all__ = ["OpenClankTui"]
