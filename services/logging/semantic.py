"""Reference-only archive adapter for the application-owned vector index.

ConversationArchive is the sole transcript authority. The RAG database keeps
empty-text chunks, exact part locators, hashes and derived vectors. No cached
transcript text or alternate conversation history is maintained here.
"""
from __future__ import annotations

import json
import threading

from services.logging.projection import (
    COLUMNS, MAX_BODY_CHARS, MAX_FACTS, MAX_RESPONSE_BYTES, SCHEMA,
    LoggingError, _check, _digest, _json, normalize_filters, owner_scope,
)
from src.openclank.conversation_archive import _replace_data_urls

ARCHIVE_INDEX_SCOPE = "openclank:canonical-conversation-archive:v1"
MAX_INDEX_BYTES = 32 * 1024 * 1024


def _identity(row):
    return tuple(row[key] for key in ("chat_id", "actor_id", "message_id", "part_id", "revision", "content_hash"))


def _text(content):
    # Strip inline media before it can leave the canonical source. Structured
    # parts retain their safe textual structure; no image bytes are embedded.
    value = content or ""
    try:
        parsed = json.loads(value)
        if isinstance(parsed, str):
            value = parsed
        elif isinstance(parsed, dict):
            part = parsed.get("mimo", {}).get("part", parsed) if isinstance(parsed.get("mimo"), dict) else parsed
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                value = part["text"]
    except (ValueError, TypeError):
        pass
    return _replace_data_urls(value)[0]


class ArchiveReferenceSource:
    def __init__(self, projection):
        self.projection = projection

    def _rows(self, owner, *, bodies=False):
        with self.projection._read() as conn:
            where, args = self.projection._scope(conn, owner, {})
            if bodies:
                total = conn.execute(f"SELECT COALESCE(SUM(length(CAST(p.content AS BLOB))),0) FROM conversation_parts p WHERE {where}", args).fetchone()[0]
                if total > MAX_INDEX_BYTES:
                    raise LoggingError("query_too_large", "The canonical conversation index exceeds its bounded text window")
            rows = conn.execute(
                f"SELECT {COLUMNS}{',p.content AS body' if bodies else ''} FROM conversation_parts p WHERE {where} ORDER BY p.seq LIMIT ?",
                [*args, MAX_FACTS + 1],
            ).fetchall()
            if len(rows) > MAX_FACTS:
                raise LoggingError("query_too_large", "The canonical conversation index exceeds its bounded source window")
            if bodies and sum(len((row["body"] or "").encode()) for row in rows) > MAX_INDEX_BYTES:
                raise LoggingError("query_too_large", "The canonical conversation index exceeds its bounded text window")
            return rows

    def watermark(self, owner):
        return _digest([_identity(row) for row in self._rows(owner)])

    def references(self, owner):
        refs = []
        for row in self._rows(owner, bodies=True):
            text = _text(row["body"])
            if not text.strip():
                continue
            identity = _identity(row)
            refs.append({
                "source_uri": "conversation-archive://part/" + _digest(identity[:4]),
                "revision": row["revision"], "content_hash": row["content_hash"], "text": text,
                "locator": {"type": "conversation_archive_part", **dict(zip(
                    ("chat_id", "actor_id", "message_id", "part_id", "revision", "content_hash"), identity))},
            })
        return refs

    def hydrate(self, owner, rows):
        # Always read the current owner source and deletion intent. Persisted
        # locators never authorize historical revisions or resurrected content.
        current = {_identity(row): row for row in self._rows(owner, bodies=True)}
        hydrated = []
        for row in rows:
            try:
                locator = json.loads(row["locator_json"])
                identity = tuple(locator[key] for key in ("chat_id", "actor_id", "message_id", "part_id", "revision", "content_hash"))
                source = current.get(identity)
                if source is None or locator.get("type") != "conversation_archive_part":
                    continue
                text = _text(source["body"])[int(locator["start"]):int(locator["end"])]
                from src.frankenmemory_rag import _hash
                if _hash(text) != row["chunk_hash"]:
                    continue
                hydrated.append({**dict(row), "text": text})
            except (ValueError, TypeError, KeyError):
                continue
        return hydrated


