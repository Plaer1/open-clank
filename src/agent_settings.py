"""Validated owner-scoped native agent settings and effective budget math."""
from __future__ import annotations
from typing import Any, Mapping
from dataclasses import dataclass
import hashlib, json, re, math
from types import MappingProxyType
from copy import deepcopy

@dataclass(frozen=True)
class AdmittedAgentSettings:
    """Immutable settings captured when a turn is admitted."""
    owner: str
    generation: int
    effective: Mapping[str, Any]

@dataclass(frozen=True)
class AdmittedGcfPreference:
    """Exact installed projection captured by one worker admission."""
    revision: str
    requested: bool
    provider_id: str
    model_id: str
    json_whitespace_requested: bool = False
    gcf_memoization_requested: bool = False
    terminal_ansi_requested: bool = False
    html_markdown_requested: bool = False
    structured_search_requested: bool = False
    repeated_lines_requested: bool = False
    unified_diff_requested: bool = False
    schema_annotations_requested: bool = False
    toon_requested: bool = False
    source_outlines_requested: bool = False
    tap13_requested: bool = False
    gcf_argument_decode_requested: bool = False
    base64_lines_requested: bool = False
    ndjson_logs_requested: bool = False
    line_cap_requested: bool = False
    line_cap_rows: int = 40
    json_shape_requested: bool = False
    read_unnumbered_requested: bool = False
    progress_redraw_requested: bool = False
    lockfile_versions_requested: bool = False
    markdown_slim_requested: bool = False
    adaptive_verbosity_requested: bool = False
    gcf_same_request_rows_requested: bool = False
    git_diff_no_color_requested: bool = False
    unittest_success_requested: bool = False
    cargo_artifacts_requested: bool = False
    untracked_status_requested: bool = False
    gcf_graph_references_requested: bool = False
    gcf_session_view_requested: bool = False

@dataclass(frozen=True)
class AdmittedBrowserSnapshotPreference:
    revision: str
    requested: bool
    provider_id: str
    model_id: str

def admitted_browser_snapshot_preference(snapshot, provider_id: str, model_id: str) -> AdmittedBrowserSnapshotPreference | None:
    try:
        provider = snapshot.providers[provider_id]
        model = provider['models'][model_id]
        raw = (model.get('options') or {}).get('_openclankAgentSettings',
            (provider.get('options') or {}).get('_openclankAgentSettings'))
        if raw is None: return None
        settings = validate_agent_settings(raw)
        return AdmittedBrowserSnapshotPreference(agent_settings_revision(settings),
            settings.get('tokenomics', {}).get('browser_snapshot_repeat', False), provider_id, model_id)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

def admitted_gcf_preference(snapshot, provider_id: str, model_id: str) -> AdmittedGcfPreference | None:
    try:
        provider = snapshot.providers[provider_id]
        model = provider['models'][model_id]
        raw = (model.get('options') or {}).get('_openclankAgentSettings',
            (provider.get('options') or {}).get('_openclankAgentSettings'))
        if raw is None: return None
        settings = validate_agent_settings(raw)
        return AdmittedGcfPreference(agent_settings_revision(settings),
            settings.get('tokenomics', {}).get('gcf_filtering', False), provider_id, model_id,
            settings.get('tokenomics', {}).get('json_whitespace', False),
            settings.get('tokenomics', {}).get('gcf_memoization', False),
            settings.get('tokenomics', {}).get('terminal_ansi', False),
            settings.get('tokenomics', {}).get('html_markdown', False),
            settings.get('tokenomics', {}).get('structured_search', False),
            settings.get('tokenomics', {}).get('repeated_lines', False),
            settings.get('tokenomics', {}).get('unified_diff', False),
            settings.get('tokenomics', {}).get('schema_annotations', False),
            settings.get('tokenomics', {}).get('toon', False),
            settings.get('tokenomics', {}).get('source_outlines', False),
            settings.get('tokenomics', {}).get('tap13', False),
            settings.get('tokenomics', {}).get('gcf_argument_decode', False),
            settings.get('tokenomics', {}).get('base64_lines', False),
            settings.get('tokenomics', {}).get('ndjson_logs', False),
            settings.get('tokenomics', {}).get('line_cap', False),
            settings.get('tokenomics', {}).get('line_cap_rows', 40),
            settings.get('tokenomics', {}).get('json_shape', False),
            settings.get('tokenomics', {}).get('read_unnumbered', False),
            settings.get('tokenomics', {}).get('progress_redraw', False),
            settings.get('tokenomics', {}).get('lockfile_versions', False),
            settings.get('tokenomics', {}).get('markdown_slim', False),
            settings.get('tokenomics', {}).get('adaptive_verbosity', False),
            settings.get('tokenomics', {}).get('gcf_same_request_rows', False),
            settings.get('tokenomics', {}).get('git_diff_no_color', False),
            settings.get('tokenomics', {}).get('unittest_success', False),
            settings.get('tokenomics', {}).get('cargo_artifacts', False),
            settings.get('tokenomics', {}).get('untracked_status', False),
            settings.get('tokenomics', {}).get('gcf_graph_references', False),
            settings.get('tokenomics', {}).get('gcf_session_view', False))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

