"""thesius_identity — file-based agent identity system.

Single source of truth for agent identity. Reads roster + md bootstrap files,
composes the full system prompt, and writes mimo agent config files.

Clobber safety: all file writes use atomic temp+rename with .bak backup.
"""

import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# Add odysseus source to path for tool_security import
_ODYSSEUS_ROOT = Path(__file__).resolve().parents[2]
if _ODYSSEUS_ROOT.exists():
    sys.path.insert(0, str(_ODYSSEUS_ROOT))

logger = logging.getLogger(__name__)

# Bridged tool names (from lifetools_server.py) used for permission mapping.
# These are the names mimo sees as lifetools:<tool>.
_BRIDGED_TOOL_NAMES = {
    "create_document", "edit_document", "suggest_document", "update_document",
    "search_chats", "chat_with_model", "create_session", "list_sessions",
    "send_to_session", "pipeline", "manage_session", "manage_memory",
    "list_models", "ui_control", "manage_tasks", "manage_calendar",
    "manage_notes", "api_call", "ask_teacher", "manage_skills",
    "manage_endpoints", "manage_mcp", "manage_webhooks", "manage_tokens",
    "manage_documents", "manage_settings", "download_model", "serve_model",
    "list_served_models", "stop_served_model", "tail_serve_output",
    "list_downloads", "cancel_download", "search_hf_models",
    "list_cookbook_servers", "list_serve_presets", "adopt_served_model",
    "serve_preset", "list_cached_models", "app_api", "edit_image",
    "trigger_research", "resolve_contact", "manage_contact",
    "list_email_accounts", "send_email", "list_emails", "read_email",
    "reply_to_email", "bulk_email", "delete_email", "archive_email",
    "mark_email_read", "manage_bg_jobs",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
ROSTER_PATH = Path(os.environ.get(
    "OPENCLANK_ROSTER_PATH",
    str(REPO_ROOT / "config" / "openclank" / "roster.json5"),
))
CHECKSUMS_PATH = Path(os.environ.get(
    "OPENCLANK_CHECKSUMS_PATH",
    str(REPO_ROOT / "config" / "openclank" / ".checksums.json"),
))

# Bootstrap files in load order
BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md"]

# Base behavior block — mimo's SystemPrompt.provider() text for Anthropic models.
# When agent.prompt is set, mimo does NOT prepend the provider base prompt
# (session/llm.ts:246), so we must include it ourselves.
# Source: packages/mimo-code/packages/opencode/src/session/prompt/anthropic.txt
_BASE_BEHAVIOR_PATH = Path(os.environ.get(
    "OPENCLANK_BASE_BEHAVIOR_PATH",
    str(REPO_ROOT / "packages" / "mimo-code" / "packages" / "opencode" / "src" / "session" / "prompt" / "anthropic.txt"),
))

# Default MIMOCODE_HOME if not set externally
_DEFAULT_MIMOCODE_HOME = Path(os.environ.get(
    "OPENCLANK_MIMOCODE_HOME",
    str(REPO_ROOT / ".mimocode_home"),
))

# Fallback persona when roster home is missing/corrupt
_FALLBACK_PERSONA = (
    "You are a helpful AI assistant. Be concise, direct, and accurate. "
    "Follow the user's instructions. Ask when uncertain."
)


# ---------------------------------------------------------------------------
# A2.1 — compose_system_prompt(home)
# ---------------------------------------------------------------------------

def _read_base_behavior() -> str:
    """Read the base behavior block from the vendored mimo prompt file."""
    try:
        return _BASE_BEHAVIOR_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning("base behavior file not at %s, using minimal default", _BASE_BEHAVIOR_PATH)
        return (
            "You are an interactive CLI tool that helps users with software engineering tasks. "
            "Be concise, direct, and accurate."
        )


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown file.

    Returns (frontmatter_dict, body). If no frontmatter, returns ({}, text).
    """
    if not text.startswith("---"):
        return {}, text

    # Find closing ---
    end = text.find("---", 3)
    if end == -1:
        return {}, text

    frontmatter_str = text[3:end].strip()
    body = text[end + 3:].strip()

    try:
        import yaml
        data = yaml.safe_load(frontmatter_str) or {}
    except Exception:
        # Fallback: try a simple key: value parse
        data = {}
        for line in frontmatter_str.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                data[key.strip()] = val.strip().strip('"').strip("'")

    return data, body


def compose_system_prompt(home: str | Path) -> str:
    """Read md bootstrap files from home, concatenate into persona block,
    prepend base-behavior block.

    Load order: AGENTS.md → SOUL.md → IDENTITY.md → TOOLS.md → USER.md.
    Missing files are silently skipped.

    Returns the full system prompt string (base_behavior + persona).
    """
    home = Path(home)
    base = _read_base_behavior()
    persona_parts: list[str] = []

    for fname in BOOTSTRAP_FILES:
        fpath = home / fname
        if not fpath.exists():
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("failed to read %s: %s", fpath, e)
            continue

        # For AGENTS.md, strip frontmatter (it goes to config fields, not prompt)
        if fname == "AGENTS.md":
            _, body = _parse_frontmatter(text)
            if body:
                persona_parts.append(body)
        else:
            if text.strip():
                persona_parts.append(text.strip())

    persona = "\n\n".join(persona_parts)
    if not persona:
        logger.warning("no persona files found in %s, using fallback", home)
        return base + "\n\n" + _FALLBACK_PERSONA

    return base + "\n\n" + persona


# ---------------------------------------------------------------------------
# C1 — Permission computation: odysseus identity gate → hard denies
# ---------------------------------------------------------------------------

def _compute_permission_rules(owner: str | None, fm_permission: dict | None) -> dict[str, Any]:
    """Compute the merged permission map for the agent file frontmatter.

    Axis 1: odysseus identity gate → hard denies from blocked_tools_for_owner.
    Axis 2: per-agent rules from AGENTS.md frontmatter (fm_permission).

    Returns a dict suitable for the agent file's `permission:` frontmatter key.
    When loaded by mimo's ConfigAgent.load, this becomes the agent's Ruleset
    via Permission.fromConfig.

    Merge order: axis 2 (frontmatter) first, then axis 1 (hard denies) last.
    Since fromConfig is last-key-wins, axis 1 hard denies cannot be relaxed
    by frontmatter. For example, a frontmatter `lifetools:manage_calendar: ask`
    can make a specific bridged tool an ask (narrower than the blanket deny),
    but cannot override the blanket `lifetools:*: deny` which comes last.

    Note: frontmatter CAN override per-tool denies for native tools (e.g.
    `bash: allow`). For truly inviolable invariants, use hardPermission (C3).
    """
    from src.tool_security import blocked_tools_for_owner, owner_is_admin_or_single_user

    rules: dict[str, Any] = {}

    is_admin = owner_is_admin_or_single_user(owner)

    # Axis 2: per-agent frontmatter rules go FIRST (lower priority)
    if fm_permission:
        rules.update(fm_permission)

    # Axis 1: identity gate hard denies go LAST (higher priority, last-match-wins)
    # This ensures frontmatter cannot relax a hard deny.
    if not is_admin:
        # Blanket deny the entire lifetools:* namespace for non-admin
        rules["lifetools:*"] = "deny"

        # Deny specific native mimo tools that are also blocked for non-admin
        blocked = blocked_tools_for_owner(owner)
        for tool_name in blocked:
            # If the tool is bridged, the blanket lifetools:* deny covers it.
            # Only emit per-tool denies for native mimo tools.
            if tool_name not in _BRIDGED_TOOL_NAMES:
                rules[tool_name] = "deny"

    return rules


# ---------------------------------------------------------------------------
# A2.2 — compose_agent_file(roster_entry)
# ---------------------------------------------------------------------------

def compose_agent_file(entry: dict[str, Any], owner: str | None = None) -> tuple[str, dict[str, Any], str]:
    """Compose a mimo agent file from a roster entry.

    Args:
        entry: roster entry dict
        owner: the owner identifier for permission gate computation

    Returns (name, frontmatter_dict, body_str).
    """
    home = Path(entry["home"])
    name = entry["id"]

    body = compose_system_prompt(home)

    # Read frontmatter from AGENTS.md if present
    agents_path = home / "AGENTS.md"
    fm: dict[str, Any] = {}
    if agents_path.exists():
        try:
            fm, _ = _parse_frontmatter(agents_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("failed to parse frontmatter from %s: %s", agents_path, e)

    # Build frontmatter: AGENTS.md fields take precedence, roster fills gaps
    frontmatter: dict[str, Any] = {}

    # Description
    frontmatter["description"] = fm.get("description", f"Agent: {entry.get('name', name)}")

    # Mode
    frontmatter["mode"] = fm.get("mode", "primary")

    # Model: frontmatter > roster entry > sensible default
    model = fm.get("model") or entry.get("model")
    if model:
        frontmatter["model"] = model

    # Temperature
    if "temperature" in fm:
        frontmatter["temperature"] = fm["temperature"]

    # C1: Permission ruleset — merge identity gate (axis 1) + frontmatter (axis 2)
    fm_permission = fm.get("permission")
    permission = _compute_permission_rules(owner, fm_permission)
    if permission:
        frontmatter["permission"] = permission

    # Tool allowlist
    if "tool_allowlist" in fm:
        frontmatter["tool_allowlist"] = fm["tool_allowlist"]

    return name, frontmatter, body


# ---------------------------------------------------------------------------
# A2.3 — sync_to_mimo_config()
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write file atomically: temp file + rename, with .bak backup of existing."""
    path.parent.mkdir(parents=True, exist_ok=True)

    # Backup existing file
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, bak)
        except Exception as e:
            logger.warning("failed to create backup %s: %s", bak, e)

    # Write to temp file in same directory, then rename
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _render_agent_md(frontmatter: dict[str, Any], body: str) -> str:
    """Render a mimo agent .md file (YAML frontmatter + body)."""
    try:
        import yaml
        fm_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except ImportError:
        # Minimal fallback: key: value lines
        lines: list[str] = []
        for k, v in frontmatter.items():
            if isinstance(v, str):
                lines.append(f"{k}: '{v}'")
            elif isinstance(v, (list, dict)):
                import json as _json
                lines.append(f"{k}: {_json.dumps(v)}")
            else:
                lines.append(f"{k}: {v}")
        fm_str = "\n".join(lines) + "\n"
    return f"---\n{fm_str}---\n{body}\n"


