"""BQ-8 pure page model: digest recipe, counts, prefix rule. No I/O."""
import hashlib
import json

from src.openclank import fm_journal_coverage as core


def row(seq, klass='included', reason='host_user_body', **kw):
    base = {'seq': seq, 'actor_id': 'main', 'message_id': f'm{seq}', 'part_id': 'body', 'revision': 1, 'content_hash': 'h%d' % seq,
            'workspace_id': 'ws', 'project_id': None, 'role': 'user', 'part_type': 'text', 'class': klass, 'reason': reason, 'selector': {}}
    return {**base, **kw}


class Proof:
    def __init__(self, *rows): self.rows = rows


def test_page_digest_is_the_documented_canonical_json_sha256():
    rows = [row(1), row(2, 'omitted', 'assistant_without_pair', selector={'seq': 2})]
    expected = hashlib.sha256(json.dumps({'version': 1, 'kind': 'journal_delta_page', 'consumer': 'journal_delta.v1', 'account_id': 'a', 'chat_id': 'c',
        'workspace_id': 'ws', 'ack_seq': 0, 'high_seq': 5, 'end_seq': 2, 'rows': [{k: r[k] for k in core.DIGEST_ROW_KEYS} for r in rows]},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
    assert core.page_digest(account_id='a', chat_id='c', workspace_id='ws', ack_seq=0, high_seq=5, end_seq=2, rows=rows) == expected
    with_proof = [dict(r, proof={'source': {'x': 1}}) for r in rows]          # source/authority are never digested
    assert core.page_digest(account_id='a', chat_id='c', workspace_id='ws', ack_seq=0, high_seq=5, end_seq=2, rows=with_proof) == expected


def test_counts_and_blocking_gaps():
    rows = [row(1), row(2, 'gap', 'source_missing'), row(3, 'gap', 'overlap_pending'), row(4, 'omitted', 'tool_or_media_part'), row(5, 'superseded', 'tombstoned')]
    c = core.counts(rows)
    assert (c['included'], c['gap'], c['omitted'], c['superseded']) == (1, 2, 1, 1) and c['gap_by_reason'] == {'overlap_pending': 1, 'source_missing': 1}
    assert core.blocking_gaps(rows) == 2


def test_prefix_rule_gap_blocks_omitted_superseded_and_covered_do_not():
    covered = Proof((1, 1, 'h1'))
    assert core.prefix_refusal([row(1), row(2, 'omitted', 'unsupported_part'), row(3, 'superseded', 'later_revision'), row(4, 'already_covered', 'already_covered')], [covered]) is None
    assert core.prefix_refusal([row(1), row(2, 'gap', 'source_denied')], [covered]) == 'gap_blocks_prefix'
    assert core.prefix_refusal([row(1), row(2)], [covered]) == 'uncovered_row'
    assert core.prefix_refusal([row(1)], [Proof((1, 1, 'other'))]) == 'proof_mismatch'
    assert core.prefix_refusal([row(1)], [covered, covered]) == 'uncovered_row'