class ConversationSemanticSearch:
    def __init__(self, projection, index):
        self.projection, self.index = projection, index
        self.source = ArchiveReferenceSource(projection)
        self._build_lock = threading.Lock()
        if index is not None:
            index.register_external_source(ARCHIVE_INDEX_SCOPE, self.source)

    def status(self, owner, *, cancel_event=None, deadline=None):
        owner = self.projection._owner(owner)
        _check(cancel_event, deadline)
        if self.index is None:
            return {"health": "unavailable", "reason": "semantic_index_unavailable"}
        result = self.index.external_vector_status(owner=owner, workspace_id=ARCHIVE_INDEX_SCOPE)
        _check(cancel_event, deadline)
        return result

    @staticmethod
    def setup():
        return {
            "label": "Select embedding model", "settings_tab": "services",
            "message": "Choose your embeddings model in Services, then build and publish the conversation index. Building sends retained conversation text to your selected embedding route and may incur its configured cost. Text remains in the canonical archive; only locators, hashes and vectors are indexed.",
            "build_label": "Build and publish conversation index", "build_path": "/semantic/build",
        }

    def build(self, *, owner):
        owner = self.projection._owner(owner)
        if self.index is None:
            raise LoggingError("semantic_index_unavailable")
        if not self._build_lock.acquire(blocking=False):
            raise LoggingError("semantic_build_busy", "A conversation generation is already building; refresh to review its state")
        try:
            # Existing authority resolves only this owner's explicitly selected
            # managed binding. It never guesses a provider/model or ambient key.
            client = self.index._client_for_owner(self.index._embedding_client, owner)
            if client is None:
                raise LoggingError("embedding_binding_required", "Select an embeddings model in Services before building")
            try:
                self.index._client_dimension(client)
            except Exception as exc:
                raise LoggingError("embedding_binding_unavailable", "The selected embeddings binding is unavailable; review Services") from exc
            refs = self.index.index_external_references(owner=owner, workspace_id=ARCHIVE_INDEX_SCOPE)
            result = self.index.build_embedding_generation(owner=owner, workspace_id=ARCHIVE_INDEX_SCOPE, embedding_client=client, publish=True)
            return {"schema": SCHEMA, "owner_scope": owner_scope(owner), **result, "source": refs}
        finally:
            self._build_lock.release()

    def search(self, *, owner, payload, cancel_event=None, deadline=None):
        owner = self.projection._owner(owner)
        filters = normalize_filters(payload.get("filters"))
        query = payload.get("query", "")
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise LoggingError("invalid_query")
        limit = self.projection._limit(payload.get("limit", 50))
        _check(cancel_event, deadline)
        result = self.index.search_external_vectors(query, owner=owner, workspace_id=ARCHIVE_INDEX_SCOPE) if self.index else {"health": "unavailable", "reason": "semantic_index_unavailable", "items": [], "query_execution": "not_dispatched"}
        _check(cancel_event, deadline)
        ready = result.get("query_execution") == "vector_cosine"
        readiness = {key: value for key, value in result.items() if key != "items"}
        readiness["conversation_source"] = {"state": "bound", "authority": "conversation_archive", "storage": "reference_only"}
        coverage = {"state": "complete" if ready and result["health"] == "current" else "partial" if ready else "unavailable",
                    "reason": result.get("reason"), "authority": "frankenmemory_canonical_index", "readiness": readiness}
        if not ready or result.get("health") == "lagging":
            coverage["setup"] = self.setup()
        # Deduplicate chunk hits to canonical parts. Rehydrate/filter in a fresh
        # source read after embedding, preventing a deletion during that call
        # from returning retained transcript text.
        scores = {}
        for hit in result.get("items", []):
            loc = hit["locator"]
            identity = tuple(loc[key] for key in ("chat_id", "actor_id", "message_id", "part_id", "revision", "content_hash"))
            scores[identity] = max(scores.get(identity, 0), hit["score"])
        with self.projection._read(cancel_event, deadline) as conn:
            where, args = self.projection._scope(conn, owner, filters)
            rows = conn.execute(f"SELECT {COLUMNS},substr(p.content,1,{MAX_BODY_CHARS}) AS body FROM conversation_parts p WHERE {where} ORDER BY p.seq LIMIT ?", [*args, MAX_FACTS + 1]).fetchall()
            if len(rows) > MAX_FACTS:
                raise LoggingError("query_too_large", "Narrow the semantic source filters")
            matching = [row for row in rows if _identity(row) in scores]
            matching.sort(key=lambda row: (-scores[_identity(row)], row["seq"]))
            revision = _digest({"archive": self.projection._revision(conn, owner), "corpus": self.source.watermark(owner), "generation": result.get("generation_id")})
            cursor_query = {"kind": "semantic", "filters": filters, "query": query}
            offset = self.projection._position(payload.get("cursor"), owner, cursor_query, revision)
            items, size = [], 0
            for row in matching[offset:offset + limit]:
                _check(cancel_event, deadline)
                item = self.projection._part(owner, row, True)
                item["semantic_score"] = round(scores[_identity(row)], 6)
                size += len(_json(item).encode())
                if size > MAX_RESPONSE_BYTES:
                    break
                items.append(item)
            next_cursor = self.projection._cursor(owner, cursor_query, revision, offset + len(items)) if offset + len(items) < len(matching) else None
        # Coverage reflects changes committed during query embedding/hydration,
        # including a deletion fence that kept a matched part out of this page.
        if ready:
            final = self.status(owner, cancel_event=cancel_event, deadline=deadline)
            readiness.update(final)
            coverage["state"] = "complete" if final["health"] == "current" else "partial"
            coverage["reason"] = final.get("reason")
            if final["health"] != "current":
                coverage["setup"] = self.setup()
        return {"schema": SCHEMA, "owner_scope": owner_scope(owner), "items": items, "filters": filters,
                "page": {"next_cursor": next_cursor, "limit": limit}, "coverage": coverage}