def admit_agent_settings(owner: str, settings: Mapping[str, Any], *, generation: int, context_window: int, max_output: int) -> AdmittedAgentSettings:
    effective = effective_agent_settings(settings, context_window=context_window, max_output=max_output)
    return AdmittedAgentSettings(str(owner), int(generation), _freeze(effective))

DEFAULT_AGENT_SETTINGS = {
    "compaction": {"auto": True, "prune": True, "tail_turns": 2, "preserve_recent_tokens": None, "reserved": None, "max_context": None},
    "checkpoint": {"reserved": 13000, "max_writer_failures": 3, "fork": False, "push_caps": {}},
    "suggestions": {"enabled": False},
    "worker_naming": {"enabled": False},
    "dream": {"auto": False, "interval_days": 7},
    "distill": {"auto": False, "interval_days": 30},
    "tokenomics": {"image_downscale": False, "browser_snapshot_repeat": False, "gcf_argument_decode": False, "gcf_filtering": False, "json_whitespace": False, "gcf_memoization": False, "terminal_ansi": False, "html_markdown": False, "structured_search": False, "repeated_lines": False, "unified_diff": False, "schema_annotations": False, "toon": False, "source_outlines": False, "tap13": False, "base64_lines": False, "ndjson_logs": False, "line_cap": False, "line_cap_rows": 40, "json_shape": False, "read_unnumbered": False, "progress_redraw": False, "lockfile_versions": False, "markdown_slim": False, "adaptive_verbosity": False, "gcf_same_request_rows": False, "git_diff_no_color": False, "unittest_success": False, "cargo_artifacts": False, "untracked_status": False, "gcf_graph_references": False, "gcf_session_view": False},
}

PUSH_CAPS = ("tasks_ledger", "focus_task", "actor_ledger", "memory_titles", "global", "checkpoint", "memory", "notes", "design_decisions", "open_notes", "recent_user", "recent_user_per_msg")

