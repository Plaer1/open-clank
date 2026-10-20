"""The startup reaper owns only bundled Open Clank agent workers.

Personal mimo processes and another live Open Clank instance are never targets.
"""
from pathlib import Path

from app import _is_orphaned_open_clank_agent_worker


_BUNDLED_MIMO = str((Path(__file__).resolve().parents[1] / "bin" / "mimo").resolve())


def test_flags_subreaper_orphan():
    assert _is_orphaned_open_clank_agent_worker(
        f"{_BUNDLED_MIMO} acp --hostname 127.0.0.1 --port 57483",
        ppid=1452, me=1447823, parent_cmd="/usr/lib/systemd/systemd --user",
    ) is True


def test_flags_parent_already_gone():
    assert _is_orphaned_open_clank_agent_worker(
        f"{_BUNDLED_MIMO} acp", ppid=1, me=1447823, parent_cmd=None
    ) is True


def test_spares_own_child():
    assert _is_orphaned_open_clank_agent_worker(
        f"{_BUNDLED_MIMO} acp",
        ppid=1447823,
        me=1447823,
        parent_cmd="anything",
    ) is False


def test_spares_concurrent_app_py_child():
    assert _is_orphaned_open_clank_agent_worker(
        f"{_BUNDLED_MIMO} acp", ppid=999999, me=1447823,
        parent_cmd="/usr/bin/python /workspace/open-clank/app.py",
    ) is False


def test_ignores_personal_mimo_serve():
    assert _is_orphaned_open_clank_agent_worker(
        "/workspace/bin/mimo serve --hostname 127.0.0.1 --port 0",
        ppid=1,
        me=1447823,
        parent_cmd=None,
    ) is False


def test_ignores_other_process():
    assert _is_orphaned_open_clank_agent_worker(
        "/usr/bin/python something_else", ppid=1, me=1447823, parent_cmd=None
    ) is False


if __name__ == "__main__":
    test_flags_subreaper_orphan()
    test_flags_parent_already_gone()
    test_spares_own_child()
    test_spares_concurrent_app_py_child()
    test_ignores_personal_mimo_serve()
    test_ignores_other_process()
    print("ok: all predicate checks passed")
