import pytest
from src.memory_provider import MemoryRevisionRef
from src.openclank.memory_context_admission import Candidate, select_candidates


def hit(block, text, owner="alice", workspace="ws"):
    return Candidate(MemoryRevisionRef(owner=owner, workspace_id=workspace, project_id=None,
        block_id=block, revision=1, value_sha256="a" * 64), text)


def choose(candidates, **changes):
    args = dict(owner="alice", workspace_id="ws", project_id=None, privacy="normal",
                query="release plan", context=(), candidates=tuple(candidates))
    args.update(changes)
    return select_candidates(**args).candidates


def test_scoped_relevant_bounded_selection_and_privacy():
    candidates = [hit("x", "Release plan for the launch"), hit("foreign", "Release plan", owner="bob"),
                  hit("duplicate", "release plan for the launch"), hit("y", "Release checklist"),
                  hit("z", "Plan dependencies"), hit("extra", "Plan review")]
    assert [x.revision.block_id for x in choose(candidates)] == ["x", "y", "z"]
    assert choose(candidates, privacy="no_memory") == ()
    assert choose(candidates, privacy="incognito") == ()
    assert choose(candidates, query="release") == ()
    assert [x.revision.block_id for x in choose(candidates, context=("Release plan for the launch",))] == ["y", "z", "extra"]
    assert choose([hit("long", "release plan " + "x" * 1190)]) == ()
    with pytest.raises(ValueError):
        choose(candidates * 4)


def test_prompt_limits_keep_whole_scoped_revisions():
    records = [hit("long", "Release plan for the orchard expansion"),
               hit("foreign", "Release plan", owner="bob"), hit("short", "Release plan"),
               hit("next", "Release cycle")]
    assert choose(records, char_budget=12, limit=1) == (records[2],)
    assert choose(records, char_budget=1) == ()
    with pytest.raises(ValueError):
        choose(records, limit=True)
