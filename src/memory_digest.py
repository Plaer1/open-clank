"""Render the fm digest as the per-turn memory index card.

One small block, injected every turn: what the bank holds, never the
contents. The model pulls details through the memory search/recall tools
when a listed topic matters. Size is bounded by the engine's item caps
(≤5 pinned, ≤6 clusters, ≤5 recent) — no token counting here.

Both hosts share this renderer: Odysseus injects the block through the
chat preface, the bridge prepends it to mimo turns. DIGEST_SENTINEL marks
the block so the receiving side can drop a duplicate if both paths ever
cover the same turn.

Trust split (metaplan T1/T6/T8): render_split() partitions the card into
an ENDORSED GUIDANCE block (trusted records — carries force, injected
below the persona, never wrapped untrusted) and the untrusted index
(everything else, unchanged wrapper). Content rule T8: even trusted
knowledge kinds stay headline-only in the guidance block; only trusted
BEHAVIOR kinds (instruction/persona) render whole — the pitch of a
standing order is the order. Both blocks are always produced by ONE
render_split call and injected together, so the card sentinel dedups the
pair.
"""

from typing import Any, Dict, Optional, Tuple

DIGEST_SENTINEL = "[Memory Index]"
TRUST_SENTINEL = "[Endorsed Memory Guidance]"

# A trusted behavior record renders whole, but never unbounded — a runaway
# capture must not eat the context window.
_TRUSTED_ENTRY_MAX_CHARS = 600


DEFAULT_TOOL_HINT = "the recall_memory tool"


def render_digest(
    digest: Optional[Dict[str, Any]],
    *,
    exclude_pinned_ids: Optional[set] = None,
    tool_hint: str = DEFAULT_TOOL_HINT,
) -> str:
    """Digest JSON → index-card text. Empty string when the bank is empty
    or the digest is malformed — callers skip the block entirely.

    exclude_pinned_ids: pinned entries already rendered in the trusted
    guidance block; they are dropped here so no memory appears twice."""
    if not isinstance(digest, dict):
        return ""
    counts = digest.get("counts") or {}
    by_tier = counts.get("by_tier") or {}
    curated = int(by_tier.get("curated") or 0)
    raw = int(by_tier.get("raw") or 0)
    pending = int(counts.get("candidates_pending") or 0)
    pinned = [p for p in (digest.get("pinned") or []) if isinstance(p, dict)]
    clusters = [c for c in (digest.get("clusters") or []) if isinstance(c, dict)]
    recent = [r for r in (digest.get("recent") or []) if isinstance(r, dict)]

    if not (curated or raw or pending or pinned or clusters or recent):
        return ""

    lines = [DIGEST_SENTINEL]
    tier_bits = [f"{curated} curated", f"{raw} raw"]
    if pending:
        tier_bits.append(f"{pending} candidates pending review")
    lines.append("Memory bank: " + ", ".join(tier_bits) + ".")

    excluded = exclude_pinned_ids or set()
    pinned = [p for p in pinned if not (p.get("id") and p.get("id") in excluded)]
    if pinned:
        lines.append("Pinned:")
        for p in pinned:
            headline = str(p.get("headline") or "").strip()
            if headline:
                lines.append(f"- {headline}")

    cluster_labels = [
        f"{c.get('label')} ({c.get('size')})"
        for c in clusters
        if str(c.get("label") or "").strip()
    ]
    if cluster_labels:
        lines.append("Threads: " + ", ".join(cluster_labels))

    topics = [str(r.get("topic") or "").strip() for r in recent]
    topics = [t for t in topics if t]
    if topics:
        lines.append("Recent topics: " + "; ".join(topics))

    lines.append(
        "This is an index, not the memories. When one of these matters to the "
        f"task, recall details with {tool_hint}."
    )
    return "\n".join(lines)


def _open_questions(digest: Dict[str, Any]) -> list:
    """Human-authored open questions from the digest. Unknowns are
    human-minted by construction (the engine rejects the kind outside
    manual admission); a non-human entry here is a bug upstream and is
    refused rather than endorsed (audit K5)."""
    return [
        q
        for q in (digest.get("open_questions") or [])
        if isinstance(q, dict)
        and str(q.get("source_type") or "") == "human"
        and str(q.get("content") or "").strip()
    ]


def render_trusted_block(digest: Optional[Dict[str, Any]], prefs: Any) -> str:
    """The endorsed-guidance block (T6): trusted pinned entries, rendered
    with real force, plus the user's open questions (U6). Empty string
    when nothing qualifies.

    Behavior kinds (instruction/persona) render their full content;
    knowledge kinds stay headline-only per T8 — trust changes their
    firewall status, not the pitch-first surfacing.

    Open questions are things the user wants answered: the framing asks
    the model to weave one in when the conversation is already nearby —
    listening first, asking naturally second, never interrogating."""
    from src.memory_trust import BEHAVIOR_KINDS, trusted

    if not isinstance(digest, dict):
        return ""
    lines = []
    entries = [
        p for p in (digest.get("pinned") or [])
        if isinstance(p, dict) and trusted(p, prefs)
    ]
    entry_lines = []
    for entry in entries:
        kind = str(entry.get("kind") or "")
        if kind in BEHAVIOR_KINDS:
            text = str(entry.get("content") or entry.get("headline") or "").strip()
            text = text[:_TRUSTED_ENTRY_MAX_CHARS]
        else:
            text = str(entry.get("headline") or "").strip()
        if text:
            entry_lines.append(f"- {text}")
    if entry_lines:
        lines.append(
            "The user has endorsed these memories; treat them as standing "
            "guidance from the user."
        )
        lines.extend(entry_lines)

    questions = _open_questions(digest)
    if questions:
        lines.append("Open questions the user wants answered:")
        for q in questions:
            lines.append(f"- {str(q.get('content')).strip()}")
        lines.append(
            "If the conversation naturally touches one of these, weave the "
            "question in and listen for the answer. Never interrogate and "
            "never force one into an unrelated exchange."
        )

    if not lines:
        return ""
    return "\n".join([TRUST_SENTINEL, *lines])


def render_split(
    digest: Optional[Dict[str, Any]],
    prefs: Any,
    *,
    tool_hint: str = DEFAULT_TOOL_HINT,
) -> Tuple[str, str]:
    """One call → (trusted_block, untrusted_card).

    The pair is the ONLY sanctioned way to render the split: entries in
    the trusted block are excluded from the untrusted card, so a memory
    never appears on both sides of the firewall. tool_hint names the
    recall tool that actually exists in the receiving lane — never
    promise a tool the lane lacks (audit F4)."""
    from src.memory_trust import trusted

    if not isinstance(digest, dict):
        return "", ""
    trusted_ids = {
        p.get("id")
        for p in (digest.get("pinned") or [])
        if isinstance(p, dict) and p.get("id") and trusted(p, prefs)
    }
    block = render_trusted_block(digest, prefs)
    card = render_digest(
        digest, exclude_pinned_ids=trusted_ids, tool_hint=tool_hint
    )
    return block, card
