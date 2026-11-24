"""Branches of `fm_journal_delta.settle` that the runner tests do not drive: the hint list (`staged`), the no-hint pass, and a runner that raises."""
import asyncio

from src.openclank import fm_journal_delta as delta
from tests.test_journal_runner import page


def settle(one_pass, run_job, staged=None):
    return asyncio.run(delta.settle(one_pass, run_job, staged))


def make(first, second=None, *, hold_page=True):
    passes = []

    async def one_pass(hold):
        passes.append(hold)
        if hold is not None and hold_page and first['state'] in ('staged', 'selected'):
            hold.append(page())
        return first if len(passes) == 1 else (second or {'state': 'acked'})
    return one_pass, passes


def test_a_staged_hint_receives_the_selection_and_the_wake_never_runs_the_model_without_a_runner():
    one_pass, passes = make({'state': 'staged'})
    staged = []
    assert settle(one_pass, None, staged)['state'] == 'staged'
    assert len(staged) == 1 and isinstance(staged[0], delta.JournalSelection) and len(passes) == 1


def test_the_no_hint_pass_returns_the_first_result_unchanged_and_runs_nothing():
    ran = []

    async def run_job(selection):
        ran.append(selection)
        return True
    for state in ('idle', 'blocked_gap', 'off', 'acked'):
        one_pass, passes = make({'state': state})
        staged = []
        assert settle(one_pass, run_job, staged) == {'state': state} and staged == [] and ran == [] and len(passes) == 1


def test_no_selection_held_means_no_run_even_when_the_state_says_staged():
    ran = []

    async def run_job(selection):
        ran.append(selection)
        return True
    one_pass, passes = make({'state': 'staged'}, hold_page=False)
    assert settle(one_pass, run_job, [])['state'] == 'staged' and ran == [] and len(passes) == 1


def test_a_runner_that_raises_is_deferred_and_the_second_pass_is_not_taken():
    async def run_job(selection):
        raise RuntimeError('router unavailable')
    one_pass, passes = make({'state': 'staged'})
    out = settle(one_pass, run_job, [])
    assert out['state'] == 'deferred' and out['reason'] == 'run_unavailable' and len(passes) == 1


def test_a_ran_job_takes_the_second_pass_and_returns_its_result():
    async def run_job(selection):
        return True
    one_pass, passes = make({'state': 'selected'}, {'state': 'acked'})
    staged = []
    assert settle(one_pass, run_job, staged)['state'] == 'acked' and len(staged) == 1 and passes[1] is None
