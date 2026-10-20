"""
memory_extractor.py

Background auto-extraction of facts from chat conversations.
After each LLM response, this module sends the last few messages to the LLM
asking it to extract memorable facts, then stores them in both memory.json
and the FAISS vector index.

Periodically audits all memories via LLM to consolidate duplicates,
rewrite vague entries, and remove junk.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

from services.memory.perspective import MEMORY_SELF_REFERENCE_RULES


async def _complete_text(**kwargs) -> str:
    """Lazy import keeps the lightweight extractor importable in isolation."""
    from src.openclank.modality_facade import complete_text

    return await complete_text(**kwargs)


def _tidy_state_path(memory_manager) -> str:
    """Sidecar JSON next to memory.json that remembers the fingerprint of
    the last successfully-audited state per owner. Lets the audit short-
    circuit when nothing has changed since the previous tidy — running
    the LLM again on an already-clean list was wasting 30-120s per call
    and occasionally timing out on the second pass."""
    return os.path.join(os.path.dirname(memory_manager.memory_file), "memory_tidy_state.json")


def _fingerprint_entries(entries) -> str:
    """Stable hash of an owner's memories — order-independent, depends
    only on id+text+category. Any add/edit/delete invalidates it."""
    items = sorted(
        (str(e.get("id", "")), e.get("text", ""), e.get("category", ""))
        for e in _memory_dicts(entries)
    )
    h = hashlib.sha256()
    for triple in items:
        h.update(("\x1f".join(triple) + "\x1e").encode("utf-8"))
    return h.hexdigest()


def _memory_dicts(entries):
    for entry in entries or []:
        if isinstance(entry, dict):
            yield entry


def _load_tidy_state(memory_manager) -> dict:
    path = _tidy_state_path(memory_manager)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tidy_state(memory_manager, owner: Optional[str], fingerprint: str) -> None:
    path = _tidy_state_path(memory_manager)
    state = _load_tidy_state(memory_manager)
    state[owner or ""] = {"fingerprint": fingerprint}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logger.warning(f"Could not persist tidy fingerprint: {e}")

EXTRACT_SYSTEM_PROMPT = (
    "You are a memory extraction assistant. Analyze the conversation and extract ONLY "
    "durable personal facts about the user that would be useful across many future conversations.\n\n"
    "Good examples: name, job title, city, family members, long-term projects, strong preferences.\n"
    "Bad examples: what they asked about today, temporary moods, generic statements, "
    "things the assistant said, one-off tasks, opinions on the current topic.\n\n"
    "Rules:\n"
    "- MAX 2 facts per conversation — only the most important\n"
    "- Only extract facts the USER stated or clearly implied\n"
    "- Each fact must be a single short sentence (under 15 words)\n"
    "- If a fact is similar to something likely already known, skip it\n"
    "- If nothing durable was revealed, return []\n\n"
    + MEMORY_SELF_REFERENCE_RULES
    + "\n"
    "Return a JSON array of objects with 'text' and 'category' fields.\n"
    "Categories: 'identity', 'preference', 'fact', 'contact', 'project', 'goal'\n\n"
    "Return ONLY valid JSON, no markdown fences."
)

# How many recent messages to include for extraction
CONTEXT_WINDOW = 6

AUDIT_SYSTEM_PROMPT = (
    "You are a memory database curator. Be CONSERVATIVE: remove only TRUE "
    "duplicates and clearly useless entries. Every distinct fact must survive. "
    "When in doubt, KEEP the entry. Account for every input entry exactly once.\n\n"
    "Rules:\n"
    "1. MERGE only entries that state the SAME fact in different words. If you "
    "are not sure two entries are the same fact, KEEP BOTH.\n"
    "   Merge: 'User's name is Sam' + 'The user is called Sam' -> one.\n"
    "   Do NOT merge related-but-distinct facts: 'Likes Python' and 'Uses "
    "Python at work' are DIFFERENT — keep both.\n"
    "2. REMOVE only entries that are genuinely worthless: about what the AI did "
    "(not the user), empty, or meaningless. Do NOT drop a real fact just "
    "because it seems minor or niche.\n"
    "3. Keep the original wording. Only lightly trim obvious redundancy — do "
    "NOT aggressively rewrite or shorten.\n"
    "4. Preserve the 'id' of the entry you keep when merging.\n"
    "5. Never invent facts. When unsure, KEEP.\n"
    "6. Every input id MUST appear once in `operations`. Omitting an id is "
    "not a deletion instruction.\n"
    "7. A deletion MUST use action `delete` and include a short `reason`. "
    "For a merge, include `retained_id` for the kept record.\n\n"
    "Return one JSON object with this exact shape:\n"
    "{\"operations\":[\n"
    "  {\"id\":\"input-id\",\"action\":\"keep\",\"text\":\"original or lightly edited text\",\"category\":\"original category\"},\n"
    "  {\"id\":\"duplicate-id\",\"action\":\"delete\",\"reason\":\"exact duplicate\",\"retained_id\":\"input-id\"}\n"
"]}\n"
    "Return ONLY valid JSON, no markdown fences."
)

AUDIT_INTERVAL = 5  # audit every N new memories added
_extractions_since_audit = 0


def _message_text(message) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p).strip()
    return ""


def _message_role(message) -> str:
    role = getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("role")
    return str(role or "").lower()


def _clean_memory_value(value: str, max_len: int = 80) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" .,!?:;\"'`“”‘’")
    value = re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.I)
    if not value or len(value) > max_len:
        return ""
    if re.search(r"https?://|@|[{}<>]", value):
        return ""
    return value


def _fallback_memory_candidates(messages) -> list[dict]:
    """Extract obvious durable facts without relying on the LLM.

    This is deliberately narrow. The LLM remains the main extractor, but
    simple identity/preference/goal statements should not silently vanish just
    because the background model judged them too conversational.
    """
    candidates = []
    seen = set()

    def add(text: str, category: str):
        text = _clean_memory_value(text, 120)
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append({"text": text, "category": category})

    for msg in messages:
        if _message_role(msg) != "user":
            continue
        text = _message_text(msg)
        if not text:
            continue

        m = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            name = _clean_memory_value(m.group(1), 50)
            if name:
                add(f"User's name is {name}.", "identity")

        m = re.search(r"\bcall me\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            name = _clean_memory_value(m.group(1), 50)
            if name:
                add(f"User wants to be called {name}.", "identity")

        m = re.search(r"\bi (?:live in|am from|'m from)\s+([^.!?\n]{2,80})", text, re.I)
        if m:
            place = _clean_memory_value(m.group(1), 80)
            if place:
                add(f"User lives in {place}.", "identity")

        m = re.search(r"\bi (prefer|like|love|hate|do not like|don't like)\s+([^.!?\n]{4,100})", text, re.I)
        if m:
            preference = _clean_memory_value(m.group(2), 100)
            if preference:
                # The same pattern catches likes and dislikes; keep the stored
                # sentiment faithful instead of recording every match as a
                # preference ("I hate cilantro" must not become "User prefers
                # cilantro").
                verb = m.group(1).lower()
                if verb in ("hate", "do not like", "don't like"):
                    add(f"User dislikes {preference}.", "preference")
                else:
                    add(f"User prefers {preference}.", "preference")

        m = re.search(
            r"\bi (?:(?:want|would like|plan|hope) to|wanna) "
            r"(?:go|travel|move|visit) to\s+([^.!?\n]{2,80})",
            text,
            re.I,
        )
        if m:
            destination = _clean_memory_value(m.group(1), 80)
            if destination:
                add(f"User wants to visit {destination}.", "goal")

    return candidates[:2]


def _is_text_duplicate(new_text: str, existing: list, threshold: float = 0.6) -> bool:
    """Check if new_text is too similar to any existing memory (Jaccard similarity)."""
    new_tokens = set(new_text.lower().split())
    if not new_tokens:
        return False
    for entry in _memory_dicts(existing):
        old_tokens = set(entry.get("text", "").lower().split())
        if not old_tokens:
            continue
        intersection = new_tokens & old_tokens
        union = new_tokens | old_tokens
        if len(intersection) / len(union) >= threshold:
            return True
    return False


def _parse_extraction_json(raw: str) -> list:
    """Parse the extraction LLM's reply into a list of facts, tolerating
    reasoning-model noise.

    The model emits <think>…</think> (and sometimes a prose preamble or a
    ```json fence) AROUND the JSON array; without stripping it, json.loads
    bombs and the run silently yields "0 candidates". Pure str -> list (no
    LLM/network); returns [] on any parse failure instead of raising.
    """
    text = (raw or "").strip()
    try:
        from src.text_helpers import strip_think as _strip_think
        text = _strip_think(text, prose=True, prompt_echo=True).strip()
    except Exception:
        pass
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    # JSON may still be embedded in surrounding commentary (leading prose or
    # trailing remarks like "[...] Done!") — slice from the first '[' to the
    # last ']' whenever both exist. Slice unconditionally: a reply that starts
    # with '[' can still carry trailing commentary that breaks json.loads.
    _start = text.find("[")
    _end = text.rfind("]")
    if 0 <= _start < _end:
        text = text[_start : _end + 1]

    try:
        facts = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("Memory extraction returned non-JSON: %r", (raw or "")[:120])
        return []
    except Exception:
        logger.debug("Memory extraction returned non-JSON: %r", (raw or "")[:120])
        return []
    return facts if isinstance(facts, list) else []


async def extract_and_store(
    session,
    memory_manager,
    memory_vector,
    endpoint_url: Optional[str] = None,
    model: Optional[str] = None,
    headers: Optional[dict] = None,
    owner: Optional[str] = None,
    root_operation_id: Optional[str] = None,
):
    """Extract facts from recent conversation and store them.

    Designed to run as a background task (asyncio.create_task).
    Errors are logged, never raised.

    ``endpoint_url``, ``model``, and ``headers`` remain compatibility-only;
    execution is resolved by the owner's managed ``utility`` route.
    """
    try:
        # The authenticated request owner is authoritative. Keep the session
        # fallback for legacy/internal callers that predate explicit scoping.
        _owner = owner if owner is not None else getattr(session, "owner", None)

        # Get last N messages from session
        messages = session.get_context_messages()
        recent = messages[-CONTEXT_WINDOW:] if len(messages) > CONTEXT_WINDOW else messages

        if len(recent) < 2:
            return  # Need at least a user message and assistant response

        # Strip media (images/audio) from messages — background memory extraction
        # only needs the text. The VL-generated descriptions are already in the
        # text content of the messages. This avoids sending image tokens to
        # non-vision models and prevents accidental "vision grounding" triggers.
        stripped_recent = []
        for msg in recent:
            role = msg.get("role")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Filter out multimodal blocks that aren't text
                text_only = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if not text_only and content:
                    continue
                content = text_only
            stripped_recent.append({"role": role, "content": content})

        if not stripped_recent:
            return

        fallback_facts = _fallback_memory_candidates(stripped_recent)

        # Flatten the window into a SINGLE user message instead of appending the
        # raw alternating role messages. Passed as raw chat messages, the model
        # treats the window as a conversation to CONTINUE rather than a transcript
        # to ANALYZE, so it reliably extracts nothing — typically returning `[]`
        # (and, depending on the input, sometimes an empty or <think>-only
        # completion when the window ends on an assistant turn). This was the real
        # cause of auto-memory logging "0 candidates" on every run. Reframing it as
        # one "analyze this transcript, return the JSON array" user message makes
        # the model actually extract. Controlled repro on this model: 0/6 trials
        # with the old structure vs 6/6 with this one. The skill extractor flattens
        # for the same reason.
        def _flatten_msg(m):
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    b.get("text", "") for b in c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            return f"{m.get('role', '?')}: {c}"

        transcript = "\n\n".join(_flatten_msg(m) for m in stripped_recent)
        extraction_messages = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Conversation to analyze:\n\n" + transcript
                + "\n\nReturn the JSON array of durable facts now (or [] if none)."
            )},
        ]

        facts = []
        try:
            raw = await _complete_text(
                owner=_owner or "",
                messages=extraction_messages,
                purpose="memory",
                root_operation_id=root_operation_id,
                temperature=0.1,
                # A reasoning model spends most of its budget on <think> tokens
                # BEFORE emitting the JSON, so the old 500 truncated the response
                # before any JSON appeared → every run logged "0 candidates". The
                # audit path hit the same wall and raised to 16384; extraction's
                # output (a short facts list) is small, so an ample ceiling is
                # enough once thinking has room.
                max_output_tokens=4096,
            )

            # Parse JSON, tolerating reasoning-model noise (<think> blocks, a
            # ```json fence, and leading/trailing commentary). See
            # _parse_extraction_json — returns [] rather than raising.
            facts = _parse_extraction_json(raw)
        except Exception as e:
            logger.warning(f"LLM memory extraction failed; using fallback candidates if available: {e}")

        if not isinstance(facts, list):
            facts = []

        if fallback_facts:
            facts = list(facts) + fallback_facts

        if not facts:
            logger.info("Auto memory extraction ran: 0 candidates")
            return

        existing = memory_manager.load_all()
        added = 0

        for fact in facts:
            if isinstance(fact, str):
                fact_text = fact
                category = "fact"
            elif isinstance(fact, dict):
                fact_text = fact.get("text", "").strip()
                category = fact.get("category", "fact")
            else:
                continue

            if not fact_text or len(fact_text) < 5:
                continue

            # Dedup: check vector similarity first (fast), then exact text match.
            # A runtime embedding/ChromaDB failure (backend OOM, model evicted,
            # remote endpoint down) must not abort the whole batch — fall through
            # to the text/fuzzy dedup below instead of losing every validated
            # fact extracted this session. (`.healthy` is only set at init, so
            # it does not catch failures that develop later.)
            if memory_vector and memory_vector.healthy:
                try:
                    existing_id = memory_vector.find_similar(fact_text, threshold=0.72)
                except Exception as e:
                    logger.warning(f"Memory dedup (vector) unavailable, using text fallback: {e}")
                    existing_id = None
                if existing_id:
                    # The vector store is a single shared collection with no
                    # owner metadata, so find_similar can return ANOTHER
                    # tenant's memory. Only treat it as a duplicate when the
                    # match is this user's own (or a legacy unowned) memory —
                    # otherwise the user's freshly-extracted fact would be
                    # silently dropped. Mirror the owner predicate used by the
                    # text dedup below; cross-tenant/stale matches fall through.
                    _match = next((e for e in existing if e.get("id") == existing_id), None)
                    if _match is not None and (_match.get("owner") == _owner or _match.get("owner") is None):
                        logger.debug(f"Memory dedup (vector): '{fact_text[:50]}' matches {existing_id}")
                        continue

            # Text dedup fallback: exact match + fuzzy similarity
            user_existing = [e for e in existing if e.get("owner") == _owner or e.get("owner") is None] if _owner else existing
            if memory_manager.find_duplicates(fact_text, user_existing):
                continue
            # Fuzzy text similarity check (catches rephrased duplicates when vector index is unavailable)
            if _is_text_duplicate(fact_text, user_existing):
                logger.debug(f"Memory dedup (fuzzy): '{fact_text[:50]}' too similar to existing")
                continue

            entry = memory_manager.add_entry(fact_text, source="auto", category=category, owner=_owner)
            # Auto-pin identity facts (name, job, location) — core context
            if category == "identity":
                entry["pinned"] = True
            if hasattr(session, "session_id"):
                entry["session_id"] = session.session_id
            elif hasattr(session, "name"):
                entry["session_id"] = session.name

            existing.append(entry)

            # Add to vector index. The JSON store (saved below) is the source of
            # truth and the keyword path can still retrieve this entry, so a vector
            # write failure must not drop the fact or abort the remaining batch.
            if memory_vector and memory_vector.healthy:
                try:
                    memory_vector.add(entry["id"], fact_text)
                except Exception as e:
                    logger.warning(f"Memory vector add failed for {entry['id']}: {e}")

            added += 1

        if added > 0:
            memory_manager.save(existing)
            try:
                from src.event_bus import fire_event
                for _ in range(added):
                    fire_event("memory_added", _owner)
            except Exception:
                logger.debug("memory_added event dispatch failed", exc_info=True)
            logger.info(f"Auto-extracted {added} memories from session")

            global _extractions_since_audit
            _extractions_since_audit += added
            if _extractions_since_audit >= AUDIT_INTERVAL:
                _extractions_since_audit = 0
                logger.info("Audit threshold reached, running memory audit")
                await audit_memories(
                    memory_manager,
                    memory_vector,
                    endpoint_url,
                    model,
                    headers,
                    owner=_owner,
                    root_operation_id=root_operation_id,
                )
        else:
            logger.info("Auto memory extraction ran: 0 added")

    except Exception as e:
        logger.error(f"Memory extraction failed: {e}")


