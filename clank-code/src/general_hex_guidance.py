"""Owner-reviewed changes to General Hexes, backed by scoped Memery evidence."""
import hashlib
import json

from src.memory_scope import chat_workspace
from src.general_hex_library import (
    GeneralHexConflict, GeneralHexError, GeneralHexNotFound, GeneralHexRepository,
    _identity, _json, _revision, normalize_tags,
)


def _ids(values):
    if not isinstance(values, list) or len(values) > 16:
        raise GeneralHexError("Choose at most 16 references")
    return list(dict.fromkeys(_identity(value) for value in values))


def _page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise GeneralHexError("Invalid page")


def _current(check):
    if check is not None and check() is not True:
        raise GeneralHexConflict("Account or Memery authority changed")


def _guidance(record):
    value = record["provenance"].get("guidance")
    return value if isinstance(value, dict) else {}


class GeneralHexGuidance:
    def __init__(self, db_path):
        self.repo = GeneralHexRepository(db_path)

    def evidence(self, owner, workspace_id=None, *, account_id=None, limit=100, offset=0, search=""):
        _page(limit, offset)
        if not isinstance(search, str) or len(search) > 512:
            raise GeneralHexError("Invalid memory search")
        workspace = _identity(chat_workspace() if workspace_id is None else workspace_id)
        with self.repo._connection() as conn:
            # Page cheap, scoped candidate IDs first. Only current, visible
            # revisions from the canonical reader may supply displayed text.
            rows = conn.execute("""
                SELECT id FROM curated WHERE owner=? AND workspace_id IN (?, 'global') AND archived=0
                  AND kind!='unknown' AND instr(lower(content), lower(?))>0
                ORDER BY updated_at DESC,id LIMIT ? OFFSET ?
            """, (_identity(owner), workspace, search, limit + 1, offset)).fetchall()
            items = []
            for (key,) in rows[:limit]:
                try:
                    _, display = self._sources(conn, owner, workspace, [key], account_id=account_id)
                    if search.casefold() in display[0]["label"].casefold():
                        items.extend(display)
                except GeneralHexConflict:
                    continue
        return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    @staticmethod
    def _sources(conn, owner, workspace, ids, *, account_id=None):
        from src.memery_v2 import list_current_records, _provider_dossier_evidence_blocked
        refs, display = [], []
        for key in ids:
            records = list_current_records(owner=owner, workspace_id=workspace, identity=key,
                filters={"status":"active"}, require_legacy_presence=True, _connection=conn, _guidance_account=account_id or "")
            if (len(records) != 1 or records[0]["id"] != key or not records[0]["text"].strip()
                    or records[0]["kind"] in ("unknown", "open_question")
                    or records[0]["trust"]["state"] == "revoked"):
                raise GeneralHexConflict("A supporting memory is unavailable")
            record = records[0]
            block, revision = record["metadata"]["block_id"], record["metadata"]["revision"]
            if _provider_dossier_evidence_blocked(conn, owner, block, revision):
                raise GeneralHexConflict("A supporting memory is unavailable")
            # A source may change and later regain identical wording. Pin the
            # typed block and revision too, not timestamps or recall scores.
            material = [record["text"], record["workspace_id"], record["kind"],
                        record["metadata"]["block_id"], record["metadata"]["revision"]]
            refs.append(dict(id=key, sha256=hashlib.sha256(_json(material).encode()).hexdigest(),
                             content_sha256=hashlib.sha256(_json(material[:3]).encode()).hexdigest()))
            display.append(dict(id=key, label=record["text"][:240]))
        return refs, display

    def _parents(self, conn, owner, target, ids):
        # Each edge points to an already persisted revision. Follow current edges
        # too so a new proposal cannot introduce a loop in the live topic graph.
        visited, refs = set(), []
        def visit(key):
            if key == target:
                raise GeneralHexError("A Hex cannot be its own ancestor")
            if key in visited:
                return
            visited.add(key)
            if len(visited) > 512:
                raise GeneralHexError("Parent graph exceeds review limits")
            node = self.repo._get(conn, owner, key)
            if key in ids and (not node["accepted"] or not node["enabled"]):
                raise GeneralHexConflict("Parent Hex is not active")
            if key in ids:
                refs.append(dict(id=key, revision=node["revision"]))
            for parent in _ids(_guidance(node).get("parent_ids", [])):
                visit(parent)
        for key in ids:
            visit(key)
        return sorted(refs, key=lambda ref: ref["id"])

    def propose(self, owner, *, account_id, hex_id, expected_revision, action, reason, evidence_ids,
                workspace_id=None, parent_ids=None, tags=None, body=None, actor="assistant", current=None):
        owner, workspace_id, account_id = map(_identity, (owner, workspace_id if workspace_id is not None else chat_workspace(), account_id))
        if action not in ("revise", "retire") or actor not in ("assistant", "owner"):
            raise GeneralHexError("Unsupported guidance change")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise GeneralHexError("Explain which assumption behind the existing guidance is outdated")
        evidence_ids = _ids(evidence_ids)
        if not evidence_ids:
            raise GeneralHexError("Select a saved memory documenting the changed circumstances")
        if action == "retire" and body is not None:
            raise GeneralHexError("Retirement preserves the original wording")
        with self.repo._connection(write=True) as conn:
            before = self.repo._get(conn, owner, hex_id)
            reactivate = (actor == "owner" and action == "revise" and not before["enabled"]
                          and _guidance(before).get("action") == "retire")
            if before["revision"] != _revision(expected_revision) or not before["accepted"] or (not before["enabled"] and not reactivate):
                raise GeneralHexConflict("Review the currently active Hex revision")
            after = before["body"] if action == "retire" else body
            if not isinstance(after, str) or not after.strip() or len(after) > 100000:
                raise GeneralHexError("Enter the proposed guidance")
            prior_parents = _guidance(before).get("parent_ids", [])
            parents = _ids(prior_parents if parent_ids is None else parent_ids)
            labels = normalize_tags(before["tags"] if tags is None else tags)
            if action == "retire" and (parents != prior_parents or labels != before["tags"]):
                raise GeneralHexError("Retirement preserves the existing links and tags")
            if action == "revise" and not reactivate and after.strip() == before["body"] and labels == before["tags"] and parents == prior_parents:
                raise GeneralHexError("The proposal does not change the guidance")
            parent_refs = self._parents(conn, owner, hex_id, parents) if action != "retire" else []
            sources, display = self._sources(conn, owner, workspace_id, evidence_ids, account_id=account_id)
            draft = dict(kind="guidance", account_id=account_id, actor=actor, hex_id=hex_id,
                         title=before["title"], before=before["body"], after=after, reason=reason.strip(),
                         action=action, reactivate=reactivate, workspace_id=workspace_id, expected_revision=expected_revision,
                         parent_ids=parents, parents=parent_refs, tags=labels, sources=sources)
            encoded = _json(draft)
            key = hashlib.sha256(encoded.encode()).hexdigest()
            # Retried tool calls return the same review, including after dismissal.
            row = conn.execute("SELECT consumed FROM fm_general_hex_exchange_drafts WHERE owner_id=? AND draft_id=?",
                               (owner, key)).fetchone()
            if row and row[0]:
                raise GeneralHexConflict("This exact proposal has already been reviewed")
            _current(current)
            if row is None:
                conn.execute("INSERT INTO fm_general_hex_exchange_drafts VALUES(?,?,1,?,0)", (owner, key, encoded))
            return dict(draft, draft_id=key, evidence=display)

    def pending(self, owner, account_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self.repo._connection() as conn:
            rows = conn.execute("""
                SELECT draft_id,payload_json FROM fm_general_hex_exchange_drafts
                WHERE owner_id=? AND consumed=0 AND json_extract(payload_json,'$.kind')='guidance'
                AND json_extract(payload_json,'$.account_id')=? ORDER BY rowid DESC LIMIT ? OFFSET ?
            """, (_identity(owner), _identity(account_id), limit + 1, offset)).fetchall()
            items = []
            for key, raw in rows[:limit]:
                draft = json.loads(raw)
                try:
                    refs, display = self._sources(conn, owner, draft["workspace_id"], [r["id"] for r in draft["sources"]], account_id=account_id)
                    stale = refs != draft["sources"]
                except GeneralHexConflict:
                    display, stale = [], True
                items.append(dict(draft, draft_id=key, evidence=display, stale_evidence=stale))
            return {"proposals": items, "next_offset": offset + limit if len(rows) > limit else None}

    def validate_reactivation(self, conn, owner, account_id, before, restored):
        """A historical receipt cannot reactivate stale or subsequently retired guidance."""
        if not restored["enabled"]:
            return
        old, selected = _guidance(before), _guidance(restored)
        if not selected and not old:
            return
        if not selected or selected.get("action") != "revise":
            raise GeneralHexConflict("Create a fresh guidance proposal with current evidence to reactivate this Hex")
        row = conn.execute("""
            SELECT payload_json FROM fm_general_hex_exchange_drafts
             WHERE owner_id=? AND draft_id=? AND consumed=1
        """, (owner, selected.get("draft_id"))).fetchone()
        draft = json.loads(row[0]) if row else {}
        if (draft.get("kind") != "guidance" or draft.get("account_id") != account_id
                or draft.get("action") != "revise" or draft.get("hex_id") != before["hex_id"]
                or draft["after"] != restored["body"] or draft["tags"] != restored["tags"]
                or draft["sources"] != selected.get("sources") or draft["parent_ids"] != selected.get("parent_ids")):
            raise GeneralHexConflict("A new guidance review is required")
        refs, _ = self._sources(conn, owner, draft["workspace_id"], [ref["id"] for ref in draft["sources"]], account_id=account_id)
        if refs != draft["sources"] or self._parents(conn, owner, before["hex_id"], draft["parent_ids"]) != draft["parents"]:
            raise GeneralHexConflict("Supporting memory or parent changed. Review a fresh guidance proposal")

    def decide(self, owner, account_id, draft_id, *, accept, current=None):
        if type(accept) is not bool:
            raise GeneralHexError("Invalid review decision")
        owner, account_id, draft_id = map(_identity, (owner, account_id, draft_id))
        with self.repo._connection(write=True) as conn:
            row = conn.execute("SELECT payload_json,consumed FROM fm_general_hex_exchange_drafts WHERE owner_id=? AND draft_id=?",
                               (owner, draft_id)).fetchone()
            draft = json.loads(row[0]) if row else {}
            if draft.get("kind") != "guidance" or draft.get("account_id") != account_id:
                raise GeneralHexNotFound("Guidance proposal not found")
            if row[1]:
                raise GeneralHexConflict("Proposal has already been reviewed")
            result = {"state": "rejected", "draft_id": draft_id}
            if accept:
                before = self.repo._get(conn, owner, draft["hex_id"])
                if before["revision"] != draft["expected_revision"]:
                    raise GeneralHexConflict("Hex changed since this proposal")
                refs, _ = self._sources(conn, owner, draft["workspace_id"], [r["id"] for r in draft["sources"]], account_id=account_id)
                if refs != draft["sources"]:
                    raise GeneralHexConflict("Supporting memory changed since this proposal")
                retire = draft["action"] == "retire"
                if not retire and self._parents(conn, owner, draft["hex_id"], draft["parent_ids"]) != draft["parents"]:
                    raise GeneralHexConflict("A parent Hex changed since this proposal")
                guidance = {k: draft[k] for k in ("actor","reason","action","workspace_id","parent_ids","parents","sources")}
                guidance["draft_id"] = draft_id
                provenance = dict(before["provenance"], guidance=guidance, acceptance={
                    "owner_id": owner, "source": "owner-guidance-review"})
                values = dict(before, body=draft["after"], tags=draft["tags"], enabled=not retire,
                              accepted=True, authorship="agent" if draft["actor"] == "assistant" else "user",
                              provenance=provenance)
                result = self.repo._save(conn, owner, before["hex_id"], before["revision"] + 1, values)
            conn.execute("UPDATE fm_general_hex_exchange_drafts SET consumed=1,revision=2 WHERE owner_id=? AND draft_id=?",
                         (owner, draft_id))
            _current(current)
            return result