# Only fields consumed by the immutable managed native admission are writable.
FIELD_SPECS = {
    "suggestions.enabled": ("boolean", None, None, "Suggest a next prompt after a response using this chat’s model and thinking. Additional model usage; cost is unknown."),
    "worker_naming.enabled": ("boolean", None, None, "Request names for eligible new workers using the initiating model and Thinking. Current process, route and source checks apply; additional model usage and cost are unknown."),
    "tokenomics.image_downscale": ("boolean", None, None, "Optional image model view. Off by default; selected-model fidelity and cost qualification is unavailable."),
    "tokenomics.untracked_status": ("boolean", None, None, "Request bounded untracked-status-shaped text views"),
    "tokenomics.cargo_artifacts": ("boolean", None, None, "Request bounded Cargo artifact-output summaries"),
    "tokenomics.unittest_success": ("boolean", None, None, "Request bounded unittest success-output views"),
    "tokenomics.git_diff_no_color": ("boolean", None, None, "Request the exact no-color Git diff command substitution"),
    "tokenomics.gcf_session_view": ("boolean", None, None, "Experimental. Keep a scoped GCF source descriptor and explicitly reanchor authorized originals. Off by default."),
    "tokenomics.gcf_same_request_rows": ("boolean", None, None, "Request eligible same-request GCF row views"),
    "tokenomics.browser_snapshot_repeat": ("boolean", None, None, "Experimental. References exact repeated sanitized Browser snapshot text within one request. Off by default."),
    "tokenomics.gcf_graph_references": ("boolean", None, None, "Experimental. Sends a later Code Graph result as a difference against an earlier one in the same request when that is smaller. Off by default. Needs the GCF codec."),
    "tokenomics.adaptive_verbosity": ("boolean", None, None, "Request an advisory concise style for crowded eligible text context"),
    "tokenomics.markdown_slim": ("boolean", None, None, "Request bounded web Markdown omission views"),
    "tokenomics.lockfile_versions": ("boolean", None, None, "Request bounded npm lockfile version views"),
    "tokenomics.progress_redraw": ("boolean", None, None, "Request bounded carriage-return progress views"),
    "tokenomics.read_unnumbered": ("boolean", None, None, "Request native read views without generated line gutters"),
    "tokenomics.json_shape": ("boolean", None, None, "Request JSON structure views that omit scalar values"),
    "tokenomics.line_cap": ("boolean", None, None, "Request bounded native grep line-cap views"),
    "tokenomics.line_cap_rows": ("integer", 1, 100, "Maximum whole grep matches retained in a line-cap view"),
    "tokenomics.gcf_argument_decode": ("boolean", None, None, "Request bounded GCF arguments for eligible read-only tools; addon availability is checked per turn"),
    "tokenomics.ndjson_logs": ("boolean", None, None, "Request bounded NDJSON log model views"),
    "tokenomics.base64_lines": ("boolean", None, None, "Request base64-shaped line elision in eligible output"),
    "tokenomics.tap13": ("boolean", None, None, "Request bounded TAP 13 test-report views"),
    "tokenomics.toon": ("boolean", None, None, "Request bounded TOON model views for eligible MCP results"),
    "tokenomics.source_outlines": ("boolean", None, None, "Request source outline views when available"),
    "tokenomics.schema_annotations": ("boolean", None, None, "Request compact tool schema annotations for eligible main turns"),
    "tokenomics.repeated_lines": ("boolean", None, None, "Request exact repeated-line model views"),
    "tokenomics.unified_diff": ("boolean", None, None, "Request bounded unified-diff context views"),
    "tokenomics.terminal_ansi": ("boolean", None, None, "Request bounded terminal SGR model views"),
    "tokenomics.html_markdown": ("boolean", None, None, "Request bounded HTML-to-Markdown model views"),
    "tokenomics.structured_search": ("boolean", None, None, "Request bounded structured search model views"),
    "tokenomics.gcf_memoization": ("boolean", None, None, "Memoize eligible GCF encodings in memory for the admitted scope"),
    "tokenomics.gcf_filtering": ("boolean", None, None, "Request GCF filtering when the admitted codec, route and source are qualified"),
    "tokenomics.json_whitespace": ("boolean", None, None, "Request compact JSON tool views when the source and route are qualified"),
    "compaction.auto": ("boolean", None, None, "Automatic context compaction"),
    "compaction.prune": ("boolean", None, None, "Prune old tool output"),
    "compaction.tail_turns": ("integer", 0, 1000, "Recent turns kept during compaction"),
    "compaction.preserve_recent_tokens": ("integer", 0, 8_000_000, "Recent token budget; zero is explicit"),
    "compaction.reserved": ("integer", 0, 2_000_000, "Compaction token reserve"),
    "compaction.max_context": ("quantity", None, None, "Token count, K/M/G shorthand, percentage, or model-pattern map; zero removes the budget"),
    "checkpoint.thresholds": ("string_array", None, None, "Fill thresholds; an empty list disables scheduled checkpoints"),
    "checkpoint.reserved": ("integer", 0, 2_000_000, "Checkpoint token reserve"),
    "checkpoint.max_writer_failures": ("integer", 1, 100, "Consecutive writer failures before retries stop"),
    "checkpoint.fork": ("boolean", None, None, "Reuse the parent prefix when the provider supports it"),
    **{f"{section}.auto": ("boolean", None, None, "Automatic lifecycle operation evaluated at top-level turn admission") for section in ("dream", "distill")},
    **{f"{section}.interval_days": ("integer", 0, None, "Minimum interval between automatic lifecycle operations") for section in ("dream", "distill")},
    "checkpoint.task_archive_days": ("integer", 1, None, "Days after completion before tasks leave the active list; records remain preserved"),
    "checkpoint.memory_reconcile_on_search": ("boolean", None, None, "Reconcile native memory state on search"),
    "checkpoint.memory_search_score_floor": ("number", 0, 1, "Relative memory search score floor; zero keeps all matches"),
    "workflow.maxConcurrentAgents": ("integer", 1, None, "Concurrent workflow agents across this owner worker generation"),
    "workflow.maxDepth": ("integer", 1, None, "Maximum workflow nesting depth"),
    "workflow.maxLifecycleAgents": ("integer", 1, None, "Maximum agents spawned by one workflow run"),
    "workflow.scriptDeadlineMs": ("integer", 1, None, "Workflow script wall clock deadline in milliseconds"),
    "agent": ("object", None, None, "Named profiles: model_route_id or model_group (exclusive), prompt, description, mode, variant, temperature, top_p, steps, hidden, disable, color, tool_allowlist. Omitted model inherits the canonical selected model. Chat/Plan roles and permissions stay authoritative."),
    "default_agent": ("string", None, None, "Default eligible primary profile for Agent turns without a session override and native helper/default operations. Chat/Plan stay reserved. Main turns retain the selected canonical model."),
    "model_groups": ("object", None, None, "Named model groups: {default_model_route_id, model_route_ids:[...]}; every model resolves through the current owner canonical routes. Only explicit references select a different model."),
    "command": ("object", None, None, "Named command definitions: template (required), description, agent, model_route_id (canonical host route), subtask. Omitted model inherits."),
    "formatter": ("json", None, None, "Boolean or formatter records: disabled, command array, extensions array, environment references {env:NAME}. Account override preserves lower-layer files."),
    "lsp": ("json", None, None, "Boolean or LSP records: disabled:true or command array, extensions array, env references {env:NAME}, initialization JSON. Custom enabled entries require extensions."),
    "instructions": ("string_array", None, None, "Nonempty native file/URL instructions await canonical source owner integration. Use General Hex; empty or reset preserves trusted lower layers."),
    "watcher.ignore": ("string_array", None, None, "Workspace watcher ignore patterns"),
    "runtime_flags.try_best_handoff": ("boolean", None, None, "Enable Try-best detection on later worker generation; saved thresholds are inactive while this is false"),
    "never_ask": ("boolean", None, None, "Question owner: choose rather than ask on later worker generation; does not auto-approve permissions or answer pending questions"),
    "snapshot": ("boolean", None, None, "Legacy Git snapshot option; file recovery uses Lore."),
    "experimental.continue_loop_on_deny": ("boolean", None, None, "Continue after a denied tool call"),
    "experimental.try_best.edit_window": ("integer", 1, None, "Recent edit events compared for loop detection"),
    "experimental.try_best.edit_matches": ("integer", 1, None, "Similar prior edits before pausing"),
    "experimental.try_best.action_streak": ("integer", 1, None, "Edit or verification streak before pausing"),
    "experimental.try_best.edit_similarity": ("number", 0, 1, "Edit similarity threshold for loop detection"),
    **{f"checkpoint.push_caps.{key}": ("integer", 0 if key == "recent_user" else 1, None, "Rebuild context section token cap") for key in PUSH_CAPS},
}

