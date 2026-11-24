"""Per-turn tool policy composition for agent execution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Set, Tuple


GUIDE_ONLY_DIRECTIVE = (
    "## GUIDE-ONLY MODE - TOOL POLICY\n"
    "The latest user turn explicitly forbids tool use. Do not call tools, do not "
    "run shell commands, and do not inspect local files or the environment. "
    "Respond in normal text by guiding the user or asking them to paste the "
    "output they will produce locally."
)

WEB_TOOL_NAMES = frozenset({"web_search", "web_fetch"})
BROWSER_TOOL_NAMES = frozenset({"browser_status", "browser_observe", "browser_control"})
TURN_POLICY_VERSION = 1
TURN_POLICY_KEYS = frozenset({
    "version", "mode", "allowBash", "allowWebSearch", "useWeb",
    "useResearch", "compareMode", "incognito", "noMemory",
})

def tool_toggle_enabled(value: object) -> bool:
    """Return true only for explicit true-like tool toggle values."""

    return str(value).lower() == "true"


def tool_toggle_explicitly_denied(value: object) -> bool:
    """Return true when a caller explicitly supplied a non-true toggle value."""

    return value is not None and not tool_toggle_enabled(value)


def is_web_search_explicitly_denied(allow_web_search: object) -> bool:
    """Whether the web-search agent toggle was explicitly set to false."""

    return tool_toggle_explicitly_denied(allow_web_search)


def web_search_enabled_for_turn(allow_web_search: object, use_web: object = None) -> bool:
    """Return true only when this request explicitly enables web search.

    Agent mode sends ``allow_web_search``; chat-mode pre-search sends
    ``use_web``. If both are present, an explicit ``allow_web_search=false``
    wins so a stale or conflicting intent path cannot re-enable web tools.
    """

    if is_web_search_explicitly_denied(allow_web_search):
        return False
    return tool_toggle_enabled(allow_web_search) or tool_toggle_enabled(use_web)


_COMMON_TOOL_NAMES = {
    "evolve_hexes",
    "search_code",
    "shared_text",
    "api_call",
    "app_api",
    "archive_email",
    "ask_teacher",
    "ask_user",
    "bash",
    "bulk_email",
    "builtin_browser",
    "cancel_download",
    "chat_with_model",
    "create_document",
    "create_session",
    "delete_email",
    "download_model",
    "edit_document",
    "edit_file",
    "edit_image",
    "generate_image",
    "glob",
    "grep",
    "gcf_arguments",
    "json_preview",
    "toon_preview",
    "gcf_preview",
    "list_cached_models",
    "list_cookbook_servers",
    "list_downloads",
    "list_emails",
    "list_models",
    "list_serve_presets",
    "list_served_models",
    "list_sessions",
    "ls",
    "manage_calendar",
    "manage_contact",
    "manage_documents",
    "manage_endpoints",
    "manage_mcp",
    "manage_memory",
    "manage_notes",
    "read_copal",
    "manage_copal",
    "manage_research",
    "manage_session",
    "manage_settings",
    "manage_skills",
    "manage_tasks",
    "manage_tokens",
    "manage_webhooks",
    "mark_email_read",
    "pipeline",
    "python",
    "read_email",
    "read_file",
    "recall_memory",
    "reply_to_email",
    "resolve_contact",
    "search_chats",
    "search_hf_models",
    "send_email",
    "send_to_session",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "suggest_document",
    "trigger_research",
    "ui_control",
    "update_document",
    "update_plan",
    "vault_get",
    "vault_search",
    "vault_unlock",
    "web_fetch",
    "web_search",
    "write_file",
    "workflow_artifact",
}


_GUIDE_ONLY_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\bguide[-\s]?only mode\b", "guide-only mode requested"),
        (r"\bno[-\s]?tools? mode\b", "no-tools mode requested"),
        (r"\bdo not use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bdon'?t use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bnot allowed to use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bnot allowed to:?.{0,120}\buse (?:any )?tools?\b", "user forbade tool use"),
        (r"\bask (?:me )?(?:for confirmation )?before using tools?\b", "user requested confirmation before tools"),
    )
)


@dataclass(frozen=True)
class ToolPolicy:
    """Effective tool behavior for one agent turn."""

    disabled_tools: frozenset[str] = frozenset()
    hidden_tools: frozenset[str] = frozenset()
    reasons: Mapping[str, str] = field(default_factory=dict)
    mode: str = "normal"
    block_all_tool_calls: bool = False
    disable_mcp: bool = False

    def all_disabled_names(self) -> Set[str]:
        return set(self.disabled_tools) | set(self.hidden_tools)

    def blocks(self, tool_name: Optional[str]) -> bool:
        if not tool_name:
            return False
        return self.block_all_tool_calls or tool_name in self.disabled_tools or tool_name in self.hidden_tools

    def reason_for(self, tool_name: Optional[str]) -> str:
        if tool_name and tool_name in self.reasons:
            return self.reasons[tool_name]
        if self.block_all_tool_calls and self.mode == "guide_only":
            return "Tool use is disabled for this guide-only turn."
        return "Tool use is disabled for this turn."


def detect_guide_only_turn(message: object) -> Optional[str]:
    """Return a reason when the latest user turn strongly requests no tools."""

    if not isinstance(message, str) or not message.strip():
        return None
    text = re.sub(r"\s+", " ", message.strip())
    for pattern, reason in _GUIDE_ONLY_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def known_tool_names() -> Set[str]:
    """Best-effort set of native tool names for prompt hiding and denylisting."""

    names = set(_COMMON_TOOL_NAMES)
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

        for schema in FUNCTION_TOOL_SCHEMAS:
            name = (schema.get("function") or {}).get("name") or schema.get("name")
            if name:
                names.add(name)
    except Exception:
        pass
    try:
        from src.agent_loop import TOOL_SECTIONS

        names.update(TOOL_SECTIONS.keys())
    except Exception:
        pass
    try:
        from src.tool_security import PLAN_MODE_READONLY_TOOLS, _PLAN_MODE_KNOWN_MUTATORS

        names.update(PLAN_MODE_READONLY_TOOLS)
        names.update(_PLAN_MODE_KNOWN_MUTATORS)
    except Exception:
        pass
    return names


def build_effective_tool_policy(
    *,
    disabled_tools: Optional[Iterable[str]] = None,
    last_user_message: object = "",
) -> ToolPolicy:
    """Compose the effective policy for one agent turn.

    Existing callers still provide the already-composed disabled-tool denylist.
    This function adds higher-level turn policy on top so enforcement is not
    delegated to prompt compliance.
    """

    disabled = {str(t) for t in (disabled_tools or []) if t}
    hidden: Set[str] = set()
    reasons = {tool: "Tool is disabled for this request." for tool in disabled}

    guide_reason = detect_guide_only_turn(last_user_message)
    if guide_reason:
        all_tools = known_tool_names()
        disabled.update(all_tools)
        hidden.update(all_tools)
        reasons.update({tool: f"{guide_reason}." for tool in all_tools})
        return ToolPolicy(
            disabled_tools=frozenset(disabled),
            hidden_tools=frozenset(hidden),
            reasons=MappingProxyType(dict(reasons)),
            mode="guide_only",
            block_all_tool_calls=True,
            disable_mcp=True,
        )

    return ToolPolicy(
        disabled_tools=frozenset(disabled),
        hidden_tools=frozenset(hidden),
        reasons=MappingProxyType(dict(reasons)),
    )


def normalize_turn_policy_snapshot(value: object) -> dict[str, Any]:
    """Validate and canonicalize immutable request choices for one turn."""
    if not isinstance(value, dict) or set(value) != TURN_POLICY_KEYS:
        raise ValueError("turnPolicy must contain exactly the version 1 fields")
    version = value.get("version")
    if isinstance(version, bool) or version != TURN_POLICY_VERSION:
        raise ValueError("turnPolicy version is unsupported")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in {"agent", "chat", "plan"}:
        raise ValueError("turnPolicy mode is invalid")
    for key in ("allowBash", "allowWebSearch"):
        if value.get(key) is not None and not isinstance(value.get(key), bool):
            raise ValueError(f"turnPolicy {key} must be boolean or null")
    for key in ("useWeb", "useResearch", "compareMode", "incognito", "noMemory"):
        if not isinstance(value.get(key), bool):
            raise ValueError(f"turnPolicy {key} must be boolean")
    return {
        "version": TURN_POLICY_VERSION,
        "mode": mode,
        "allowBash": value.get("allowBash"),
        "allowWebSearch": value.get("allowWebSearch"),
        "useWeb": value["useWeb"],
        "useResearch": value["useResearch"],
        "compareMode": value["compareMode"],
        "incognito": value["incognito"],
        "noMemory": value["noMemory"],
    }


def conservative_legacy_turn_policy() -> dict[str, Any]:
    """Effective fallback for old receipts; never rewrite their identity."""
    return {
        "version": TURN_POLICY_VERSION,
        "mode": "chat",
        "allowBash": False,
        "allowWebSearch": False,
        "useWeb": False,
        "useResearch": False,
        "compareMode": False,
        "incognito": False,
        "noMemory": True,
    }


@dataclass(frozen=True)
class TurnPolicyResolution:
    snapshot: Mapping[str, Any]
    policy: ToolPolicy
    allowed_tools: Optional[frozenset[str]]
    research_requested: bool


def resolve_turn_policy(
    snapshot: Mapping[str, Any],
    *,
    disabled_tools: Optional[Iterable[str]] = None,
    last_user_message: object = "",
    memory_read_allowed: bool = True,
    theme_status_only: bool = False,
) -> TurnPolicyResolution:
    """Resolve request choices against caller-supplied live deny authority.

    The function is pure apart from importing the server-owned mode allowlists.
    Callers must supply current privilege, global, email, and workspace denies;
    this resolver never treats request choices as authority.
    """
    choices = normalize_turn_policy_snapshot(dict(snapshot))
    disabled = {str(tool) for tool in (disabled_tools or ()) if tool}
    allowed_tools: Optional[Set[str]] = None

    if choices["allowBash"] is not None and not choices["allowBash"]:
        disabled.add("bash")
    if not web_search_enabled_for_turn(choices["allowWebSearch"], choices["useWeb"]):
        disabled.update(WEB_TOOL_NAMES)
    if choices["incognito"]:
        disabled.update({
            "manage_memory", "recall_memory", "manage_rag", "workflow_artifact", "search_chats", "manage_skills",
            "create_session", "list_sessions", "manage_session", "send_to_session",
            "chat_with_model",
        })
    if choices["noMemory"] or not memory_read_allowed:
        disabled.update({"manage_memory", "recall_memory", "manage_rag", "workflow_artifact"})

    # Preserve the pre-mode decision: never remove an explicit denial from
    # a merged set. This exception describes only the status operation.
    theme_status = bool(theme_status_only and "theme_emoji_assets" not in disabled and not choices["incognito"])
    if choices["compareMode"]:
        from src.tool_security import COMPARE_READONLY_TOOLS, compare_mode_disabled_tools
        allowed_tools = set(COMPARE_READONLY_TOOLS)
        mode_denied = compare_mode_disabled_tools()
        if theme_status:
            mode_denied = mode_denied - {"theme_emoji_assets"}
            allowed_tools.add("theme_emoji_assets")
        disabled.update(mode_denied)
    if choices["mode"] == "plan":
        from src.tool_security import plan_mode_disabled_tools
        mode_denied = plan_mode_disabled_tools()
        if theme_status: mode_denied = mode_denied - {"theme_emoji_assets"}
        disabled.update(mode_denied)
    elif choices["mode"] == "chat":
        from src.tool_security import chat_mode_disabled_tools
        mode_denied = chat_mode_disabled_tools()
        if theme_status: mode_denied = mode_denied - {"theme_emoji_assets"}
        disabled.update(mode_denied)

    effective = build_effective_tool_policy(
        disabled_tools=disabled,
        last_user_message=last_user_message,
    )
    return TurnPolicyResolution(
        snapshot=MappingProxyType(choices),
        policy=effective,
        allowed_tools=frozenset(allowed_tools) if allowed_tools is not None else None,
        research_requested=choices["useResearch"],
    )
