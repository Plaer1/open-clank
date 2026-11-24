"""Read witnessed outgoing edges of one committed Copal page."""
from __future__ import annotations

import asyncio
import re
from itertools import islice

from src.openclank.document_code_links import _checked_segments, _code_mention, document_text, scope
from src.openclank.document_ingestion import read_journal
from src.openclank.document_journeys import references

MEMORY_LINK = re.compile(r"memory:([a-zA-Z0-9][a-zA-Z0-9._~-]{0,255})")
LIMIT = 8


async def selected_evidence(request, owner, document, *, copal_scope, code, tasks, current):
    raw = document_text(document)
    if raw is None or not current():
        raise ValueError("Page unavailable")
    rows, coverage = [], {}
    page = {"kind": "document", "id": document["id"], "revision": document["head"],
            "workspace": copal_scope["workspace_id"]}

    def edge(kind, start, end, line, **values):
        return {"kind": kind, "from": start, "to": end, "line": line, "timestamp": document.get("updatedAt"), **values}

    for task in tasks[:LIMIT]:
        rows.append(edge("tracks", page, {"kind": "task", "id": task["id"]}, task["line"],
                         label=task["text"][:256], checked=task["checked"]))
    coverage["tasksOmitted"] = max(0, len(tasks) - LIMIT)
    links = []
    for token in references(raw):
        match = MEMORY_LINK.fullmatch(token["target"])
        if token["syntax"] == "wiki" and raw[token["start"]] != "!" and match:
            links.append((match[1], raw.count("\n", 0, token["start"]) + 1))
            if len(links) > LIMIT:
                break
    coverage["moreFactLinks"] = len(links) > LIMIT
    fact_heads = {}
    if links:
        from src.openclank.analysis_snapshot_host import snapshot_named_human
        from src.openclank.memory_policy import STORE
        from services.memory.memery_history import account_current

        state = request.app.state
        account = state.auth_manager.account_id(owner)
        provider = state.memory_provider

        async def readable():
            if not current() or snapshot_named_human(request) != owner or not account_current(state, owner, account):
                return False
            policy = await asyncio.to_thread(STORE.effective_snapshot, owner, read_privilege=True,
                write_privilege=True, incognito=False, no_memory=False, compare_mode=False)
            return policy.get("read_allowed", False)

        facts = []
        for ident, line in links[:LIMIT]:
            try:
                if not await readable():
                    break
                detail = await provider.versioned_detail(ident, owner=owner)
                head = detail["current"]
                if head.get("status") != "active":
                    continue
                fact = {"kind": "fact", "id": ident, "revision": head["revision"], "workspace": detail["workspace_id"]}
                batch = [edge("cites", page, fact, line, label=head["text"][:256], timestamp=head["created_at"])]
                from src.memory_versioned import read_source_reference
                for item in islice((e for e in detail["evidence"] if e["id"] in head["evidence_ids"]), LIMIT):
                    persisted = await asyncio.to_thread(read_source_reference, db_path=provider._local_db_path(),
                        owner=detail["owner"], workspace_id=detail["workspace_id"] or "", reference={
                            "kind": "memory_evidence", "evidence_id": item["id"], "source_revision": item["source_revision"]})
                    if (not persisted or persisted.get("superseded_quote") or persisted["forget_state"] != "active" or
                        persisted["latest_revision"] != persisted["source_revision"]):
                        continue
                    support = {"kind": "support", "id": item["id"], "sourceId": item["source_id"],
                               "revision": item["source_revision"]}
                    batch.append(edge("supports", support, fact, line, label=item["quote"][:256],
                                      timestamp=item["created_at"], sourceType=item["source_type"],
                                      quote=(persisted["excerpt"] or "")[:1024]))
                if await readable():
                    facts.extend(batch)
                    fact_heads[ident] = head["content_hash"]
            except Exception:
                coverage["factsUnavailable"] = True
    if code:
        receipt, query, target, segments, code_current = scope(request, owner, code)
        retained = await read_journal(request, copal_scope, document, audience_current=code_current)
        parsed = _checked_segments(raw, retained if retained is not None else (await segments([raw]))[0])
        qualified = [m for m in parsed["mentions"] if "/" in m["spelling"] or "#" in m["spelling"]]
        coverage["codeMentionsOmitted"] = parsed["omitted"] + max(0, len(qualified) - LIMIT)
        for mention in qualified[:LIMIT]:
            result = await _code_mention(query, {"code_mentions": [mention]}, {"mention_index": 0, "code_receipt": receipt})
            if result["state"] != "candidate":
                continue
            from src.openclank.code_query import CodeQueryError
            try:
                source = (await target(result["reference"]))["source"]
            except (ValueError, CodeQueryError):
                continue
            rows.append(edge("mentions", page, {"kind": "code", "id": result["reference"]["id"],
                "workspace": receipt["workspace_id"], "snapshot": receipt["source_snapshot_id"]},
                mention["line"], label=mention["spelling"], span=source["span"], path=source["rel_path"]))
    if links and fact_heads and await readable():
        for ident, expected in fact_heads.items():
            try:
                latest = await provider.versioned_detail(ident, owner=owner)
            except Exception:
                continue
            if latest["current"]["content_hash"] == expected:
                evidence = {e["id"] for e in latest["evidence"]}
                rows.extend(row for row in facts if row["to"]["id"] == ident and
                    (row["kind"] != "supports" or row["from"]["id"] in evidence))
        if not await readable():
            rows = [row for row in rows if row["kind"] not in {"supports", "cites"}]
    if not current():
        raise ValueError("Page access changed")
    return {"page": page, "rows": rows, "coverage": coverage}