async def audit_memories(
    memory_manager,
    memory_vector,
    endpoint_url: Optional[str] = None,
    model: Optional[str] = None,
    headers: Optional[dict] = None,
    owner: Optional[str] = None,
    root_operation_id: Optional[str] = None,
):
    """Send all memories to the LLM for deduplication and consolidation.

    - Merges near-duplicate entries
    - Rewrites vague entries to be concise
    - Removes junk / non-personal entries
    - Rebuilds the vector index afterwards

    Safe to call manually or from the automatic trigger in extract_and_store.
    Errors are logged, never raised.

    ``endpoint_url``, ``model``, and ``headers`` remain compatibility-only;
    execution is resolved by the owner's managed ``utility`` route.
    """
    before_count = 0
    try:
        existing = memory_manager.load(owner=owner)
        if not existing:
            logger.info("Memory audit: nothing to audit")
            return _audit_result(
                ok=True,
                status="unchanged",
                before=0,
                after=0,
                already_tidy=True,
            )

        before_count = len(existing)

        # Skip the LLM call entirely when this exact set of memories was
        # already audited — the previous tidy left them in a clean state
        # and nothing has changed since. Returns instantly so the UI shows
        # "Already clean" without spending 30-120s on a wasted LLM round.
        # The fingerprint includes id+text+category; any add/edit/delete
        # invalidates it and the audit runs normally.
        current_fp = _fingerprint_entries(existing)
        last_state = _load_tidy_state(memory_manager).get(owner or "") or {}
        if last_state.get("fingerprint") == current_fp:
            logger.info("Memory audit: state unchanged since last tidy — skipping LLM")
            return _audit_result(
                ok=True,
                status="unchanged",
                before=before_count,
                after=before_count,
                already_tidy=True,
            )

        originals = {}
        for entry in existing:
            if not isinstance(entry, dict):
                return _audit_failure(
                    before_count,
                    "invalid_memory_store",
                    "Memory Tidy found an invalid stored memory and did not change anything.",
                )
            memory_id = entry.get("id")
            text = entry.get("text")
            category = entry.get("category", "fact")
            if (
                not isinstance(memory_id, str)
                or not memory_id.strip()
                or memory_id in originals
                or not isinstance(text, str)
                or not isinstance(category, str)
            ):
                return _audit_failure(
                    before_count,
                    "invalid_memory_store",
                    "Memory Tidy found an invalid stored memory and did not change anything.",
                )
            originals[memory_id] = {
                "id": memory_id,
                "text": text,
                "category": category,
                "entry": entry,
            }

        # Build payload: list of {id, text, category} for the LLM
        memory_payload = [
            {"id": m["id"], "text": m["text"], "category": m["category"]}
            for m in originals.values()
        ]

        audit_messages = [
            {"role": "system", "content": AUDIT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(memory_payload, ensure_ascii=False)},
        ]

        try:
            raw = await asyncio.wait_for(
                _complete_text(
                    owner=owner or "",
                    messages=audit_messages,
                    purpose="memory",
                    root_operation_id=root_operation_id,
                    temperature=0.1,
                    # 16384 (was 2000): the deduped list of all memories can be
                    # large, and reasoning can otherwise consume the output budget
                    # before emitting the JSON.
                    max_output_tokens=16384,
                ),
                # Keep the legacy bounded-wait behavior at the application seam.
                timeout=120,
            )
        except asyncio.TimeoutError:
            logger.warning("Memory audit model call timed out")
            return _audit_failure(
                before_count,
                "model_timeout",
                "The memory model timed out. No memories were changed.",
            )
        except Exception:
            logger.exception("Memory audit model call failed")
            return _audit_failure(
                before_count,
                "model_request_failed",
                "The memory model request failed. No memories were changed.",
            )

        if not isinstance(raw, str) or not raw.strip():
            logger.warning("Memory audit model returned empty output")
            return _audit_failure(
                before_count,
                "empty_model_output",
                "The memory model returned no usable Tidy result. No memories were changed.",
            )

        parsed = _parse_audit_response(raw)
        if parsed is None:
            logger.warning("Memory audit returned non-JSON output (%s bytes)", len(raw))
            return _audit_failure(
                before_count,
                "invalid_model_output",
                "The memory model returned an unreadable Tidy result. No memories were changed.",
            )

        operations, validation_error = _validate_audit_operations(parsed, originals)
        if validation_error is not None:
            code, message = validation_error
            logger.warning("Memory audit proposal rejected: %s", code)
            return _audit_failure(before_count, code, message)

        proposal = _build_audit_proposal(
            operations,
            originals,
            source_fingerprint=current_fp,
        )
        after_count = before_count - len(proposal["deletes"])
        if _unsafe_audit_removal(before_count, after_count):
            logger.warning(
                "Memory audit would cut %s -> %s; refusing as unsafe",
                before_count,
                after_count,
            )
            return _audit_failure(
                before_count,
                "unsafe_removal",
                "Memory Tidy proposed removing too many memories. No memories were changed.",
            )

        if not proposal["updates"] and not proposal["deletes"]:
            _save_tidy_state(memory_manager, owner, current_fp)
            return _audit_result(
                ok=True,
                status="unchanged",
                before=before_count,
                after=before_count,
                already_tidy=True,
            )

        final_entries = []
        for operation in operations:
            if operation["action"] == "delete":
                continue
            entry = originals[operation["id"]]["entry"].copy()
            entry["text"] = operation["text"]
            entry["category"] = operation["category"]
            final_entries.append(entry)

        # Merge audited entries back with other users' entries
        if owner:
            all_entries = memory_manager.load_all()
            audited_ids = {e["id"] for e in final_entries}
            other_entries = [e for e in all_entries if e.get("owner") != owner and (e.get("owner") is not None)]
            # Also keep legacy entries that weren't part of this audit
            for e in all_entries:
                if e.get("owner") is None and e["id"] not in audited_ids and e["id"] not in {o["id"] for o in other_entries}:
                    other_entries.append(e)
            saved_entries = final_entries + other_entries
        else:
            saved_entries = final_entries
        memory_manager.save(saved_entries)
        logger.info(
            f"Memory audit complete: {before_count} -> {after_count} entries "
            f"({before_count - after_count} removed/merged)"
        )

        # Rebuild vector index from the full saved set, not just this owner's
        # slice — otherwise the shared collection is wiped of every other
        # owner's entries until they happen to run their own audit.
        if memory_vector and memory_vector.healthy:
            memory_vector.rebuild(saved_entries)

        # Persist the post-tidy fingerprint so the next call short-circuits
        # if nothing has changed in the meantime.
        _save_tidy_state(memory_manager, owner, _fingerprint_entries(final_entries))

        return _audit_result(
            ok=True,
            status="applied",
            before=before_count,
            after=after_count,
            updated=len(proposal["updates"]),
            applied=True,
            proposal=proposal,
        )

    except Exception:
        logger.exception("Memory audit failed")
        return _audit_failure(
            before_count,
            "native_mutation_failed",
            "Memory Tidy failed before it could confirm the result.",
        )