FIELD_REFERENCE_PATHS = {
    "agent": ["agent.*", *[f"agent.*.{key}" for key in ("model", "variant", "temperature", "top_p", "prompt", "description", "disable", "mode", "hidden", "color", "steps", "tool_allowlist", "tool_allowlist[]")]],
    "command": ["command.*", *[f"command.*.{key}" for key in ("template", "description", "agent", "model", "subtask")]],
    "model_groups": ["model_groups.*", "model_groups.*.default", "model_groups.*.models", "model_groups.*.models[]"],
    "formatter": ["formatter.*", *[f"formatter.*.{key}" for key in ("disabled", "command", "command[]", "extensions", "extensions[]", "environment", "environment.*")]],
    "lsp": ["lsp.*", *[f"lsp.*.{key}" for key in ("disabled", "command", "command[]", "extensions", "extensions[]", "env", "env.*", "initialization", "initialization.*")]],
    "compaction.max_context": ["compaction.max_context.*"],
}

PROFILE_FIELDS = frozenset({"checkpoint.task_archive_days", "checkpoint.memory_reconcile_on_search", "checkpoint.memory_search_score_floor", "workflow.maxConcurrentAgents", "workflow.maxDepth", "workflow.maxLifecycleAgents", "workflow.scriptDeadlineMs", "watcher.ignore", "agent", "default_agent", "model_groups", "command", "formatter", "lsp", "instructions", "never_ask", "runtime_flags.try_best_handoff"})

