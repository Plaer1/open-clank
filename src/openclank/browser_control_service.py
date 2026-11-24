"""Owner/session-bound adapter for protected Playwright MCP clients.

Tab indexes are only short-lived observations: Playwright MCP does not expose
durable tab identifiers. Every action therefore verifies the selected index
and exact URL before dispatch, serializes on the connection, and refreshes it
after the one non-replayable call.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from src.openclank.browser_control import BrowserTargetError, BrowserTargetRegistry

_ACTIVE_SERVICE = None


def configure_browser_control_service(service) -> None:
    global _ACTIVE_SERVICE
    _ACTIVE_SERVICE = service


def configured_browser_control_service():
    return _ACTIVE_SERVICE


# Match the pinned adapter's tab-list rows, including literal brackets in titles
# and parentheses in URLs. Crashed rows are deliberately unselectable.
_TAB_LINE = re.compile(r"^- (\d+): (?:(\(current\)) )?\[(.*)\]\(([^\s]+)\)$", re.M)
_OBSERVE_TOOLS = {
    "snapshot": "browser_snapshot",
    "screenshot": "browser_take_screenshot",
    "console": "browser_console_messages",
    "network": "browser_network_requests",
}
_CONTROL_TOOLS = {
    "navigate": "browser_navigate",
    "back": "browser_navigate_back",
    "click": "browser_click",
    "hover": "browser_hover",
    "drag": "browser_drag",
    "type": "browser_type",
    "fill": "browser_fill_form",
    "select": "browser_select_option",
    "press": "browser_press_key",
}
_UNSUPPORTED = (
    "paired_snapshot_and_screenshot", "network_timing", "dom_styles", "performance",
    "upload", "download", "clear_site_data", "clear_history",
)
_PUBLIC_URL_RE = re.compile(r"https?://[^\s\])>\"']+", re.I)

# Public arguments are deliberately narrower than the upstream MCP surface.
# In particular, upstream filename options write files in the MCP host process.
BROWSER_ACTION_ARGUMENTS = {
    "snapshot": {"target": {"type": "string"}, "depth": {"type": "number", "minimum": 1, "maximum": 8}, "boxes": {"type": "boolean"}},
    "screenshot": {"target": {"type": "string"}, "element": {"type": "string"}, "type": {"type": "string", "enum": ["png", "jpeg", "webp"]}, "fullPage": {"type": "boolean"}, "scale": {"type": "string", "enum": ["css", "device"]}},
    "console": {"level": {"type": "string", "enum": ["error", "warning", "info", "debug"]}},
    "network": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
    "navigate": {"url": {"type": "string", "minLength": 1, "maxLength": 4096}},
    "back": {},
    "click": {"target": {"type": "string", "minLength": 1, "maxLength": 1000}, "element": {"type": "string", "maxLength": 1000}, "doubleClick": {"type": "boolean"}, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "modifiers": {"type": "array", "items": {"type": "string", "enum": ["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]}, "maxItems": 5}},
    "hover": {"target": {"type": "string", "minLength": 1, "maxLength": 1000}, "element": {"type": "string", "maxLength": 1000}},
    "drag": {"startTarget": {"type": "string", "minLength": 1, "maxLength": 1000}, "endTarget": {"type": "string", "minLength": 1, "maxLength": 1000}, "startElement": {"type": "string", "maxLength": 1000}, "endElement": {"type": "string", "maxLength": 1000}},
    "type": {"target": {"type": "string", "minLength": 1, "maxLength": 1000}, "element": {"type": "string", "maxLength": 1000}, "text": {"type": "string", "maxLength": 10000}, "submit": {"type": "boolean"}, "slowly": {"type": "boolean"}},
    "fill": {"fields": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 1000}, "target": {"type": "string", "minLength": 1, "maxLength": 1000}, "element": {"type": "string", "maxLength": 1000}, "type": {"type": "string", "enum": ["textbox", "checkbox", "radio", "combobox", "slider"]}, "value": {"type": "string", "maxLength": 10000}}, "required": ["target", "name", "type", "value"]}}},
    "select": {"target": {"type": "string", "minLength": 1, "maxLength": 1000}, "values": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string", "maxLength": 1000}}},
    "press": {"key": {"type": "string", "minLength": 1, "maxLength": 100}},
}
BROWSER_REQUIRED_ARGUMENTS = {
    "navigate": ["url"], "click": ["target"], "hover": ["target"],
    "drag": ["startTarget", "endTarget"], "type": ["target", "text"],
    "fill": ["fields"], "select": ["target", "values"], "press": ["key"],
}


def validate_browser_arguments(action: str, arguments: Any) -> dict:
    schema = BROWSER_ACTION_ARGUMENTS.get(action)
    if schema is None or not isinstance(arguments, dict):
        raise ValueError("Browser action arguments are invalid")
    if set(arguments) - set(schema):
        raise ValueError("Browser action contains unadmitted arguments")
    for name in BROWSER_REQUIRED_ARGUMENTS.get(action, []):
        if name not in arguments:
            raise ValueError(f"Browser action requires {name}")
    for name, value in arguments.items():
        spec = schema[name]
        types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        valid = any((typ == "string" and isinstance(value, str)) or (typ == "integer" and type(value) is int) or (typ == "number" and isinstance(value, (int, float)) and type(value) is not bool) or (typ == "boolean" and type(value) is bool) or (typ == "array" and isinstance(value, list)) or (typ == "object" and isinstance(value, dict)) for typ in types)
        if not valid:
            raise ValueError(f"Browser argument {name} has the wrong type")
        if isinstance(value, str):
            if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", 100000):
                raise ValueError(f"Browser argument {name} is outside its size limit")
            if "enum" in spec and value not in spec["enum"]:
                raise ValueError(f"Browser argument {name} has an unsupported value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                raise ValueError(f"Browser argument {name} is outside its range")
        if isinstance(value, list):
            if len(value) < spec.get("minItems", 0) or len(value) > spec.get("maxItems", 1000):
                raise ValueError(f"Browser argument {name} is outside its item limit")
            item_spec = spec.get("items", {})
            for item in value:
                if item_spec.get("type") == "string":
                    if not isinstance(item, str) or len(item) > item_spec.get("maxLength", 100000) or ("enum" in item_spec and item not in item_spec["enum"]):
                        raise ValueError(f"Browser argument {name} contains an invalid item")
                elif item_spec.get("type") == "object":
                    if not isinstance(item, dict) or set(item) - set(item_spec["properties"]):
                        raise ValueError(f"Browser argument {name} contains an invalid object")
                    if not set(item_spec.get("required", [])) <= set(item):
                        raise ValueError(f"Browser argument {name} object is incomplete")
                    for field, field_value in item.items():
                        field_spec = item_spec["properties"][field]
                        allowed_types = field_spec["type"] if isinstance(field_spec["type"], list) else [field_spec["type"]]
                        if not any((t == "string" and isinstance(field_value, str)) or (t == "boolean" and type(field_value) is bool) for t in allowed_types):
                            raise ValueError(f"Browser argument {name} object has an invalid field")
                        if "enum" in field_spec and field_value not in field_spec["enum"]:
                            raise ValueError(f"Browser argument {name} object has an unsupported field")
                        if isinstance(field_value, str) and len(field_value) > field_spec.get("maxLength", 100000):
                            raise ValueError(f"Browser argument {name} object field is too large")
    return dict(arguments)


def _safe_url(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Browser target URL must be HTTP(S) without embedded credentials")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        if parsed.port:
            host += f":{parsed.port}"
    except ValueError as exc:
        raise ValueError("Browser target URL has an invalid port") from exc
    return urlunsplit((parsed.scheme.lower(), host, parsed.path, parsed.query, parsed.fragment))


def _safe_tab_url(value: str) -> str:
    if str(value or "").strip() == "about:blank":
        return "about:blank"
    return _safe_url(value)


def _public_url(value: str) -> str:
    parsed = urlsplit(_safe_url(value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _redact_url(value: str) -> str:
    try:
        return _public_url(value)
    except Exception:
        return "[redacted URL]"


def _sanitize_result(value: Any, coverage: dict[str, Any] | None = None) -> Any:
    """Remove URL credentials and query/fragment material from public output."""
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"headers", "body", "requestbody", "responsebody", "cookies"}:
                continue
            if lowered == "images" and isinstance(item, list):
                safe[str(key)] = [
                    {"data": row["data"], "mimeType": row.get("mimeType", "image/png")}
                    if isinstance(row, dict) and isinstance(row.get("data"), str) and len(row["data"]) <= 12 * 1024 * 1024
                    else {"omitted": "image exceeds 12 MiB response limit"}
                    for row in item
                ]
                continue
            safe[str(key)] = _sanitize_result(item, coverage)
        return safe
    if isinstance(value, list):
        return [_sanitize_result(v, coverage) for v in value]
    if not isinstance(value, str):
        return value
    sanitized = _PUBLIC_URL_RE.sub(lambda match: _redact_url(match.group(0)), value)
    omitted = max(0, len(sanitized) - 12000)
    if not omitted:
        return sanitized
    if coverage is not None:
        coverage["clipped"] = True
        coverage["clippedStrings"] += 1
        coverage["omittedCharacters"] += omitted
    return sanitized[:12000] + f"\n[Browser text locally clipped; {omitted} characters omitted.]"


_DIAGNOSTIC_MAX_CHARS = 256 * 1024
_CONSOLE_COUNTS = re.compile(r"Total messages: (\d{1,8}) \(Errors: (\d{1,8}), Warnings: (\d{1,8})\)")
_REQUEST_LINE = re.compile(
    r"(\d{1,8})\. \[(GET|HEAD|POST|PUT|DELETE|CONNECT|OPTIONS|TRACE|PATCH)\] (\S+)(?: => \[(\d{3}|FAILED)\](?: .*)?)?"
)


def _diagnostic_result_lines(result, section="Result"):
    """Read only the requested pinned MCP section, with a bounded local parse."""
    raw = result.get("stdout")
    if not isinstance(raw, str):
        return [], False, False
    clipped = len(raw) > _DIAGNOSTIC_MAX_CHARS
    sample = raw[:_DIAGNOSTIC_MAX_CHARS]
    if clipped:
        sample = sample.rpartition("\n")[0]
    lines = sample.splitlines()
    try:
        start = lines.index(f"### {section}") + 1
    except ValueError:
        return [], clipped, False
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("### ")), len(lines))
    return lines[start:end], clipped, True


def _console_diagnostics(result):
    lines, _, found = _diagnostic_result_lines(result)
    counts = _CONSOLE_COUNTS.fullmatch(lines[0]) if found and lines else None
    if counts is None:
        return {"state": "unavailable", "reason": "console_summary_missing"}
    total, errors, warnings = map(int, counts.groups())
    return {"state": "partial", "scope": "since_navigation", "total": total, "errors": errors,
            "warnings": warnings, "messageText": "omitted", "upstreamCompleteness": "unknown"}


def _network_diagnostics(result, page_url, limit):
    lines, clipped, found = _diagnostic_result_lines(result)
    if not found:
        return {"state": "unavailable", "reason": "network_summary_missing"}
    if page_url == "about:blank":
        return {"state": "unavailable", "reason": "no_http_origin"}
    page = urlsplit(page_url)
    requests = []
    matched = 0
    for line in lines:
        match = _REQUEST_LINE.fullmatch(line)
        if match is None:
            continue
        number, method, value, status = match.groups()
        try:
            url = urlsplit(_safe_url(value))
            if ((page.scheme, page.hostname, page.port or (443 if page.scheme == "https" else 80))
                    != (url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80))):
                continue
        except ValueError:
            continue
        matched += 1
        if len(requests) == limit:
            continue
        response_status = int(status) if status and status.isdigit() else None
        outcome = ("failed" if status == "FAILED" or (response_status and response_status >= 400)
                   else "response" if response_status else "unknown")
        requests.append({"index": int(number), "method": method, "outcome": outcome, "status": response_status})
    return {"state": "partial", "scope": "same_origin_selected_tab", "requests": requests,
            "parsedSameOriginCount": matched, "clipped": clipped or matched > limit,
            "timing": "unavailable", "upstreamCompleteness": "unknown"}


def _redact_browser_event_section(result):
    """Keep bounded transfer states without exposing adapter filenames or paths."""
    raw = result.get("stdout")
    if not isinstance(raw, str):
        return result, None
    lines = raw.splitlines(keepends=True)
    event_section = False
    filtered = []
    started = finished = 0
    for line in lines:
        if line.startswith("### "):
            event_section = line.strip() == "### Events"
        if event_section and line.startswith("- Downloading file "):
            started = min(started + 1, 100)
        elif event_section and line.startswith("- Downloaded file "):
            finished = min(finished + 1, 100)
        elif not event_section:
            filtered.append(line)
    event = None
    if started or finished:
        event = {"direction": "download", "startedReported": started, "completedReported": finished,
                 "filesResult": "unavailable"}
    return {**result, "stdout": "".join(filtered)}, event


class BrowserControlService:
    """Hidden-manager adapter; only explicitly selected targets are callable."""

    def __init__(self, manager, *, registry: BrowserTargetRegistry | None = None):
        self.manager = manager
        self.registry = registry or BrowserTargetRegistry()
        self._connections: dict[str, dict[str, Any]] = {}
        self._tab_witness_key = secrets.token_bytes(32)
        self._scope_locks: dict[str, Any] = {}
        self._turn_policies: dict[str, dict[str, Any]] = {}
        self._managed_turn_policies: dict[tuple, dict[str, Any]] = {}
        self._ui_generations: dict[str, tuple[int, float]] = {}

    def admit_ui_generation(self, *, owner: str, client_id: str, generation: int) -> bool:
        if not re.fullmatch(r"[a-f0-9]{32}", str(client_id or "")) or type(generation) is not int or generation < 1:
            return False
        key = f"{owner}\0{client_id}"
        now = time.monotonic()
        for old_key, (_generation, touched) in list(self._ui_generations.items()):
            if now - touched > 1800:
                self._ui_generations.pop(old_key, None)
        current = self._ui_generations.get(key, (0, now))[0]
        if generation <= current:
            return False
        self._ui_generations[key] = (generation, now)
        if len(self._ui_generations) > 2048:
            oldest = min(self._ui_generations, key=lambda item: self._ui_generations[item][1])
            self._ui_generations.pop(oldest, None)
        return True

    def ui_generation_is_current(self, *, owner: str, client_id: str | None, generation: int | None) -> bool:
        if not client_id or generation is None:
            return True
        current = self._ui_generations.get(f"{owner}\0{client_id}")
        if current is None or time.monotonic() - current[1] > 1800:
            return False
        return current[0] == generation

    def publish_turn_policy(self, *, owner: str, host_session_id: str, workspace_id: str,
                            binding_revision: int, turn_id: str, envelope: dict) -> None:
        """Project the host's current turn policy for per-call broker checks."""
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        mode = str(envelope.get("mode") or "chat").lower()
        if str(envelope.get("interaction_policy") or "").lower() in {"guide", "guide_only"}:
            mode = "guide_only"
        self._turn_policies[key] = {
            "turn_id": str(turn_id), "mode": mode,
            "lane": str(envelope.get("lane") or "chat").lower(),
            "allowed": None if envelope.get("allowed_tools") is None else set(map(str, envelope.get("allowed_tools") or [])),
            "disabled": set(map(str, envelope.get("disabled_tools") or [])),
        }

    def clear_turn_policy(self, *, owner: str, host_session_id: str, workspace_id: str,
                          binding_revision: int, turn_id: str) -> None:
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        current = self._turn_policies.get(key)
        if current and current.get("turn_id") == str(turn_id):
            self._turn_policies.pop(key, None)

    def retire_chat_turn_policies(self, *, owner: str, host_session_id: str) -> None:
        prefix = f"{owner}\0{host_session_id}\0"
        for key in [key for key in self._turn_policies if key.startswith(prefix)]:
            self._turn_policies.pop(key, None)

    @staticmethod
    def _managed_policy_key(owner, host_session_id, workspace_id, binding_revision, identity):
        names = ("sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID")
        if not isinstance(identity, dict) or set(identity) != set(names):
            return None
        if any(not isinstance(identity[name], str) or not identity[name] for name in names if name != "executionRevision"):
            return None
        if type(identity["executionRevision"]) is not int or identity["executionRevision"] < 0:
            return None
        return (owner, host_session_id, workspace_id, binding_revision, *(identity[name] for name in names))

    def publish_managed_turn_policy(self, *, owner, host_session_id, workspace_id, binding_revision,
                                    identity, envelope):
        key = self._managed_policy_key(owner, host_session_id, workspace_id, binding_revision, identity)
        if key is None:
            raise ValueError("invalid managed Browser identity")
        mode = str(envelope.get("mode") or "chat").lower()
        if envelope.get("block_all"):
            mode = "guide_only"
        self._managed_turn_policies[key] = {
            "turn_id": secrets.token_urlsafe(16), "mode": mode, "lane": "agent",
            "allowed": None if envelope.get("allowed_tools") is None else set(envelope["allowed_tools"]),
            "disabled": set(envelope.get("disabled_tools") or ())}

    def clear_managed_turn_policy(self, *, owner, host_session_id, workspace_id, binding_revision, identity):
        key = self._managed_policy_key(owner, host_session_id, workspace_id, binding_revision, identity)
        self._managed_turn_policies.pop(key, None)

    def _retire_managed_selections(self, matches, state):
        for connection in tuple(self._connections.values()):
            selection = connection.get("selection")
            if selection and selection.get("actor") and matches(selection["actor"], selection.get("actorAccount")):
                self._release_selection(connection, state)
            paused = connection.get("pausedController")
            if paused and matches(paused["actor"], paused["actorAccount"]):
                connection.pop("pausedController")
                connection["selectionState"] = state

    def retire_managed_root(self, *, owner: str, host_session_id: str, root_operation_id: str) -> None:
        """Revoke one parent's Browser grants and the target its execution held."""
        if not owner or not host_session_id or not root_operation_id:
            return
        def matches(key):
            return key[:2] == (owner, host_session_id) and key[5] == root_operation_id

        for key in tuple(self._managed_turn_policies):
            if matches(key):
                self._managed_turn_policies.pop(key, None)
        self._retire_managed_selections(lambda key, _account: matches(key), "parent_retired")

    def retire_managed_execution(self, *, owner: str, account_id: str, host_session_id: str,
                                 execution_key: tuple[str, str, str, int]) -> None:
        """Retire only the proved completed actor's selected tab or suspended association."""
        if not owner or not account_id or not host_session_id:
            return
        # Managed inner turns already retire their grants. At execution finish
        # only the account-qualified actor's tab or suspended association remains.
        self._retire_managed_selections(
            lambda key, account: account == account_id and key[:2] == (owner, host_session_id)
            and key[4:8] == execution_key,
            "actor_retired",
        )

    def turn_policy_allows(self, *, owner: str, host_session_id: str, workspace_id: str,
                           binding_revision: int, tool_name: str) -> bool:
        return self.current_turn_policy_revision(
            owner=owner, host_session_id=host_session_id, workspace_id=workspace_id,
            binding_revision=binding_revision, tool_name=tool_name,
        ) is not None

    def current_turn_policy_revision(self, *, owner: str, host_session_id: str, workspace_id: str,
                                    binding_revision: int, tool_name: str, managed_identity: dict | None = None) -> str | None:
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        managed_key = self._managed_policy_key(owner, host_session_id, workspace_id, binding_revision, managed_identity)
        policy = self._managed_turn_policies.get(managed_key) if managed_identity is not None else self._turn_policies.get(key)
        return self._policy_revision(policy, tool_name)

    @staticmethod
    def _policy_revision(policy, tool_name):
        if not policy or policy.get("lane") == "auxiliary" or policy.get("mode") in {"guide", "guide_only"}:
            return None
        name = str(tool_name)
        if "browser" in policy["disabled"] or name in policy["disabled"]:
            return None
        if policy["allowed"] is not None and name not in policy["allowed"]:
            return None
        if policy["mode"] == "plan" and name not in {"browser_status", "browser_observe"}:
            return None
        return policy.get("turn_id") if name in {"browser_status", "browser_observe", "browser_control"} else None

    def _scope_lock(self, key: str):
        return self._scope_locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _scope_key(owner: str, session_id: str, workspace_id: str, revision: int) -> str:
        return "\0".join((owner, session_id, workspace_id, str(revision)))

    @staticmethod
    def _parse_tabs(result: dict) -> list[dict[str, Any]]:
        if result.get("exit_code") or result.get("outcome_unknown"):
            raise BrowserControlRejected("Browser tabs are unavailable")
        text = str(result.get("stdout") or result.get("output") or "")
        tabs = []
        for match in _TAB_LINE.finditer(text):
            try:
                _safe_tab_url(match.group(4))
            except ValueError:
                continue
            tabs.append({"index": int(match.group(1)), "current": bool(match.group(2)),
                         "title": match.group(3)[:300], "url": match.group(4)})
        return tabs

    @staticmethod
    def _current_tab(tabs):
        current = [row for row in tabs if row["current"]]
        return current[0] if len(current) == 1 else None

    async def _tab_rows(self, connection, action="list", **arguments):
        raw = await self.manager.call_browser_tool_once(
            connection["id"], "browser_tabs", {"action": action, **arguments}, tab_control=True,
        )
        return self._parse_tabs(raw)

    def _tab_witness(self, connection, row):
        payload = json.dumps((connection["generation"], row["index"], row["url"], row["title"])).encode()
        return hmac.new(self._tab_witness_key, payload, hashlib.sha256).hexdigest()

    def _public_tab(self, connection, row):
        return {**row, "url": "about:blank" if row["url"] == "about:blank" else _public_url(row["url"]),
                "selectionWitness": self._tab_witness(connection, row)}

    def _release_selection(self, connection, state):
        selection = connection.pop("selection", None)
        if selection is None:
            return False
        self.registry.release(target_id=selection["view"]["targetId"], **connection["scope"])
        connection["selectionState"] = state
        receipt = connection.get("latestAction")
        if receipt and receipt["targetId"] == selection["view"]["targetId"] and receipt["state"] == "dispatching":
            receipt["state"] = "outcome_unknown"
        return True

    def _readback(self, connection):
        if not connection:
            return {"connected": False, "selectionState": "unselected"}
        selection = connection.get("selection")
        supported = connection["tools"]
        observations = [name for name, tool in _OBSERVE_TOOLS.items() if tool in supported]
        controls = [name for name, tool in _CONTROL_TOOLS.items() if tool in supported]
        policy = self._managed_turn_policies.get(selection.get("policyKey")) if selection else None
        observe = control = False
        if policy and selection["managedTurnIsCurrent"]():
            observe = self._policy_revision(policy, "browser_observe") is not None
            control = selection.get("controlCapability") and self._policy_revision(policy, "browser_control") is not None
        return {
            "connected": True, "connectionGeneration": connection["generation"],
            "adapter": {"name": "Playwright MCP", "mode": connection["mode"],
                        "profile": connection["profile"],
                        "observationActions": observations, "controlActions": controls,
                        "unsupported": list(_UNSUPPORTED)},
            "selectionState": connection["selectionState"],
            "selectedTarget": dict(selection["view"]) if selection else None,
            "controllingActorId": selection["actor"][6] if selection and selection.get("actor") else None,
            "effectiveActions": {"observe": observations if observe else [], "control": controls if control else []},
            "latestAction": dict(connection["latestAction"]) if connection.get("latestAction") else None,
        }

    def connection_readback(self, *, owner: str, host_session_id: str, workspace_id: str, binding_revision: int):
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        return self._readback(self._connections.get(key))

    async def open_isolated(self, *, owner: str, host_session_id: str, workspace_id: str, binding_revision: int,
                            mode: str = "isolated", profile_dir_name: str | None = None,
                            client_id: str | None = None, client_generation: int | None = None,
                            verify_access: Callable[[], None] | None = None) -> dict:
        """Start an explicitly selected isolated or installed-extension client."""
        from src.builtin_mcp import _find_npx, build_browser_mcp_startup

        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        async with self._scope_lock(key):
            current = lambda: self.ui_generation_is_current(owner=owner, client_id=client_id, generation=client_generation)
            if mode not in {"isolated", "extension"}:
                raise ValueError("Browser connection mode is invalid")
            if mode == "extension" and (not isinstance(profile_dir_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", profile_dir_name)):
                raise ValueError("Choose an installed Chrome profile directory name")
            if not current():
                raise BrowserControlRejected("Browser connection request was superseded")
            if verify_access:
                verify_access()
            await self._close_scope_locked(key)
            if verify_access:
                verify_access()
            startup = build_browser_mcp_startup(mode, profile_dir_name=profile_dir_name)
            opened = await self.manager.open_browser_client(
                name="Open Clank Browser", command=startup["command"] or _find_npx(),
                args=startup["args"], env=startup["env"],
            )
            if not opened.get("connected"):
                raise BrowserControlRejected("Browser is unavailable")
            connection = {
                "id": opened["connection_id"], "generation": opened["connection_generation"],
                "scope": {"owner_id": owner, "host_session_id": host_session_id, "workspace_id": workspace_id,
                          "binding_revision": binding_revision},
                "tools": frozenset(row["name"] for row in opened.get("tools", []) if isinstance(row, dict) and "name" in row),
                "mode": mode, "profile": profile_dir_name if mode == "extension" else None,
                "selectionState": "unselected",
            }
            try:
                if verify_access:
                    verify_access()
                if not current():
                    raise BrowserControlRejected("Browser connection request was superseded")
                if "browser_tabs" not in connection["tools"]:
                    raise BrowserControlRejected("Browser tab selection is unsupported by this connection")
                rows = await self._tab_rows(connection)
                if verify_access:
                    verify_access()
                if not current():
                    raise BrowserControlRejected("Browser connection request was superseded")
            except BaseException:
                await self.manager.close_browser_client(connection["id"])
                raise
            self._connections[key] = connection
            return {**self._readback(connection), "tabs": [self._public_tab(connection, row) for row in rows]}

    def _verify_selection(self, connection, rows):
        selection = connection.get("selection")
        if selection is None:
            return None
        current = self._current_tab(rows)
        if current is None or current["index"] != selection["index"] or current["url"] != selection["url"]:
            self._release_selection(connection, "stale")
            return None
        return selection

    async def list_connection_tabs(self, *, owner: str, host_session_id: str, workspace_id: str,
                                   binding_revision: int, verify_access: Callable[[], None] | None = None) -> dict:
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        async with self._scope_lock(key):
            connection = self._connections.get(key)
            if not connection:
                return {"connected": False, "tabs": [], "selectionState": "unselected"}
            if verify_access:
                verify_access()
            try:
                rows = await self._tab_rows(connection)
            except BrowserControlRejected:
                self._release_selection(connection, "stale")
                raise
            self._verify_selection(connection, rows)
            return {**self._readback(connection), "tabs": [self._public_tab(connection, row) for row in rows]}

    async def select_tab(self, *, owner: str, host_session_id: str, workspace_id: str,
                         binding_revision: int, tab_index: int, selection_witness: str,
                         client_id: str, client_generation: int,
                         verify_access: Callable[[], None] | None = None) -> dict:
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        async with self._scope_lock(key):
            connection = self._connections.get(key)
            if not connection:
                raise BrowserControlRejected("No Browser connection is active for this chat")
            if verify_access:
                verify_access()
            rows = await self._tab_rows(connection)
            matching_rows = [row for row in rows if row["index"] == tab_index]
            if len(matching_rows) != 1 or not hmac.compare_digest(
                    self._tab_witness(connection, matching_rows[0]), selection_witness):
                self._verify_selection(connection, rows)
                raise BrowserControlRejected("Browser tab changed. Refresh tabs and select it again.")
            if not self.ui_generation_is_current(owner=owner, client_id=client_id, generation=client_generation):
                raise BrowserControlRejected("Browser selection was superseded")
            if verify_access:
                verify_access()
            self._release_selection(connection, "unselected")
            current = self._current_tab(await self._tab_rows(connection, "select", index=tab_index))
            if verify_access:
                verify_access()
            if not current or not hmac.compare_digest(self._tab_witness(connection, current), selection_witness):
                raise BrowserControlRejected("Browser tab selection could not be verified")
            if not self.ui_generation_is_current(owner=owner, client_id=client_id, generation=client_generation):
                raise BrowserControlRejected("Browser selection was superseded")
            view = self.registry.register_selected_tab(
                **connection["scope"], connection_id=connection["id"],
                connection_generation=connection["generation"], tab_id=f"index:{tab_index}",
                title=current["title"], url=current["url"],
            )
            controller = connection.pop("pausedController", {})
            connection["selection"] = {"view": view, "index": tab_index, "url": current["url"],
                                       "actor": controller.get("actor"), "actorAccount": controller.get("actorAccount")}
            connection["selectionState"] = "selected"
            return view

    def stop_tab(self, *, owner: str, host_session_id: str, workspace_id: str,
                 binding_revision: int, target_id: str, intent: str = "stop") -> dict:
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        connection = self._connections.get(key)
        selection = connection.get("selection") if connection else None
        if intent == "pause_for_sign_in" and connection and connection["mode"] != "extension":
            raise BrowserControlRejected("Manual sign-in needs an attached browser that you can open")
        state = "sign_in_paused" if intent == "pause_for_sign_in" else "operator_stopped"
        stopped = bool(selection and selection["view"]["targetId"] == target_id
                       and self._release_selection(connection, state))
        if stopped and intent == "pause_for_sign_in" and selection.get("actor"):
            # The original worker may resume only after another explicit
            # operator selection creates a fresh target and generations.
            connection["pausedController"] = {
                "actor": selection["actor"], "actorAccount": selection["actorAccount"],
            }
        return {"stopped": stopped, **self._readback(connection)}

    @staticmethod
    def _claim_selection(selection, managed_key, account_id, control_capability, managed_turn_is_current):
        actor = managed_key[:-1] if managed_key else None
        if selection["actor"] is not None and (selection["actor"] != actor or selection["actorAccount"] != account_id):
            raise BrowserControlRejected("The selected tab is held by another Browser execution")
        selection["actor"] = actor
        selection["actorAccount"] = account_id
        selection["policyKey"] = managed_key
        selection["controlCapability"] = control_capability
        selection["managedTurnIsCurrent"] = managed_turn_is_current

    async def call(self, *, owner: str, host_session_id: str, workspace_id: str, binding_revision: int,
                   tool_name: str, arguments: dict, require_active_policy: bool = False,
                   expected_policy_revision: str | None = None, managed_identity: dict | None = None,
                   control_capability: bool = True,
                   binding_is_current: Callable[[], bool] | None = None,
                   managed_turn_is_current: Callable[[], bool] | None = None,
                   operator_preview: bool = False, managed_account_id: str | None = None) -> dict:
        scope = dict(owner=owner, host_session_id=host_session_id, workspace_id=workspace_id,
                     binding_revision=binding_revision)
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        managed_key = self._managed_policy_key(owner, host_session_id, workspace_id, binding_revision, managed_identity)

        def policy_revision(name):
            return self.current_turn_policy_revision(**scope, tool_name=name, managed_identity=managed_identity)

        def require_policy():
            if binding_is_current and not binding_is_current():
                raise BrowserControlRejected("Browser chat binding changed")
            if managed_identity is not None and (not managed_account_id or not managed_turn_is_current
                                                or not managed_turn_is_current()):
                raise BrowserControlRejected("Browser execution is no longer active")
            if require_active_policy and (expected_policy_revision is None
                                          or policy_revision(tool_name) != expected_policy_revision):
                raise BrowserControlRejected("Browser tool is unavailable under the current chat turn policy")

        if operator_preview and (tool_name != "browser_observe" or managed_identity is not None or
                                 binding_is_current is None or not isinstance(arguments, dict) or
                                 arguments.get("action") != "snapshot"):
            raise BrowserControlRejected("Only a signed-in selected tab preview is available")
        if require_active_policy and expected_policy_revision is None:
            expected_policy_revision = policy_revision(tool_name)
        require_policy()

        if tool_name == "browser_status":
            if arguments:
                raise ValueError("Browser status takes no arguments")
            async with self._scope_lock(key):
                require_policy()
                connection = self._connections.get(key)
                if not connection:
                    return self._readback(None)
                selection = None
                if connection.get("selection"):
                    try:
                        rows = await self._tab_rows(connection)
                    except BrowserControlRejected:
                        self._release_selection(connection, "stale")
                        raise
                    require_policy()
                    selection = self._verify_selection(connection, rows)
                if selection:
                    self._claim_selection(selection, managed_key, managed_account_id, control_capability, managed_turn_is_current)
                readback = self._readback(connection)
                latest = readback.get("latestAction")
                if latest and (not selection or latest["targetId"] != selection["view"]["targetId"]):
                    readback.pop("latestAction")
                if not require_active_policy and selection:
                    readback["effectiveActions"] = {
                        "observe": readback["adapter"]["observationActions"],
                        "control": readback["adapter"]["controlActions"] if control_capability else [],
                    }
                return readback

        if tool_name not in {"browser_observe", "browser_control"} or not isinstance(arguments, dict):
            raise ValueError("Invalid Browser tool request")
        control = tool_name == "browser_control"
        if control and not control_capability:
            raise BrowserControlRejected("Browser actions are disabled for this session")
        action = str(arguments.get("action") or "")
        mcp_tool = (_CONTROL_TOOLS if control else _OBSERVE_TOOLS).get(action)
        if not mcp_tool:
            raise ValueError("Browser action is not supported by this adapter")
        expected = {"target_id", "connection_generation", "navigation_generation",
                    "observation_generation", "action", "arguments"}
        if (set(arguments) != expected or not isinstance(arguments["arguments"], dict)
                or not isinstance(arguments["target_id"], str) or not isinstance(arguments["connection_generation"], str)
                or any(type(arguments[field]) is not int or arguments[field] < 0
                       for field in ("navigation_generation", "observation_generation"))):
            raise ValueError("Browser target and observed generations are required")
        action_args = validate_browser_arguments(action, arguments["arguments"])
        # Console summaries are scoped to this navigation. Network limits
        # are applied to typed rows locally, never forwarded as upstream args.
        diagnostic_limit = action_args.pop("limit", 40) if action == "network" else None
        if action == "console":
            action_args = {"level": action_args.get("level", "info"), "all": False}
        if action == "navigate":
            action_args = {"url": _safe_url(action_args["url"])}

        async with self._scope_lock(key):
            require_policy()
            connection = self._connections.get(key)
            selection = connection.get("selection") if connection else None
            if selection is None or arguments["target_id"] != selection["view"]["targetId"]:
                raise BrowserControlRejected("Select a current Browser tab in the connection panel")
            if mcp_tool not in connection["tools"]:
                raise BrowserControlRejected("Browser action is unavailable on this connection")
            actor = managed_key[:-1] if managed_key else None
            if (not operator_preview and selection["actor"] is not None
                    and (selection["actor"] != actor or selection["actorAccount"] != managed_account_id)):
                raise BrowserControlRejected("The selected tab is held by another Browser execution")
            receipt = None
            observed = None
            verification_started = False

            async def observe(target):
                nonlocal observed, verification_started
                verification_started = True
                observed = self._current_tab(await self._tab_rows(connection))
                if not observed:
                    return {"current": False}
                if not control and observed["url"] != target.current_url:
                    # Observations belong to the original navigation only.
                    return {"current": False}
                return {"current": True, "tab_id": f"index:{observed['index']}",
                        "title": observed["title"], "url": observed["url"]}

            async def dispatch(connection_id, name, args):
                nonlocal receipt
                require_policy()
                if not operator_preview:
                    self._claim_selection(selection, managed_key, managed_account_id, control_capability, managed_turn_is_current)
                if control:
                    receipt = {
                        "action": action, "state": "dispatching", "at": time.time(),
                        "targetId": selection["view"]["targetId"], "tabId": selection["view"]["tabId"],
                        "origin": selection["view"]["origin"], "actorId": managed_key[6] if managed_key else None,
                        "connectionGeneration": arguments["connection_generation"],
                        "navigationGeneration": arguments["navigation_generation"],
                        "observationGeneration": arguments["observation_generation"],
                    }
                    connection["latestAction"] = receipt
                result = await self.manager.call_browser_tool_once(connection_id, name, args)
                if result.get("outcome_unknown") or result.get("exit_code", 0):
                    raise BrowserControlRejected("Browser adapter could not confirm the result")
                return result

            try:
                result = await self.registry.dispatch_once(
                    **connection["scope"], target_id=arguments["target_id"],
                    connection_generation=str(arguments["connection_generation"]),
                    navigation_generation=int(arguments["navigation_generation"]),
                    observation_generation=int(arguments["observation_generation"]),
                    tool_name=mcp_tool, arguments=action_args,
                    verify_selected_tab=observe, call=dispatch, refresh_selected_tab=observe,
                )
                require_policy()
            except (Exception, asyncio.CancelledError) as exc:
                current = connection.get("selection") is selection
                if receipt:
                    receipt["state"] = "outcome_unknown"
                    if current:
                        self._release_selection(connection, "stale")
                    if not isinstance(exc, asyncio.CancelledError):
                        raise BrowserActionOutcomeUnknown(
                            "Browser action may have completed. Check the tab before taking another action."
                        ) from exc
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if current and verification_started:
                    self._release_selection(connection, "stale")
                if isinstance(exc, BrowserTargetError):
                    raise BrowserControlRejected(str(exc)) from exc
                raise BrowserControlRejected("Browser request could not be completed on the selected tab") from exc
            selection["view"] = {**selection["view"], "observedAt": time.time(), **{
                field: result[field] for field in ("navigationGeneration", "observationGeneration", "origin", "title")
            }}
            selection["url"] = observed["url"]
            if action not in {"console", "network"} and not operator_preview:
                result["result"], transfer = _redact_browser_event_section(result["result"])
                if transfer:
                    result["transfer"] = transfer
                    if receipt:
                        receipt["transfer"] = transfer
            if receipt:
                receipt["state"] = "completed"
                receipt["resultOrigin"] = result["origin"]
                receipt["resultNavigationGeneration"] = result["navigationGeneration"]
                result["actionReceipt"] = dict(receipt)
        if operator_preview:
            lines, clipped, found = _diagnostic_result_lines(result["result"], "Snapshot")
            if not found or not lines or lines[0] != "```yaml":
                raise BrowserControlRejected("Selected page structure is unavailable on this adapter")
            text = "\n".join(line for line in lines[:50] if not line.startswith("```"))
            text = _sanitize_result(text[:3000])[:3000].strip()
            if not text:
                raise BrowserControlRejected("The selected page returned no structure to preview")
            return {field: result[field] for field in ("targetId", "connectionGeneration", "navigationGeneration",
                                                      "observationGeneration", "origin")} | {
                "text": text, "state": "partial", "kind": "accessibility_structure", "observedAt": time.time(),
                "clipped": clipped or len(lines) > 50 or len("\n".join(lines[:50])) > 3000,
            }
        if action == "console":
            result["result"] = _console_diagnostics(result["result"])
            return result
        if action == "network":
            result["result"] = _network_diagnostics(result["result"], selection["url"], diagnostic_limit)
            return result
        coverage = {"version": 1, "clipped": False, "clippedStrings": 0,
                    "omittedCharacters": 0, "upstreamCompleteness": "unknown"}
        result["result"] = _sanitize_result(result.get("result"), coverage)
        result["textCoverage"] = coverage
        return result

    async def close_scope(self, *, owner: str, host_session_id: str, workspace_id: str, binding_revision: int) -> bool:
        key = self._scope_key(owner, host_session_id, workspace_id, binding_revision)
        async with self._scope_lock(key):
            return await self._close_scope_locked(key)

    async def _close_scope_locked(self, key):
        connection = self._connections.pop(key, None)
        if not connection:
            return False
        self._release_selection(connection, "unselected")
        self.registry.release_connection(connection_id=connection["id"], connection_generation=connection["generation"])
        await self.manager.close_browser_client(connection["id"])
        return True

    async def close_all(self) -> None:
        for key in tuple(self._connections):
            async with self._scope_lock(key):
                await self._close_scope_locked(key)


class BrowserControlRejected(RuntimeError):
    """Request failed before a browser action was sent."""


class BrowserActionOutcomeUnknown(RuntimeError):
    """A browser action was sent but its outcome could not be confirmed."""
