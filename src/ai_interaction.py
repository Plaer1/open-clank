"""
ai_interaction.py

AI-to-AI interaction tools: pipeline and manage_memory, plus the shared
session-manager singleton and dispatch_ai_tool.

As part of the tool -> registry migration (#3629), chat_with_model, ask_teacher
and list_models moved to src/agent_tools/model_interaction_tools.py, and
create_session, list_sessions, send_to_session and manage_session moved to
src/agent_tools/session_tools.py. Those modules reuse get_session_manager from
here while model selection stays in the normalized provider control plane.

These are agent tools — the LLM writes fenced code blocks and they execute
through the standard agent_tools.py pipeline.
"""

import asyncio
import json
import logging
import uuid
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from src.generated_images import (
    gallery_owner_key,
)

logger = logging.getLogger(__name__)

MAX_DEBATE_ROUNDS = 5
MAX_PIPELINE_STEPS = 10

# ---------------------------------------------------------------------------
# Global managers (set from app.py, same pattern as _mcp_manager)
# _session_manager is kept as a local cache for performance (avoiding
# repeated get_session_manager_instance() calls). It's synced with
# the authoritative singleton in core.models.
_session_manager = None
_memory_manager = None
_memory_vector = None
_memory_provider = None
_memory_lifecycle = None
_rag_manager = None
_personal_docs_manager = None


def set_session_manager(mgr):
    """Set the global session manager. Syncs local cache + core singleton."""
    global _session_manager
    _session_manager = mgr
    from core.models import set_session_manager_instance
    set_session_manager_instance(mgr)


def get_session_manager():
    """Get the global session manager."""
    return _session_manager


def set_memory_manager(mgr, vector=None, provider=None, lifecycle=None):
    global _memory_manager, _memory_vector, _memory_provider, _memory_lifecycle
    _memory_manager = mgr
    _memory_vector = vector
    _memory_provider = provider
    _memory_lifecycle = lifecycle


def set_rag_manager(rag_mgr, personal_docs_mgr=None):
    global _rag_manager, _personal_docs_manager
    _rag_manager = rag_mgr
    _personal_docs_manager = personal_docs_mgr


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------



async def stream_ai_tool(tool: str, content: str, session_id: Optional[str] = None, owner: Optional[str] = None):
    """Dispatcher for streaming AI tools. Yields events as async generator."""
    # Fallback: run non-streaming and yield final result
    desc, result = await dispatch_ai_tool(tool, content, session_id, owner=owner)
    yield {"_final": True, "desc": desc, "result": result}


async def do_pipeline(
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    root_operation_id: Optional[str] = None,
) -> Dict:
    """Execute a multi-step pipeline where each model's output feeds the next.

    Content format (JSON):
      {"steps": [
        {"model": "model_a", "instruction": "Draft an essay about X"},
        {"model": "model_b", "instruction": "Critique the following draft"},
        {"model": "model_a", "instruction": "Revise based on this critique"}
      ]}

    Or line format:
      Line 1: step1_model | step1_instruction
      Line 2: step2_model | step2_instruction
      ...
    """
    from src.openclank.chat_routing import resolve_chat_model_spec
    from src.openclank.modality_facade import complete_text

    # Try JSON parse first
    steps = None
    try:
        data = json.loads(content.strip())
        if isinstance(data, dict) and "steps" in data:
            steps = data["steps"]
        elif isinstance(data, list):
            steps = data
    except (json.JSONDecodeError, TypeError):
        pass

    # Fall back to line format: model | instruction
    if not steps:
        steps = []
        for line in content.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                parts = line.split("|", 1)
                steps.append({"model": parts[0].strip(), "instruction": parts[1].strip()})
            else:
                return {"error": "Each line must be: model | instruction (or use JSON format)"}

    if not steps:
        return {"error": "No pipeline steps provided"}
    if len(steps) > MAX_PIPELINE_STEPS:
        return {"error": f"Maximum {MAX_PIPELINE_STEPS} steps allowed"}

    # Resolve all models first (fail fast)
    resolved = []
    for i, step in enumerate(steps):
        model_spec = step.get("model", "").strip()
        instruction = step.get("instruction", "").strip()
        if not model_spec or not instruction:
            return {"error": f"Step {i + 1}: both 'model' and 'instruction' are required"}
        try:
            route = await asyncio.to_thread(
                resolve_chat_model_spec,
                owner=owner,
                model_spec=model_spec,
            )
            resolved.append((route, instruction))
        except ValueError as e:
            return {"error": f"Step {i + 1}: {e}"}

    # Execute pipeline
    step_outputs = []
    previous_output = None

    try:
        for i, (route, instruction) in enumerate(resolved):
            if previous_output:
                user_content = (
                    f"Previous step's output:\n\n{previous_output}\n\n"
                    f"Your task: {instruction}"
                )
            else:
                user_content = instruction

            messages = [
                {"role": "system", "content": f"You are step {i + 1} in a processing pipeline. {instruction}"},
                {"role": "user", "content": user_content},
            ]

            response = await complete_text(
                owner=owner or "local-installation",
                purpose="utility",
                messages=messages,
                model_route_id=route.model_route_id,
                grant_id=route.provider_grant_id,
                root_operation_id=root_operation_id,
            )

            step_outputs.append({
                "step": i + 1,
                "model": route.provider_model_id,
                "instruction": instruction,
                "output": response[:5000] if len(response) > 5000 else response,
            })

            previous_output = response

        # Build readable result
        result_lines = [f"# Pipeline Results ({len(resolved)} steps)\n"]
        for so in step_outputs:
            result_lines.append(f"## Step {so['step']}: {so['model']}")
            result_lines.append(f"*Instruction: {so['instruction']}*\n")
            result_lines.append(so["output"])
            result_lines.append("\n---\n")

        return {
            "results": "\n".join(result_lines),
            "steps": step_outputs,
            "final_output": previous_output,
        }
    except Exception as e:
        logger.error(f"pipeline failed at step {len(step_outputs) + 1}: {e}")
        return {"error": f"Pipeline failed at step {len(step_outputs) + 1}: {e}"}