def profile_agent_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    """Only typed instance consumers enter the canonical owner inline layer."""
    result = {}
    parsed = validate_agent_settings(value)
    for path in PROFILE_FIELDS:
        if path in {"never_ask", "runtime_flags.try_best_handoff"}: continue  # startup environment, not native Config
        source = parsed; parts = path.split(".")
        for part in parts:
            if not isinstance(source, Mapping) or part not in source: break
            source = source[part]
        else:
            target = result
            for part in parts[:-1]: target = target.setdefault(part, {})
            target[parts[-1]] = deepcopy(source)
    return result


def runtime_agent_settings(value: Mapping[str, Any]) -> dict[str, str]:
    parsed = validate_agent_settings(value)
    result = {"OPENCLANK_NEVER_ASK": "1" if parsed["never_ask"] else "0"} if "never_ask" in parsed else {}
    flags = parsed.get("runtime_flags", {})
    if "try_best_handoff" in flags: result["OPENCLANK_ENABLE_TRY_BEST_HANDOFF"] = "1" if flags["try_best_handoff"] else "0"
    return result

def agent_settings_schema() -> dict[str, Any]:
    fields = []
    for path, (kind, low, high, help_text) in FIELD_SPECS.items():
        item = {"path": path, "type": kind, "help": help_text, "scope": "account", "apply": "next_admission", "available": True, "owner": "agent_settings", "reset": "remove_override"}
        if path in PROFILE_FIELDS:
            item["scope"] = "worker-generation"
            item["apply"] = "next_generation"
            item["source"] = "owner_generation_env" if path in {"never_ask", "runtime_flags.try_best_handoff"} else "owner_inline_config"
        else: item["source"] = "managed_admission_overlay"
        item["reference_paths"] = [path, *FIELD_REFERENCE_PATHS.get(path, []), *([path + "[]"] if kind == "string_array" else [])]
        if path == "suggestions.enabled":
            item.update(label="Next-prompt suggestions", source="host_owner", apply="next_readback")
        if path == "worker_naming.enabled":
            item.update(label="Automatic worker names", source="host_owner", apply="next_admission",
                available=True, supported=True, resettable=True)
        if path == "tokenomics.image_downscale":
            item.update(available=False, supported=False, resettable=True, apply="unavailable",
                reason_code="IMAGE_MODEL_QUALIFICATION_UNAVAILABLE",
                reason="Selected-model image fidelity and cost qualification is unavailable; original images remain in use.")
        if path == "snapshot":
            item.update(available=False, supported=False, resettable=True, source="legacy_compatibility", apply="unavailable", reason_code="LORE_RECOVERY_OWNER", reason="Saved legacy value only. Git snapshot capture is unavailable; file recovery uses Lore.")
        if path == "checkpoint.max_writer_failures":
            item.update(available=False, supported=False, resettable=True, reason_code="CLASS_PROGRESS_RETRY_OWNER", reason="Saved legacy value only. Checkpoint retries use failure class and context progress; clear this override with Inherit.")
        if path == "instructions":
            item.update(available=False, supported=False, resettable=True, reason_code="INSTRUCTION_SOURCE_OWNER_REQUIRED", reason="Instruction paths/URLs need the existing Files permission checks; you can clear this override with Inherit")
        if path.startswith("dream."):
            item.update(available=False, supported=False, resettable=True, reason_code="MEMORY_POLICY_OWNER", reason="Compatibility value only. Use the Memory consolidation control for automatic maintenance.")
        if path.startswith("distill."):
            item.update(available=True, supported=True, resettable=True, source="account_scheduler_intent",
                apply="next_scheduler_wake", help="Saved request. The scheduler evaluates the current profile, configured teacher and eligible repeated sources when it runs; eligibility is not evaluated here.")
            if path == "distill.interval_days": item["max"] = 365
        if path.startswith("experimental.try_best."): item["gate"] = "runtime_flags.try_best_handoff"
        if low is not None: item["min"] = low
        if high is not None: item["max"] = high
        fields.append(item)
    return {"version": 1, "owner": "agent_settings", "scope": "account", "apply": "next_admission", "fields": fields}