def _get_mimocode_home() -> Path:
    """Resolve MIMOCODE_HOME/config directory."""
    env_home = os.environ.get("MIMOCODE_HOME")
    if env_home:
        return Path(env_home) / "config"
    return _DEFAULT_MIMOCODE_HOME / "config"


def sync_to_mimo_config(owner: str | None = None) -> dict[str, str]:
    """Write mimo agent files for all roster entries.

    Args:
        owner: owner identifier for permission gate computation

    Returns {agent_id: written_path} for each agent written.
    """
    roster = load_roster()
    config_dir = _get_mimocode_home()
    written: dict[str, str] = {}

    for entry in roster:
        try:
            name, frontmatter, body = compose_agent_file(entry, owner=owner)
        except Exception as e:
            logger.error("failed to compose agent file for %s: %s", entry.get("id", "?"), e)
            continue

        agent_dir = config_dir / "agent"
        agent_path = agent_dir / f"{name}.md"

        rendered = _render_agent_md(frontmatter, body)

        try:
            _atomic_write(agent_path, rendered)
            written[name] = str(agent_path)
            logger.info("wrote agent file: %s", agent_path)
        except Exception as e:
            logger.error("failed to write agent file %s: %s", agent_path, e)

    return written


# ---------------------------------------------------------------------------
# A2.4 — checksum(home)
# ---------------------------------------------------------------------------

