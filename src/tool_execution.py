"""
tool_execution.py

Tool dispatcher and result formatter for the agent loop.
Routes tool blocks to MCP servers or native implementations.

Extracted from agent_tools.py.
"""

import asyncio
import collections
import contextvars
import hashlib
import json
import logging
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple



from src.tool_security import (
    BUILTIN_EMAIL_TOOLS,
    email_tool_policy_names,
    is_scoped_file_tool_allowed,
    is_public_blocked_tool,
    owner_is_admin_or_single_user,
)
from src.tool_policy import ToolPolicy, known_tool_names
from src.constants import MAX_OUTPUT_CHARS, MAX_READ_CHARS, MAX_DIFF_LINES, DATA_DIR
from src.tool_utils import _truncate, get_mcp_manager

# The dispatcher owns every result envelope sent back to a model. Keep result
# integrity, preview, and shell notices at this seam instead of adding a second
# policy or approval authority to individual tools.
TOOL_RESULT_PREVIEW_MAX_LINES = 2_000
TOOL_RESULT_PREVIEW_MAX_BYTES = 50 * 1024

_TOOL_CAPABILITY_REGISTRY = {
    "shell_execution": frozenset({"bash", "python"}),
    "local_read": frozenset({"read_file", "grep", "glob", "ls", "get_workspace"}),
    "local_mutation": frozenset({"write_file", "apply_patch", "todowrite", "manage_files"}),
}
_RAW_TOOL_RESULT_FIELDS = frozenset({"output", "stdout", "stderr"})
_DISPATCHER_ONLY_TOOL_NAMES = frozenset({
    "adopt_served_model", "app_api", "apply_patch", "cancel_download",
    "create_document", "create_session", "download_model", "edit_document",
    "edit_file", "edit_image", "generate_image", "list_cached_models",
    "list_cookbook_servers", "list_downloads", "list_serve_presets",
    "list_served_models", "list_sessions", "manage_bg_jobs", "manage_calendar",
    "manage_copal", "manage_endpoints", "manage_mcp", "manage_memory",
    "manage_notes", "manage_research", "manage_session", "manage_settings",
    "manage_skills", "manage_tasks", "manage_tokens", "manage_webhooks",
    "pipeline", "publish_file", "read_copal", "recall_memory", "resolve_contact",
    "search_chats", "search_hf_models", "send_to_session", "serve_model",
    "serve_preset", "stop_served_model", "suggest_document", "tail_serve_output",
    "todowrite", "trigger_research", "ui_control", "update_document", "vault_get",
    "vault_search", "vault_unlock",
    "json", "xml",
})


