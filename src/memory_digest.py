"""Render the fm digest as the per-turn memory index card.

One small block, injected every turn: what the bank holds, never the
contents. The model pulls details through the memory search/recall tools
when a listed topic matters. Size is bounded by the engine's item caps
(≤5 pinned, ≤6 clusters, ≤5 recent) — no token counting here.

Both hosts share this renderer: Odysseus injects the block through the
chat preface, the bridge prepends it to mimo turns. DIGEST_SENTINEL marks
the block so the receiving side can drop a duplicate if both paths ever
cover the same turn.
"""

from typing import Any, Dict, Optional

DIGEST_SENTINEL = "[Memory Index]"


def render_digest(digest: Optional[Dict[str, Any]]) -> str:
    """Digest JSON → index-card text. Empty string when the bank is empty
    or the digest is malformed — callers skip the block entirely."""
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
        "task, recall details with the memory search tool."
    )
    return "\n".join(lines)