def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping): return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list): return tuple(_freeze(item) for item in value)
    return value

def agent_settings_revision(value: Mapping[str, Any] | None) -> str:
    value = deepcopy(value or {})
    tokenomics = value.get("tokenomics") if isinstance(value, dict) else None
    if isinstance(tokenomics, dict) and tokenomics.get("line_cap") is True:
        tokenomics.setdefault("line_cap_rows", 40)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def _quantity(item: Any) -> bool:
    if type(item) in (int, float): return math.isfinite(item) and item >= 0
    if not isinstance(item, str): return False
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(%|[KMGkmg])?", item.strip())
    return bool(match and (match.group(2) != "%" or float(match.group(1)) <= 100))


def _string_list(value: Any, *, nonempty: bool = False) -> bool:
    return isinstance(value, list) and (bool(value) or not nonempty) and all(isinstance(item, str) for item in value)

SAFE_RUNTIME_ENV = frozenset({"HOME", "USERPROFILE", "TMPDIR", "PATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"})

def _safe_env_refs(value: Any) -> bool:
    if isinstance(value, str): return "{file:" not in value.lower() and all(name in SAFE_RUNTIME_ENV for name in re.findall(r"\{env:([^}]+)\}", value))
    if isinstance(value, Mapping): return all(_safe_env_refs(item) for item in value.values())
    if isinstance(value, list): return all(_safe_env_refs(item) for item in value)
    return True

def _safe_json(value: Any) -> bool:
    from src.settings_scrub import is_secret_key
    if isinstance(value, Mapping): return all(isinstance(key, str) and not is_secret_key(key) and _safe_json(item) for key, item in value.items())
    if isinstance(value, list): return all(_safe_json(item) for item in value)
    return value is None or type(value) in (bool, str, int) or (type(value) is float and math.isfinite(value))

def _definitions(path: str, value: Any) -> bool:
    if path in {"formatter", "lsp"} and type(value) is bool: return True
    if not isinstance(value, Mapping): return False
    for name, entry in value.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, Mapping): return False
        if path == "model_groups":
            if set(entry) - {"default_model_route_id", "model_route_ids"} or not isinstance(entry.get("default_model_route_id"), str) or not entry["default_model_route_id"].strip(): return False
            if "model_route_ids" in entry and not _string_list(entry["model_route_ids"]): return False
            if any(not item.strip() for item in entry.get("model_route_ids", [])): return False
        elif path == "agent":
            allowed = {"model_route_id", "model_group", "variant", "temperature", "top_p", "prompt", "description", "disable", "mode", "hidden", "color", "steps", "tool_allowlist"}
            if set(entry) - allowed or ("model_route_id" in entry and "model_group" in entry): return False
            if any(key in entry and (not isinstance(entry[key], str) or not entry[key].strip()) for key in ("model_route_id", "model_group", "variant")): return False
            if any(key in entry and not isinstance(entry[key], str) for key in ("prompt", "description")): return False
            if "model_group" in entry and "/" in entry["model_group"]: return False
            if any(key in entry and type(entry[key]) is not bool for key in ("disable", "hidden")): return False
            if "mode" in entry and entry["mode"] not in {"primary", "subagent", "all"}: return False
            if name in {"build", "plan", "chat"} and (entry.get("disable") or entry.get("hidden") or entry.get("mode") == "subagent"): return False
            if "steps" in entry and (type(entry["steps"]) is not int or not 0 < entry["steps"] <= 9_007_199_254_740_991): return False
            if any(key in entry and (type(entry[key]) not in (int, float) or not math.isfinite(entry[key])) for key in ("temperature", "top_p")): return False
            if "tool_allowlist" in entry and not _string_list(entry["tool_allowlist"]): return False
            if "color" in entry and (not isinstance(entry["color"], str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}|primary|secondary|accent|error|warning|success|info", entry["color"])): return False
        elif path == "command":
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name): return False
            if set(entry) - {"template", "description", "agent", "model_route_id", "subtask"} or not isinstance(entry.get("template"), str): return False
            if any(key in entry and not isinstance(entry[key], str) for key in ("description", "agent", "model_route_id")): return False
            if "subtask" in entry and type(entry["subtask"]) is not bool: return False
        else:
            allowed = {"disabled", "command", "extensions", "environment"} if path == "formatter" else {"disabled", "command", "extensions", "env", "initialization"}
            if set(entry) - allowed: return False
            if "disabled" in entry and type(entry["disabled"]) is not bool: return False
            if "command" in entry and not _string_list(entry["command"], nonempty=True): return False
            if "extensions" in entry and not _string_list(entry["extensions"]): return False
            if path == "lsp" and entry.get("disabled") is not True and (not _string_list(entry.get("command"), nonempty=True) or not _string_list(entry.get("extensions"))): return False
            from src.settings_scrub import is_secret_key
            refs = entry.get("environment" if path == "formatter" else "env", {})
            if not isinstance(refs, Mapping) or any(not isinstance(key, str) or is_secret_key(key) or not isinstance(ref, str) or not re.fullmatch(r"\{env:[A-Za-z_][A-Za-z0-9_]*\}", ref) for key, ref in refs.items()): return False
            if "initialization" in entry and (not isinstance(entry["initialization"], Mapping) or not _safe_json(entry["initialization"])): return False
    return True

