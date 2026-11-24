"""Pure page model for the ``journal_delta.v1`` consumer (BQ-8, contract A).

No I/O, no clock, no storage knowledge. The archive (``ConversationArchive.read_journal_delta_page``) classifies every
main-actor row of one frozen window ``(ack_seq, high_seq]``; this module owns the closed vocabularies, the canonical
page digest (the Native side recomputes it byte for byte: ``repository/s08_journal.rs``) and the ACK prefix rule.
"Covered" means a recorded proposal or an explicit recorded ``no_finding`` for the exact selection, never an outcome of
another job and never a timestamp.
"""
from __future__ import annotations

import hashlib
import json

CONSUMER = 'journal_delta.v1'
PAGE_ROWS = 32          # frozen decision 8
PAGE_BYTES = 16384

INCLUDED, SUPERSEDED, GAP, OMITTED, ALREADY_COVERED = 'included', 'superseded', 'gap', 'omitted', 'already_covered'
CLASSES = (INCLUDED, SUPERSEDED, GAP, OMITTED, ALREADY_COVERED)
# Frozen decision 1: `gap` BLOCKS the prefix (uncertain / later resolvable); `omitted` is permanently outside this profile
# version by type, is recorded with its selector and does NOT block.
GAP_REASONS = frozenset(('source_missing', 'source_denied', 'proof_unavailable', 'oversize', 'overlap_pending', 'superseding_without_proof'))
OMITTED_REASONS = frozenset(('unsupported_part', 'tool_or_media_part', 'untrusted_engine_part', 'assistant_without_pair'))
INCLUDED_REASON = 'host_user_body'
SUPERSEDED_REASONS = frozenset(('later_revision', 'tombstoned'))
COVERED_REASON = 'already_covered'

DIGEST_ROW_KEYS = ('seq', 'actor_id', 'message_id', 'part_id', 'revision', 'content_hash', 'workspace_id', 'project_id',
                   'role', 'part_type', 'class', 'reason', 'selector')


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha256_hex(value) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def digest_row(row) -> dict:
    """The digested projection of one row: exactly the retained fields plus class/reason/selector (never source/authority)."""
    return {key: row[key] for key in DIGEST_ROW_KEYS}


def page_digest(*, account_id, chat_id, workspace_id, ack_seq, high_seq, end_seq, rows) -> str:
    """Same recipe as the Native recomputation (Wire (frozen) section of the BQ-8 contract); contains the internal account id."""
    return sha256_hex({'version': 1, 'kind': 'journal_delta_page', 'consumer': CONSUMER, 'account_id': account_id,
                       'chat_id': chat_id, 'workspace_id': workspace_id, 'ack_seq': ack_seq, 'high_seq': high_seq,
                       'end_seq': end_seq, 'rows': [digest_row(row) for row in rows]})


def counts(rows) -> dict:
    """Content-free class counts (the read-only route's body): classes, gap reasons, omitted count."""
    out = {name: 0 for name in CLASSES}
    gaps = {}
    for row in rows:
        out[row['class']] += 1
        if row['class'] == GAP:
            gaps[row['reason']] = gaps.get(row['reason'], 0) + 1
    out['gap_by_reason'] = dict(sorted(gaps.items()))
    return out


def blocking_gaps(rows) -> int:
    return sum(row['class'] == GAP for row in rows)


def prefix_refusal(rows, coverage):
    """Why this page's prefix may NOT be acknowledged, or None.

    Every row must be superseded, omitted (recorded, never blocking), ``already_covered`` (its receipt is attached to the row
    and proved at selection) or ``included`` and covered by exactly one proof whose row entry equals ``(seq, revision,
    content_hash)``. A ``gap`` blocks (S08: "A gap blocks advancing that prefix").
    """
    by_row = {}
    for proof in coverage:
        for entry in proof.rows:
            by_row.setdefault(entry[0], []).append((proof, entry))
    for row in rows:
        kind = row['class']
        if kind == GAP:
            return 'gap_blocks_prefix'
        if kind in (SUPERSEDED, OMITTED, ALREADY_COVERED):
            continue
        found = by_row.get(row['seq'], [])
        if len(found) != 1:
            return 'uncovered_row'
        if found[0][1] != (row['seq'], row['revision'], row['content_hash']):
            return 'proof_mismatch'
    return None