def _split_shell_argv(command: str) -> list[list[str]]:
    """Split literal shell words into command argv groups without evaluating them."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    groups: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(char in ";&|\n" for char in token):
            if current:
                groups.append(current)
                current = []
        else:
            current.append(token)
    if current:
        groups.append(current)
    return groups


def _command_start_index(argv: list[str]) -> int:
    """Skip literal command wrappers without interpreting a shell command."""
    index = 0
    while index < len(argv) and (argv[index] == "command" or ("=" in argv[index] and not argv[index].startswith("-"))):
        index += 1
    if index < len(argv) and argv[index] == "env":
        index += 1
        while index < len(argv) and (argv[index].startswith("-") or ("=" in argv[index] and not argv[index].startswith("-"))):
            index += 1
    if index < len(argv) and os.path.basename(argv[index]) == "sudo":
        index += 1
        options_with_value = {"-C", "-D", "-g", "-h", "-p", "-r", "-t", "-u", "--chdir", "--close-from", "--group", "--host", "--prompt", "--role", "--type", "--user"}
        while index < len(argv) and argv[index].startswith("-"):
            option = argv[index]
            index += 2 if option in options_with_value else 1
    return index


def _git_command_parts(argv: list[str]) -> tuple[Optional[str], list[str]]:
    """Return a direct Git subcommand and its arguments from a tokenized argv."""
    index = _command_start_index(argv)
    if index >= len(argv) or os.path.basename(argv[index]) != "git":
        return None, []
    index += 1
    options_with_value = {
        "-C", "-c", "--git-dir", "--work-tree", "--namespace",
        "--exec-path", "--config-env", "--super-prefix",
    }
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return None, []
        if token in options_with_value:
            index += 2
            continue
        if token.startswith("--git-dir=") or token.startswith("--work-tree="):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, argv[index + 1:]
    return None, []


def _shell_command_substitutions(command: str) -> list[str]:
    """Extract executable ``$(...)`` bodies while respecting literal quotes."""
    bodies: list[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char in {"'", '"'}:
            quote = None if quote == char else (char if quote is None else quote)
            index += 1
            continue
        if quote != "'" and command.startswith("$(", index):
            depth, start = 1, index + 2
            cursor, nested_quote = start, None
            while cursor < len(command) and depth:
                nested = command[cursor]
                if nested == "\\" and nested_quote != "'":
                    cursor += 2
                    continue
                if nested in {"'", '"'}:
                    nested_quote = None if nested_quote == nested else (nested if nested_quote is None else nested_quote)
                elif nested_quote != "'" and nested == "(":
                    depth += 1
                elif nested_quote != "'" and nested == ")":
                    depth -= 1
                cursor += 1
            if depth == 0:
                bodies.append(command[start:cursor - 1])
                index = cursor
                continue
        index += 1
    return bodies


def _shell_backtick_substitutions(command: str) -> list[str]:
    """Extract legacy backtick command substitutions outside single quotes."""
    bodies: list[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char == "'":
            if quote is None:
                quote = "'"
            elif quote == "'":
                quote = None
            index += 1
            continue
        if char == '"':
            if quote is None:
                quote = '"'
            elif quote == '"':
                quote = None
            index += 1
            continue
        if char == "`" and quote != "'":
            cursor = index + 1
            while cursor < len(command):
                if command[cursor] == "\\":
                    cursor += 2
                    continue
                if command[cursor] == "`":
                    bodies.append(command[index + 1:cursor])
                    index = cursor + 1
                    break
                cursor += 1
            else:
                index += 1
            continue
        index += 1
    return bodies


def _nested_shell_block_reason(command: str, context: Dict) -> Optional[str]:
    """Inspect literal shell syntax for a blocked Git mutation without running it."""
    for argv in _split_shell_argv(command):
        # Shell grouping punctuation is a syntactic wrapper, not a command.
        while argv and argv[0] in {"(", "{"}:
            argv = argv[1:]
        while argv and argv[-1] in {")", "}"}:
            argv = argv[:-1]
        if not argv:
            continue
        reason = _isolated_git_command_block_reason(argv, context)
        if reason:
            return reason
        start = _command_start_index(argv)
        if start < len(argv) and os.path.basename(argv[start]) in {"bash", "sh", "zsh"}:
            try:
                script_index = argv.index("-c", start + 1) + 1
            except ValueError:
                script_index = -1
            if 0 <= script_index < len(argv):
                nested_reason = _nested_shell_block_reason(argv[script_index], context)
                if nested_reason:
                    return nested_reason
    for body in [*_shell_command_substitutions(command), *_shell_backtick_substitutions(command)]:
        nested_reason = _nested_shell_block_reason(body, context)
        if nested_reason:
            return nested_reason
    return None


def _git_text(workspace: str, *args: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", workspace, *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _isolated_worktree_context(workspace: object) -> Optional[Dict]:
    root = os.path.realpath(str(workspace or ""))
    registered = _git_text(root, "worktree", "list", "--porcelain")
    if not registered:
        return None
    registered_paths = {
        os.path.realpath(line[9:])
        for line in registered.splitlines()
        if line.startswith("worktree ")
    }
    if root not in registered_paths or len(registered_paths) < 2:
        return None
    git_dir = _git_text(root, "rev-parse", "--git-dir")
    if not git_dir:
        return None
    git_dir = git_dir.strip()
    if not os.path.isabs(git_dir):
        git_dir = os.path.realpath(os.path.join(root, git_dir))
    # Linked worktrees have a per-worktree Git directory, while the primary
    # checkout points at the common .git directory.
    if "/worktrees/" not in git_dir.replace("\\", "/"):
        return None
    branch = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return {"root": root, "git_dir": git_dir, "branch": (branch or "").strip()}


def _has_force_or_delete(arguments: list[str]) -> bool:
    return any(
        argument in {"-f", "-D", "-d", "--force", "--delete", "--force-with-lease"}
        or argument.startswith("--force-with-lease=")
        or (argument.startswith("-") and not argument.startswith("--") and "f" in argument[1:])
        for argument in arguments
    )


_GIT_RECOVERY_FLAGS = frozenset({
    "--abort", "--continue", "--skip", "--quit", "--edit-todo",
    "--show-current-patch",
})


def _git_positionals(arguments: list[str]) -> list[str]:
    """Return simple positional arguments from an already-tokenized Git argv."""
    values: list[str] = []
    after_separator = False
    for argument in arguments:
        if after_separator:
            values.append(argument)
        elif argument == "--":
            after_separator = True
        elif argument == "-" or not argument.startswith("-"):
            values.append(argument)
    return values


def _git_flag_value(arguments: list[str], flags: frozenset[str]) -> Optional[str]:
    for index, argument in enumerate(arguments):
        for flag in flags:
            if argument == flag:
                return arguments[index + 1] if index + 1 < len(arguments) else ""
            if argument.startswith(flag + "="):
                return argument[len(flag) + 1:]
    return None


def _owned_git_ref(name: str, branch: str) -> bool:
    normalized = name.removeprefix("refs/heads/")
    return bool(branch) and normalized in {branch, "HEAD"}


def _git_checkout_target_is_ref(context: Dict, target: str) -> bool:
    """Whether Git resolves a checkout target as an object or ref, not a path."""
    root = str(context.get("root") or "")
    return _git_text(root, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}") is not None


def _push_target(refspec: str) -> str:
    spec = refspec.removeprefix("+")
    target = spec.rsplit(":", 1)[-1]
    return target.removeprefix("refs/heads/")


def _isolated_git_command_block_reason(argv: list[str], context: Dict) -> Optional[str]:
    subcommand, arguments = _git_command_parts(argv)
    if not subcommand:
        return None
    subcommand = subcommand.lower()
    branch = str(context.get("branch") or "")
    if subcommand in {"replace", "pack-refs"}:
        return "direct Git ref mutation"
    if subcommand == "worktree":
        if not arguments or arguments[0] != "list":
            return "shared Git worktree registry mutation"
        return None
    if subcommand in {"merge", "rebase"}:
        if set(arguments).intersection(_GIT_RECOVERY_FLAGS):
            return None
        return f"Git {subcommand}"
    if subcommand == "branch":
        mutates = _has_force_or_delete(arguments) or bool(
            set(arguments).intersection({"-m", "-M", "--move", "--copy"})
        )
        if not mutates:
            return None
        positionals = _git_positionals(arguments)
        if positionals and all(_owned_git_ref(name, branch) for name in positionals):
            return None
        if mutates:
            return "forced, deleted, or renamed Git branch"
        return None
    if subcommand == "tag":
        if _has_force_or_delete(arguments):
            return "forced or deleted Git tag"
        return None
    if subcommand == "push":
        positionals = _git_positionals(arguments)
        refspecs = positionals[1:]
        mutates = _has_force_or_delete(arguments) or any(
            spec.startswith(":") or spec.startswith("+") for spec in refspecs
        )
        if not mutates or not refspecs:
            return None
        if all(_owned_git_ref(_push_target(spec), branch) for spec in refspecs):
            return None
        return "forced or deleted Git push to a foreign ref"
    if subcommand == "update-ref":
        positionals = _git_positionals(arguments)
        if positionals and _owned_git_ref(positionals[0], branch):
            return None
        return "direct Git ref mutation"
    if subcommand == "symbolic-ref":
        positionals = _git_positionals(arguments)
        if len(positionals) < 2 and not set(arguments).intersection({"--delete", "-d"}):
            return None
        return "direct Git symbolic-ref mutation"
    if subcommand in {"checkout", "switch"}:
        if "--" in arguments:
            return None
        created = _git_flag_value(arguments, frozenset({"-b", "-c", "--orphan"}))
        if created is not None:
            return None
        forced = _git_flag_value(arguments, frozenset({"-B", "-C"}))
        if forced is not None:
            return None if _owned_git_ref(forced, branch) else "force-created foreign Git branch"
        positionals = _git_positionals(arguments)
        if len(positionals) >= 2:
            return None
        if not positionals:
            return "detached Git checkout" if "--detach" in arguments else None
        target = positionals[0]
        target_path = target if os.path.isabs(target) else os.path.join(str(context.get("root") or ""), target)
        if _owned_git_ref(target, branch):
            return None
        # A same-named path must not allow checkout of a foreign branch. Git
        # resolves refs before paths, so allow the path form only when it does
        # not resolve as a Git object or ref.
        if os.path.exists(target_path) and not _git_checkout_target_is_ref(context, target):
            return None
        return "foreign Git checkout" if subcommand == "checkout" else "foreign Git switch"
    return None


def isolated_worktree_ref_mutation_guard(command: object, workspace: object) -> Optional[str]:
    """Bound Git ref/worktree mutations only for registered linked children."""
    if not isinstance(command, str):
        return None
    context = _isolated_worktree_context(workspace)
    if not context:
        return None
    reason = _nested_shell_block_reason(command, context)
    if reason:
        return (
            f"{reason} is blocked in an isolated child worktree; "
            "use that worktree's own branch or a nonisolated session"
        )
    return None


def _tool_result_source(result: Dict) -> str:
    """Build a stable digest input without altering raw tool-result fields."""
    envelope_fields = {
        "preview", "preview_truncated", "preview_mode", "tail_digest",
        "result_integrity", "merge_conflict", "output_capture",
    }
    chunks = []
    for key in sorted(key for key in result if key not in envelope_fields):
        value = result[key]
        if isinstance(value, str):
            rendered = value
        else:
            try:
                rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            except (TypeError, ValueError):
                rendered = repr(value)
        chunks.append(f"[{key}]\n{rendered}")
    return "\n".join(chunks)


def _limit_preview_bytes(value: str, *, tail: bool) -> tuple[str, bool]:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= TOOL_RESULT_PREVIEW_MAX_BYTES:
        return value, False
    selected = raw[-TOOL_RESULT_PREVIEW_MAX_BYTES:] if tail else raw[:TOOL_RESULT_PREVIEW_MAX_BYTES]
    return selected.decode("utf-8", errors="ignore"), True


def _tool_result_preview(result: Dict) -> Dict:
    """Bound model-visible output while preserving every raw result field."""
    source = _tool_result_source(result)
    lines = source.splitlines()
    failed = result.get("exit_code") not in (None, 0) or bool(result.get("error"))
    if len(lines) > TOOL_RESULT_PREVIEW_MAX_LINES:
        selected = lines[-TOOL_RESULT_PREVIEW_MAX_LINES:] if failed else lines[:TOOL_RESULT_PREVIEW_MAX_LINES]
        line_truncated = True
    else:
        selected = lines
        line_truncated = False
    preview, byte_truncated = _limit_preview_bytes("\n".join(selected), tail=failed)
    encoded_source = source.encode("utf-8", errors="replace")
    return {
        "preview": preview,
        "preview_truncated": line_truncated or byte_truncated,
        "preview_mode": "error-tail" if failed else "head",
        "tail_digest": {
            "algorithm": "sha256",
            "value": hashlib.sha256(encoded_source).hexdigest(),
            "source_lines": len(lines),
            "source_bytes": len(encoded_source),
            "max_lines": TOOL_RESULT_PREVIEW_MAX_LINES,
            "max_bytes": TOOL_RESULT_PREVIEW_MAX_BYTES,
        },
    }


def _workspace_git_conflict_state(workspace: object) -> Optional[Dict]:
    """Read Git's index and operation files; terminal text is never evidence."""
    context = _isolated_worktree_context(workspace)
    root = context["root"] if context else os.path.realpath(str(workspace or ""))
    git_dir = _git_text(root, "rev-parse", "--git-dir")
    if not git_dir:
        return None
    git_dir = git_dir.strip()
    if not os.path.isabs(git_dir):
        git_dir = os.path.realpath(os.path.join(root, git_dir))
    unmerged = _git_text(root, "ls-files", "--unmerged", "-z") or ""
    paths = sorted({entry.rsplit("\t", 1)[-1] for entry in unmerged.split("\0") if "\t" in entry})
    operations = []
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if os.path.exists(os.path.join(git_dir, name)):
            operations.append(name.removesuffix("_HEAD").lower())
    if os.path.isdir(os.path.join(git_dir, "rebase-merge")):
        operations.append("rebase")
    if os.path.isdir(os.path.join(git_dir, "rebase-apply")):
        operations.append("rebase-or-am")
    if not paths and not operations:
        return None
    return {
        "detected": bool(paths),
        "state": "conflicted" if paths else "operation_in_progress",
        "unmerged_paths": paths,
        "in_progress_operations": operations,
        "resolution": "manual_required",
    }


def _declared_tool_capability(tool: object, dynamic_handlers: Optional[Dict] = None) -> Optional[Dict]:
    """Resolve every dispatcher effect through its one existing authority."""
    if not isinstance(tool, str) or not tool:
        return None
    for effect, names in _TOOL_CAPABILITY_REGISTRY.items():
        if tool in names:
            return {"effect": effect, "capability": effect}
    if tool in _MCP_TOOL_MAP:
        return {"effect": "external_tool_execution", "capability": "mcp_tool"}
    if tool in BUILTIN_EMAIL_TOOLS:
        return {"effect": "external_tool_execution", "capability": "email_tool"}
    if dynamic_handlers and tool in dynamic_handlers:
        return {"effect": "registered_tool_execution", "capability": "registered_tool"}
    if tool in _ADMIN_TOOLS or tool in {"publish_file"}:
        return {"effect": "privileged_tool_execution", "capability": "privileged_tool"}
    if tool in _DISPATCHER_ONLY_TOOL_NAMES or tool in known_tool_names() or tool.startswith("mcp__"):
        return {"effect": "declared_tool_execution", "capability": "tool_policy_registered"}
    return None