def validate_agent_settings(value: Any) -> dict[str, Any]:
    if value is None: return {}
    if not isinstance(value, Mapping): raise ValueError("agent_settings must be an object")
    if not _safe_env_refs(value): raise ValueError("Only runner-owned nonsecret environment references are supported; native file expansion requires canonical Files authority")
    def visit(obj: Mapping, prefix: str = "") -> dict:
        result = {}
        for key, item in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if path not in FIELD_SPECS:
                if not any(field.startswith(path + ".") for field in FIELD_SPECS):
                    raise ValueError(f"Unsupported agent setting: {path}")
                if not isinstance(item, Mapping): raise ValueError(f"{path} must be an object")
                result[key] = visit(item, path)
                continue
            if path == "instructions" and item not in (None, []):
                raise ValueError("instructions requires canonical source owner integration (INSTRUCTION_SOURCE_OWNER_REQUIRED)")
            if item is None: continue  # owner inherit/reset; never a native null value
            kind, low, high, _ = FIELD_SPECS[path]
            if kind == "boolean": valid = type(item) is bool
            elif kind == "string": valid = isinstance(item, str) and bool(item.strip()) and (path != "default_agent" or item not in {"plan", "chat"})
            elif kind == "integer": valid = type(item) is int and item >= low and (high is None or item <= high) and item <= 9_007_199_254_740_991
            elif kind == "number": valid = type(item) in (int, float) and math.isfinite(item) and low <= item <= high
            elif kind == "quantity":
                valid = _quantity(item) or (isinstance(item, Mapping) and all(isinstance(name, str) and bool(name.strip()) and _quantity(quantity) for name, quantity in item.items()))
            elif kind == "string_array":
                valid = isinstance(item, list) and all(isinstance(entry, str) and (path != "checkpoint.thresholds" or (_quantity(entry) and float(re.match(r"\d+(?:\.\d+)?", entry.strip()).group()) > 0)) for entry in item)
            elif kind in {"object", "json"}: valid = _definitions(path, item)
            else: valid = False
            if not valid: raise ValueError(f"{path} has an invalid {kind} value")
            result[key] = deepcopy(item)
        return result
    return visit(value)