def _parse_audit_response(raw: str):
    """Parse the one JSON container returned by the Tidy model.

    Reasoning models sometimes leave harmless prose or a fenced JSON payload
    around the actual response.  We tolerate that presentation noise, but do
    not turn an invalid response into an empty list: an empty or malformed
    result must remain a typed failure at the caller.
    """
    if not isinstance(raw, str):
        return None

    text = raw.strip()
    text = re.sub(
        r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>",
        "",
        text,
        flags=re.I,
    ).strip()

    def loads_container(value):
        if not value:
            return None
        for candidate in (value, re.sub(r",(\s*[}\]])", r"\1", value)):
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, (list, dict)):
                return parsed
        return None

    parsed = loads_container(text)
    if parsed is not None:
        return parsed

    fenced = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text, flags=re.I)
    if fenced:
        parsed = loads_container(fenced.group(1).strip())
    if parsed is not None:
        return parsed

    starts = [
        (index, closing)
        for index, closing in ((text.find("{"), "}"), (text.find("["), "]"))
        if index >= 0
    ]
    if starts:
        start, closing = min(starts, key=lambda item: item[0])
        end = text.rfind(closing)
        if end > start:
            return loads_container(text[start : end + 1])
    return None


def _parse_audit_json(raw: str):
    """Compatibility parser for the legacy JSON-list audit path."""
    parsed = _parse_audit_response(raw)
    return parsed if isinstance(parsed, list) else None