def checksum(home: str | Path) -> str:
    """Compute sha256 over sorted md file contents + AGENTS.md frontmatter.

    This is the change-detection hash for a roster agent's identity.
    """
    home = Path(home)
    hasher = hashlib.sha256()

    for fname in sorted(BOOTSTRAP_FILES):
        fpath = home / fname
        if not fpath.exists():
            # Include filename in hash so removals are detected
            hasher.update(f"MISSING:{fname}".encode())
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except Exception as e:
            hasher.update(f"ERROR:{fname}:{e}".encode())
            continue

        # For AGENTS.md, include the raw text (frontmatter + body)
        # so frontmatter changes also flip the checksum
        hasher.update(f"{fname}:".encode())
        hasher.update(text.encode("utf-8"))

    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# A3 — Persona injection cadence (checksum-gated)
# ---------------------------------------------------------------------------

def _load_checksums() -> dict[str, str]:
    """Load the last-known checksums from the sidecar file."""
    if not CHECKSUMS_PATH.exists():
        return {}
    try:
        return json.loads(CHECKSUMS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_checksums(checksums: dict[str, str]) -> None:
    """Save checksums to the sidecar file (atomic write)."""
    _atomic_write(CHECKSUMS_PATH, json.dumps(checksums, indent=2))


def sync_if_changed(owner: str | None = None) -> dict[str, str]:
    """Check per-agent checksums; only re-write mimo files when checksum flips.

    Args:
        owner: owner identifier for permission gate computation

    Returns {agent_id: path} for agents that were re-written.
    """
    roster = load_roster()
    stored = _load_checksums()
    updated: dict[str, str] = {}
    changed = False

    for entry in roster:
        aid = entry["id"]
        home = entry["home"]
        current = checksum(home)

        if stored.get(aid) == current:
            continue

        # Checksum flipped — re-compose and write
        try:
            name, frontmatter, body = compose_agent_file(entry, owner=owner)
            config_dir = _get_mimocode_home()
            agent_path = config_dir / "agent" / f"{name}.md"
            rendered = _render_agent_md(frontmatter, body)
            _atomic_write(agent_path, rendered)
            updated[name] = str(agent_path)
            stored[aid] = current
            changed = True
            logger.info("identity changed for %s, re-wrote %s", aid, agent_path)
        except Exception as e:
            logger.error("failed to sync agent %s: %s", aid, e)

    if changed:
        _save_checksums(stored)

    return updated


def initial_sync(owner: str | None = None) -> dict[str, str]:
    """Force-sync all agents (boot path). Writes all agent files unconditionally
    and stores checksums.

    Args:
        owner: owner identifier for permission gate computation
    """
    roster = load_roster()
    checksums: dict[str, str] = {}
    written: dict[str, str] = {}

    for entry in roster:
        aid = entry["id"]
        try:
            name, frontmatter, body = compose_agent_file(entry, owner=owner)
            config_dir = _get_mimocode_home()
            agent_path = config_dir / "agent" / f"{name}.md"
            rendered = _render_agent_md(frontmatter, body)
            _atomic_write(agent_path, rendered)
            written[name] = str(agent_path)
            checksums[aid] = checksum(entry["home"])
            logger.info("initial sync: wrote %s", agent_path)
        except Exception as e:
            logger.error("initial sync failed for %s: %s", aid, e)

    _save_checksums(checksums)
    return written


# ---------------------------------------------------------------------------
# A4 — CrewMember DB upsert (checksum-synced view)
# ---------------------------------------------------------------------------

def upsert_crew_member(entry: dict[str, Any], db_session) -> None:
    """Upsert a CrewMember row from the composed identity.

    Args:
        entry: roster entry dict
        db_session: SQLAlchemy session (caller manages lifecycle/commit)

    Identity-bearing fields are sourced from the roster files.
    Non-identity fields (sort_order, is_active, session_id) are preserved
    if the row already exists.
    """
    from core.database import CrewMember

    aid = entry["id"]
    home = Path(entry["home"])

    # Compose the prompt for the personality field
    try:
        persona = compose_system_prompt(home)
    except Exception:
        persona = _FALLBACK_PERSONA

    # Parse AGENTS.md frontmatter for structured fields
    agents_path = home / "AGENTS.md"
    fm: dict[str, Any] = {}
    if agents_path.exists():
        try:
            fm, _ = _parse_frontmatter(agents_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    model = fm.get("model") or entry.get("model")
    enabled_tools = fm.get("tool_allowlist")
    name = entry.get("name", aid)
    timezone = fm.get("timezone")

    existing = db_session.query(CrewMember).filter(CrewMember.id == aid).first()

    if existing:
        # Update identity-bearing fields only
        existing.name = name
        existing.personality = persona
        existing.model = model
        if enabled_tools is not None:
            existing.enabled_tools = json.dumps(enabled_tools)
        if timezone:
            existing.timezone = timezone
        logger.info("updated CrewMember %s from roster files", aid)
    else:
        crew = CrewMember(
            id=aid,
            name=name,
            personality=persona,
            model=model,
            enabled_tools=json.dumps(enabled_tools) if enabled_tools else None,
            timezone=timezone,
            is_active=True,
            is_default_assistant=entry.get("default", False),
        )
        db_session.add(crew)
        logger.info("created CrewMember %s from roster files", aid)


def sync_crew_db(db_session) -> None:
    """Sync all roster agents to the CrewMember table.

    Only upserts when the checksum has changed (checked by caller or
    called after sync_if_changed/initial_sync).
    """
    roster = load_roster()
    for entry in roster:
        try:
            upsert_crew_member(entry, db_session)
        except Exception as e:
            logger.error("failed to upsert CrewMember for %s: %s", entry.get("id", "?"), e)
    db_session.commit()


# ---------------------------------------------------------------------------
# A5 — Fallback persona + notify
# ---------------------------------------------------------------------------

def get_persona_with_fallback(entry: dict[str, Any]) -> tuple[str, bool]:
    """Get the composed persona for an agent, falling back on error.

    Returns (persona_text, is_fallback).
    """
    home = Path(entry["home"])

    if not home.exists():
        logger.warning("roster home missing: %s, using fallback persona", home)
        return _FALLBACK_PERSONA, True

    try:
        persona = compose_system_prompt(home)
        # Sanity check: if we got less than the base behavior, something is wrong
        if len(persona) < 100:
            logger.warning("composed prompt suspiciously short for %s, using fallback", home)
            return _FALLBACK_PERSONA, True
        return persona, False
    except Exception as e:
        logger.error("failed to compose persona for %s: %s, using fallback", home, e)
        return _FALLBACK_PERSONA, True


# ---------------------------------------------------------------------------
# Roster loading
# ---------------------------------------------------------------------------

def _strip_json5_comments(text: str) -> str:
    """Strip // line comments from JSON5 text (naive but sufficient for our roster)."""
    import re
    # Remove // comments (not inside strings — good enough for simple roster files)
    return re.sub(r'//.*', '', text)


def load_roster() -> list[dict[str, Any]]:
    """Load roster.json5 and return list of agent entries."""
    if not ROSTER_PATH.exists():
        logger.error("roster file not found: %s", ROSTER_PATH)
        return []

    try:
        text = ROSTER_PATH.read_text(encoding="utf-8")
        text = _strip_json5_comments(text)
        return json.loads(text)
    except Exception as e:
        logger.error("failed to parse roster %s: %s", ROSTER_PATH, e)
        return []


def get_mimocode_home() -> Path:
    """Public accessor for the resolved MIMOCODE_HOME/config path."""
    return _get_mimocode_home()