def _validate_untrusted_tool_result(result: object) -> Optional[str]:
    """Reject malformed result content before it reaches the model formatter."""
    if not isinstance(result, dict):
        return "tool handler returned a non-object result"
    for field in _RAW_TOOL_RESULT_FIELDS:
        if field in result and not isinstance(result[field], str):
            return f"tool result field '{field}' is not text"
    if "exit_code" in result and result["exit_code"] is not None and not isinstance(result["exit_code"], int):
        return "tool result exit_code is not an integer"
    return None


def finalize_tool_result(
    tool: str,
    result: Dict,
    *,
    capability: Optional[Dict] = None,
    workspace: Optional[str] = None,
) -> Dict:
    """Apply the dispatcher's capability and untrusted-result integrity gate."""
    capability = capability or _declared_tool_capability(tool)
    issue = _validate_untrusted_tool_result(result)
    if issue:
        return {
            "error": f"tool result rejected by integrity gate: {issue}",
            "exit_code": 1,
            "blocked": True,
            "result_integrity": {
                "trusted": False,
                "source": "tool-result",
                "validated": False,
                "authority": "tool_dispatcher",
            },
        }
    preview = _tool_result_preview(result)
    capture = result.get("output_capture")
    if capture:
        # A subprocess may retain only bounded inline text. Its streaming
        # metadata is the authority for a complete observed-stream digest.
        preview["tail_digest"]["scope"] = "inline_result"
        preview["tail_digest"]["source_complete"] = False
    result.update(preview)
    result["result_integrity"] = {
        "trusted": False,
        "source": "tool-result",
        "validated": True,
        "capability": (capability or {}).get("capability", "tool_execution"),
        "effect": (capability or {}).get("effect", "tool_execution"),
        "authority": "tool_dispatcher",
    }
    conflict = _workspace_git_conflict_state(workspace) if tool in {"bash", "python"} and workspace else None
    if conflict:
        result["merge_conflict"] = conflict
    return result

# Persistent working directory for agent subprocesses.
# Resolves to <repo_root>/data, which is the bind-mounted volume in Docker
# (/app/data) and the local data directory for manual installs.
# Using this as cwd and HOME prevents the agent from silently creating files
# in ephemeral container layers that are lost on the next rebuild.
_AGENT_WORKDIR = DATA_DIR



# ---------------------------------------------------------------------------
# Path confinement for read_file / write_file
# ---------------------------------------------------------------------------
# File tools are server-scoped, but the path the agent supplies is
# model-controlled. Prompt-injection in an admin's chat can weaponise
# "read /etc/shadow" or "write ~/.ssh/authorized_keys" without the admin
# noticing; regular users additionally require an administrator-issued
# visibility assignment and the Rust lane remains authoritative.
#
# Policy:
#   1. Sensitive-subpath deny list — checked FIRST. Blocks .ssh,
#      .gnupg, shell rc files, token/env files even if the root above
#      them is on the allowlist.
#   2. Allowlist — only the directories the agent legitimately needs
#      (project data/, system tmp). $HOME is NOT on the default list.
#   3. Opt-in extra roots — admin can add broader roots via the
#      "tool_path_extra_roots" setting (list of path strings).
# ---------------------------------------------------------------------------

_SENSITIVE_BASENAMES: set[str] = {
    ".ssh", ".gnupg", ".gitconfig",
    ".bashrc", ".bash_profile", ".bash_logout",
    ".zshrc", ".zprofile", ".zshenv",
    ".profile", ".tcshrc", ".cshrc",
    ".env", ".netrc",
}

_SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    "authorized_keys", "id_rsa", "id_ed25519", "id_ecdsa",
    "known_hosts",
)

# Case-folded views used for matching. On a case-insensitive filesystem
# (Windows, default macOS) ".SSH/AUTHORIZED_KEYS" and ".env" resolve to the
# same protected files as their lowercase forms, so the deny-list has to fold
# case before comparing — the sibling resolver already normcases paths for the
# same reason. casefold (not os.path.normcase) because normcase is a no-op on
# POSIX, which is exactly where the macOS read-exfil path lives.
_SENSITIVE_BASENAMES_CF: frozenset[str] = frozenset(b.casefold() for b in _SENSITIVE_BASENAMES)
_SENSITIVE_FILE_PATTERNS_CF: frozenset[str] = frozenset(p.casefold() for p in _SENSITIVE_FILE_PATTERNS)


def _is_sensitive_path(resolved: str) -> bool:
    """Return True if *resolved* falls under a sensitive directory or
    matches a sensitive filename — regardless of what root it sits under.

    Matching is case-insensitive: on Windows / default macOS a case-variant
    name (``.SSH``, ``AUTHORIZED_KEYS``, ``Id_Rsa``) points at the same file as
    the lowercase form, so a case-sensitive check would let it slip past the
    deny-list in every file tool that relies on it.
    """
    parts = [p.casefold() for p in resolved.split(os.sep)]
    filename = parts[-1] if parts else ""

    # Check if any path component is a sensitive directory.
    for part in parts:
        if part in _SENSITIVE_BASENAMES_CF:
            return True

    # Check filename against known sensitive files.
    return filename in _SENSITIVE_FILE_PATTERNS_CF


def _control_data_roots() -> list[str]:
    """Canonical roots owned by Open Clank rather than the active workspace."""
    from src.constants import AUTH_FILE, DATA_DIR, FM_DB_PATH

    mimocode_home = os.environ.get("MIMOCODE_HOME")
    candidates = [
        DATA_DIR,
        os.path.dirname(AUTH_FILE),
        AUTH_FILE,
        FM_DB_PATH,
        f"{FM_DB_PATH}-wal",
        f"{FM_DB_PATH}-shm",
        f"{FM_DB_PATH}-journal",
        os.environ.get("OPEN_CLANK_CONTROL_DATA_DIR"),
        os.environ.get("OPEN_CLANK_SKILLS_DIR"),
        mimocode_home,
    ]
    home = os.path.expanduser("~")
    candidates.extend(
        os.path.join(
            os.environ.get(env_name) or os.path.join(home, fallback),
            "mimocode",
        )
        for env_name, fallback in (
            ("XDG_DATA_HOME", ".local/share"),
            ("XDG_CONFIG_HOME", ".config"),
            ("XDG_STATE_HOME", ".local/state"),
            ("XDG_CACHE_HOME", ".cache"),
        )
    )
    roots: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        resolved = os.path.realpath(os.path.expanduser(candidate))
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _is_control_data_path(resolved: str) -> bool:
    """Return True when a canonical path is Open Clank runtime control data."""
    target = os.path.normcase(os.path.realpath(resolved)).casefold()
    for root in _control_data_roots():
        normalized_root = os.path.normcase(root).casefold()
        try:
            if os.path.commonpath([target, normalized_root]) == normalized_root:
                return True
        except ValueError:
            continue
    return False


def _reject_control_data_path(raw_path: str, resolved: str) -> None:
    if _is_control_data_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is Open Clank control data and is not accessible "
            "through model file tools"
        )


def _tool_path_roots() -> list[str]:
    """Return the list of directory roots that read_file / write_file
    may touch. Open Clank's data root remains the legacy default candidate but
    is rejected by the control-data guard. Extra roots are loaded from the
    ``tool_path_extra_roots`` setting.
    """
    roots: list[str] = []

    # Kept as the legacy default so an unbound call fails with the explicit
    # control-data error instead of silently switching its workspace to /tmp.
    from src.constants import DATA_DIR
    roots.append(DATA_DIR)

    # /tmp (and its macOS realpath /private/tmp).
    roots.append("/tmp")
    try:
        private_tmp = os.path.realpath("/tmp")
        if private_tmp != "/tmp":
            roots.append(private_tmp)
    except OSError:
        pass

    # $TMPDIR — per-user temp root on macOS (e.g. /var/folders/.../T/).
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.append(tmpdir)

    # Opt-in extra roots from settings.
    try:
        from src.settings import get_setting
        extra = get_setting("tool_path_extra_roots")
        if isinstance(extra, list):
            roots.extend(str(r) for r in extra if r)
    except Exception:
        pass

    # Deduplicate; resolve symlinks so containment is unambiguous.
    seen: set[str] = set()
    out: list[str] = []
    for r in roots:
        try:
            real = os.path.realpath(r)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(real)
    return out


def _resolve_tool_path(raw_path: str) -> str:
    """Resolve and confine a model-supplied path.

    Order of checks:
      1. Non-empty path.
      2. Open Clank control-data deny list.
      3. Sensitive-subpath deny list (blocks .ssh, .gnupg, etc.
         even when the root is on the allowlist).
      4. Allowlist containment (must land under one of the roots).

    Returns the realpath on success. Raises ValueError on rejection.
    Symlinks are resolved before comparison.

    When a workspace is active for this turn, paths are confined to it instead
    of the default allowlist (see _resolve_tool_path_in_workspace).
    """
    ws = get_active_workspace()
    if ws:
        return _resolve_tool_path_in_workspace(ws, raw_path)
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("path is required")
    expanded = os.path.expanduser(str(raw_path).strip())
    resolved = os.path.realpath(expanded)

    _reject_control_data_path(raw_path, resolved)
    if _is_sensitive_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is inside a sensitive directory "
            f"(e.g. .ssh, .gnupg) or matches a sensitive filename"
        )

    for root in _tool_path_roots():
        if resolved == root:
            return resolved
        try:
            common = os.path.commonpath([resolved, root])
        except ValueError:
            continue
        if common == root:
            return resolved
    raise ValueError(
        f"path '{raw_path}' is outside the allowed roots"
    )