def patch_agent_settings(current: Mapping[str, Any], update: Any, *, reset: Any = None) -> dict[str, Any]:
    """Sparse object merge; arrays/maps are values, reset deletes only a path."""
    parsed = validate_agent_settings(update)
    distill = update.get("distill") if isinstance(update, Mapping) else None
    if isinstance(distill, Mapping) and distill.get("interval_days") is not None:
        value = distill["interval_days"]
        if type(value) is not int or not 0 <= value <= 365:
            raise ValueError("distill.interval_days must be between 0 and 365")
    if isinstance(update, Mapping) and update.get("snapshot") is not None:
        raise ValueError("snapshot is unavailable (LORE_RECOVERY_OWNER)")
    if isinstance(update, Mapping) and isinstance(update.get("checkpoint"), Mapping) and update["checkpoint"].get("max_writer_failures") is not None:
        raise ValueError("checkpoint.max_writer_failures is unavailable (CLASS_PROGRESS_RETRY_OWNER)")
    result = deepcopy(dict(current))
    def merge(target: dict, source: dict, prefix: str = "") -> None:
        for key, item in source.items():
            path = f"{prefix}.{key}" if prefix else key
            # Quantities include maps; replace a quantity map as one value.
            if isinstance(item, dict) and path not in FIELD_SPECS:
                if item:
                    if not isinstance(target.get(key), dict): target[key] = {}
                    merge(target[key], item, path)
            else: target[key] = deepcopy(item)
    merge(result, parsed)
    null_paths = []
    def inherited(obj, prefix=""):
        if not isinstance(obj, Mapping): return
        for key, item in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if path in FIELD_SPECS:
                if item is None: null_paths.append(path)
            else: inherited(item, path)
    inherited(update)
    if reset is not None or null_paths:
        if reset is not None and (not isinstance(reset, list) or any(not isinstance(path, str) or path not in FIELD_SPECS for path in reset)):
            raise ValueError("agent_settings_reset must list supported field paths")
        for path in [*(reset or []), *null_paths]:
            parts = path.split("."); node = result; ancestors = []
            for key in parts[:-1]:
                if not isinstance(node.get(key), dict): break
                ancestors.append((node, key)); node = node[key]
            else:
                node.pop(parts[-1], None)
                for parent, key in reversed(ancestors):
                    if parent[key] == {}: parent.pop(key)
    return validate_agent_settings(result)

def effective_agent_settings(settings: Mapping[str, Any], *, context_window: int, max_output: int) -> dict[str, Any]:
    c = {**DEFAULT_AGENT_SETTINGS["compaction"], **dict(settings.get("compaction", {}))}
    k = {**DEFAULT_AGENT_SETTINGS["checkpoint"], **dict(settings.get("checkpoint", {}))}
    k.pop("max_writer_failures", None)  # retained saved legacy value is not an admitted native option
    usable = max(0, int(context_window))
    recent = c["preserve_recent_tokens"] if c["preserve_recent_tokens"] is not None else max(2000, min(8000, usable // 4))
    # Native overflow.py keeps an explicitly configured reserve verbatim. Its
    # output-headroom rule is applied separately, so do not approximate it here.
    reserve = c["reserved"]
    max_context = c["max_context"]
    if isinstance(max_context, str) and max_context.lower().endswith("k"): max_context = int(float(max_context[:-1]) * 1000)
    if isinstance(max_context, (int, float)) and max_context > 0: usable = min(usable, int(max_context))
    return {"compaction": {**c, "preserve_recent_tokens": recent, "reserved": reserve, "effective_window": usable}, "checkpoint": k,
            "dream": {**DEFAULT_AGENT_SETTINGS["dream"], **dict(settings.get("dream", {}))},
            "distill": {**DEFAULT_AGENT_SETTINGS["distill"], **dict(settings.get("distill", {}))},
            "tokenomics": {**DEFAULT_AGENT_SETTINGS["tokenomics"], **dict(settings.get("tokenomics", {}))}}


def model_route_references(settings: Mapping[str, Any]):
    for section in ("command", "agent"):
        for name, entry in settings.get(section, {}).items():
            if entry.get("model_route_id"): yield f"{section}.{name}.model_route_id", entry["model_route_id"]
    for name, entry in settings.get("model_groups", {}).items():
        yield f"model_groups.{name}.default_model_route_id", entry["default_model_route_id"]
        for index, route_id in enumerate(entry.get("model_route_ids", [])):
            yield f"model_groups.{name}.model_route_ids[{index}]", route_id