def _audit_result(
    *,
    ok: bool,
    status: str,
    before: int,
    after: Optional[int] = None,
    updated: int = 0,
    applied: bool = False,
    already_tidy: bool = False,
    proposal: Optional[dict] = None,
    rollback: Optional[dict] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> dict:
    """Build the stable Tidy outcome envelope used by provider callers.

    Counts alone cannot distinguish a clean store from a failed model call;
    every return value therefore carries an explicit success flag and state.
    ``error`` is deliberately structured and does not include raw provider
    output, which can be an unreadable HTML gateway page or sensitive text.
    """
    before = max(0, int(before or 0))
    after = before if after is None else max(0, int(after or 0))
    result = {
        "ok": bool(ok),
        "status": status,
        "before": before,
        "after": after,
        "removed": max(0, before - after),
        "updated": max(0, int(updated or 0)),
        "applied": bool(applied),
        "already_tidy": bool(already_tidy),
    }
    if proposal is not None:
        result["proposal"] = proposal
    if rollback is not None:
        result["rollback"] = rollback
    if error_code:
        result["error"] = {
            "code": error_code,
            "message": error_message or "Memory Tidy failed without changing memories.",
        }
    return result


def _audit_failure(
    before: int,
    code: str,
    message: str,
    *,
    after: Optional[int] = None,
    updated: int = 0,
    applied: bool = False,
    status: str = "failed",
    proposal: Optional[dict] = None,
    rollback: Optional[dict] = None,
) -> dict:
    return _audit_result(
        ok=False,
        status=status,
        before=before,
        after=after,
        updated=updated,
        applied=applied,
        proposal=proposal,
        rollback=rollback,
        error_code=code,
        error_message=message,
    )


def _validate_audit_operations(parsed, originals: dict[str, dict]):
    """Return normalized, fully-accounted audit operations or a typed error.

    A former list response used omission as a deletion signal.  That means a
    truncated response can silently erase valid memories.  New object-shaped
    responses require an explicit action for every source id.  We accept a
    legacy list only when it accounts for all IDs, which allows safe edit-only
    compatibility but never lets a missing list item become a deletion.
    """
    legacy_list = isinstance(parsed, list)
    if legacy_list:
        operations = parsed
    elif isinstance(parsed, dict):
        operations = parsed.get("operations")
        if not isinstance(operations, list):
            return None, (
                "invalid_model_output",
                "The memory model returned an invalid Tidy proposal. No memories were changed.",
            )
    else:
        return None, (
            "invalid_model_output",
            "The memory model returned an invalid Tidy proposal. No memories were changed.",
        )

    if not operations and originals:
        return None, (
            "incomplete_model_output",
            "The memory model did not account for every memory. No memories were changed.",
        )

    normalized = []
    seen_ids = set()
    for item in operations:
        if not isinstance(item, dict):
            return None, (
                "invalid_model_output",
                "The memory model returned a malformed Tidy proposal. No memories were changed.",
            )
        record_id = item.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            return None, (
                "invalid_model_output",
                "The memory model returned a memory without a valid id. No memories were changed.",
            )
        if record_id not in originals:
            return None, (
                "unknown_memory_id",
                "The memory model referenced an unknown memory. No memories were changed.",
            )
        if record_id in seen_ids:
            return None, (
                "duplicate_memory_id",
                "The memory model proposed more than one action for a memory. No memories were changed.",
            )
        seen_ids.add(record_id)

        action = "keep" if legacy_list else item.get("action")
        if not isinstance(action, str) or action not in {"keep", "delete"}:
            return None, (
                "invalid_model_output",
                "The memory model returned an unsupported Tidy action. No memories were changed.",
            )

        if action == "keep":
            text = item.get("text")
            category = item.get("category")
            if not isinstance(text, str) or not text.strip():
                return None, (
                    "invalid_model_output",
                    "The memory model returned a memory without usable text. No memories were changed.",
                )
            if not isinstance(category, str) or not category.strip():
                return None, (
                    "invalid_model_output",
                    "The memory model returned a memory without a category. No memories were changed.",
                )
            normalized.append({
                "id": record_id,
                "action": "keep",
                "text": text.strip(),
                "category": category.strip(),
            })
            continue

        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return None, (
                "invalid_model_output",
                "The memory model proposed a deletion without a reason. No memories were changed.",
            )
        retained_id = item.get("retained_id")
        if retained_id is not None and (
            not isinstance(retained_id, str) or retained_id not in originals
        ):
            return None, (
                "invalid_model_output",
                "The memory model proposed a deletion with an invalid retained memory. No memories were changed.",
            )
        normalized.append({
            "id": record_id,
            "action": "delete",
            "reason": reason.strip(),
            **({"retained_id": retained_id} if retained_id else {}),
        })

    if seen_ids != set(originals):
        return None, (
            "incomplete_model_output",
            "The memory model did not account for every memory. No memories were changed.",
        )
    actions_by_id = {operation["id"]: operation["action"] for operation in normalized}
    for operation in normalized:
        retained_id = operation.get("retained_id")
        if retained_id and actions_by_id.get(retained_id) != "keep":
            return None, (
                "invalid_model_output",
                "The memory model proposed a deletion without a retained memory. No memories were changed.",
            )
    return normalized, None


def _build_audit_proposal(
    operations: list[dict],
    originals: dict[str, dict],
    *,
    source_fingerprint: str,
) -> dict:
    updates = []
    deletes = []
    for operation in operations:
        original = originals[operation["id"]]
        if operation["action"] == "delete":
            deletes.append({
                "id": operation["id"],
                "reason": operation["reason"],
                **(
                    {"retained_id": operation["retained_id"]}
                    if operation.get("retained_id")
                    else {}
                ),
            })
        elif (
            operation["text"] != original["text"]
            or operation["category"] != original["category"]
        ):
            updates.append({
                "id": operation["id"],
                "text": operation["text"],
                "category": operation["category"],
            })
    return {
        "source_fingerprint": source_fingerprint,
        "updates": updates,
        "deletes": deletes,
    }


def _provider_fingerprint(records) -> str:
    items = sorted(
        (
            str(getattr(record, "id", "")),
            str(getattr(record, "text", "")),
            str(getattr(record, "category", "")),
        )
        for record in records or []
    )
    digest = hashlib.sha256()
    for item in items:
        digest.update(("\x1f".join(item) + "\x1e").encode("utf-8"))
    return digest.hexdigest()


def _unsafe_audit_removal(before_count: int, after_count: int) -> bool:
    """Reject a proposal that would erase a corpus or most of a large one."""
    if before_count and after_count == 0:
        return True
    return before_count >= 8 and after_count < before_count * 0.5


async def _rollback_provider_audit(
    memory_provider,
    memory_lifecycle,
    *,
    owner: Optional[str],
    attempted_updates,
    completed_deletes,
) -> dict:
    """Best-effort compensation after a provider audit apply failure.

    Deletes travel through the lifecycle coordinator, which gives successful
    deletes a recoverable tombstone.  Updates are compensated with their
    original text/category.  The caller reports ``partial`` if either repair
    step cannot be confirmed; it never converts an uncertain mutation into a
    clean/no-op outcome.
    """
    rollback = {
        "updates_restored": 0,
        "deletes_restored": 0,
        "errors": [],
    }
    for record, deletion in reversed(completed_deletes):
        tombstone_id = (
            deletion.get("tombstone_id")
            if isinstance(deletion, dict)
            else None
        )
        if not isinstance(tombstone_id, str) or not tombstone_id:
            rollback["errors"].append("delete_restore_unavailable")
            continue
        try:
            await memory_lifecycle.forget(
                "restore",
                owner=owner or "",
                tombstone_id=tombstone_id,
            )
            rollback["deletes_restored"] += 1
        except Exception:
            logger.exception("Provider memory audit could not restore deleted memory")
            rollback["errors"].append("delete_restore_failed")

    for record, original_text, original_category in reversed(attempted_updates):
        try:
            restored = await memory_provider.update(
                record.id,
                text=original_text,
                category=original_category,
                owner=owner,
            )
            if restored is None or restored is False:
                raise RuntimeError("provider refused update rollback")
            rollback["updates_restored"] += 1
        except Exception:
            logger.exception("Provider memory audit could not restore edited memory")
            rollback["errors"].append("update_restore_failed")
    return rollback


async def audit_provider_memories(
    memory_provider,
    endpoint_url: Optional[str] = None,
    model: Optional[str] = None,
    headers: Optional[dict] = None,
    owner: Optional[str] = None,
    memory_lifecycle=None,
    root_operation_id: Optional[str] = None,
    apply: bool = True,
):
    """Audit records through the active provider's mutation interface.

    The native audit above owns JSON/vector persistence. External providers
    must be changed through their contract so the UI never reports a tidy that
    only modified the unused ``memory.json`` store.

    ``endpoint_url``, ``model``, and ``headers`` remain compatibility-only;
    execution is resolved by the owner's managed ``utility`` route.
    """
    before_count = 0
    try:
        existing = await memory_provider.list_memories(owner=owner, limit=1000)
    except asyncio.TimeoutError:
        logger.warning("Provider memory audit timed out while loading memories")
        return _audit_failure(
            0,
            "provider_timeout",
            "Memory Tidy timed out while loading memories. No memories were changed.",
        )
    except Exception:
        logger.exception("Provider memory audit could not load memories")
        return _audit_failure(
            0,
            "provider_read_failed",
            "Memory Tidy could not read memories. No memories were changed.",
        )

    if not isinstance(existing, list):
        return _audit_failure(
            0,
            "provider_read_failed",
            "Memory Tidy received an invalid memory list. No memories were changed.",
        )
    before_count = len(existing)
    if before_count >= 1000:
        return _audit_failure(
            before_count,
            "audit_scope_limit_reached",
            "Memory Tidy reached its safe memory limit and did not change anything.",
        )
    if not existing:
        return _audit_result(
            ok=True,
            status="unchanged",
            before=0,
            after=0,
            already_tidy=True,
        )

    originals = {}
    for record in existing:
        record_id = getattr(record, "id", None)
        text = getattr(record, "text", None)
        category = getattr(record, "category", None)
        if (
            not isinstance(record_id, str)
            or not record_id.strip()
            or record_id in originals
            or not isinstance(text, str)
            or not isinstance(category, str)
        ):
            return _audit_failure(
                before_count,
                "invalid_memory_store",
                "Memory Tidy found an invalid stored memory and did not change anything.",
            )
        originals[record_id] = {
            "id": record_id,
            "text": text,
            "category": category,
            "record": record,
        }

    source_fingerprint = _provider_fingerprint(existing)
    payload = [
        {"id": record.id, "text": record.text, "category": record.category}
        for record in existing
    ]
    try:
        raw = await asyncio.wait_for(
            _complete_text(
                owner=owner or "",
                messages=[
                    {"role": "system", "content": AUDIT_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                purpose="memory",
                root_operation_id=root_operation_id,
                temperature=0.1,
                max_output_tokens=16384,
            ),
            timeout=120,
        )
    except asyncio.TimeoutError:
        logger.warning("Provider memory audit model call timed out")
        return _audit_failure(
            before_count,
            "model_timeout",
            "The memory model timed out. No memories were changed.",
        )
    except Exception:
        logger.exception("Provider memory audit model call failed")
        return _audit_failure(
            before_count,
            "model_request_failed",
            "The memory model request failed. No memories were changed.",
        )

    if not isinstance(raw, str) or not raw.strip():
        logger.warning("Provider memory audit model returned empty output")
        return _audit_failure(
            before_count,
            "empty_model_output",
            "The memory model returned no usable Tidy result. No memories were changed.",
        )

    parsed = _parse_audit_response(raw)
    if parsed is None:
        logger.warning("Provider memory audit returned non-JSON output (%s bytes)", len(raw))
        return _audit_failure(
            before_count,
            "invalid_model_output",
            "The memory model returned an unreadable Tidy result. No memories were changed.",
        )

    operations, validation_error = _validate_audit_operations(parsed, originals)
    if validation_error is not None:
        code, message = validation_error
        logger.warning("Provider memory audit proposal rejected: %s", code)
        return _audit_failure(before_count, code, message)

    proposal = _build_audit_proposal(
        operations,
        originals,
        source_fingerprint=source_fingerprint,
    )
    after_count = before_count - len(proposal["deletes"])
    if _unsafe_audit_removal(before_count, after_count):
        logger.warning(
            "Provider memory audit would cut %s -> %s; refusing as unsafe",
            before_count,
            after_count,
        )
        return _audit_failure(
            before_count,
            "unsafe_removal",
            "Memory Tidy proposed removing too many memories. No memories were changed.",
        )

    if not proposal["updates"] and not proposal["deletes"]:
        return _audit_result(
            ok=True,
            status="unchanged",
            before=before_count,
            after=before_count,
            already_tidy=True,
        )

    if not apply:
        return _audit_result(
            ok=True,
            status="preview",
            before=before_count,
            after=after_count,
            updated=len(proposal["updates"]),
            applied=False,
            proposal=proposal,
        )

    if proposal["deletes"] and (
        memory_lifecycle is None
        or not callable(getattr(memory_lifecycle, "delete", None))
        or not callable(getattr(memory_lifecycle, "forget", None))
    ):
        return _audit_failure(
            before_count,
            "coordinated_deletion_unavailable",
            "Memory Tidy cannot safely apply deletions right now. No memories were changed.",
            proposal=proposal,
        )

    # The model can take a long time. Re-read immediately before mutation so a
    # changed store rejects this stale proposal rather than applying it to a
    # different generation. Provider-level atomic CAS remains a future step,
    # but this closes the normal concurrent-edit window without mutation.
    try:
        current = await memory_provider.list_memories(owner=owner, limit=1000)
    except Exception:
        logger.exception("Provider memory audit could not verify its snapshot")
        return _audit_failure(
            before_count,
            "provider_preflight_failed",
            "Memory Tidy could not verify the current memories. No memories were changed.",
            proposal=proposal,
        )
    if not isinstance(current, list) or _provider_fingerprint(current) != source_fingerprint:
        return _audit_failure(
            before_count,
            "stale_audit_snapshot",
            "Memories changed while Tidy was running. No memories were changed.",
            proposal=proposal,
        )

    attempted_updates = []
    completed_deletes = []
    try:
        for update in proposal["updates"]:
            record = originals[update["id"]]["record"]
            attempted_updates.append((
                record,
                originals[update["id"]]["text"],
                originals[update["id"]]["category"],
            ))
            updated = await memory_provider.update(
                record.id,
                text=update["text"],
                category=update["category"],
                owner=owner,
            )
            if updated is None or updated is False:
                raise RuntimeError("provider refused a Tidy update")

        for deletion in proposal["deletes"]:
            record = originals[deletion["id"]]["record"]
            deleted = await memory_lifecycle.delete(
                record.id,
                owner=owner or "",
            )
            if not deleted:
                raise RuntimeError("provider refused a Tidy deletion")
            completed_deletes.append((record, deleted))
    except Exception:
        logger.exception("Provider memory audit apply failed; attempting compensation")
        rollback = await _rollback_provider_audit(
            memory_provider,
            memory_lifecycle,
            owner=owner,
            attempted_updates=attempted_updates,
            completed_deletes=completed_deletes,
        )
        observed_after = before_count
        recovered = False
        try:
            recovered_records = await memory_provider.list_memories(
                owner=owner,
                limit=1000,
            )
            if isinstance(recovered_records, list):
                observed_after = len(recovered_records)
                recovered = _provider_fingerprint(recovered_records) == source_fingerprint
        except Exception:
            logger.exception("Provider memory audit could not verify compensation")
        rollback["verified"] = recovered
        status = "failed" if recovered else "partial"
        return _audit_failure(
            before_count,
            "provider_mutation_failed",
            (
                "Memory Tidy could not apply its proposal; no lasting changes were found."
                if status == "failed"
                else "Memory Tidy partially applied a proposal and could not fully restore it."
            ),
            after=before_count if status == "failed" else observed_after,
            updated=0 if status == "failed" else len(attempted_updates),
            applied=status == "partial",
            status=status,
            proposal=proposal,
            rollback=rollback,
        )

    logger.info("Provider memory audit complete: %s -> %s entries", before_count, after_count)
    return _audit_result(
        ok=True,
        status="applied",
        before=before_count,
        after=after_count,
        updated=len(proposal["updates"]),
        applied=True,
        proposal=proposal,
    )
