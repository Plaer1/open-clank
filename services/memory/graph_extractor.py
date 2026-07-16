"""Per-turn knowledge-graph extraction — the Odysseus port of mimo's
memory/graph-extract.ts.

Turn capture is owned by the Odysseus post-response seam for every
transport, so the graph enrichment that used to ride on the mimo child's
capture pipeline lives here now, feature-for-feature: same prompt, same
canonical tag vocabulary, same forgiving wire schema (small models drift —
"type" for "kind", "nodes" for "entities", "source"/"target" for
"src"/"dst", bare name strings for endpoints), same per-session throttle
policy, same rule that capture NEVER depends on extraction succeeding.

The LLM ride is Odysseus-native: the task endpoint (resolve_task_endpoint),
the same background channel the legacy native extractor uses, queued
through the sequential post-response gate so it never races the main
completion (issue #2927).
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Canonical tag vocabulary — .futures/frankenmemory-update/tag-vocabulary.md
# (finalized 2026-07-08). The prompt instructs reuse-before-mint; fm's groom
# tag_normalize op merges strays back into these.
CANONICAL_TAGS = [
    "is", "has", "uses", "makes", "runs", "talks_to", "lives_in", "made_by",
    "works_on", "wants", "likes", "dislikes", "before", "blocks", "fixes", "about",
    "imports", "calls", "defines", "extends", "tests", "configures",
]

NODE_KINDS = ["person", "project", "tool", "concept", "place", "event", "topic", "file"]

SYSTEM = f"""You extract a small knowledge graph from one chat turn.

Rules:
- Only extract what the text actually states. No speculation, no world knowledge.
- Entities: concrete people, projects, tools, places, events, concepts. Skip generic words.
- Edge tags: snake_case verb phrases, read left to right (src TAG dst).
  Use one of these canonical tags whenever one fits: {", ".join(CANONICAL_TAGS)}.
  Mint a new tag ONLY when none of them fits.