def _resolve_tool_path_in_workspace(workspace: str, raw_path: str) -> str:
    """Confine a model-supplied path to the active workspace.

    Layered on top of upstream's path policy: the workspace is the allowed
    root (relative paths resolve under it; paths that escape it are rejected).
    Control data and the sensitive-file deny list (.ssh, .gnupg, id_rsa, …)
    still apply inside it. When no workspace is set, callers use
    _resolve_tool_path instead.
    """
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("path is required")
    base = os.path.realpath(workspace)
    expanded = os.path.expanduser(str(raw_path).strip())
    candidate = expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
    resolved = os.path.realpath(candidate)
    _reject_control_data_path(raw_path, resolved)
    if _is_sensitive_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is inside a sensitive directory "
            f"(e.g. .ssh, .gnupg) or matches a sensitive filename"
        )
    if resolved != base:
        # normcase so containment holds on case-insensitive filesystems
        # (Windows, default macOS): it lowercases on Windows and is a no-op on
        # POSIX. commonpath raises ValueError across Windows drives (C: vs D:)
        # or mixed abs/rel — both mean "outside", so the except rejects them.
        nbase = os.path.normcase(base)
        try:
            if os.path.commonpath([os.path.normcase(resolved), nbase]) != nbase:
                raise ValueError
        except ValueError:
            raise ValueError(f"path '{raw_path}' is outside the workspace ({workspace})")
    return resolved



# ---------------------------------------------------------------------------
# Active workspace (per-turn, context-local)
# ---------------------------------------------------------------------------
# Set ONCE in execute_tool_block from the request's `workspace`. The path
# resolvers (_resolve_tool_path / _resolve_search_root) and the subprocess cwd
# helper (agent_cwd) read it from here, so confinement is enforced in a single
# place: any tool that resolves paths through these helpers is confined
# automatically and cannot accidentally bypass the workspace. contextvars are
# task-local, so concurrent turns don't leak into each other.
_active_workspace: contextvars.ContextVar = contextvars.ContextVar(
    "agent_active_workspace", default=None
)


def get_active_workspace() -> Optional[str]:
    """The folder the agent is confined to this turn, or None."""
    return _active_workspace.get()


