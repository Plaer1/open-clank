# routes/memory_routes.py
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File, Query
from typing import Dict, Any, Optional, List
import json
import os
import re
import tempfile
import time
from datetime import datetime
import logging

# Leading list-marker like "1.", "12)", or "3:" plus surrounding whitespace.
# Strips one prefix per call so import-from-LLM-output doesn't leave the
# numbering inside the saved memory text. Bullet markers (-, *, •) are
# also peeled here for the same reason.
_LIST_PREFIX_RE = re.compile(r"^\s*(?:\d{1,3}[.):]\s+|[-*•]\s+)")


def _strip_list_prefix(text: str) -> str:
    if not text:
        return text
    return _LIST_PREFIX_RE.sub("", text, count=1).strip()

from services.memory import MemoryManager
from core.session_manager import SessionManager
from src.request_models import MemoryAddRequest
from core.database import SessionLocal
from src.llm_core import llm_call_async
from services.memory.memory_extractor import audit_provider_memories
from src.auth_helpers import get_current_user, require_user
from src.endpoint_resolver import resolve_endpoint
from src.task_endpoint import resolve_task_endpoint
from src.upload_limits import read_upload_limited, MEMORY_IMPORT_MAX_BYTES

logger = logging.getLogger(__name__)


def setup_memory_routes(memory_manager: MemoryManager, session_manager: SessionManager, memory_provider=None):
    # memory_manager remains only for its stateless text helpers
    # (find_duplicates, extract_memory_from_chat suggestions); all storage
    # goes through memory_provider — provider-always, no native fallbacks.
    """Set up memory-related routes."""
    router = APIRouter(prefix="/api/memory", tags=["memory"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    def _provider_record(record) -> dict:
        metadata = dict(getattr(record, "metadata", {}) or {})
        return {
            "id": record.id,
            "text": record.text,
            "timestamp": record.timestamp,
            "category": record.category,
            "source": record.source,
            "owner": record.owner,
            "session_id": record.session_id,
            "pinned": bool(getattr(record, "pinned", False) or metadata.get("pinned", False)),
            "metadata": metadata,
            "kind": getattr(record, "kind", "fact"),
            "source_type": getattr(record, "source_type", "human"),
            "priority": getattr(record, "priority", None),
            "trust_score": getattr(record, "trust_score", None),
            "confidence_score": getattr(record, "confidence_score", None),
            "importance_score": getattr(record, "importance_score", None),
            "scene_name": getattr(record, "scene_name", None),
            "tags": list(getattr(record, "tags", None) or []),
            "source_message_ids": list(getattr(record, "source_message_ids", None) or []),
            "workspace_id": getattr(record, "workspace_id", None),
            "workspace_path": getattr(record, "workspace_path", None),
            "archived": bool(getattr(record, "archived", False)),
            "exempt_from_decay": bool(getattr(record, "exempt_from_decay", False)),
            "exempt_from_dedup": bool(getattr(record, "exempt_from_dedup", False)),
            "last_accessed_at": getattr(record, "last_accessed_at", None),
            "created_at": getattr(record, "created_at", None),
            "updated_at": getattr(record, "updated_at", None),
            "uses": int(getattr(record, "uses", 0) or 0),
        }

    async def _all_provider_records(user: Optional[str]) -> list:
        if not hasattr(memory_provider, "list_page"):
            return await memory_provider.list_memories(owner=user, limit=1000)
        records = []
        cursor = None
        while True:
            page, cursor = await memory_provider.list_page(
                owner=user, limit=1000, cursor=cursor
            )
            records.extend(page)
            if cursor is None:
                return records

    def _assert_session_owner(session_obj, user):
        """SECURITY: 404 if the caller does not own this session.

        SessionManager.get_session is NOT owner-scoped — it returns any
        session by id. These routes accept a caller-supplied session id, so
        without this gate a user could target another tenant's session and
        leak their chat history, their session-scoped LLM credentials, or the
        session title. Mirrors session_routes / webhook_routes ownership.
        """
        if user is not None and getattr(session_obj, "owner", None) != user:
            raise HTTPException(404, "Session not found")

    @router.post("/debug")
    async def debug_memory_relevance(request: Request, query: str = Form(...)):
        """Debug which memories would be triggered for a query"""
        user = _owner(request)
        try:
            hits = await memory_provider.recall(query, owner=user, top_k=20)
        except Exception as exc:
            logger.warning("Provider debug recall failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")

        return {
            "query": query,
            "relevant_count": len(hits),
            "relevant_memories": [{"text": h.memory.text, "category": h.memory.category}
                                 for h in hits]
        }

    @router.post("/add", response_model=Dict[str, Any])
    async def api_add_memory(
        request: Request,
        memory_data: Optional[MemoryAddRequest] = None
    ):
        """Add a new memory entry with optional category, source, and session reference."""
        from src.auth_helpers import require_privilege
        require_privilege(request, "can_manage_memory")
        if memory_data is None:
            form = await request.form()
            memory_data = MemoryAddRequest(
                text=form.get("text"),
                category=form.get("category", "fact"),
                source=form.get("source", "user"),
                session_id=form.get("session_id")
            )

        user = _owner(request)
        text = (memory_data.text or "").strip()
        if not text:
            raise HTTPException(400, "empty memory")
        try:
            user_mem = [_provider_record(record) for record in await _all_provider_records(user)]
        except Exception as exc:
            logger.warning("Provider memory duplicate check failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")
        if memory_manager.find_duplicates(text, user_mem):
            return {"ok": True, "count": len(user_mem), "message": "Memory already exists"}

        if memory_data.session_id:
            try:
                session_obj = session_manager.get_session(memory_data.session_id)
            except KeyError:
                raise HTTPException(404, "Session not found")
            _assert_session_owner(session_obj, user)

        try:
            record = await memory_provider.remember(
                text, owner=user, session_id=memory_data.session_id,
                category=memory_data.category, source=memory_data.source,
            )
            try:
                from src.event_bus import fire_event
                fire_event("memory_added", user)
            except Exception:
                logger.debug("memory_added event dispatch failed", exc_info=True)
            return {"ok": True, "memory_id": record.id, "message": "Memory added via provider"}
        except Exception as e:
            logger.warning("Provider add failed: %s", e)
            raise HTTPException(503, "Active memory provider is unavailable")

    @router.get("")
    async def api_get_memory(
        request: Request,
        limit: int = Query(1000, ge=1, le=1000),
        cursor: Optional[str] = Query(None),
    ):
        """Return one explicit page of memory entries with their metadata."""
        user = _owner(request)
        page_limit = limit if isinstance(limit, int) else 1000
        page_cursor = cursor if isinstance(cursor, str) else None
        try:
            if hasattr(memory_provider, "list_page"):
                records, next_cursor = await memory_provider.list_page(
                    owner=user, limit=page_limit, cursor=page_cursor
                )
            else:
                records = await memory_provider.list_memories(
                    owner=user, limit=page_limit
                )
                next_cursor = None
            return {
                "memory": [_provider_record(record) for record in records],
                "provider": getattr(memory_provider, "provider_id", "unknown"),
                "next_cursor": next_cursor,
            }
        except Exception as exc:
            logger.warning("Provider memory list failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")

    @router.get("/inspect")
    async def inspect_memory_tier(
        request: Request,
        tier: str = Query("raw"),
        status: Optional[str] = Query(None),
        limit: int = Query(500, ge=1, le=1000),
    ):
        """Inspect honest Frankenmemory tiers without making raw/rejected data recallable."""
        if tier not in {"raw", "candidate", "curated", "quarantine"}:
            raise HTTPException(400, "tier must be raw, candidate, curated, or quarantine")
        if status not in {None, "pending", "accepted", "rejected", "quarantined"}:
            raise HTTPException(400, "invalid candidate status")
        if not memory_provider or not hasattr(memory_provider, "inspect_tier"):
            raise HTTPException(503, "Active memory provider does not expose tier inspection")
        try:
            rows = await memory_provider.inspect_tier(
                tier,
                owner=_owner(request),
                status=status,
                limit=limit,
            )
        except Exception as exc:
            logger.warning("Provider tier inspection failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable") from exc
        return {"tier": tier, "items": rows, "total": len(rows)}

    @router.get("/quality")
    async def memory_quality(request: Request, rebuild_graph_fts: bool = False):
        if not memory_provider or not hasattr(memory_provider, "memory_quality"):
            raise HTTPException(503, "Active memory provider does not expose quality status")
        if rebuild_graph_fts:
            from core.middleware import require_admin
            require_admin(request)
        try:
            return await memory_provider.memory_quality(rebuild_graph_fts=rebuild_graph_fts)
        except Exception as exc:
            logger.warning("Provider quality check failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable") from exc

    @router.get("/graph")
    async def memory_graph(
        request: Request,
        op: str = Query("overview"),
        query: Optional[str] = Query(None),
        node: Optional[str] = Query(None),
        to_node: Optional[str] = Query(None),
        tag: Optional[str] = Query(None),
        direction: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ):
        """Owner-scoped graph_walk passthrough (op=overview|cues|tags|expand|fetch|trace)."""
        if op not in {"overview", "cues", "rank", "tags", "expand", "fetch", "trace"}:
            raise HTTPException(400, "invalid graph op")
        if not memory_provider or not hasattr(memory_provider, "graph"):
            raise HTTPException(503, "Active memory provider does not expose the graph")
        try:
            return await memory_provider.graph(
                op,
                owner=_owner(request),
                query=query,
                node_id=node,
                to_node_id=to_node,
                tag=tag,
                direction=direction,
                limit=limit,
            )
        except Exception as exc:
            logger.warning("Provider graph op failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable") from exc

    @router.get("/digest-preview")
    async def memory_digest_preview(request: Request):
        """The EXACT index card injected each turn: raw digest dict + the
        rendered text (shared renderer — byte-identical to injection, no
        drift)."""
        if not memory_provider or not hasattr(memory_provider, "digest"):
            raise HTTPException(503, "Active memory provider does not expose a digest")
        from src.memory_digest import render_digest

        try:
            digest = await memory_provider.digest(owner=_owner(request))
        except Exception as exc:
            logger.warning("Provider digest preview failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable") from exc
        return {
            "digest": digest,
            "rendered": render_digest(digest),
        }

    @router.post("/candidate/{candidate_id}/review")
    async def review_memory_candidate(request: Request, candidate_id: str):
        from src.auth_helpers import require_privilege
        require_privilege(request, "can_manage_memory")
        if not memory_provider or not hasattr(memory_provider, "review_candidate"):
            raise HTTPException(503, "Active memory provider does not expose candidate review")
        body = await request.json()
        accept = body.get("accept")
        if not isinstance(accept, bool):
            raise HTTPException(400, "accept must be boolean")
        workspace_id = str(body.get("workspace_id") or "").strip()
        if not workspace_id:
            raise HTTPException(400, "workspace_id is required")
        reason = str(body.get("reason") or ("approved_by_user" if accept else "rejected_by_user")).strip()[:500]
        owner = _owner(request)
        if owner is None:
            owner = str(body.get("owner") or "legacy").strip()
        try:
            result = await memory_provider.review_candidate(
                candidate_id,
                accept=accept,
                reason=reason,
                owner=owner,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            logger.warning("Candidate review failed: %s", exc)
            raise HTTPException(400, "Candidate could not be reviewed in this scope") from exc
        return result

    @router.post("/search")
    async def search_memories(request: Request, query: str = Form(...), session_id: str = Form(None), category: str = Form(None)):
        """Search across all memories with optional filters."""
        user = _owner(request)

        try:
            hits = await memory_provider.recall(query, owner=user, top_k=20)
            results = [{
                "text": h.memory.text,
                "category": h.memory.category,
                "id": h.memory.id,
                "score": h.score,
                "owner": h.memory.owner,
                "session_id": h.memory.session_id,
                "source": h.memory.source,
            } for h in hits]
            if session_id:
                results = [r for r in results if r.get("session_id") == session_id]
            if category:
                results = [r for r in results if category in r.get("category", "")]
            return {"memories": results, "total": len(results), "query": query}
        except Exception as e:
            logger.warning("Provider search failed: %s", e)
            raise HTTPException(503, "Active memory provider is unavailable")

    @router.get("/timeline")
    async def memory_timeline(request: Request):
        """Get memories in chronological order with source session information."""
        user = _owner(request)
        try:
            memories = [_provider_record(record) for record in await _all_provider_records(user)]
        except Exception as exc:
            logger.warning("Provider memory timeline failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")
        sorted_memories = sorted(memories, key=lambda x: x.get("timestamp", 0), reverse=True)

        results = []
        for memory in sorted_memories:
            if "timestamp" in memory:
                try:
                    dt = datetime.fromtimestamp(memory["timestamp"])
                    memory["timestamp_str"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OSError, OverflowError):
                    memory["timestamp_str"] = "Unknown"
            else:
                memory["timestamp_str"] = "Unknown"

            session_id = memory.get("session_id")
            if session_id and session_id in session_manager.sessions:
                try:
                    session = session_manager.get_session(session_id)
                    if session:
                        _assert_session_owner(session, user)
                    memory["session_name"] = session.name if session else f"Session {session_id[:6]}"
                except KeyError:
                    memory["session_name"] = "Unknown"
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    memory["session_name"] = "Unknown"
            else:
                memory["session_name"] = "Unknown"

            results.append(memory)

        return {"timeline": results, "total": len(results)}

    @router.get("/by-session/{session_id}")
    async def get_memory_by_session(request: Request, session_id: str):
        """Get all memories associated with a specific session."""
        user = _owner(request)
        try:
            _session_obj = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, f"Session {session_id} not found")
        _assert_session_owner(_session_obj, user)
        try:
            memories = [_provider_record(record) for record in await _all_provider_records(user)]
        except Exception as exc:
            logger.warning("Provider session memory list failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")
        session_memories = [m for m in memories if m.get("session_id") == session_id]

        session_memories.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

        try:
            session = session_manager.get_session(session_id)
            session_name = session.name if session else f"Session {session_id[:6]}"
        except KeyError:
            session_name = f"Session {session_id[:6]}"

        for memory in session_memories:
            memory["session_name"] = session_name

        return {
            "session_id": session_id,
            "session_name": session_name,
            "memory_count": len(session_memories),
            "memories": session_memories
        }

    @router.post("/extract")
    async def extract_memory(request: Request, session: str = Form(...)) -> Dict[str, List[str]]:
        """Analyze a session's chat history and return memory suggestions."""
        require_user(request)
        try:
            sess = session_manager.get_session(session)
        except KeyError:
            raise HTTPException(404, "Session not found")
        _assert_session_owner(sess, _owner(request))

        system_msg = {
            "role": "system",
            "content": (
                "You are a helpful assistant. Analyze the entire conversation history provided and extract any "
                "useful factual statements, contacts, addresses, phone numbers, or other information that the user "
                "might want to remember for future interactions. Return each piece of information as a JSON object "
                "with a 'text' field. For example: [{'text': 'Alice lives at 123 Main St'}, {'text': 'Bob works at Acme Corp'}]. "
                "Only include information that is specific and likely to be useful later."
            ),
        }
        messages = [system_msg] + sess.get_context_messages()

        t_url, t_model, t_headers = resolve_task_endpoint(
            sess.endpoint_url, sess.model, sess.headers, owner=_owner(request)
        )

        try:
            suggestion_text = await llm_call_async(
                t_url,
                t_model,
                messages,
                temperature=0.2,
                max_tokens=500,
                headers=t_headers,
                owner=_owner(request),
                session_id=session,
            )
            try:
                suggestions = json.loads(suggestion_text)
                if isinstance(suggestions, list):
                    suggestions = [s if isinstance(s, str) else s.get("text", "") for s in suggestions]
                else:
                    suggestions = []
            except json.JSONDecodeError:
                suggestions = [line.strip() for line in suggestion_text.splitlines() if line.strip()]

            return {"suggestions": [s for s in suggestions if s]}
        except Exception as e:
            logger.error(f"LLM memory extraction failed (session {session}): {e}")
            fallback = memory_manager.extract_memory_from_chat(sess.history, session)
            return {"suggestions": [item["text"] for item in fallback]}

    @router.post("/audit")
    async def api_audit_memories(request: Request, session: str = Form(None)):
        """Deduplicate and consolidate memories via LLM.

        Uses task/utility/default settings through the shared resolver, with
        the active session as fallback when no task or utility model is set.
        Returns before and after memory counts.
        """
        from src.auth_helpers import require_privilege
        require_privilege(request, "can_manage_memory")
        user = _owner(request)
        fallback_url = fallback_model = None
        fallback_headers = None
        if session:
            try:
                sess = session_manager.get_session(session)
                _assert_session_owner(sess, user)
                fallback_url = sess.endpoint_url
                fallback_model = sess.model
                fallback_headers = sess.headers
            except KeyError:
                pass

        endpoint_url, model, headers = resolve_task_endpoint(
            fallback_url, fallback_model, fallback_headers, owner=user
        )

        if not endpoint_url or not model:
            raise HTTPException(400, "No default model configured — set one in Settings")

        result = await audit_provider_memories(
            memory_provider,
            endpoint_url,
            model,
            headers,
            owner=user,
        )

        if "error" in result and "before" not in result:
            raise HTTPException(502, f"Audit failed: {result['error']}")

        return {
            "ok": "error" not in result,
            "before": result.get("before", 0),
            "after": result.get("after", 0),
            "removed": result.get("before", 0) - result.get("after", 0),
            # True when the audit skipped the LLM because nothing changed
            # since the last tidy. Frontend already says "Already clean"
            # for removed==0, so this is here for future use / debugging.
            "already_tidy": bool(result.get("already_tidy")),
        }

    @router.post("/import")
    async def import_memories_from_file(
        request: Request,
        session: str | None = Form(None),
        file: UploadFile = File(...)
    ):
        """Extract memory suggestions from an uploaded file (PDF, TXT, MD, etc.)."""
        from src.auth_helpers import require_privilege
        require_privilege(request, "can_manage_memory")

        endpoint_url = None
        model = None
        headers = {}

        user = _owner(request)

        if session:
            try:
                sess = session_manager.get_session(session)
                _assert_session_owner(sess, user)
            except KeyError:
                sess = None
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                sess = None

            if sess is None:
                logger.warning("Session %s not found or inaccessible, falling back to utility endpoint", session)
                endpoint_url, model, headers = resolve_endpoint("utility", owner=user)
            else:
                endpoint_url, model, headers = resolve_task_endpoint(
                    sess.endpoint_url, sess.model, sess.headers, owner=user
                )
        else:
            endpoint_url, model, headers = resolve_task_endpoint(owner=user)
    
        if not endpoint_url or not model:
            raise HTTPException(400, "No LLM model configured. Set a default model in Settings.")

        content = await read_upload_limited(file, MEMORY_IMPORT_MAX_BYTES, "Memory import")
        filename = file.filename or "upload"
        _, ext = os.path.splitext(filename.lower())

        allowed = {".txt", ".md", ".pdf", ".csv", ".log", ".json", ".py", ".js", ".html"}
        if ext not in allowed:
            raise HTTPException(400, f"Unsupported file type: {ext}")

        # Extract text based on file type
        if ext == ".pdf":
            from src.document_processor import _process_pdf
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                text = _process_pdf(tmp_path, owner=_owner(request))
            finally:
                os.unlink(tmp_path)
        else:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                from charset_normalizer import detect
                encoding = (detect(content) or {}).get("encoding") or "utf-8"
                text = content.decode(encoding, errors="replace")

        if not text.strip():
            return {"suggestions": [], "message": "No readable content found"}

        # Fast path: a .json upload that already looks like a memories export
        # (list of {text, category, ...} dicts, or list of strings) round-trips
        # directly without spending an LLM call to re-extract its own output.
        # Without this, re-importing a memories.json from another account
        # ran the file through the extractor, which often re-emitted the
        # entries as a numbered list (and the numbering leaked into the
        # `text` field).
        if ext == ".json":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list) and parsed:
                direct = []
                for item in parsed:
                    if isinstance(item, dict) and item.get("text"):
                        direct.append({
                            "text": _strip_list_prefix(str(item["text"])),
                            "category": item.get("category") or "fact",
                        })
                    elif isinstance(item, str) and item.strip():
                        direct.append({
                            "text": _strip_list_prefix(item.strip()),
                            "category": "fact",
                        })
                if direct:
                    return {"suggestions": direct, "filename": filename}

        # Truncate very long documents
        if len(text) > 15000:
            text = text[:15000] + "\n[Truncated]"

        # Send to LLM for memory extraction
        import_prompt = (
            "You are a memory extraction assistant. The user uploaded a document. "
            "Analyze the text below and extract specific, useful facts — things like "
            "names, preferences, jobs, locations, relationships, opinions, projects, "
            "goals, contacts, or any other personal details worth remembering.\n\n"
            "Rules:\n"
            "- Each fact should be a short, self-contained statement\n"
            "- Do NOT extract generic knowledge\n"
            "- Focus on personal, memorable information\n"
            "- If there are no useful facts, return an empty array\n\n"
            "Return a JSON array of objects with 'text' and 'category' fields.\n"
            "Categories: 'identity', 'preference', 'fact', 'contact', 'project', 'goal'\n\n"
            "Return ONLY valid JSON, no markdown fences."
        )

        try:
            raw = await llm_call_async(
                endpoint_url,
                model,
                [
                    {"role": "system", "content": import_prompt},
                    {"role": "user", "content": f"Document: {filename}\n\n{text}"},
                ],
                temperature=0.2,
                max_tokens=2000,
                headers=headers,
                owner=user,
                session_id=session or None,
            )

            # Parse JSON
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            suggestions = json.loads(raw)
            if isinstance(suggestions, list):
                normalized = []
                for s in suggestions:
                    if not s:
                        continue
                    if isinstance(s, dict):
                        s = dict(s)
                        if s.get("text"):
                            s["text"] = _strip_list_prefix(str(s["text"]))
                        normalized.append(s)
                    else:
                        normalized.append({"text": _strip_list_prefix(str(s)), "category": "fact"})
                suggestions = normalized
            else:
                suggestions = []

            return {"suggestions": suggestions, "filename": filename}

        except json.JSONDecodeError:
            # Fallback: split by lines, stripping any "1.", "2)" markdown-list
            # numbering the model added so saved memories don't keep the prefix.
            lines = [_strip_list_prefix(l.strip()) for l in raw.splitlines() if l.strip() and len(l.strip()) > 5]
            return {"suggestions": [{"text": l, "category": "fact"} for l in lines[:20]], "filename": filename}
        except Exception as e:
            logger.error(f"Memory import extraction failed: {e}")
            raise HTTPException(502, f"LLM extraction failed: {str(e)}")

    @router.post("/{memory_id}/pin")
    async def pin_memory(request: Request, memory_id: str, pinned: bool = Form(True)):
        """Pin or unpin a memory. Pinned memories are always included in context."""
        from src.auth_helpers import require_privilege
        require_privilege(request, "can_manage_memory")
        user = _owner(request)
        try:
            if not await memory_provider.pin(memory_id, pinned, owner=user):
                raise HTTPException(404, f"Memory item {memory_id} not found")
            return {"ok": True, "pinned": pinned}
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Provider memory pin failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")

    # Wildcard routes MUST come last — otherwise they swallow /import, /search, etc.
    @router.get("/{memory_id}")
    async def get_memory_item(request: Request, memory_id: str):
        """Get a specific memory item by ID."""
        user = _owner(request)
        try:
            record = await memory_provider.get(memory_id, owner=user)
            if record is not None:
                return {"memory": _provider_record(record)}
            raise HTTPException(404, "Memory not found")
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Provider memory get failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")

    @router.put("/{memory_id}")
    async def update_memory(request: Request, memory_id: str, text: str = Form(...), category: str = Form(None)):
        """Update an existing memory item with new text and optional category."""
        from src.auth_helpers import require_privilege
        require_privilege(request, "can_manage_memory")
        user = _owner(request)
        try:
            record = await memory_provider.update(
                memory_id, text=text, category=category, owner=user
            )
            if record is None:
                raise HTTPException(404, f"Memory item {memory_id} not found")
            return {"ok": True, "memory": _provider_record(record)}
        except HTTPException:
            raise
        except NotImplementedError:
            raise HTTPException(501, "Active memory provider does not support updates")
        except Exception as exc:
            logger.warning("Provider memory update failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")

    @router.delete("/{memory_id}")
    async def delete_memory(request: Request, memory_id: str):
        """Delete a memory item by its ID."""
        from src.auth_helpers import require_privilege
        require_privilege(request, "can_manage_memory")
        user = _owner(request)
        try:
            if not await memory_provider.delete(memory_id, owner=user):
                raise HTTPException(404, f"Memory item {memory_id} not found")
            return {"ok": True, "message": "Memory deleted successfully"}
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Provider memory delete failed: %s", exc)
            raise HTTPException(503, "Active memory provider is unavailable")

    return router
