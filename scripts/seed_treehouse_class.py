#!/usr/bin/env python3
"""Seed the Open Clank + Copal TreeHouse sample class.

Run: python scripts/seed_treehouse_class.py
Idempotent: skips if the course already exists.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.openclank.copal_treehouse import (
    apply_treehouse_command,
    compute_treehouse_projections,
    new_treehouse_state,
    public_treehouse_snapshot,
    validate_treehouse_state,
)


def _run(state: dict, cmd: str, payload: dict | None = None, **kw) -> tuple:
    return apply_treehouse_command(
        state,
        {"type": cmd, "payload": payload or {}},
        actor_id=kw.get("actor", "owner"),
        command_id=kw.get("cid", f"seed:{cmd}:{state['revision']}"),
        expected_revision=state["revision"] if kw.get("rev") is None else kw["rev"],
        now=kw.get("now"),
    )


# -- Skill tree ---------------------------------------------------------------

SKILLS = [
    {"id": "skill:system-basics", "title": "System Basics", "description": "Navigate Open Clank's startup, health, logs, and data directories.", "prerequisiteIds": []},
    {"id": "skill:models-auth", "title": "Models & Auth", "description": "Configure LLM providers, manage API keys, and understand capability certification.", "prerequisiteIds": ["skill:system-basics"]},
    {"id": "skill:agent-tools", "title": "Agent & Tools", "description": "Use the Agent loop, MCP servers, built-in tools, permissions, and sub-agents.", "prerequisiteIds": ["skill:models-auth"]},
    {"id": "skill:memory", "title": "Memory & Trust", "description": "Capture, review, promote, and recall memory. Understand trust boundaries.", "prerequisiteIds": ["skill:agent-tools"]},
    {"id": "skill:copal-views", "title": "Copal Views", "description": "Work with Notes, Wiki, Events, Timeline, Bases, Tasks, and Calendar.", "prerequisiteIds": ["skill:agent-tools"]},
    {"id": "skill:security", "title": "Security & Privacy", "description": "Understand owner scope, credential handling, backup/restore, and threat model.", "prerequisiteIds": ["skill:system-basics"]},
    {"id": "skill:capstone", "title": "Capstone Mastery", "description": "Demonstrate end-to-end mastery: inspect the app, build Copal knowledge, demonstrate recovery.", "prerequisiteIds": ["skill:memory", "skill:copal-views", "skill:security"]},
]

# -- Module/lesson content ----------------------------------------------------

COURSE_ID = "course:openclank-copal-101"
COURSE_TITLE = "Open Clank + Copal: System Mastery"
COURSE_DESC = (
    "A hands-on course teaching you to understand, operate, troubleshoot, and safely extend "
    "Open Clank and Copal. Every lesson maps to the actual current app — no fabricated claims."
)

MODULES = [
    {
        "id": "module:orientation",
        "title": "1. System Orientation",
        "description": "Start here. Understand what Open Clank is, how it starts, and where things live.",
        "activities": [
            {
                "id": "act:what-is-openclank",
                "title": "What is Open Clank?",
                "activityType": "lesson",
                "points": 10,
                "skillIds": ["skill:system-basics"],
                "content": (
                    "# What is Open Clank?\n\n"
                    "Open Clank is a **local-first AI assistant** that runs on your "
                    "machine. It wraps multiple LLM providers behind a single interface with persistent memory, "
                    "documents, tools, and a database-native knowledge layer called Copal.\n\n"
                    "## Key principles\n"
                    "- **Local-first**: your data stays on your machine unless you explicitly send it somewhere\n"
                    "- **Multi-provider**: supports OpenAI, Anthropic, Google, local models (Ollama), and MiMo\n"
                    "- **Persistent**: memory, documents, sessions, and settings survive restarts\n"
                    "- **Extensible**: MCP servers, skills, tools, and hooks let you customize behavior\n\n"
                    "## What you will learn\n"
                    "This course walks through every major surface of the app: startup, models, agent tools, "
                    "memory, Copal views, security, and a hands-on capstone.\n\n"
                    "---\n\n"
                    "**Next**: Learn about the data directories and how Open Clank stores state."
                ),
            },
            {
                "id": "act:data-directories",
                "title": "Data Directories & Startup",
                "activityType": "lesson",
                "points": 10,
                "skillIds": ["skill:system-basics"],
                "content": (
                    "# Data Directories & Startup\n\n"
                    "Open Clank stores everything under the app's `data/` directory:\n\n"
                    "```\n"
                    "data/\n"
                    "  app.db              # Main SQLite database (sessions, models, tasks)\n"
                    "  frankenmemory.db    # Frankenmemory store (memory, candidates, graph)\n"
                    "  auth.json           # User accounts and roles\n"
                    "  user_prefs.json     # Per-user preferences\n"
                    "  runtime/            # Agent engine, logs, generated content\n"
                    "  personal_docs/      # Personal documents (RAG)\n"
                    "```\n\n"
                    "## Startup sequence\n"
                    "1. `systemctl --user start open-clank` (or `python app.py`)\n"
                    "2. Loads settings, validates config\n"
                    "3. Initializes database (SQLite via SQLAlchemy)\n"
                    "4. Connects Frankenmemory provider (fm-mcp over MCP stdio)\n"
                    "5. Starts FastAPI server on `127.0.0.1:7000`\n\n"
                    "## Health check\n"
                    "Visit `/health` or use the Diagnostics view in the UI to verify all services are running.\n\n"
                    "---\n\n"
                    "**Next**: Understand the settings and configuration model."
                ),
            },
            {
                "id": "act:settings-config",
                "title": "Settings & Configuration",
                "activityType": "lesson",
                "points": 10,
                "skillIds": ["skill:system-basics"],
                "content": (
                    "# Settings & Configuration\n\n"
                    "Open Clank's settings live in `settings.json` and are managed through the Settings UI.\n\n"
                    "## Settings model\n"
                    "- **Global settings**: API keys, provider selection, default model\n"
                    "- **Per-session settings**: persona, context budget, tool permissions\n"
                    "- **Owner scope**: the `owner` profile controls who has admin access\n\n"
                    "## Key settings\n"
                    "| Setting | Purpose |\n"
                    "|---------|---------|\n"
                    "| `defaultProvider` | Which LLM provider to use |\n"
                    "| `defaultModel` | Default model ID |\n"
                    "| `apiKeys` | Encrypted API keys per provider |\n"
                    "| `contextBudget` | Max tokens for context window |\n"
                    "| `toolPermissions` | Which tools are allowed |\n\n"
                    "## Environment variables\n"
                    "Some settings can be overridden via env vars: `OPENCLANK_PORT`, `OPENCLANK_DEBUG`, etc.\n\n"
                    "---\n\n"
                    "**Next**: Understand logging and shutdown."
                ),
            },
            {
                "id": "act:logs-shutdown",
                "title": "Logs, Shutdown & Backup",
                "activityType": "lesson",
                "points": 10,
                "skillIds": ["skill:system-basics"],
                "content": (
                    "# Logs, Shutdown & Backup\n\n"
                    "## Logs\n"
                    "Open Clank writes logs to stdout and optionally to a file:\n"
                    "- Application logs: request/response cycles, errors, tool calls\n"
                    "- Session logs: per-session transcripts in `~/.odysseus/sessions/`\n"
                    "- Diagnostics view shows live log output\n\n"
                    "## Graceful shutdown\n"
                    "Ctrl+C triggers `shutdown_lifecycle.py`:\n"
                    "1. Finishes in-flight requests\n"
                    "2. Flushes pending writes\n"
                    "3. Closes database connections\n"
                    "4. Saves session state\n\n"
                    "## Backup\n"
                    "The backup route (`/backup`) exports:\n"
                    "- All documents (Copal)\n"
                    "- Memory files\n"
                    "- Settings\n"
                    "- Session history\n\n"
                    "Restore imports with deduplication to avoid duplicates.\n\n"
                    "---\n\n"
                    "**Checkpoint**: You now understand the system foundation. Move on to Models & Auth."
                ),
            },
        ],
    },
    {
        "id": "module:models-auth",
        "title": "2. Models, Auth & Providers",
        "description": "Configure LLM providers, manage API keys, and understand how model routing works.",
        "activities": [
            {
                "id": "act:provider-overview",
                "title": "Provider Architecture",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:models-auth"],
                "content": (
                    "# Provider Architecture\n\n"
                    "Open Clank supports multiple LLM providers through a unified dispatch layer:\n\n"
                    "## Supported providers\n"
                    "- **OpenAI**: GPT-4o, GPT-4o-mini, o1, o3\n"
                    "- **Anthropic**: Claude 3.5 Sonnet, Claude 3 Opus\n"
                    "- **Google**: Gemini Pro, Gemini Flash\n"
                    "- **Local**: Ollama, llama.cpp, vLLM\n"
                    "- **MiMo**: Xiaomi's MiMo models (via custom provider)\n\n"
                    "## Model dispatch\n"
                    "The `model_dispatch.py` module routes requests to the correct provider based on:\n"
                    "1. The model ID prefix (e.g., `gpt-4o` → OpenAI)\n"
                    "2. Provider-specific adapters\n"
                    "3. Fallback chains when a provider is unavailable\n\n"
                    "## Capability certification\n"
                    "Not all models support all features. The `model_capabilities.py` module tracks:\n"
                    "- Tool calling support\n"
                    "- Vision/multimodal\n"
                    "- Context window size\n"
                    "- Streaming support\n\n"
                    "---\n\n"
                    "**Next**: Learn about API key management."
                ),
            },
            {
                "id": "act:api-keys",
                "title": "API Key Management",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:models-auth"],
                "content": (
                    "# API Key Management\n\n"
                    "Open Clank stores API keys securely:\n\n"
                    "## Storage\n"
                    "- Keys are stored in `settings.json` (encrypted at rest)\n"
                    "- Never logged or exposed in API responses\n"
                    "- Environment variable overrides: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.\n\n"
                    "## Key manager\n"
                    "The `api_key_manager.py` module handles:\n"
                    "- Loading keys from settings or env vars\n"
                    "- Validating key format before use\n"
                    "- Atomic save (no partial writes)\n"
                    "- File permission enforcement (0600)\n\n"
                    "## Adding a provider\n"
                    "1. Open Settings → Providers\n"
                    "2. Enter your API key\n"
                    "3. Select a default model\n"
                    "4. Test with the Diagnostics view\n\n"
                    "---\n\n"
                    "**Next**: Understand the Agent loop and tool system."
                ),
            },
        ],
    },
    {
        "id": "module:agent-tools",
        "title": "3. Agent, Tools & MCP",
        "description": "Master the Agent loop, MCP servers, built-in tools, permissions, and sub-agents.",
        "activities": [
            {
                "id": "act:agent-loop",
                "title": "The Agent Loop",
                "activityType": "lesson",
                "points": 20,
                "skillIds": ["skill:agent-tools"],
                "content": (
                    "# The Agent Loop\n\n"
                    "The Agent is Open Clank's core execution engine:\n\n"
                    "## How it works\n"
                    "1. User sends a message\n"
                    "2. Context is assembled (system prompt + history + tools)\n"
                    "3. Message is sent to the LLM provider\n"
                    "4. LLM responds with text and/or tool calls\n"
                    "5. Tools are executed, results fed back\n"
                    "6. Loop continues until LLM produces a final response\n\n"
                    "## Key files\n"
                    "- `agent_loop.py`: Main execution loop\n"
                    "- `agent_runs.py`: Run management and state\n"
                    "- `tool_schemas.py`: Tool definitions sent to LLM\n"
                    "- `tool_execution.py`: Tool call routing\n\n"
                    "## Context budget\n"
                    "The context window has a finite budget. The `context_compactor.py` module compresses older "
                    "messages when the budget is exceeded, preserving the most relevant context.\n\n"
                    "---\n\n"
                    "**Next**: Learn about the built-in tool system."
                ),
            },
            {
                "id": "act:tool-system",
                "title": "Built-in Tools",
                "activityType": "lesson",
                "points": 20,
                "skillIds": ["skill:agent-tools"],
                "content": (
                    "# Built-in Tools\n\n"
                    "Open Clank ships with a rich set of tools:\n\n"
                    "## File tools\n"
                    "- `read`: Read files and directories\n"
                    "- `edit`: Precise string replacements\n"
                    "- `write`: Create or overwrite files\n"
                    "- `glob`: Pattern-based file search\n"
                    "- `grep`: Content search with regex\n\n"
                    "## Shell tools\n"
                    "- `bash`: Execute shell commands\n"
                    "- `bash-interactive`: Interactive terminal sessions\n\n"
                    "## Knowledge tools\n"
                    "- `webfetch`: Fetch and parse web content\n"
                    "- `websearch`: Real-time web search\n"
                    "- `memory`: Search persistent memory\n"
                    "- `history`: Search conversation history\n\n"
                    "## Orchestration tools\n"
                    "- `actor`: Spawn sub-agents\n"
                    "- `task`: Track work items\n"
                    "- `workflow`: Multi-agent scripts\n"
                    "- `skill`: Load specialized skills\n\n"
                    "---\n\n"
                    "**Next**: Learn about MCP server integration."
                ),
            },
            {
                "id": "act:mcp-integration",
                "title": "MCP Server Integration",
                "activityType": "lesson",
                "points": 20,
                "skillIds": ["skill:agent-tools"],
                "content": (
                    "# MCP Server Integration\n\n"
                    "MCP (Model Context Protocol) extends Open Clank with external tool and resource providers:\n\n"
                    "## What is MCP?\n"
                    "A JSON-RPC based protocol that lets external servers provide:\n"
                    "- **Tools**: Functions the agent can call\n"
                    "- **Resources**: Data the agent can read\n"
                    "- **Prompts**: Reusable prompt templates\n\n"
                    "## Configuring MCP servers\n"
                    "MCP servers are configured in settings:\n"
                    "```json\n"
                    "{\n"
                    "  \"mcpServers\": {\n"
                    "    \"my-server\": {\n"
                    "      \"command\": \"node\",\n"
                    "      \"args\": [\"server.js\"],\n"
                    "      \"env\": { \"API_KEY\": \"...\" }\n"
                    "    }\n"
                    "  }\n"
                    "}\n"
                    "```\n\n"
                    "## Security\n"
                    "- MCP tools go through the same permission system as built-in tools\n"
                    "- Tool results are treated as **data**, not instructions\n"
                    "- Prompt injection from tool outputs is a known risk — be cautious\n\n"
                    "---\n\n"
                    "**Next**: Learn about skills and permissions."
                ),
            },
            {
                "id": "act:skills-permissions",
                "title": "Skills & Permissions",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:agent-tools"],
                "content": (
                    "# Skills & Permissions\n\n"
                    "## Skills\n"
                    "Skills are markdown files that provide specialized instructions for specific tasks:\n"
                    "- Located in `.claude/skills/`, `.agents/skills/`, or `.opencode/skill/`\n"
                    "- Invoked via `/<skill-name>` or automatically when a task matches\n"
                    "- Add guidance without changing the tool set\n\n"
                    "## Permission model\n"
                    "Every tool call goes through a permission evaluator:\n"
                    "1. Agent-level permissions\n"
                    "2. User/session config\n"
                    "3. Hard rules (never overridden)\n\n"
                    "### Permission decisions\n"
                    "- `allow`: execute immediately\n"
                    "- `ask`: prompt the user\n"
                    "- `deny`: block the call\n\n"
                    "### Sensitive operations\n"
                    "- Reading `*.env` files: `ask`\n"
                    "- External directory reads: `ask` (unless whitelisted)\n"
                    "- Question tool: `deny` (primary agents only)\n\n"
                    "---\n\n"
                    "**Checkpoint**: You understand the agent system. Move on to Memory & Trust."
                ),
            },
        ],
    },
    {
        "id": "module:memory",
        "title": "4. Memory, Trust & Review",
        "description": "Frankenmemory tiers, off/automatic/manual modes, candidate review, and trust boundaries.",
        "activities": [
            {
                "id": "act:memory-capture",
                "title": "Memory Architecture",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:memory"],
                "content": (
                    "# Memory Architecture\n\n"
                    "Open Clank uses **Frankenmemory** — a tiered memory engine (fm-mcp, Rust) over MCP stdio.\n\n"
                    "## Memory tiers\n"
                    "- **Raw**: every captured turn (evidence trail, not recallable)\n"
                    "- **Candidates**: extracted facts pending user review\n"
                    "- **Curated**: approved memories (recallable, injected via digest)\n"
                    "- **Quarantine**: suspicious or duplicate items (not recallable)\n\n"
                    "## Per-turn flow\n"
                    "1. After each chat turn, background capture writes to candidates (pending)\n"
                    "2. Graph enrichment extracts entities/edges/cues (LLM-assisted, throttled)\n"
                    "3. User reviews candidates: accept (→ curated) or reject\n"
                    "4. Digest injection: index card per turn, details via `recall_memory` tool\n\n"
                    "## Recall\n"
                    "- The `recall_memory` tool is read-only (chat lane)\n"
                    "- `manage_memory` tool can add/edit/delete (agent lane)\n"
                    "- Both respect owner scope and trust firewall\n\n"
                    "---\n\n"
                    "**Next**: Understand memory modes and the policy gate."
                ),
            },
            {
                "id": "act:memory-trust",
                "title": "Modes, Trust & Candidate Review",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:memory"],
                "content": (
                    "# Modes, Trust & Candidate Review\n\n"
                    "## Memory modes (per-user pref: `memory_mode`)\n"
                    "- **off**: no memory writes at all. Existing memories preserved.\n"
                    "- **automatic**: capture enters candidates (pending). Manual/tool writes go to curated.\n"
                    "- **manual**: capture enters candidates as pending and STAYS there until you approve. "
                    "Direct writes are refused — you must use candidate review.\n\n"
                    "Mode switching is non-destructive: switching modes never deletes, approves, or discards memories.\n\n"
                    "## Trust firewall\n"
                    "At injection, every memory is classified:\n"
                    "- **Trusted** (hand-authored or pinned): carries force, injected as standing guidance\n"
                    "- **Untrusted** (auto-captured): wrapped in firewall, never carries instructions\n"
                    "- Controlled by `memory_trust_auto` master toggle + per-kind switches\n\n"
                    "## Candidate review\n"
                    "- View pending: `/api/memory/inspect?tier=candidate&status=pending`\n"
                    "- Accept: promotes to curated (recallable)\n"
                    "- Reject: marks rejected (not recallable)\n"
                    "- Provenance recorded on every review\n\n"
                    "---\n\n"
                    "**Checkpoint**: You understand memory. Move on to Copal Views."
                ),
            },
        ],
    },
    {
        "id": "module:copal-views",
        "title": "5. Copal Views",
        "description": "Work with Notes, Wiki, Events, Timeline, Bases, Tasks, and Calendar.",
        "activities": [
            {
                "id": "act:copal-notes-wiki",
                "title": "Notes & Wiki",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:copal-views"],
                "content": (
                    "# Notes & Wiki\n\n"
                    "Copal provides a database-native document system:\n\n"
                    "## Documents\n"
                    "- Stored in the Copal database (Redb via stdio bridge)\n"
                    "- Support markdown content with frontmatter\n"
                    "- Versioned with head hashes\n"
                    "- Full-text searchable\n\n"
                    "## Wiki\n"
                    "- `.wiki/` namespace for interlinked docs\n"
                    "- Backlinks and graph connections\n"
                    "- Built-in shared/global docs projected per-user\n"
                    "- Import from Obsidian vaults or filesystem\n\n"
                    "## Bases\n"
                    "- Structured data containers with custom fields\n"
                    "- Query and filter across note frontmatter\n"
                    "- Owner-scoped (Brain Vault import is e-only)\n\n"
                    "## Appearance\n"
                    "- Per-entry visibility controls (show/hide in nav)\n"
                    "- One registry drives nav + panel\n"
                    "- Show-all/Reset available in UI\n\n"
                    "---\n\n"
                    "**Next**: Learn about Events and Timeline."
                ),
            },
            {
                "id": "act:copal-events-timeline",
                "title": "Events & Timeline",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:copal-views"],
                "content": (
                    "# Events & Timeline\n\n"
                    "Copal tracks durable events for every significant action:\n\n"
                    "## Event system\n"
                    "- Every command in TreeHouse emits events\n"
                    "- Events are immutable once written\n"
                    "- Projections are rebuilt from events on every read\n\n"
                    "## Timeline view\n"
                    "- Chronological view of all events\n"
                    "- Filterable by type, actor, and entity\n"
                    "- Shows the full history of state changes\n\n"
                    "## Why events?\n"
                    "- **Audit trail**: Every change is traceable\n"
                    "- **Consistency**: Projections are always derived from the source of truth\n"
                    "- **Recovery**: State can be rebuilt from events if projections are corrupted\n\n"
                    "---\n\n"
                    "**Next**: Learn about Bases, Tasks, and Calendar."
                ),
            },
            {
                "id": "act:copal-bases-tasks",
                "title": "Bases, Tasks & Calendar",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:copal-views"],
                "content": (
                    "# Bases, Tasks & Calendar\n\n"
                    "## Bases\n"
                    "- Structured data containers in Copal\n"
                    "- Custom fields and views\n"
                    "- Query and filter capabilities\n\n"
                    "## Tasks\n"
                    "- Built-in task management\n"
                    "- Assign, track, and complete work items\n"
                    "- Integration with the scheduler\n\n"
                    "## Calendar\n"
                    "- CalDAV sync for external calendars\n"
                    "- Calendar projection for in-app views\n"
                    "- Event creation and management\n\n"
                    "---\n\n"
                    "**Checkpoint**: You understand all major Copal views. Move on to Security."
                ),
            },
        ],
    },
    {
        "id": "module:security",
        "title": "6. Security & Privacy",
        "description": "Understand owner scope, credential handling, backup/restore, and the threat model.",
        "activities": [
            {
                "id": "act:owner-scope",
                "title": "Owner Scope & Access Control",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:security"],
                "content": (
                    "# Owner Scope & Access Control\n\n"
                    "Open Clank enforces access boundaries:\n\n"
                    "## Owner scope\n"
                    "- The `owner` profile has admin access to everything\n"
                    "- Other profiles see only what they're authorized for\n"
                    "- Workspace isolation prevents cross-user data access\n\n"
                    "## Authentication\n"
                    "- Device flow for API access\n"
                    "- Token-based authentication for routes\n"
                    "- Session-level permission context\n\n"
                    "## TreeHouse roles\n"
                    "- `admin`: Full access to all features\n"
                    "- `instructor`: Can create/edit courses, grade, manage skills\n"
                    "- `learner`: Can enroll, complete activities, submit work\n\n"
                    "---\n\n"
                    "**Next**: Understand credential handling."
                ),
            },
            {
                "id": "act:credential-safety",
                "title": "Credential Handling & Threat Model",
                "activityType": "lesson",
                "points": 15,
                "skillIds": ["skill:security"],
                "content": (
                    "# Credential Handling & Threat Model\n\n"
                    "## Credential safety rules\n"
                    "1. **Never** put credentials in first-party code\n"
                    "2. **Never** log API keys, tokens, or passwords\n"
                    "3. **Never** send secrets in chat output\n"
                    "4. Env vars with KEY/TOKEN/SECRET are radioactive\n\n"
                    "## Threat model\n"
                    "Open Clank's `THREAT_MODEL.md` covers:\n"
                    "- Prompt injection from untrusted content\n"
                    "- Tool result trust boundaries\n"
                    "- MCP server isolation\n"
                    "- Local file system access scope\n\n"
                    "## Backup & restore\n"
                    "- Full backup via `/backup` endpoint\n"
                    "- Restore with cross-user deduplication\n"
                    "- Isolated test configurations for safe experimentation\n\n"
                    "---\n\n"
                    "**Final module**: You're ready for the Capstone."
                ),
            },
        ],
    },
    {
        "id": "module:capstone",
        "title": "7. Capstone Project",
        "description": "Demonstrate end-to-end mastery with a hands-on project.",
        "activities": [
            {
                "id": "act:capstone-inspect",
                "title": "Inspect the App",
                "activityType": "lesson",
                "points": 20,
                "skillIds": ["skill:capstone"],
                "content": (
                    "# Capstone: Inspect the App\n\n"
                    "Complete these verification steps:\n\n"
                    "## 1. Startup verification\n"
                    "- Start Open Clank from a clean configuration\n"
                    "- Verify health endpoint returns OK\n"
                    "- Check that all routes are accessible\n\n"
                    "## 2. Provider verification\n"
                    "- Verify at least one LLM provider is configured\n"
                    "- Send a test message through the Agent\n"
                    "- Confirm the response appears in session history\n\n"
                    "## 3. Tool verification\n"
                    "- Use the `read` tool to inspect a file\n"
                    "- Use the `bash` tool to run a safe command\n"
                    "- Verify the tool call appears in the session log\n\n"
                    "---\n\n"
                    "**Next**: Build Copal knowledge."
                ),
            },
            {
                "id": "act:capstone-copal",
                "title": "Build Copal Knowledge",
                "activityType": "lesson",
                "points": 20,
                "skillIds": ["skill:capstone"],
                "content": (
                    "# Capstone: Build Copal Knowledge\n\n"
                    "## 1. Create a document\n"
                    "- Create a new note in Copal\n"
                    "- Add markdown content\n"
                    "- Verify it appears in the document list\n\n"
                    "## 2. Build a Base\n"
                    "- Create a new Base with custom fields\n"
                    "- Add entries\n"
                    "- Verify the data persists across restart\n\n"
                    "## 3. Timeline verification\n"
                    "- Check the Timeline view shows your recent events\n"
                    "- Verify event ordering is correct\n\n"
                    "---\n\n"
                    "**Next**: Demonstrate recovery."
                ),
            },
            {
                "id": "act:capstone-recovery",
                "title": "Recovery & Restart",
                "activityType": "lesson",
                "points": 20,
                "skillIds": ["skill:capstone"],
                "content": (
                    "# Capstone: Recovery & Restart\n\n"
                    "## 1. Backup\n"
                    "- Export a full backup via the backup route\n"
                    "- Verify the backup contains your documents and settings\n\n"
                    "## 2. Restart\n"
                    "- Gracefully shut down Open Clank\n"
                    "- Restart the application\n"
                    "- Verify all state is preserved\n\n"
                    "## 3. Integrity check\n"
                    "- Run the TreeHouse integrity check\n"
                    "- Verify event count matches expectations\n"
                    "- Confirm projections are consistent\n\n"
                    "---\n\n"
                    "**Congratulations**: You have demonstrated end-to-end mastery of Open Clank and Copal!"
                ),
            },
        ],
    },
]

# -- Assignments --------------------------------------------------------------

ASSIGNMENTS = [
    {
        "id": "asgn:system-check",
        "moduleId": "module:orientation",
        "title": "System Health Check",
        "prompt": "Start Open Clank, visit the Diagnostics view, and describe what you see. List the services that are running and any that are degraded.",
        "maxPoints": 50,
        "skillIds": ["skill:system-basics"],
    },
    {
        "id": "asgn:provider-setup",
        "moduleId": "module:models-auth",
        "title": "Configure a Provider",
        "prompt": "Configure at least one LLM provider (OpenAI, Anthropic, or local). Send a test message and paste the response. Explain which model you chose and why.",
        "maxPoints": 100,
        "skillIds": ["skill:models-auth"],
    },
    {
        "id": "asgn:tool-exercise",
        "moduleId": "module:agent-tools",
        "title": "Tool Usage Exercise",
        "prompt": "Use the Agent to: (1) read a file, (2) search for a pattern, (3) run a shell command. Describe each tool call and its result.",
        "maxPoints": 100,
        "skillIds": ["skill:agent-tools"],
    },
    {
        "id": "asgn:memory-review",
        "moduleId": "module:memory",
        "title": "Memory Audit",
        "prompt": "Search your memory for any entries about this project. Identify one entry that is stale or needs updating, and explain what the correct information should be.",
        "maxPoints": 75,
        "skillIds": ["skill:memory"],
    },
    {
        "id": "asgn:copal-build",
        "moduleId": "module:copal-views",
        "title": "Build a Copal Knowledge Base",
        "prompt": "Create a Copal document about your favorite topic. Add it to a Base with at least 3 fields. Verify it appears in the Timeline. Screenshot or describe what you see.",
        "maxPoints": 100,
        "skillIds": ["skill:copal-views"],
    },
    {
        "id": "asgn:security-audit",
        "moduleId": "module:security",
        "title": "Security Self-Audit",
        "prompt": "Review your Open Clank configuration for security: Are API keys stored safely? Are file permissions correct? Is the owner scope properly set? List three things you verified and one you improved.",
        "maxPoints": 75,
        "skillIds": ["skill:security"],
    },
    {
        "id": "asgn:capstone-project",
        "moduleId": "module:capstone",
        "title": "Capstone: Full System Walkthrough",
        "prompt": "Perform a complete walkthrough: start Open Clank, configure a provider, create a Copal document, build a Base, verify the Timeline, run a backup, restart, and verify integrity. Document each step with what you observed.",
        "maxPoints": 200,
        "skillIds": ["skill:capstone"],
    },
]

# -- Badges -------------------------------------------------------------------

BADGES = [
    {"id": "badge:opener", "title": "Course Opener", "description": "Complete the first module", "criteria": {"type": "course", "courseId": COURSE_ID}},
    {"id": "badge:centurion", "title": "Centurion", "description": "Earn 100+ points", "criteria": {"type": "points", "threshold": 100}},
    {"id": "badge:security-aware", "title": "Security Aware", "description": "Complete the security module", "criteria": {"type": "skill", "skillId": "skill:security", "threshold": 60}},
    {"id": "badge:master", "title": "Open Clank Master", "description": "Complete all modules and earn 500+ points", "criteria": {"type": "points", "threshold": 500}},
]

# -- Quests -------------------------------------------------------------------

QUESTS = [
    {"id": "quest:first-steps", "title": "First Steps", "description": "Complete the first activity in each module", "activityIds": ["act:what-is-openclank", "act:provider-overview", "act:agent-loop", "act:memory-capture", "act:copal-notes-wiki", "act:owner-scope", "act:capstone-inspect"], "rewardPoints": 50},
    {"id": "quest:all-assignments", "title": "Assignment Champion", "description": "Submit all assignments", "assignmentIds": ["asgn:system-check", "asgn:provider-setup", "asgn:tool-exercise", "asgn:memory-review", "asgn:copal-build", "asgn:security-audit", "asgn:capstone-project"], "rewardPoints": 100},
]


def seed_class(state: dict) -> dict:
    """Seed the Open Clank + Copal course into a TreeHouse state. Idempotent."""
    validate_treehouse_state(state)

    # Skip if already seeded
    if COURSE_ID in state["courses"]:
        return state

    ts = datetime.now(UTC)

    # Create skills
    for skill in SKILLS:
        state, _, _ = _run(state, "skill.create", {
            "id": skill["id"], "title": skill["title"],
            "description": skill["description"],
            "prerequisiteIds": skill["prerequisiteIds"],
            "masteryThreshold": 60, "evidencePoints": 25,
        }, now=ts)

    # Create course
    state, _, _ = _run(state, "course.create", {
        "id": COURSE_ID, "title": COURSE_TITLE, "description": COURSE_DESC,
        "tags": ["openclank", "copal", "tutorial", "mastery"],
    }, now=ts)

    # Create modules and activities
    for module_spec in MODULES:
        state, _, _ = _run(state, "module.create", {
            "id": module_spec["id"], "courseId": COURSE_ID,
            "title": module_spec["title"], "description": module_spec["description"],
        }, now=ts)

        for act_spec in module_spec["activities"]:
            state, _, _ = _run(state, "activity.create", {
                "id": act_spec["id"], "moduleId": module_spec["id"],
                "title": act_spec["title"], "activityType": act_spec["activityType"],
                "content": act_spec["content"], "points": act_spec["points"],
                "skillIds": act_spec["skillIds"],
            }, now=ts)

    # Create and publish assignments
    for asgn in ASSIGNMENTS:
        state, _, _ = _run(state, "assignment.create", {
            "id": asgn["id"], "moduleId": asgn["moduleId"],
            "title": asgn["title"], "prompt": asgn["prompt"],
            "maxPoints": asgn["maxPoints"], "skillIds": asgn["skillIds"],
        }, now=ts)
        state, _, _ = _run(state, "assignment.publish", {"assignmentId": asgn["id"]}, now=ts)

    # Create badges
    for badge in BADGES:
        state, _, _ = _run(state, "badge.create", {
            "id": badge["id"], "title": badge["title"],
            "description": badge["description"], "criteria": badge["criteria"],
        }, now=ts)

    # Create quests
    for quest in QUESTS:
        state, _, _ = _run(state, "quest.create", {
            "id": quest["id"], "title": quest["title"],
            "description": quest["description"],
            "activityIds": quest.get("activityIds", []),
            "assignmentIds": quest.get("assignmentIds", []),
            "rewardPoints": quest["rewardPoints"],
        }, now=ts)

    # Publish the course
    state, _, _ = _run(state, "course.publish", {"courseId": COURSE_ID}, now=ts)

    return state


if __name__ == "__main__":
    state_path = Path.home() / ".odysseus" / ".copal" / "treehouse-state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        validate_treehouse_state(state)
    else:
        state = new_treehouse_state("Open Clank Learner")
        state_path.parent.mkdir(parents=True, exist_ok=True)

    if COURSE_ID in state["courses"]:
        print(f"Course '{COURSE_TITLE}' already exists (id: {COURSE_ID}). Skipping.")
    else:
        state = seed_class(state)
        state_path.write_text(json.dumps(state, indent=None, separators=(",", ":"), ensure_ascii=False))
        print(f"Seeded '{COURSE_TITLE}' with {len(MODULES)} modules, {sum(len(m['activities']) for m in MODULES)} activities, {len(ASSIGNMENTS)} assignments, {len(BADGES)} badges, {len(QUESTS)} quests.")

    proj = compute_treehouse_projections(state)
    print(f"Total events: {len(state['events'])}")
    print(f"Skills: {len(state['skills'])}")
    print(f"Courses: {len(state['courses'])}")