# ---------------------------------------------------------------------------
# Session management tool
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Memory management tool
# ---------------------------------------------------------------------------

async def do_recall_memory(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Read-only memory recall — the chat lane's ONLY tool (T8 pull
    affordance). No write actions exist here by design; manage_memory's
    add/edit/delete stay agent-lane.

    Content format:
      Line 1: search query, or "id: <memory_id>" for an exact fetch.

    Results inherit trust tiering (memory-trust metaplan): endorsed
    records (hand-authored/pinned/toggled kinds) return as plain trusted
    lines; everything else returns inside the untrusted guard wrapper so
    a pulled memory can't smuggle instructions past the firewall."""
    if not _memory_provider:
        return {"error": "Memory provider not available"}
    query = content.strip()
    if not query:
        return {"error": "Need a search query (or 'id: <memory_id>')"}

    from src.memory_trust import trusted
    from src.prompt_security import untrusted_context_message

    try:
        if query.lower().startswith("id:"):
            memory_id = query[3:].strip()
            record = await _memory_provider.get(memory_id, owner=owner)
            records = [record] if record else []
        else:
            hits = await _memory_provider.recall(query, owner=owner, top_k=5)
            records = [h.memory for h in hits]
    except Exception as exc:
        return {"error": f"Memory recall failed: {exc}"}
    if not records:
        return {"results": "No matching memories."}

    try:
        from routes.prefs_routes import _load_for_user

        prefs = _load_for_user(owner) or {}
    except Exception:
        prefs = {}

    handler_label = _resolve_handler_label(owner)

    def _render(text) -> str:
        return _render_memory_text(text, handler_label)

    def _line(record) -> str:
        kind = getattr(record, "kind", "") or getattr(record, "category", "")
        if kind == "unknown":
            # A pulled question must read as a question, not as a fact
            # of unknown provenance.
            kind = "open question"
        return f"- [{kind}] {_render(record.text)}"

    endorsed = [r for r in records if trusted(r, prefs)]
    reference = [r for r in records if r not in endorsed]
    sections = []
    if endorsed:
        sections.append(
            "Endorsed by the user (treat as reliable):\n"
            + "\n".join(_line(r) for r in endorsed)
        )
    if reference:
        wrapped = untrusted_context_message(
            "memory recall results",
            "\n".join(_line(r) for r in reference),
        )
        sections.append(str(wrapped["content"]))

    used_ids = [r.id for r in records if getattr(r, "id", None)]
    if used_ids and hasattr(_memory_provider, "record_access"):
        try:
            await _memory_provider.record_access(used_ids, owner=owner)
        except Exception:
            logger.debug("recall_memory access accounting failed", exc_info=True)

    return {"results": "\n\n".join(sections)}


def _resolve_handler_label(owner) -> str:
    """Owner-scoped Handler label for read-time %USER% rendering (fail-safe)."""
    try:
        from services.memory.principal_context import resolve_handler_display_label

        return resolve_handler_display_label(owner)
    except Exception:
        return "Handler"


def _render_memory_text(text, handler_label: str) -> str:
    """Read-time identity rendering for model-facing memory text.

    Stored claims keep the %USER% token; only this output projection shows
    the Handler label.
    """
    try:
        from services.memory.principal_context import render_identity_template

        return render_identity_template(str(text or ""), handler_label=handler_label)
    except Exception:
        return str(text or "")


def _normalize_question(text: str) -> str:
    """Mirror of the engine's question normalization (collapse
    whitespace, one trailing '?') so the record echoed back to the model
    matches what the store keeps."""
    collapsed = " ".join(text.split())
    trimmed = collapsed.rstrip("? ").rstrip()
    return f"{trimmed}?" if trimmed else ""