- Do not store inverses ("blocked_by"); flip src/dst and use the canonical tag.
- Each edge carries "fact": one short plain sentence a stranger would understand.
- Cues: verbatim words/phrases from the text useful as future search entry points.
- Empty arrays are fine. A turn with nothing memorable yields nothing.
- Respond with a single JSON object EXACTLY in this shape (field names matter):
  {{"entities":[{{"kind":"person","name":"Ada"}}],"cues":["ada","loom project"],"edges":[{{"src":{{"kind":"person","name":"Ada"}},"tag":"works_on","dst":{{"kind":"project","name":"loom"}},"fact":"Ada works on the loom project."}}]}}"""


# ── Throttle (per-session; policy identical to graph-extract.ts) ──────────

_throttle: Dict[str, Dict[str, float]] = {}


def should_extract(cfg: Dict[str, Any], state: Dict[str, float], now_ms: float) -> bool:
    """Pure throttle decision so the policy stays unit-testable."""
    every_n = int(cfg.get("every_n_turns") or 1)
    if int(state.get("turns", 0)) % every_n != 0:
        return False
    min_gap_ms = float(cfg.get("min_interval_seconds") or 0) * 1000.0
    last = float(state.get("last_extract_ms", 0))
    if min_gap_ms > 0 and last > 0 and now_ms - last < min_gap_ms:
        return False
    return True


def _graph_config() -> Dict[str, Any]:
    try:
        from src.settings import get_setting

        cfg = get_setting("memory_graph")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


# ── Forgiving wire-schema normalization ───────────────────────────────────

def _normalize_side(side: Any, kinds: Dict[str, str]) -> Optional[Dict[str, str]]:
    if side is None:
        return None
    if isinstance(side, str):
        name = side
        kind = None
    elif isinstance(side, dict) and side.get("name"):
        name = str(side["name"])
        kind = side.get("kind")
    else:
        return None
    return {"kind": str(kind or kinds.get(name.lower()) or "concept"), "name": name}


def _parse_extraction(raw: str) -> Optional[Dict[str, Any]]:
    try:
        from src.text_helpers import strip_think

        raw = strip_think(raw)
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def normalize_extraction(payload: Dict[str, Any]) -> Tuple[List[dict], List[dict], List[str]]:
    """Wire payload → (nodes, edges, cues) in graph_upsert shape."""
    raw_entities = payload.get("entities") or payload.get("nodes") or []
    raw_edges = payload.get("edges") or []
    raw_cues = payload.get("cues") or []

    entities = [
        {"kind": str(e.get("kind") or e.get("type") or "concept"), "name": str(e["name"])}
        for e in raw_entities
        if isinstance(e, dict) and e.get("name")
    ]
    kinds = {e["name"].lower(): e["kind"] for e in entities}

    edges = []
    for e in raw_edges:
        if not isinstance(e, dict) or not e.get("tag"):
            continue
        src = _normalize_side(e.get("src") or e.get("source"), kinds)
        dst = _normalize_side(e.get("dst") or e.get("target") or e.get("dest"), kinds)
        if not src or not dst:
            continue
        edges.append({"src": src, "tag": str(e["tag"]), "dst": dst, "fact": str(e.get("fact") or "")})

    cues = [str(c) for c in raw_cues if str(c).strip()]
    for e in raw_edges:
        if isinstance(e, dict):
            cues.extend(str(c) for c in (e.get("cues") or []) if str(c).strip())
    return entities, edges, cues


# ── Extraction call + graph_upsert ────────────────────────────────────────

async def extract_and_upsert(
    memory_provider,
    user_text: str,
    assistant_text: str,
    *,
    session_id: str,
    owner: Optional[str],
    endpoint_url: str,
    model: str,
    headers: Optional[dict],
) -> None:
    cfg = _graph_config()
    if cfg.get("enabled") is False:
        return

    state = _throttle.setdefault(session_id, {"turns": 0, "last_extract_ms": 0})
    go = should_extract(cfg, state, time.time() * 1000)
    state["turns"] = int(state.get("turns", 0)) + 1
    if not go:
        logger.debug("graph extraction throttled for session %s (turn %s)", session_id, state["turns"])
        return
    state["last_extract_ms"] = time.time() * 1000

    if not endpoint_url or not model:
        logger.debug("graph extraction skipped: no task endpoint configured")
        return

    from src.llm_core import llm_call_async

    text = f"USER:\n{user_text}\n\nASSISTANT:\n{assistant_text}"
    raw = await llm_call_async(
        endpoint_url,
        model,
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": text},
        ],
        temperature=0,
        max_tokens=4096,
        headers=headers,
    )

    payload = _parse_extraction(raw)
    if payload is None:
        logger.warning("graph extraction returned unparseable output; dropped")
        return
    entities, edges, cues = normalize_extraction(payload)

    # Usage is TRACKED, never throttled — grep target for token-burn reports.
    logger.info(
        "graph extraction usage model=%s entities=%d edges=%d cues=%d",
        model, len(entities), len(edges), len(cues),
    )
    if not entities and not edges and not cues:
        return

    # Cues attach to the first extracted entity when present — entry points
    # need a node to land on. Turns with edges but no entities still work:
    # edge endpoints auto-create nodes fm-side.
    anchor = entities[0] if entities else (edges[0]["src"] if edges else None)
    from src.memory_scope import chat_workspace

    await memory_provider._call_tool(
        "graph_upsert",
        {
            "owner": owner or "",
            "workspace_id": chat_workspace(),
            "nodes": entities,
            "edges": edges,
            "cues": [{"cue": cue, "node": anchor} for cue in cues] if anchor else [],
        },
    )


async def capture_turn_and_enrich(
    memory_provider,
    user_text: str,
    assistant_text: str,
    *,
    session_id: str,
    owner: Optional[str],
    endpoint_url: str,
    model: str,
    headers: Optional[dict],
) -> None:
    """The full per-turn pipeline mimo's capture.ts used to run child-side:
    candidate capture, then graph enrichment when the capture was accepted.
    Extraction failures never touch the captured record."""
    result = await memory_provider.capture(
        user_text, assistant_text, owner=owner, session_id=session_id,
    )
    accepted = any(str(rid).startswith("m_") for rid in (result.get("record_ids") or []))
    if not accepted:
        return
    try:
        await extract_and_upsert(
            memory_provider,
            user_text,
            assistant_text,
            session_id=session_id,
            owner=owner,
            endpoint_url=endpoint_url,
            model=model,
            headers=headers,
        )
    except Exception as exc:
        logger.warning("graph extraction failed: %s", str(exc)[:500])