def vet_workspace(raw: str) -> Optional[str]:
    """Validate a requested workspace path at bind time.

    Returns the canonical path, or None when it is unusable: not a real
    directory, control data, or itself a sensitive path (.ssh, .gnupg, ...).
    The in-workspace resolver deny-lists protected paths *inside* the workspace,
    but the empty-path search root is the workspace itself, so the root has to
    be vetted before it is ever bound.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    resolved = os.path.realpath(os.path.expanduser(raw))
    if (
        not os.path.isdir(resolved)
        or _is_sensitive_path(resolved)
        or _is_control_data_path(resolved)
    ):
        return None
    # Reject filesystem roots: binding / (or a Windows drive/UNC root) as the
    # workspace would make every absolute path "inside" it, collapsing the
    # confinement into host-wide file access. A root is its own dirname, which
    # also covers C:\ and \\server\share without platform-specific lists.
    if os.path.dirname(resolved) == resolved:
        return None
    return resolved


def _read_only_auth_snapshot(auth_path: str):
    """Read-only immutable account ID/admin snapshot for authorization checks.

    Implemented once in ``src.openclank.operation_approvals`` so tool dispatch
    and the approval lanes share a single read-only auth reader; importing it
    lazily keeps module load free of the policy store.
    """
    from src.openclank.operation_approvals import ReadOnlyAuthSnapshot

    return ReadOnlyAuthSnapshot(auth_path)


def _copal_account_id(owner: Optional[str]) -> tuple[str, Optional[str], Optional[str]]:
    """Resolve a tool username to the immutable TreeHouse account subject."""
    username = str(owner or "").strip()
    if not username:
        return username, None, "authenticated tool owner is required"
    from src.constants import AUTH_FILE
    auth_path = str(os.environ.get("OPEN_CLANK_AUTHORITY_AUTH_PATH") or AUTH_FILE).strip()
    if not os.path.isfile(auth_path):
        return username, None, "authentication identity store is unavailable"
    try:
        account_id = _read_only_auth_snapshot(auth_path).account_id(username)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return username, None, f"authentication identity store is unavailable: {exc}"
    if not account_id:
        return username, None, "authenticated tool owner does not resolve to an immutable account"
    return username, str(account_id), None


def _same_canonical_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right)
    )


def _trusted_workspace_from_id(
    raw_workspace: str,
    *,
    owner: Optional[str],
    authority_workspace_id: str,
) -> Optional[str]:
    """Re-resolve a stable Workspace ID at the final native-tool door.

    A stable ID never acts as a second, independent grant. The current auth
    identity must still own the live Workspace and the canonical People + Agent
    read intersection must still allow it. The server-derived path must also
    equal the requested cwd. Only that trusted lane may bind a filesystem root
    rejected by :func:`vet_workspace`, and only when the canonical Location was
    explicitly registered as a whole-root/volume target.
    """

    requested = str(raw_workspace or "").strip()
    owner_key = str(owner or "").strip().lower()
    workspace_id = str(authority_workspace_id or "").strip()
    if not requested or not owner_key or not workspace_id:
        return None

    from src.constants import APP_DB, AUTH_FILE
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.workspace_policy_service import (
        WorkspacePolicyServiceError,
        resolve_owned_workspace,
    )

    db_path = str(
        os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB
    ).strip()
    auth_path = str(
        os.environ.get("OPEN_CLANK_AUTHORITY_AUTH_PATH") or AUTH_FILE
    ).strip()
    if not os.path.isabs(db_path) or not os.path.isabs(auth_path):
        return None

    try:
        repository = FilePolicyRepository(db_path)
        binding = resolve_owned_workspace(
            repository,
            workspace_id=workspace_id,
            owner_username=owner_key,
            auth_manager=_read_only_auth_snapshot(auth_path),
            purpose="agent_workspace",
        )
        trusted = os.path.realpath(binding.path)
        if not _same_canonical_path(requested, trusted):
            return None

        # Stable IDs constrain ordinary folders too. If the raw validator
        # accepts the folder, keep every one of its existing safety checks.
        vetted = vet_workspace(requested)
        if vetted:
            return trusted if _same_canonical_path(vetted, trusted) else None

        # The one intentional exception is an explicitly modelled filesystem
        # root. A random raw '/', a sensitive folder, or a Location recorded as
        # an ordinary directory cannot enter this branch.
        location = repository.get_location(binding.workspace.location_id)
        if location.kind not in {"whole_root", "volume"}:
            return None
        if os.path.dirname(trusted) != trusted:
            return None
        return trusted
    except (OSError, ValueError, WorkspacePolicyServiceError):
        return None
    except Exception as error:
        # Store corruption/lock failures are authorization failures here. Keep
        # the public result existence-blind while retaining an operator trace.
        logger.warning("stable Workspace authority resolution failed: %s", error)
        return None


def agent_cwd() -> str:
    """Working directory for agent subprocesses (bash/python/background jobs):
    the active workspace when set, else the persistent data dir."""
    return get_active_workspace() or _AGENT_WORKDIR


def get_mcp_manager():
    from src import agent_tools
    return agent_tools.get_mcp_manager()




def _resolve_search_root(raw_path: str) -> str:
    """Resolve + confine a code-nav path (grep/glob/ls).

    With a workspace active, the workspace folder is the root and a supplied
    path is confined inside it. Otherwise an empty path defaults to the agent's
    legacy primary root, which the control-data guard rejects. A supplied path
    is confined by the global allowlist plus protected-path policy.
    """
    raw = (raw_path or "").strip()
    ws = get_active_workspace()
    if ws:
        if raw:
            return _resolve_tool_path_in_workspace(ws, raw)
        root = os.path.realpath(ws)
        _reject_control_data_path(ws, root)
        return root
    if not raw:
        roots = _tool_path_roots()
        root = roots[0] if roots else os.path.realpath(".")
        _reject_control_data_path(root, root)
        return root
    return _resolve_tool_path(raw)

logger = logging.getLogger(__name__)


_ADMIN_TOOLS = {
    "app_api",
    "manage_endpoints",
    "manage_mcp",
    "manage_webhooks",
    "manage_tokens",
    "manage_settings",
    "download_model",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "cancel_download",
}


def _owner_is_admin(owner: Optional[str]) -> bool:
    """Mirror route-level admin behavior for agent tool execution."""
    return owner_is_admin_or_single_user(owner)

# ---------------------------------------------------------------------------
# MCP-backed tool helpers
# ---------------------------------------------------------------------------

# Map legacy tool names -> (MCP server_id, MCP tool_name)
_MCP_TOOL_MAP = {
    "bash":           ("bash",       "bash"),
    "python":         ("python",     "python"),
    "read_file":      ("filesystem", "read_file"),
    "write_file":     ("filesystem", "write_file"),
    "web_search":     ("web_search", "web_search"),
    "web_fetch":      ("web_fetch",  "web_fetch"),
}
_EMAIL_MCP_OWNER_ARG = "_odysseus_owner"
_EMAIL_MCP_ROOT_ARG = "_open_clank_root_operation_id"
_RAG_MCP_OWNER_ARG = "_open_clank_owner"
_RAG_MCP_WORKSPACE_ARG = "_open_clank_workspace_id"
_RAG_MCP_PROJECT_ARG = "_open_clank_project_id"


def _parse_qualified_mcp_args(tool: str, content: str) -> tuple[Dict, Optional[str]]:
    raw = (content or "").strip()
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        if tool.startswith("mcp__email__"):
            return {}, "Email MCP tool arguments must be a JSON object."
        return {}, None
    if not isinstance(parsed, dict):
        if tool.startswith("mcp__email__"):
            return {}, "Email MCP tool arguments must be a JSON object."
        return {}, None
    return parsed, None


def _parse_manage_memory(content: str) -> Dict:
    lines = content.strip().split("\n")
    action = lines[0].strip().lower() if lines else ""
    args = {"action": action}
    if action == "add":
        args["text"] = lines[1].strip() if len(lines) > 1 else ""
        if len(lines) > 2 and lines[2].strip():
            args["category"] = lines[2].strip().lower()
    elif action == "edit":
        args["memory_id"] = lines[1].strip() if len(lines) > 1 else ""
        args["text"] = lines[2].strip() if len(lines) > 2 else ""
    elif action == "delete":
        args["memory_id"] = lines[1].strip() if len(lines) > 1 else ""
    elif action == "search":
        args["text"] = lines[1].strip() if len(lines) > 1 else ""
    elif action == "list":
        if len(lines) > 1 and lines[1].strip():
            args["category"] = lines[1].strip().lower()
    return args


def _parse_write_file(content: str) -> Dict:
    lines = content.split("\n", 1)
    return {"path": lines[0].strip(), "content": lines[1] if len(lines) > 1 else ""}


_MCP_ARG_PARSERS: Dict[str, Callable[[str], Dict[str, str]]] = {
    "bash":           lambda c: {"command": c},
    "python":         lambda c: {"code": c},
    "web_search":     lambda c: {"query": c.split("\n")[0].strip()},
    "web_fetch":      lambda c: {"url": c.split("\n")[0].strip()},
    "read_file":      lambda c: {"path": c.split("\n")[0].strip()},
    "write_file":     _parse_write_file,
    "manage_memory":  _parse_manage_memory,
}


# Primary argument key(s) for the legacy line-parsed tools. When a fenced
# block's content is a JSON object carrying one of these keys, it's structured
# inline args (the relaxed parser's ```web_search {"query": "..."}``` shape) —
# use the object directly instead of letting the line-based parsers wrap the
# whole JSON string as the query/url/path/prompt. Keyed off membership only
# (the primary key never changes), so this can't drift; an unrecognized object
# safely falls through to the line-based parser, i.e. the previous behavior.
#
# IMPORTANT — this only covers the MCP path. _build_mcp_args is reached via
# _call_mcp_tool only for _MCP_TOOL_MAP tools (so an entry outside that map is
# dead, as manage_memory was). Web search/fetch/read/write currently run
# via _direct_fallback -> TOOL_HANDLERS, whose handlers decode JSON themselves
# (see ReadFileTool/WriteFileTool/WebSearchTool/WebFetchTool). The entries here
# are kept as defense-in-depth for if/when those servers are added. The live
# fix for each server-less tool lives in its handler. test_write_file_inline_
# json_args and test_mcp_json_primary_keys_are_all_live pin both halves.
_MCP_JSON_PRIMARY_KEYS: Dict[str, tuple] = {
    "web_search":     ("query", "queries"),
    "web_fetch":      ("url",),
    "read_file":      ("path",),
    "write_file":     ("path",),
}


def _build_mcp_args(tool: str, content: str) -> Dict:
    """Convert fenced-block text content to structured MCP arguments."""
    primaries = _MCP_JSON_PRIMARY_KEYS.get(tool)
    if primaries and content.strip().startswith("{"):
        try:
            decoded = json.loads(content.strip())
        except (json.JSONDecodeError, TypeError):
            decoded = None
        if isinstance(decoded, dict) and any(k in decoded for k in primaries):
            return decoded
    parser = _MCP_ARG_PARSERS.get(tool)
    return parser(content) if parser else {}


async def _call_mcp_tool(
    tool: str,
    content: str,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Dict:
    """Route a legacy tool call through the MCP manager, with direct fallbacks."""
    mcp = get_mcp_manager()
    if not mcp:
        return await _direct_fallback(tool, content, progress_cb=progress_cb) or {"error": f"MCP manager not available for tool '{tool}'", "exit_code": 1}

    server_id, tool_name = _MCP_TOOL_MAP[tool]
    qualified = f"mcp__{server_id}__{tool_name}"
    args = _build_mcp_args(tool, content)
    result = await mcp.call_tool(qualified, args)

    # If MCP server not connected, try direct fallback
    if isinstance(result, dict) and result.get("exit_code") == 1 and "not connected" in result.get("error", ""):
        fallback = await _direct_fallback(tool, content, progress_cb=progress_cb)
        if fallback:
            return fallback

    return result


_BG_MARKERS = {"#!bg", "#bg", "# bg", "#background", "# background", "@background", "# @background"}


def _split_bg_marker(content: str):
    """If the bash content's first non-empty line is a background marker
    (e.g. `#!bg`), return (True, command_without_marker); else (False, content)."""
    lines = content.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip().lower() in _BG_MARKERS:
        del lines[i]
        return True, "\n".join(lines).strip()
    return False, content


def _authenticated_run_id(session_id: Optional[str]) -> Optional[str]:
    """Return the current run identity when the session authority has one."""
    if not session_id:
        return None
    try:
        from src import agent_runs
        value = agent_runs.get_run_id(str(session_id))
    except Exception:
        return None
    return str(value).strip() or None if value else None


async def _direct_fallback(
    tool: str,
    content: str,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    authority_workspace_id: Optional[str] = None,
    files_importer: Optional[Callable[..., Any]] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Optional[Dict]:
    from src.shell_policy import minimal_shell_env

    _subproc_env = minimal_shell_env(cwd=agent_cwd())
    authenticated_run_id = str(run_id).strip() if run_id else _authenticated_run_id(session_id)
    authenticated_task_id = str(task_id).strip() if task_id else None

    try:
        history_context = None
        if owner:
            try:
                from src.openclank.history_capture import trusted_tool_context
                _storage_owner, account_id, _identity_error = _copal_account_id(owner)
                if account_id:
                    history_context = trusted_tool_context(
                        actor_id=str(owner),
                        account_id=str(account_id),
                        workspace_id=str(authority_workspace_id or agent_cwd()),
                        workspace_root=agent_cwd(),
                        session_id=str(session_id or ""),
                        run_id=authenticated_run_id or "",
                        task_id=authenticated_task_id or "",
                        tool_id=str(tool or "filesystem"),
                    )
            except Exception:
                # History is best-effort; normal live tool dispatch continues.
                history_context = None
        ctx = {
            "progress_cb": progress_cb,
            "subproc_env": _subproc_env,
            "session_id": session_id,
            "owner": owner,
            "workspace": agent_cwd(),
            "authority_workspace_id": str(authority_workspace_id or ""),
            "history_context": history_context,
            "run_id": authenticated_run_id,
            "task_id": authenticated_task_id,
            "files_importer": files_importer,
        }

        from src.agent_tools import TOOL_HANDLERS
        if tool in TOOL_HANDLERS:
            return await TOOL_HANDLERS[tool](content, ctx)

    except Exception as e:
        code = getattr(e, "code", None)
        result = {"error": f"{tool}: {e}", "exit_code": 1}
        if isinstance(code, str) and code:
            result["code"] = code
        return result

    return None


async def _document_tool_dispatch(
    tool: str,
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
) -> Optional[Dict]:
    """Route a document tool through TOOL_HANDLERS with the right ctx shape."""
    from src.agent_tools import TOOL_HANDLERS
    ctx = {"session_id": session_id, "owner": owner}
    if tool in TOOL_HANDLERS:
        return await TOOL_HANDLERS[tool](content, ctx)
    return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _finalize_dispatch_return(
    tool: str,
    description: str,
    result: Dict,
    *,
    workspace: Optional[str],
) -> Tuple[str, Dict]:
    """Apply one result envelope to every public dispatcher return path."""
    try:
        agent_tools_mod = __import__("src.agent_tools", fromlist=["TOOL_HANDLERS"])
        dynamic_handlers = getattr(agent_tools_mod, "TOOL_HANDLERS", {})
    except ImportError:
        dynamic_handlers = {}
    return description, finalize_tool_result(
        tool,
        result,
        capability=_declared_tool_capability(tool, dynamic_handlers),
        workspace=workspace,
    )

async def execute_tool_block(
    block: Any,
    session_id: Optional[str] = None,
    disabled_tools: Optional[set] = None,
    owner: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    workspace: Optional[str] = None,
    authority_workspace_id: Optional[str] = None,
    copal_workspace: Optional[str] = None,
    tool_policy: Optional[Any] = None,
    root_operation_id: Optional[str] = None,
    provider_grant_id: Optional[str] = None,
    files_importer: Optional[Callable[..., Any]] = None,
) -> Tuple[str, Dict]:
    """Execute a single tool block. Returns (description, result_dict).

    Thin wrapper: bind the per-turn workspace (so the path resolvers + subprocess
    cwd confine to it) for the duration of this call, then delegate. Every
    caller is vetted here; route-level validation is not a security boundary.
    Reset on the way out so the binding never leaks to the next tool call.
    """
    owner = str(owner or "").strip()
    if not owner:
        tool = str(getattr(block, "tool_type", "") or "tool")
        return tool + ": BLOCKED", {
            "error": "Authenticated tool owner is required.",
            "code": "owner_required", "exit_code": 1, "blocked": True,
        }
    try:
        owner, account_id, identity_error = _copal_account_id(owner)
    except Exception:
        account_id = None
        identity_error = "Authentication identity authority is unavailable."
    if identity_error or not account_id:
        tool = str(getattr(block, "tool_type", "") or "tool")
        return tool + ": BLOCKED", {
            "error": identity_error or "Authenticated tool account is required.",
            "code": "identity_unavailable", "exit_code": 1, "blocked": True,
        }
    requested_workspace = str(workspace or "").strip()
    stable_workspace_id = str(authority_workspace_id or "").strip()
    if stable_workspace_id:
        bound_workspace = _trusted_workspace_from_id(
            requested_workspace,
            owner=owner,
            authority_workspace_id=stable_workspace_id,
        )
    else:
        bound_workspace = (
            vet_workspace(requested_workspace) if requested_workspace else None
        )
    tool = str(getattr(block, "tool_type", "") or "tool")
    if (requested_workspace or stable_workspace_id) and not bound_workspace:
        return _finalize_dispatch_return(
            tool,
            f"{tool}: BLOCKED",
            {
                "error": "The requested workspace was rejected by the containment policy.",
                "exit_code": 1,
                "blocked": True,
            },
            workspace=None,
        )
    token = _active_workspace.set(bound_workspace)
    try:
        output = await _execute_tool_block_impl(
            block,
            session_id=session_id,
            disabled_tools=disabled_tools,
            owner=owner,
            progress_cb=progress_cb,
            authority_workspace_id=stable_workspace_id,
            tool_policy=tool_policy,
            copal_workspace=copal_workspace,
            root_operation_id=root_operation_id,
            provider_grant_id=provider_grant_id,
            files_importer=files_importer,
        )
        description, result = output
        return _finalize_dispatch_return(
            tool,
            description,
            result,
            workspace=bound_workspace,
        )
    finally:
        _active_workspace.reset(token)


async def _execute_tool_block_impl(
    block: Any,
    session_id: Optional[str] = None,
    disabled_tools: Optional[set] = None,
    owner: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    authority_workspace_id: Optional[str] = None,
    tool_policy: Optional[Any] = None,
    copal_workspace: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    provider_grant_id: Optional[str] = None,
    files_importer: Optional[Callable[..., Any]] = None,
) -> Tuple[str, Dict]:
    """Execute a single tool block. Returns (description, result_dict).

    `progress_cb` is forwarded to long-running subprocess tools
    (bash, python) so the agent loop can emit `tool_progress` SSE
    events while the command is in flight. Ignored by other tools.
    """
    from src.tool_implementations import (
        do_search_chats, do_manage_tasks,
        do_manage_skills, do_api_call, do_manage_notes,
        do_manage_calendar,
        do_download_model, do_serve_model, do_list_served_models, do_stop_served_model,
        do_tail_serve_output,
        do_list_downloads, do_cancel_download, do_search_hf_models, do_list_cached_models,
        do_list_serve_presets, do_serve_preset, do_adopt_served_model,
        do_list_cookbook_servers,
        do_edit_image, do_trigger_research, do_manage_research, do_resolve_contact,
        do_manage_contact,
        do_vault_search, do_vault_get, do_vault_unlock,
        do_app_api,
    )

    # HACK:
    # This is a temporary workaround for a circular dependency between
    # tool_execution.py and agent_tools.__init__.py.
    #
    # See issue #4277:
    # refactor(tools): Move the registry from __init__.py into a
    # dedicated registry.py module.
    #
    # Do not copy this pattern elsewhere. This import should be removed
    # once the registry refactor is completed.
    try:
        agent_tools_mod = __import__("src.agent_tools", fromlist=["TOOL_HANDLERS"])
        dynamic_handlers = getattr(agent_tools_mod, "TOOL_HANDLERS", {})
    except ImportError:
        dynamic_handlers = {}

    tool = block.tool_type
    content = block.content
    capability = _declared_tool_capability(tool, dynamic_handlers)
    if capability is None:
        return (
            f"{tool}: BLOCKED",
            {
                "error": f"Tool '{tool}' has no declared dispatcher capability.",
                "code": "undeclared_tool_capability",
                "exit_code": 1,
                "blocked": True,
            },
        )
    if tool == "bash":
        git_guard = isolated_worktree_ref_mutation_guard(content, agent_cwd())
        if git_guard:
            return (
                "bash: BLOCKED",
                {
                    "error": git_guard,
                    "code": "isolated_worktree_ref_mutation",
                    "exit_code": 1,
                    "blocked": True,
                },
            )
    if tool in {"read_copal", "manage_copal"} and copal_workspace is not None:
        try:
            arguments = json.loads(content) if content.strip() else {}
        except json.JSONDecodeError:
            arguments = None
        expected = str(copal_workspace or "default").strip() or "default"
        requested = str(arguments.get("workspace") or "").strip() if isinstance(arguments, dict) else ""
        if requested and requested != expected:
            return (
                f"{tool}: BLOCKED",
                {
                    "error": "Copal workspace does not match the execution's pinned workspace",
                    "code": "copal_workspace_mismatch",
                    "expected": expected,
                    "received": requested,
                    "exit_code": 1,
                    "blocked": True,
                },
            )
        if isinstance(arguments, dict):
            arguments["workspace"] = expected
            content = json.dumps(arguments)

    # The block/disable gates below must match every policy-equivalent
    # spelling of the tool name (bare email names alias their mcp__email__
    # form — see email_tool_policy_names), not just the spelling the model
    # happened to emit.
    policy_names = email_tool_policy_names(tool)

    # Misformatted tool call detection: model put JSON inside ```python``` (or
    # similar) without naming the tool. Common with MiniMax-style outputs.
    # Return a helpful error so the model retries with the correct format.
    if tool in ("python", "json", "xml") and content.strip().startswith("{") and content.strip().endswith("}"):
        try:
            parsed = json.loads(content.strip())
            if isinstance(parsed, dict):
                desc = f"{tool}: misformatted tool call"
                result = {
                    "error": (
                        f"You wrote a JSON object inside a ```{tool}``` block, but that's not a tool call.\n"
                        "To call a tool, use the tool name as the fence tag, e.g.\n"
                        "```resolve_contact\n"
                        "{\"name\": \"...\"}\n"
                        "```\n"
                        "or\n"
                        "```send_email\n"
                        "{\"to\": \"...\", \"subject\": \"...\", \"body\": \"...\"}\n"
                        "```"
                    ),
                    "exit_code": 1,
                }
                return desc, result
        except (ValueError, TypeError):
            pass

    # Reject tools that the user has disabled for this request
    if disabled_tools and not policy_names.isdisjoint(disabled_tools):
        desc = f"{tool}: BLOCKED"
        result = {"error": f"Tool '{tool}' is disabled by user.", "exit_code": 1}
        logger.info(f"Tool blocked by user: {tool}")
        return desc, result

    if tool_policy and any(tool_policy.blocks(name) for name in policy_names):
        desc = f"{tool}: BLOCKED"
        result = {
            "error": f"Execution of tool '{tool}' is forbade by the active guide-only policy.",
            "exit_code": 1,
        }
        logger.warning("Tool policy blocked tool=%s", tool)
        return desc, result

    if tool in _ADMIN_TOOLS and not _owner_is_admin(owner):
        desc = f"{tool}: BLOCKED"
        result = {"error": f"Tool '{tool}' requires an admin user.", "exit_code": 1}
        logger.warning("Admin tool blocked for non-admin owner=%r tool=%s", owner, tool)
        return desc, result

    if (
        is_public_blocked_tool(tool)
        and not _owner_is_admin(owner)
        and not is_scoped_file_tool_allowed(tool, owner)
    ):
        desc = f"{tool}: BLOCKED"
        result = {
            "error": (
                f"Tool '{tool}' is restricted to admin users or an administrator-issued "
                "filesystem visibility assignment. Ask an admin to grant the needed permission."
            ),
            "exit_code": 1,
        }
        logger.warning("Public tool policy blocked owner=%r tool=%s", owner, tool)
        return desc, result


    # Background execution: a `bash` block whose first line is the `#!bg`
    # marker runs DETACHED — returns a job id immediately so the chat stream
    # isn't held open for a multi-minute install/ffmpeg/download. The always-on
    # monitor re-invokes the agent with the full output when the job finishes.
    if tool == "bash" and session_id:
        _is_bg, _bg_cmd = _split_bg_marker(content)
        if _is_bg and _bg_cmd:
            from src import bg_jobs
            from core.platform_compat import find_bash
            from src.shell_policy import (
                ShellApprovalError,
                ShellContainmentError,
                contained_argv,
                require_shell_approval,
                shell_command_argv,
            )

            workspace = agent_cwd()
            shell = find_bash() or (
                os.environ.get("ComSpec", "cmd.exe")
                if os.name == "nt"
                else "/bin/bash"
            )
            network_mode = os.getenv("OPEN_CLANK_SHELL_NETWORK", "enabled")
            try:
                _, containment = contained_argv(
                    shell_command_argv(shell, _bg_cmd),
                    workspace=workspace,
                    cwd=workspace,
                    network=network_mode,
                )
                approval_binding = await require_shell_approval(
                    _bg_cmd,
                    ctx={
                        "progress_cb": progress_cb,
                        "session_id": session_id,
                        "owner": owner,
                        "workspace": workspace,
                        "authority_workspace_id": str(
                            authority_workspace_id or ""
                        ),
                    },
                    cwd=workspace,
                    containment=containment,
                    network=network_mode,
                )
                history_context = None
                run_id = _authenticated_run_id(session_id)
                task_id = None
                try:
                    from src.openclank.history_capture import trusted_tool_context
                    _storage_owner, account_id, _identity_error = _copal_account_id(owner)
                    if account_id:
                        history_context = trusted_tool_context(
                            actor_id=str(owner), account_id=str(account_id),
                            workspace_id=str(authority_workspace_id or workspace),
                            workspace_root=workspace, session_id=str(session_id),
                            run_id=run_id or "",
                            task_id="",
                            tool_id="bash",
                        )
                except Exception:
                    history_context = None
                rec = bg_jobs.launch(
                    _bg_cmd,
                    session_id=session_id,
                    owner=owner,
                    workspace=workspace,
                    cwd=workspace,
                    network=network_mode,
                    approval_binding=approval_binding,
                    history_context=bg_jobs.history_context_mapping(history_context),
                    run_id=run_id,
                    task_id=task_id,
                    tool_id="bash",
                )
            except (ShellApprovalError, ShellContainmentError, ValueError) as exc:
                return (
                    "bash (background): blocked",
                    {"error": f"bash: {exc}", "exit_code": 126},
                )
            short = _bg_cmd.strip().split(chr(10))[0][:80]
            desc = f"bash (background): {short}"
            result = {
                "output": (
                    f"Started background job `{rec['id']}`. It is running detached; "
                    f"do NOT wait for it or poll it. You will be automatically re-invoked "
                    f"with its full output when it finishes. Continue with other work, or "
                    f"end your turn now and resume when the result arrives. If the user "
                    f"later asks to check progress or stop it, call the manage_bg_jobs "
                    f"tool yourself (output or kill); do not tell them to run a tool "
                    f"command, and do not surface raw tool syntax in your reply."
                ),
                "exit_code": 0,
                "bg_job_id": rec["id"],
            }
            logger.info(f"Tool executed: {desc} -> bg job {rec['id']}")
            return desc, result

    # Route MCP-extracted tools through the MCP manager. Forward
    # the progress callback so long-running subprocess tools
    # (bash, python) can stream `tool_progress` events to the UI.
    if tool in {"bash", "python", "read_file", "write_file"}:
        first_line = content.split(chr(10))[0][:80]
        desc = f"{tool}: {first_line}"
        result = await _direct_fallback(
            tool,
            content,
            progress_cb=progress_cb,
            session_id=session_id,
            owner=owner,
            authority_workspace_id=authority_workspace_id,
        ) or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in _MCP_TOOL_MAP:
        first_line = content.split(chr(10))[0][:80]
        desc = f"{tool}: {first_line}"
        result = await _call_mcp_tool(tool, content, progress_cb=progress_cb)
    elif tool in ("grep", "glob", "ls", "get_workspace", "manage_files"):
        # Code-navigation tools — no MCP server; run the direct implementation.
        first_line = content.split(chr(10))[0][:80]
        desc = f"{tool}: {first_line}"
        result = await _direct_fallback(
            tool,
            content,
            progress_cb=progress_cb,
            session_id=session_id,
            owner=owner,
            authority_workspace_id=authority_workspace_id,
        ) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool == "publish_file":
        desc = "publish_file"
        result = await _direct_fallback(tool, content, owner=owner) \
            or {"error": "publish_file: execution failed", "exit_code": 1}
        if result.get("filename"):
            desc = f"publish_file: {result['filename']}"
    elif tool in ("apply_patch", "todowrite"):
        first_line = content.split(chr(10))[0][:80]
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _direct_fallback(
            tool,
            content,
            session_id=session_id,
            owner=owner,
            authority_workspace_id=authority_workspace_id,
        ) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool == "manage_bg_jobs":
        # Inspect/kill detached `bash` jobs; needs session_id to scope to chat.
        desc = f"manage_bg_jobs: {content.split(chr(10))[0][:80]}"
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner) \
            or {"error": "manage_bg_jobs: execution failed", "exit_code": 1}
    elif tool in ("create_document", "update_document", "edit_document",
                  "suggest_document", "manage_documents"):
        desc = f"{tool}: {content.split(chr(10))[0][:80]}"
        result = await _document_tool_dispatch(tool, content, session_id, owner) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
        if tool in ("edit_document", "suggest_document") and "title" in (result or {}):
            desc = f"{tool}: {result.get('title', '')}"
    elif tool == "search_chats":
        query = content.split("\n")[0].strip()
        desc = f"search_chats: {query[:80]}"
        result = await do_search_chats(query, owner=owner)
    elif tool in ("chat_with_model", "ask_teacher", "list_models"):
        # Migrated to the agent_tools registry (#3629): dispatched through
        # TOOL_HANDLERS with the owner/session ctx these tools need, instead
        # of the legacy dispatch_ai_tool elif. The impls live in
        # src/agent_tools/model_interaction_tools.py.
        first_line = content.split(chr(10))[0].strip()[:60]
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _document_tool_dispatch(tool, content, session_id, owner) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in ("create_session", "list_sessions", "send_to_session", "manage_session"):
        # Migrated to the agent_tools registry (#3629): dispatched through
        # TOOL_HANDLERS with the owner/session ctx these tools need. The impls
        # live in src/agent_tools/session_tools.py.
        first_line = content.split(chr(10))[0].strip()[:60]
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _document_tool_dispatch(tool, content, session_id, owner) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in ("pipeline", "manage_memory", "recall_memory", "ui_control"):
        from src.ai_interaction import dispatch_ai_tool
        desc, result = await dispatch_ai_tool(tool, content, session_id, owner=owner)
    elif tool == "manage_tasks":
        desc = "manage_tasks"
        result = await do_manage_tasks(content, owner=owner, session_id=session_id)
    elif tool == "manage_skills":
        desc = "manage_skills"
        result = await do_manage_skills(content, owner=owner)
    elif tool == "api_call":
        first_line = content.split("\n")[0].strip()[:60]
        desc = f"api_call: {first_line}"
        result = await do_api_call(content)
    elif tool in ("manage_endpoints", "manage_mcp", "manage_webhooks", "manage_tokens", "manage_settings"):
        # Registry-dispatched (agent_tools.admin_tools); owner threaded for ownership/admin checks.
        desc = tool
        result = await _direct_fallback(tool, content, owner=owner) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool == "manage_notes":
        desc = "manage_notes"
        result = await do_manage_notes(content, owner=owner)
    elif tool == "read_copal":
        desc = "read_copal"
        try:
            from src.openclank.copal_tools import CopalReadError, read_copal
            arguments = json.loads(content) if content.strip() else {}
            storage_owner, account_id, identity_error = _copal_account_id(owner)
            if identity_error:
                raise CopalReadError(identity_error, code="identity_unavailable")
            result = await read_copal(
                arguments,
                owner=storage_owner,
                account_id=account_id,
                admin=owner_is_admin_or_single_user(owner),
            )
        except CopalReadError as exc:
            result = {"error": str(exc), "code": exc.code, "exit_code": 1}
        except Exception as exc:
            logger.exception("read_copal dispatch failed")
            result = {"error": str(exc), "exit_code": 1}
    elif tool == "manage_copal":
        desc = "manage_copal"
        try:
            from src.openclank.copal_manage import CopalManageError, manage_copal
            arguments = json.loads(content) if content.strip() else {}
            storage_owner, account_id, identity_error = _copal_account_id(owner)
            if identity_error:
                raise CopalManageError(identity_error, code="identity_unavailable")
            result = await manage_copal(
                arguments,
                owner=storage_owner,
                account_id=account_id,
                # Trusted tool context selects the concrete agent actor; the
                # model cannot forge this binding in its JSON arguments.
                actor_id=f"agent:{storage_owner}",
            )
        except CopalManageError as exc:
            result = {"error": str(exc), "code": exc.code, **exc.detail, "exit_code": 1}
        except Exception as exc:
            logger.exception("manage_copal dispatch failed")
            result = {"error": str(exc), "exit_code": 1}
    elif tool == "manage_calendar":
        desc = "manage_calendar"
        result = await do_manage_calendar(content, owner=owner)
    elif tool == "download_model":
        desc = "download_model"
        result = await do_download_model(content, owner=owner)
    elif tool == "serve_model":
        desc = "serve_model"
        result = await do_serve_model(content, owner=owner)
    elif tool == "list_served_models":
        desc = "list_served_models"
        result = await do_list_served_models(content, owner=owner)
    elif tool == "stop_served_model":
        desc = "stop_served_model"
        result = await do_stop_served_model(content, owner=owner)
    elif tool == "tail_serve_output":
        desc = "tail_serve_output"
        result = await do_tail_serve_output(content, owner=owner)
    elif tool == "list_downloads":
        desc = "list_downloads"
        result = await do_list_downloads(content, owner=owner)
    elif tool == "cancel_download":
        desc = "cancel_download"
        result = await do_cancel_download(content, owner=owner)
    elif tool == "search_hf_models":
        desc = "search_hf_models"
        result = await do_search_hf_models(content, owner=owner)
    elif tool == "list_cached_models":
        desc = "list_cached_models"
        result = await do_list_cached_models(content, owner=owner)
    elif tool == "app_api":
        desc = "app_api"
        result = await do_app_api(content, owner=owner)
    elif tool == "list_serve_presets":
        desc = "list_serve_presets"
        result = await do_list_serve_presets(content, owner=owner)
    elif tool == "serve_preset":
        desc = "serve_preset"
        result = await do_serve_preset(content, owner=owner)
    elif tool == "adopt_served_model":
        desc = "adopt_served_model"
        result = await do_adopt_served_model(content, owner=owner)
    elif tool == "list_cookbook_servers":
        desc = "list_cookbook_servers"
        result = await do_list_cookbook_servers(content, owner=owner)
    elif tool == "generate_image":
        from src.ai_interaction import do_generate_image

        desc = "generate_image"
        result = await do_generate_image(
            content,
            session_id=session_id,
            owner=owner,
            root_operation_id=root_operation_id,
            idempotency_key=(
                "tool_image_" + hashlib.sha256(
                    f"{root_operation_id}\0{content}".encode("utf-8")
                ).hexdigest()
                if root_operation_id
                else None
            ),
            grant_id=provider_grant_id,
        )
    elif tool == "edit_image":
        desc = "edit_image"
        result = await do_edit_image(
            content,
            owner=owner,
            root_operation_id=root_operation_id,
            grant_id=provider_grant_id,
        )
    elif tool == "edit_file":
        result = await _direct_fallback(tool, content) or {"error": "edit failed", "exit_code": 1}
        desc = result.get("output") or result.get("error") or "edit_file"
    elif tool == "trigger_research":
        desc = "trigger_research"
        result = await do_trigger_research(content, owner=owner)
    elif tool == "manage_research":
        desc = "manage_research"
        result = await do_manage_research(content, owner=owner)
    elif tool == "resolve_contact":
        desc = "resolve_contact"
        result = await do_resolve_contact(content, owner=owner)
    elif tool == "manage_contact":
        desc = "manage_contact"
        result = await do_manage_contact(content, owner=owner)
    elif tool == "vault_search":
        desc = "vault_search"
        result = await do_vault_search(content, owner=owner)
    elif tool == "vault_get":
        desc = "vault_get"
        result = await do_vault_get(content, owner=owner)
    elif tool == "vault_unlock":
        desc = "vault_unlock"
        result = await do_vault_unlock(content, owner=owner)
    elif tool in BUILTIN_EMAIL_TOOLS:
        # Bare email tool name from fenced-block models (e.g. Ollama) — route to MCP email server.
        # Non-admin owners never reach here: BUILTIN_EMAIL_TOOLS ⊆ NON_ADMIN_BLOCKED_TOOLS,
        # so is_public_blocked_tool() above already rejected them.
        mcp = get_mcp_manager()
        qualified = f"mcp__email__{tool}"
        desc = f"email: {tool}"
        if mcp:
            _raw = content.strip()
            args = {}
            _args_error = None
            if _raw:
                # A non-empty body is always meant to be the call's arguments,
                # and every email tool takes a JSON object. Anything that
                # isn't one is a correctable error — NOT a silent empty-args
                # call, which would read the DEFAULT mailbox/folder instead of
                # the one the model meant (#3966 class). Only an EMPTY body
                # keeps the no-arg path (e.g. ```list_email_accounts```).
                try:
                    parsed = json.loads(_raw)
                except (json.JSONDecodeError, TypeError) as _je:
                    # Covers both `{account: "work"}` (looks like JSON, bad)
                    # and `account: work` (not JSON at all).
                    _args_error = (
                        f"'{tool}' arguments are not valid JSON ({_je}). "
                        'Send a JSON object, e.g. {"account": "work"} — '
                        "keys and string values need double quotes."
                    )
                else:
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        _args_error = (
                            f"'{tool}' arguments must be a JSON object, "
                            'e.g. {"uid": "..."} — got a JSON array/value instead.'
                        )
            if _args_error is not None:
                result = {"error": _args_error, "exit_code": 1}
            else:
                if owner or root_operation_id:
                    args = dict(args)
                    if owner:
                        args[_EMAIL_MCP_OWNER_ARG] = owner
                    if root_operation_id:
                        args[_EMAIL_MCP_ROOT_ARG] = root_operation_id
                result = await mcp.call_tool(qualified, args)
        else:
            result = {"error": "MCP manager not available", "exit_code": 1}
    elif tool.startswith("mcp__"):
        # MCP tool dispatch
        mcp = get_mcp_manager()
        if mcp:
            desc = f"mcp: {tool}"
            args, parse_error = _parse_qualified_mcp_args(tool, content)
            if parse_error:
                result = {"error": parse_error, "exit_code": 1}
            elif tool.startswith("mcp__rag__") and not owner:
                result = {
                    "error": "RAG MCP tool requires an authenticated owner",
                    "exit_code": 1,
                }
            else:
                if tool.startswith("mcp__email__") and (owner or root_operation_id):
                    args = dict(args)
                    if owner:
                        args[_EMAIL_MCP_OWNER_ARG] = owner
                    if root_operation_id:
                        args[_EMAIL_MCP_ROOT_ARG] = root_operation_id
                if tool.startswith("mcp__rag__"):
                    # The global RAG MCP process must not trust tenant scope
                    # supplied by the model. Overwrite every private field from
                    # the authenticated turn and its vetted workspace binding.
                    args = dict(args)
                    args[_RAG_MCP_OWNER_ARG] = owner
                    args[_RAG_MCP_WORKSPACE_ARG] = get_active_workspace() or ""
                    args[_RAG_MCP_PROJECT_ARG] = ""
                result = await mcp.call_tool(tool, args)
        else:
            desc = f"mcp: {tool}"
            result = {"error": "MCP manager not available", "exit_code": 1}


    elif tool in dynamic_handlers:
        first_line = content.split(chr(10))[0][:80]
        desc = f"registry: {tool} {first_line}".strip()
        res = await _direct_fallback(
            tool, content, progress_cb=progress_cb, session_id=session_id,
            owner=owner, authority_workspace_id=authority_workspace_id,
            files_importer=files_importer,
        )

        if isinstance(res, tuple):
            desc, result = res
        else:
            result = res or {"error": f"{tool}: execution failed", "exit_code": 1}

    else:
        desc = f"unknown: {tool}"
        result = {
            "error": f"Unknown tool: {tool}",
            "exit_code": 1
        }

    logger.info(f"Tool executed: {desc} -> exit_code={result.get('exit_code', 'n/a')}")
    return desc, result


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

# Keys handled by the dedicated branches below — never echo them as raw JSON.
_FORMATTER_HANDLED_KEYS = {
    "stdout", "stderr", "exit_code", "content", "size",
    "response", "results", "session_id", "name", "model", "session_name",
    "success", "path", "action", "title", "doc_id", "version", "applied",
    "error", "output", "preview", "preview_truncated", "preview_mode",
    "tail_digest", "result_integrity", "merge_conflict",
}


def format_tool_result(description: str, result: Dict) -> str:
    """Format only the bounded result envelope for feeding back to the LLM."""
    parts = [f"### {description}"]
    envelope = _tool_result_preview(result)
    preview = envelope["preview"]
    if preview:
        parts.append(
            f"**result preview ({envelope['preview_mode']}):**\n```\n{preview}\n```"
        )
    else:
        parts.append("**result:** (empty)")
    if isinstance(result.get("exit_code"), int) and result["exit_code"] not in (0,):
        parts.append(f"**exit_code:** {result['exit_code']}")

    if result.get("merge_conflict", {}).get("detected"):
        parts.append("**merge conflict:** detected; manual resolution is required.")

    return "\n".join(parts)