async def do_manage_memory(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Manage memories: list, add, edit, delete, search, resolve.

    Content format:
      Line 1: action (list|add|edit|delete|search|resolve)
      Line 2+: action-specific params

    Actions:
      list                    — list all memories (optional line 2: category filter)
      add                     — line 2: text, optional line 3: category
                                (fact|event|contact|preference|unknown).
                                category "unknown" (or "question") stores an
                                OPEN QUESTION the user wants answered later
                                ("user's name?").
      edit                    — line 2: memory_id, line 3: new text
      delete                  — line 2: memory_id
      search                  — line 2: query
      resolve                 — line 2: question memory_id, remaining lines:
                                the answer. Revises the same knowledge block;
                                never creates a second fact or deletes it.
    """
    if not _memory_provider:
        return {"error": "Memory provider not available"}

    lines = content.strip().split("\n")
    if not lines:
        return {"error": "Need at least 1 line: action"}

    action = lines[0].strip().lower()
    from routes.prefs_routes import _load_for_user
    from src.memory_gate import memory_mode, write_allowed

    prefs = _load_for_user(owner) or {}
    mode = memory_mode(prefs)

    if action in {"edit", "delete", "resolve"}:
        ok, reason = write_allowed(prefs)
        if not ok:
            return {"error": reason}

    # Provider path: route through the active provider
    if _memory_provider:
        if action == "list":
            try:
                records = await _memory_provider.list_memories(owner=owner, limit=100)
                if not records:
                    return {"results": "No memories found."}
                result_lines = [f"Found {len(records)} memory entries:\n"]
                for r in records:
                    cat = r.category
                    mid = r.id[:8]
                    text = _render_memory_text(r.text, _resolve_handler_label(owner))
                    if len(text) > 150:
                        text = text[:150] + "..."
                    result_lines.append(f"- [{cat}] `{mid}` — {text}")
                return {"results": "\n".join(result_lines)}
            except Exception as e:
                return {"error": f"Provider list failed: {e}"}

        elif action == "add":
            if len(lines) < 2:
                return {"error": "Add needs line 2: memory text"}
            text = lines[1].strip()
            category = lines[2].strip().lower() if len(lines) > 2 and lines[2].strip() else "fact"
            if category in ("unknown", "question"):
                category = "unknown"
                text = _normalize_question(text)
            if not text:
                return {"error": "Memory text cannot be empty"}
            if mode == "off":
                return {"error": "Memory is off — no writes allowed."}
            review_only = mode == "manual"
            try:
                record = await _memory_provider.remember(
                    text, owner=owner, session_id=session_id,
                    category=category,
                    # Open questions are user-authored requests by contract.
                    # Agent-discovered answers remain AI-attributed facts.
                    source="user" if category == "unknown" else "ai_agent",
                    capture_mode="review_only" if review_only else "manual",
                )
                if review_only:
                    return {
                        "action": "propose",
                        "candidate_id": record.id,
                        "pending_review": True,
                        "results": f"Memory proposed for review: [{category}] {text}",
                    }
                return {"action": "add", "memory_id": record.id,
                        "results": f"Memory added: [{category}] {text}"}
            except Exception as e:
                return {"error": f"Provider add failed: {e}"}

        elif action == "edit":
            if len(lines) < 3:
                return {"error": "Edit needs line 2: memory_id, line 3: new text"}
            display_id = lines[1].strip()
            new_text = lines[2].strip()
            category = lines[3].strip().lower() if len(lines) > 3 and lines[3].strip() else None
            if not new_text:
                return {"error": "New text cannot be empty"}
            try:
                memory_id = await _memory_provider.resolve_id(display_id, owner=owner)
                record = await _memory_provider.update(
                    memory_id, text=new_text, category=category, owner=owner,
                )
                if record is None:
                    return {"error": f"Memory '{display_id}' not found"}
                return {"action": "edit", "memory_id": memory_id,
                        "results": f"Memory updated: {new_text}"}
            except Exception as e:
                return {"error": f"Provider edit failed: {e}"}

        elif action == "delete":
            if len(lines) < 2:
                return {"error": "Delete needs line 2: memory_id"}
            display_id = lines[1].strip()
            try:
                memory_id = await _memory_provider.resolve_id(display_id, owner=owner)
                if _memory_lifecycle is None:
                    return {
                        "error": "Coordinated memory deletion is unavailable"
                    }
                deleted = await _memory_lifecycle.delete(
                    memory_id,
                    owner=owner or "",
                )
                if deleted is None:
                    return {"error": f"Memory '{display_id}' not found"}
                return {"action": "delete", "memory_id": memory_id,
                        "results": f"Memory '{memory_id}' deleted"}
            except Exception as e:
                return {"error": f"Provider delete failed: {e}"}

        elif action == "search":
            if len(lines) < 2:
                return {"error": "Search needs line 2: query"}
            query = lines[1].strip()
            try:
                hits = await _memory_provider.recall(query, owner=owner, top_k=20)
                if not hits:
                    return {"results": f"No memories found matching '{query}'."}
                result_lines = [f"Found {len(hits)} matching memories:\n"]
                for h in hits:
                    cat = h.memory.category
                    mid = h.memory.id[:8]
                    text = _render_memory_text(h.memory.text, _resolve_handler_label(owner))
                    result_lines.append(f"- [{cat}] `{mid}` — {text}")
                return {"results": "\n".join(result_lines)}
            except Exception as e:
                return {"error": f"Provider search failed: {e}"}

        elif action == "resolve":
            if len(lines) < 2:
                return {"error": "Resolve needs line 2: question memory_id"}
            display_id = lines[1].strip()
            answer_value = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
            try:
                memory_id = await _memory_provider.resolve_id(display_id, owner=owner)
                resolved_by = None
                answer = answer_value
                if answer_value.startswith("memory_id:"):
                    resolved_by = await _memory_provider.resolve_id(
                        answer_value.split(":", 1)[1].strip(), owner=owner
                    )
                    answer = None
                resolved = await _memory_provider.resolve_question(
                    memory_id,
                    resolved_by=resolved_by,
                    answer=answer or None,
                    owner=owner,
                )
                if not resolved:
                    return {"error": (
                        f"Memory '{display_id}' could not be resolved — only "
                        "open questions (category unknown) resolve"
                    )}
                return {"action": "resolve", "memory_id": memory_id,
                        "results": f"Open question '{memory_id}' answered in place"}
            except Exception as e:
                return {"error": f"Provider resolve failed: {e}"}

        else:
            return {"error": f"Unknown action '{action}'. Use: list, add, edit, delete, search, resolve"}

    # Provider-always: the app wires the active provider at startup
    # (app.py set_memory_manager(..., provider=...)); the old native
    # JSON-store fallback lived only for provider-less fixtures and is gone.
    return {"error": "Memory provider not available"}


# ---------------------------------------------------------------------------
# RAG management tool
# ---------------------------------------------------------------------------

async def do_manage_rag(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Manage RAG indexed documents: list, add_directory, remove_directory.

    Content format:
      Line 1: action (list|add_directory|remove_directory)
      Line 2: directory path (for add/remove)
    """
    lines = content.strip().split("\n")
    if not lines:
        return {"error": "No action specified"}
    action = lines[0].strip().lower()

    if action == "list":
        if not _personal_docs_manager:
            return {"results": "Personal docs manager not available. RAG may not be configured."}
        try:
            files = []
            if hasattr(_personal_docs_manager, 'index'):
                files = _personal_docs_manager.index or []
            dirs = []
            if hasattr(_personal_docs_manager, 'get_indexed_directories'):
                dirs = _personal_docs_manager.get_indexed_directories()

            result_lines = []
            if dirs:
                result_lines.append(f"**Indexed directories ({len(dirs)}):**")
                for d in dirs:
                    result_lines.append(f"  - `{d}`")
            if files:
                result_lines.append(f"\n**Indexed files ({len(files)}):**")
                for f in files[:50]:
                    name = f.get("name", str(f)) if isinstance(f, dict) else str(f)
                    result_lines.append(f"  - {name}")
                if len(files) > 50:
                    result_lines.append(f"  ... and {len(files) - 50} more")

            if not result_lines:
                return {"results": "No files or directories indexed in RAG."}
            return {"results": "\n".join(result_lines)}
        except Exception as e:
            return {"error": str(e)}

    elif action == "add_directory":
        if len(lines) < 2:
            return {"error": "add_directory needs line 2: directory path"}
        directory = lines[1].strip()

        import os
        directory = os.path.expanduser(directory)
        if not os.path.isdir(directory):
            return {"error": f"Directory not found: {directory}"}

        if not _rag_manager:
            return {"error": "RAG manager not available"}

        try:
            result = _rag_manager.index_personal_documents(directory, owner=owner)
            indexed = result.get("indexed", 0) if isinstance(result, dict) else 0
            return {"action": "add_directory", "directory": directory,
                    "results": f"Directory '{directory}' added to RAG index ({indexed} files indexed)"}
        except Exception as e:
            return {"error": f"Failed to index directory: {e}"}

    elif action == "remove_directory":
        if len(lines) < 2:
            return {"error": "remove_directory needs line 2: directory path"}
        directory = lines[1].strip()

        if not _personal_docs_manager:
            return {"error": "Personal docs manager not available"}

        try:
            if hasattr(_personal_docs_manager, 'remove_directory'):
                # Performs a targeted per-directory delete (#1660). The previous
                # unconditional _rag_manager.rebuild_index() here wiped the whole
                # collection on every remove (even for untracked dirs) and has
                # been removed.
                try:
                    _personal_docs_manager.remove_directory(directory, owner=owner)
                except TypeError:
                    _personal_docs_manager.remove_directory(directory)
            return {"action": "remove_directory", "directory": directory,
                    "results": f"Directory '{directory}' removed from RAG index"}
        except Exception as e:
            return {"error": f"Failed to remove directory: {e}"}

    else:
        return {"error": f"Unknown action '{action}'. Use: list, add_directory, remove_directory"}


# ---------------------------------------------------------------------------
# UI control tool (returns events for frontend to apply)
# ---------------------------------------------------------------------------

async def do_ui_control(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Control frontend UI: toggle settings, switch model, change theme.

    Content format:
      Line 1: action
      Line 2+: action-specific params

    Actions:
      toggle <name> <on|off>  — Toggle a setting (web, bash, rag, research, incognito, document_editor)
      switch_model <model>    — Change the model for the current session
      set_theme <preset>      — Apply a built-in theme preset (clanker-dark, clanker-light, dark, light, midnight, paper, cyberpunk, retrowave, forest, ocean, ume, copper, terminal, organs, lavender, gpt, claude, cute)
      create_theme <name> <bg> <fg> <panel> <border> <accent> [key=val ...] — Create custom theme. Optional key=val: advanced color overrides AND background effects: bgPattern=<none|clanker-sweep|clanker-blueprint|dots|synapse|rain|constellations|perlin-flow|petals|sparkles|embers>, bgEffectColor=#RRGGBB, bgEffectIntensity=<num>, bgEffectSize=<num>, frosted=true|false
      open_panel <name>       — Open a panel (documents, gallery, email, sessions, notes, memories, skills, settings, cookbook, usage)
      open_email_reply <uid> [folder] [reply|reply-all|ai-reply] [body text] — Open a reply draft document for an email; does not send. ALWAYS append the body text when the user told you what to say (one-shot draft); only omit body when the user just asked to "open a reply" without content.
      get_toggles             — Return current toggle states (server-side knowledge)
    """
    lines = content.strip().split("\n")
    if not lines:
        return {"error": "No action specified"}

    parts = lines[0].strip().split(None, 2)
    action = parts[0].lower()

    if action == "toggle":
        if len(parts) < 3:
            return {"error": "toggle needs: toggle <name> <on|off>"}
        toggle_name = parts[1].lower()
        state = parts[2].lower() in ("on", "true", "1", "yes", "enable", "enabled")
        # Friendly aliases — users say "shell" / "search" naturally.
        _toggle_aliases = {
            "shell": "bash",
            "terminal": "bash",
            "search": "web",
            "websearch": "web",
            "web_search": "web",
            "deepresearch": "research",
            "deep_research": "research",
            "documents": "document_editor",
            "doc": "document_editor",
            "docs": "document_editor",
            "private": "incognito",
        }
        toggle_name = _toggle_aliases.get(toggle_name, toggle_name)
        valid_toggles = {"web", "bash", "rag", "research", "incognito", "document_editor"}
        if toggle_name not in valid_toggles:
            return {"error": f"Unknown toggle '{toggle_name}'. Valid: {', '.join(sorted(valid_toggles))}"}
        return {
            "ui_event": "toggle",
            "toggle_name": toggle_name,
            "state": state,
            "results": f"Toggle '{toggle_name}' set to {'on' if state else 'off'}",
        }

    elif action == "switch_model":
        model_spec = " ".join(parts[1:]) if len(parts) > 1 else ""
        if not model_spec:
            model_spec = lines[1].strip() if len(lines) > 1 else ""
        if not model_spec:
            return {"error": "switch_model needs a model name"}

        # Resolve the model to validate it exists
        try:
            from src.openclank.chat_routing import (
                MANAGED_ENGINE_PUBLIC_URL,
                resolve_chat_model_spec,
            )

            route = resolve_chat_model_spec(owner=owner, model_spec=model_spec)
        except ValueError as e:
            return {"error": str(e)}

        # Update current session's model if we have a session
        if session_id and _session_manager:
            from src.database import SessionLocal as SL2, Session as DbSess2
            db2 = SL2()
            try:
                db_s = db2.query(DbSess2).filter(DbSess2.id == session_id).first()
                if db_s:
                    db_s.endpoint_url = MANAGED_ENGINE_PUBLIC_URL
                    db_s.endpoint_id = route.public_endpoint_id
                    db_s.provider_model_route_id = route.model_route_id
                    db_s.model = route.provider_model_id
                    db_s.headers = {}
                    db2.commit()
            finally:
                db2.close()

            sess = _session_manager.get_session(session_id)
            if sess:
                sess.endpoint_url = MANAGED_ENGINE_PUBLIC_URL
                sess.endpoint_id = route.public_endpoint_id
                sess.provider_model_route_id = route.model_route_id
                sess.model = route.provider_model_id
                sess.headers = {}

        return {
            "ui_event": "switch_model",
            "model": route.provider_model_id,
            "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
            "model_route_id": route.model_route_id,
            "results": f"Model switched to '{route.provider_model_id}'",
        }

    elif action == "set_theme":
        theme_name = parts[1].lower() if len(parts) > 1 else ""
        # Theme colors are defined in static/js/theme.js on the frontend.
        # We pass the name; the frontend looks it up from presets + custom themes.
        # Also check user's custom themes stored in prefs.
        # Must match the THEMES keys in static/js/theme.js.
        known_presets = [
            "clanker-dark", "clanker-light", "dark", "light", "midnight", "paper", "cyberpunk", "retrowave",
            "forest", "ocean", "ume", "copper", "terminal", "organs",
            "lavender", "gpt", "claude", "cute",
        ]
        custom_themes = {}
        try:
            from routes.prefs_routes import _load as _load_prefs
            custom_themes = _load_prefs().get("custom-themes", {}) or {}
        except Exception:
            pass
        all_known = set(known_presets) | set(custom_themes.keys())
        if theme_name not in all_known:
            custom_label = f" | Custom: {', '.join(sorted(custom_themes.keys()))}" if custom_themes else ""
            return {"error": f"Unknown theme '{theme_name}'. Available: {', '.join(sorted(known_presets))}{custom_label}"}
        return {
            "ui_event": "set_theme",
            "theme_name": theme_name,
            "results": f"Theme changed to '{theme_name}'",
        }

    elif action == "create_theme":
        # Re-split without limit to get all parts
        parts = lines[0].strip().split()
        # create_theme <name> <bg> <fg> <panel> <border> <accent> [key=value ...]
        if len(parts) < 7:
            return {"error": "create_theme needs: create_theme <name> <bg> <fg> <panel> <border> <accent> (all hex colors). Optional advanced color key=value pairs (userBubbleBg, aiBubbleBg, bubbleBorder, sidebarBg, sectionAccent, brandColor, inputBg, inputBorder, sendBtnBg, sendBtnHover, codeBg, codeFg, toggleBg, toggleActive, accentPrimary, accentError). Optional background EFFECTS: bgPattern=<none|clanker-sweep|clanker-blueprint|dots|synapse|rain|constellations|perlin-flow|petals|sparkles|embers>, bgEffectColor=#RRGGBB, bgEffectIntensity=<num e.g. 1>, bgEffectSize=<num e.g. 1>, frosted=true|false"}
        name = parts[1].lower().replace(" ", "-")
        colors = {"bg": parts[2], "fg": parts[3], "panel": parts[4], "border": parts[5], "red": parts[6]}
        # Validate base hex colors
        import re as _re
        for k, v in colors.items():
            if not _re.match(r'^#[0-9a-fA-F]{6}$', v):
                return {"error": f"Invalid hex color for {k}: '{v}'. Use format #RRGGBB"}
        # Parse optional advanced key=value pairs
        adv_keys = {
            "userBubbleBg", "aiBubbleBg", "bubbleBorder", "sidebarBg",
            "sectionAccent", "brandColor", "inputBg", "inputBorder",
            "sendBtnBg", "sendBtnHover", "codeBg", "codeFg",
            "toggleBg", "toggleActive", "accentPrimary", "accentError",
        }
        advanced = {}
        # Background-effect fields (animated pattern + frosted glass). Different
        # value types than the hex-only advanced keys, so parse separately.
        _BG_PATTERNS = {"none", "clanker-sweep", "clanker-blueprint", "dots", "synapse", "rain", "constellations",
                        "perlin-flow", "petals", "sparkles", "embers"}
        bg = {}
        for part in parts[7:]:
            if "=" not in part:
                continue
            ak, av = part.split("=", 1)
            if ak in adv_keys:
                if not _re.match(r'^#[0-9a-fA-F]{6}$', av):
                    return {"error": f"Invalid hex color for advanced key {ak}: '{av}'. Use format #RRGGBB"}
                advanced[ak] = av
            elif ak == "bgPattern":
                if av not in _BG_PATTERNS:
                    return {"error": f"Invalid bgPattern '{av}'. Use one of: {', '.join(sorted(_BG_PATTERNS))}"}
                bg["pattern"] = av
            elif ak == "bgEffectColor":
                if not _re.match(r'^#[0-9a-fA-F]{6}$', av):
                    return {"error": f"Invalid hex color for bgEffectColor: '{av}'. Use format #RRGGBB"}
                bg["effectColor"] = av
            elif ak in ("bgEffectIntensity", "bgEffectSize"):
                try:
                    bg["effectIntensity" if ak == "bgEffectIntensity" else "effectSize"] = float(av)
                except ValueError:
                    return {"error": f"Invalid number for {ak}: '{av}'"}
            elif ak == "frosted":
                bg["frosted"] = av.lower() in ("true", "1", "yes", "on")
        if advanced:
            colors["advanced"] = advanced
        return {
            "ui_event": "create_theme",
            "theme_name": name,
            "colors": colors,
            "bg": bg or None,
            "results": f"Custom theme '{name}' created and applied"
                       + (f" with {len(advanced)} advanced overrides" if advanced else "")
                       + (f" + background effect ({bg.get('pattern', 'frosted' if bg.get('frosted') else 'custom')})" if bg else ""),
        }

    elif action == "highlight":
        selector = parts[1] if len(parts) > 1 else ""
        label = " ".join(parts[2:]) if len(parts) > 2 else ""
        if not selector:
            return {"error": "highlight needs: highlight <css-selector> [label]"}
        return {
            "ui_event": "highlight",
            "selector": selector,
            "label": label,
            "results": f"Highlighting '{selector}'",
        }

    elif action == "clear_highlight":
        return {
            "ui_event": "clear_highlight",
            "results": "Highlights cleared",
        }

    elif action == "open_panel":
        # Open a top-level panel/modal: documents/library, gallery,
        # email, sessions, notes, memories, skills, settings, cookbook.
        panel = parts[1].lower() if len(parts) > 1 else ""
        _panel_aliases = {
            "documents": "documents",
            "document": "documents",
            "doc": "documents",
            "docs": "documents",
            "library": "documents",
            "doclib": "documents",
            "gallery": "gallery",
            "images": "gallery",
            "email": "email",
            "emails": "email",
            "inbox": "email",
            "mail": "email",
            "sessions": "sessions",
            "chats": "sessions",
            "history": "sessions",
            "notes": "notes",
            "note": "notes",
            "todo": "notes",
            "todos": "notes",
            "memories": "memories",
            "memory": "memories",
            "brain": "memories",
            "skills": "skills",
            "settings": "settings",
            "preferences": "settings",
            "cookbook": "cookbook",
            "models": "cookbook",
            "llm": "cookbook",
            "serve": "cookbook",
            "serving": "cookbook",
            "usage": "usage",
            "stats": "usage",
        }
        target = _panel_aliases.get(panel)
        if not target:
            return {"error": f"Unknown panel '{panel}'. Valid: documents, gallery, email, sessions, notes, memories, skills, settings, cookbook, usage."}
        return {
            "ui_event": "open_panel",
            "panel": target,
            "results": f"Opening {target} panel",
        }

    elif action == "open_email_reply":
        # Two forms supported:
        #   open_email_reply <uid> [folder] [reply|reply-all|ai-reply]
        #   open_email_reply <uid> [folder] [reply|reply-all|ai-reply]
        #     <body text on subsequent lines or after the mode token>
        # The body text (if any) gets pre-filled into the reply draft so the
        # agent can compose-and-open in one tool call instead of opening an
        # empty draft and leaving the user to wonder what happened.
        first_line = lines[0].strip()
        parts = first_line.split(maxsplit=4)
        uid = parts[1].strip() if len(parts) > 1 else ""
        folder = parts[2].strip() if len(parts) > 2 else "INBOX"
        mode = parts[3].strip().lower() if len(parts) > 3 else "reply"
        # Body: everything on the first line after the mode token, plus any
        # subsequent lines. Allows multi-line bodies.
        inline_body = parts[4] if len(parts) > 4 else ""
        rest_lines = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        body = (inline_body + ("\n" + rest_lines if rest_lines else "")).strip()
        if not uid:
            return {"error": "open_email_reply needs: open_email_reply <uid> [folder] [reply|reply-all|ai-reply] [body text]"}
        if mode not in ("reply", "reply-all", "ai-reply"):
            mode = "reply"
        # Body is REQUIRED for the agent path. Opening an empty draft is what
        # users do by clicking the Reply button — they don't ask the agent
        # for that. Every agent invocation of open_email_reply MUST include
        # the body. Reject empty so the agent retries with the content the
        # user asked for. Exception: ai-reply mode triggers the existing
        # AI-Reply path on the frontend which generates its own body.
        if not body and mode != "ai-reply":
            return {
                "error": (
                    "open_email_reply called without body. The agent path REQUIRES a body — "
                    "opening an empty draft is the wrong response when the user asked you to write. "
                    "Re-call with the reply text included: "
                    f"`open_email_reply {uid} {folder or 'INBOX'} {mode} <your reply text here>`. "
                    "Compose the reply now based on the open email's content and the user's request, "
                    "then call this tool again with the body. Do NOT call create_document instead."
                ),
            }
        result = {
            "ui_event": "open_email_reply",
            "uid": uid,
            "folder": folder or "INBOX",
            "mode": mode,
            "results": f"Opening reply draft for email UID {uid}" + (" with pre-filled body" if body else ""),
        }
        if body:
            result["body"] = body
        return result

    elif action == "get_toggles":
        return {
            "results": (
                "Toggle states are managed client-side in localStorage. "
                "Available toggles: web, bash, rag, research, incognito, document_editor. "
                "Use 'toggle <name> <on|off>' to change them."
            )
        }

    else:
        return {"error": f"Unknown action '{action}'. Use: toggle, switch_model, set_theme, highlight, clear_highlight, get_toggles"}


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _managed_image_suffix(media_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(str(media_type or "").split(";", 1)[0].lower(), ".png")


def _save_managed_gallery_image(
    *,
    image_bytes: bytes,
    media_type: str,
    prompt: str,
    model_route_id: str,
    size: str,
    quality: str,
    session_id: Optional[str],
    owner: Optional[str],
) -> tuple[str, str]:
    """Persist a managed image artifact and its stable route provenance."""

    import hashlib

    owner_key = gallery_owner_key(owner)
    if owner_key is None:
        raise RuntimeError("Gallery image owner provenance is unavailable")
    from src.openclank.files_image_store import FilesImageStore
    image_id = None
    try:
        from src.database import GalleryImage, Session as DbSession, SessionLocal

        width = None
        height = None
        try:
            import io
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as decoded:
                width, height = decoded.size
        except Exception:
            pass

        store = FilesImageStore(session_factory=SessionLocal)
        gallery = store.ensure_photos(owner_key)
        managed = store.import_image(
            owner_key,
            parent_id=gallery.id,
            name=f"generated{_managed_image_suffix(media_type)}",
            data=image_bytes,
            mime_type=media_type,
            operation_key=f"generated:{owner_key}:{session_id or 'none'}:{model_route_id}:{hashlib.sha256(image_bytes).hexdigest()}",
            provenance={"prompt": prompt, "model": model_route_id, "size": size, "quality": quality},
            source_provider="gallery",
        )
        image_id = managed.id
        filename = managed.locator or f"{uuid.uuid4().hex[:12]}{_managed_image_suffix(media_type)}"
        with SessionLocal() as db:
            scoped_session_id = None
            if session_id:
                scoped_session = db.query(DbSession.id).filter(
                    DbSession.id == session_id,
                    DbSession.owner == owner_key,
                ).first()
                if scoped_session is not None:
                    scoped_session_id = session_id
            image = GalleryImage(
                id=image_id,
                filename=filename,
                prompt=prompt,
                model=model_route_id,
                size=size,
                quality=quality,
                session_id=scoped_session_id,
                owner=owner_key,
                file_hash=hashlib.sha256(image_bytes).hexdigest(),
                file_size=len(image_bytes),
                width=width,
                height=height,
            )
            db.add(image)
            db.commit()
            # FilesImageStore has already published the immutable bytes.  The
            # GalleryImage row is retained only as a compatibility projection.
    except Exception:
        logger.warning(
            "Failed to publish managed image with Gallery provenance",
            exc_info=True,
        )
        raise
    return f"/api/generated-image/{filename}", image_id


def _managed_image_error(action: str, exc: Exception) -> Dict[str, str]:
    """Return a controlled error without reflecting provider details."""

    from src.openclank.operation_router import (
        ManagedOperationDenied,
        ManagedOperationUnavailable,
    )

    logger.warning("Managed image %s failed (%s)", action, type(exc).__name__)
    if isinstance(exc, ManagedOperationDenied):
        return {"error": f"The selected image route cannot perform {action}."}
    if isinstance(exc, ManagedOperationUnavailable):
        return {
            "error": (
                "No managed image route is available. "
                "Configure an Images route in Settings → Providers."
            )
        }
    return {"error": f"Image {action} failed."}


async def do_generate_image(
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    grant_id: Optional[str] = None,
) -> Dict:
    """Generate an image through MiMo's typed operation router.

    Line 2, when present, is a stable normalized model-route ID. Provider model
    names and endpoint aliases are deliberately not execution selectors after
    the provider hard cut.
    """

    from src.openclank.modality_facade import generate_image as managed_generate_image

    lines = (content or "").strip().split("\n")
    prompt = lines[0].strip() if lines else ""
    model_route_id = (
        lines[1].strip()
        if len(lines) > 1 and lines[1].strip()
        else None
    )
    size = lines[2].strip() if len(lines) > 2 and lines[2].strip() else "1024x1024"
    quality = lines[3].strip() if len(lines) > 3 and lines[3].strip() else "medium"

    if not prompt:
        return {"error": "Image prompt is required (line 1)"}
    if not __import__("re").fullmatch(r"(?:auto|\d{2,5}x\d{2,5})", size):
        size = "1024x1024"
    if quality not in {"low", "medium", "high", "auto"}:
        quality = "medium"

    try:
        image_bytes, media_type, result = await managed_generate_image(
            owner=owner or "",
            prompt=prompt,
            size=size,
            quality=quality,
            model_route_id=model_route_id,
            grant_id=grant_id,
            root_operation_id=root_operation_id,
            idempotency_key=idempotency_key,
        )
        image_url, image_id = _save_managed_gallery_image(
            image_bytes=image_bytes,
            media_type=media_type,
            prompt=prompt,
            model_route_id=result.model_route_id,
            size=size,
            quality=quality,
            session_id=session_id,
            owner=owner,
        )
        return {
            "results": f"Generated image for: {prompt[:100]}",
            "image_url": image_url,
            "image_id": image_id,
            "image_prompt": prompt,
            "image_model": result.model_route_id,
            "image_size": size,
            "image_quality": quality,
        }
    except Exception as exc:
        return _managed_image_error("generation", exc)


async def do_edit_image(
    prompt: str,
    image_path: str,
    model_spec: str = "",
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    size: str = "1024x1024",
    quality: str = "medium",
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    grant_id: Optional[str] = None,
) -> Dict:
    """Edit one image through the managed engine, without protocol fallback."""

    import mimetypes
    from pathlib import Path

    from src.openclank.modality_facade import transform_image

    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "Image edit prompt is required"}
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        return {"error": "Attached image file was not found"}
    if not __import__("re").fullmatch(r"(?:auto|\d{2,5}x\d{2,5})", size):
        size = "1024x1024"
    if quality not in {"low", "medium", "high", "auto"}:
        quality = "medium"
    model_route_id = str(model_spec or "").strip() or None
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"

    if progress_callback:
        await progress_callback(
            {
                "status": "running",
                "message": "Editing image through the managed image route",
                "step": 0,
                "total": 1,
            }
        )
    try:
        image_bytes, output_media_type, result = await transform_image(
            owner=owner or "",
            operation="image.edit",
            image=path.read_bytes(),
            media_type=media_type,
            input={
                "prompt": prompt,
                "size": size,
                "quality": quality,
            },
            model_route_id=model_route_id,
            grant_id=grant_id,
            root_operation_id=root_operation_id,
            idempotency_key=idempotency_key,
        )
        image_url, image_id = _save_managed_gallery_image(
            image_bytes=image_bytes,
            media_type=output_media_type,
            prompt=prompt,
            model_route_id=result.model_route_id,
            size=size,
            quality=quality,
            session_id=session_id,
            owner=owner,
        )
        if progress_callback:
            await progress_callback(
                {
                    "status": "done",
                    "message": "Image edit complete",
                    "step": 1,
                    "total": 1,
                }
            )
        return {
            "results": f"Edited image for: {prompt[:100]}",
            "image_url": image_url,
            "image_id": image_id,
            "image_prompt": prompt,
            "image_model": result.model_route_id,
            "image_size": size,
            "image_quality": quality,
        }
    except Exception as exc:
        return _managed_image_error("editing", exc)


# ---------------------------------------------------------------------------
# Dispatcher (called from agent_tools.execute_tool_block)
# ---------------------------------------------------------------------------

async def dispatch_ai_tool(
    tool: str, content: str, session_id: Optional[str] = None, owner: Optional[str] = None
) -> Tuple[str, Dict]:
    """Dispatch an AI interaction tool. Returns (description, result_dict)."""

    if tool == "pipeline":
        desc = "pipeline: running steps"
        result = await do_pipeline(content, session_id, owner=owner)

    elif tool == "manage_memory":
        action = content.split("\n")[0].strip()[:40]
        desc = f"manage_memory: {action}"
        result = await do_manage_memory(content, session_id, owner=owner)

    elif tool == "recall_memory":
        query = content.split("\n")[0].strip()[:60]
        desc = f"recall_memory: {query}" if query else "recall_memory"
        result = await do_recall_memory(content, session_id, owner=owner)

    elif tool == "ui_control":
        action = content.split("\n")[0].strip()[:60]
        desc = f"ui_control: {action}"
        result = await do_ui_control(content, session_id, owner=owner)

    else:
        desc = f"unknown ai tool: {tool}"
        result = {"error": f"Unknown AI interaction tool: {tool}"}

    return desc, result
