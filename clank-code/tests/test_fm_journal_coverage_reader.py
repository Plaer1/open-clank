"""BQ-8 read helpers: exact-identity dedupe (contract B) and the content-free route view."""
from src.openclank import fm_journal_coverage as core
from src.openclank import fm_journal_coverage_reader as reader
from src.openclank.fm_journal_delta import dedupe_for

ROW = {'message_id': 'm1', 'part_id': 'body', 'revision': 1, 'content_hash': 'h1'}


def job(state, **kw):
    return {'kind': 'completed_turn_review', 'state': state, 'job_id': 'j', 'receipt_id': None, 'content_hash': 'h1', 'purpose': None, **kw}


def verdict(*jobs):
    return dedupe_for(lambda row: list(jobs))(ROW)


def test_pending_or_unknown_overlap_blocks_and_completed_turn_results_never_cover():
    for state in ('queued', 'active', 'awaiting_review', 'retry_wait', 'unknown'):
        assert verdict(job(state)) == {'state': 'pending'}
    # H2: a succeeded completed-turn/paired job (even receipted, same hash, right purpose) is a different purpose: the row stays included.
    for state in ('succeeded', 'failed_terminal', 'cancelled'):
        assert verdict(job(state)) is None
    assert verdict(job('succeeded', receipt_id='cs_r', purpose='host_user_body_review')) is None
    assert verdict(job('succeeded', receipt_id='cs_r', purpose='host_user_body_review'), job('queued')) == {'state': 'pending'}


def test_already_covered_only_from_a_recorded_journal_outcome():
    seen = []
    def covered(state):
        return lambda row: seen.append(row['part_id']) or {'state': state, 'receipt_id': 'cs_r', 'job_id': 'jj'}
    for state in ('recorded_proposal', 'recorded_no_finding'):
        assert dedupe_for(lambda row: [], covered(state))(ROW) == {'state': 'covered', 'receipt_id': 'cs_r', 'job_id': 'jj'}
    for state in ('selected', 'absent', 'terminal_failed', 'cancelled', 'unknown'):
        assert dedupe_for(lambda row: [], covered(state))(ROW) is None
    assert dedupe_for(lambda row: [], lambda row: {'state': 'recorded_proposal', 'receipt_id': None, 'job_id': 'j'})(ROW) is None
    assert dedupe_for(lambda row: [job('queued')], covered('recorded_proposal'))(ROW) == {'state': 'pending'}   # pending overlap wins


def test_an_unreadable_job_store_fails_closed_as_overlap(tmp_path):
    jobs = reader.overlap_rows_for(str(tmp_path / 'missing.db'), memory_owner='o', account_id='a', chat_id='c')
    assert dedupe_for(jobs)(ROW) == {'state': 'pending'}


def test_page_view_is_content_free_and_has_no_account_or_digest():
    page = {'account_id': 'secret-account', 'page_sha256': 'x' * 64, 'ack_seq': 3, 'high_seq': 9, 'end_seq': 7,
            'rows': [{'class': 'included', 'reason': 'host_user_body', 'message_id': 'm'}, {'class': 'gap', 'reason': 'source_missing', 'message_id': 'n'}]}
    view = reader.page_view(page)
    assert view['state'] == 'ok' and view['counts']['gap_by_reason'] == {'source_missing': 1} and view['blocking_gaps'] == 1
    text = str(view)
    assert 'secret-account' not in text and 'x' * 64 not in text and "'m'" not in text
