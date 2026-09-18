"""Generation-dir reclaim: the retire path must free a retired worker's on-disk
generation tree but never the active worker's, and must refuse paths outside the
managed owners root. Guards the fix for the ~637-dir / multi-hundred-MB leak.
"""
from pathlib import Path

from src.openclank.mimo_supervisor import MimoSupervisorPool


def _bare_pool(owners_root: Path) -> MimoSupervisorPool:
    # Bypass the heavy constructor; only the reclaim helpers' dependencies matter.
    pool = object.__new__(MimoSupervisorPool)
    pool._owners_root = owners_root
    pool._states = {}
    pool._locks = {}
    return pool


class _FakeWorker:
    def __init__(self, runtime_home):
        self._runtime_home = runtime_home


def test_retired_generation_dir_is_removed(tmp_path):
    owners = tmp_path / "owners"
    gen_dir = owners / "abc" / "generations" / "3-deadbeef"
    gen_dir.mkdir(parents=True)
    (gen_dir / "mimocode").mkdir()
    (gen_dir / "mimocode" / "state.json").write_text("{}")

    pool = _bare_pool(owners)
    pool._reclaim_generation_dir("abc", _FakeWorker(gen_dir))

    assert not gen_dir.exists(), "retired generation dir should be removed"


def test_active_generation_dir_is_preserved(tmp_path):
    owners = tmp_path / "owners"
    live_dir = owners / "abc" / "generations" / "20-alive"
    live_dir.mkdir(parents=True)

    pool = _bare_pool(owners)
    state = pool._owner_state("abc")
    state.active = _FakeWorker(live_dir)  # the live worker still owns this dir

    pool._reclaim_generation_dir("abc", state.active)

    assert live_dir.exists(), "the active worker's dir must never be reclaimed"


def test_path_outside_owners_root_is_refused(tmp_path):
    owners = tmp_path / "owners"
    owners.mkdir()
    rogue = tmp_path / "elsewhere" / "generations" / "1-stolen"
    rogue.mkdir(parents=True)

    pool = _bare_pool(owners)
    pool._reclaim_generation_dir("abc", _FakeWorker(rogue))

    assert rogue.exists(), "a path outside the managed owners root must be left alone"


def test_missing_runtime_home_is_a_noop(tmp_path):
    pool = _bare_pool(tmp_path / "owners")
    pool._reclaim_generation_dir("abc", _FakeWorker(None))  # must not raise


def test_startup_reclaims_every_stopped_generation_without_database_authority(
    tmp_path,
):
    owners = tmp_path / "owners"
    pool = _bare_pool(owners)
    generation = owners / "owner-a" / "generations" / "1-stopped"
    another = owners / "owner-b" / "generations" / "9-stopped"
    generation.mkdir(parents=True)
    another.mkdir(parents=True)
    (generation / "engine-state").write_text("regenerable", encoding="utf-8")
    (another / "engine-state").write_text("regenerable", encoding="utf-8")

    pool._reclaim_retired_generations()
    assert not generation.exists()
    assert not another.exists()


if __name__ == "__main__":
    import tempfile, sys
    with tempfile.TemporaryDirectory() as d:
        for fn in (
            test_retired_generation_dir_is_removed,
            test_active_generation_dir_is_preserved,
            test_path_outside_owners_root_is_refused,
            test_missing_runtime_home_is_a_noop,
        ):
            # pytest drives tmp_path; for __main__ smoke, make a stand-in
            class T:
                def __init__(self, p): self.p = Path(p)
                def __truediv__(self, o): return self.p / o
            fn(T(d))  # type: ignore[arg-type]
            print("ok:", fn.__name__)
    sys.exit(0)
