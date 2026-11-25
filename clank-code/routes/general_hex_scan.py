"""Read-only comparison of accepted General Hex evidence with current visible Memery records."""
import json

from fastapi import HTTPException, Request, Response

from src.general_hex_library import GeneralHexConflict, GeneralHexError, _identity
from src.memory_scope import chat_workspace


def scan_guidance(service, owner, account_id, workspace_id, *, after="", limit=32):
    owner, account_id, workspace_id = map(_identity, (owner, account_id, workspace_id))
    if type(limit) is not int or not 1 <= limit <= 32 or not isinstance(after, str) or len(after) > 512:
        raise GeneralHexError("Invalid scan page")

    counts = dict(heads=0, active=0, unlinked=0, other_workspace=0, checked_sources=0,
                  unavailable_sources=0)
    items = []
    with service.repo._connection() as conn:
        # Keyset pagination bounds the work even for accounts with thousands of rules.
        rows = conn.execute("""
            SELECT h.hex_id,r.record_json FROM fm_general_hex_heads h
              JOIN fm_general_hex_revisions r USING(owner_id,hex_id,revision)
            WHERE h.owner_id=? AND h.hex_id>? AND h.deleted=0
            ORDER BY h.hex_id LIMIT ?
        """, (owner, after, limit + 1)).fetchall()
        for hex_id, raw in rows[:limit]:
            counts["heads"] += 1
            record = json.loads(raw)
            if not (record["accepted"] and record["enabled"]):
                continue
            counts["active"] += 1
            link = record["provenance"].get("guidance")
            # Imported/user-supplied provenance may contain arbitrary JSON. Only use
            # evidence that was actually reviewed for this Hex in this account.
            draft_id = link.get("draft_id") if isinstance(link, dict) else None
            if not isinstance(draft_id, str):
                counts["unlinked"] += 1
                continue
            row = conn.execute("""SELECT payload_json FROM fm_general_hex_exchange_drafts
                WHERE owner_id=? AND draft_id=? AND consumed=1""", (owner, draft_id)).fetchone()
            draft = json.loads(row[0]) if row else {}
            if (draft.get("kind") != "guidance" or draft.get("hex_id") != hex_id
                    or draft.get("account_id") != account_id or draft.get("action") != "revise"
                    or link.get("sources") != draft.get("sources")):
                counts["unlinked"] += 1
                continue
            if draft["workspace_id"] != workspace_id:
                counts["other_workspace"] += 1
                continue
            for source in draft["sources"]:
                try:
                    refs, display = service._sources(conn, owner, workspace_id, [source["id"]])
                except GeneralHexConflict:
                    counts["unavailable_sources"] += 1
                    continue
                counts["checked_sources"] += 1
                if refs[0]["content_sha256"] != source["content_sha256"]:
                    items.append(dict(hex_id=hex_id, title=record["title"], expected_revision=record["revision"],
                                      workspace_id=workspace_id, evidence_id=source["id"],
                                      evidence_label=display[0]["label"], reason="source_changed"))
    return dict(items=items, coverage=counts, truncated=len(rows) > limit,
                next_cursor=rows[limit - 1][0] if len(rows) > limit else None)


def register_guidance_scan(router, guidance_context):
    @router.get("/guidance/scan")
    def scan(request: Request, response: Response, workspace_id: str | None = None, after: str = ""):
        service, owner, account_id, current = guidance_context(request)
        if current() is not True:
            raise HTTPException(403, "Memery access is unavailable")
        try:
            result = scan_guidance(service, owner, account_id,
                                   chat_workspace() if workspace_id is None else workspace_id, after=after)
        except GeneralHexError as exc:
            raise HTTPException(422, str(exc)) from exc
        if current() is not True:
            raise HTTPException(403, "Memery access changed")
        response.headers["Cache-Control"] = "private, no-store"
        return result
