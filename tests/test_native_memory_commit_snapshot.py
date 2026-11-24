import copy

import pytest

from src.memory import MemoryManager


def test_owner_snapshot_preserves_other_owner_and_rejects_stale_decision(tmp_path):
    manager = MemoryManager(str(tmp_path))
    alice = manager.add_entry("alice original", owner="alice")
    bob = manager.add_entry("bob original", owner="bob")
    manager.save([alice, bob])
    before = copy.deepcopy(manager.load_all())
    proposed = copy.deepcopy(before)
    proposed[0]["text"] = "alice changed"
    current = copy.deepcopy(before)
    current[1]["text"] = "bob concurrent"
    manager.save(current)
    manager.commit_owner_snapshot(before, proposed, {"alice"})
    by_owner = {row["owner"]: row for row in manager.load_all()}
    assert by_owner["alice"]["text"] == "alice changed"
    assert by_owner["bob"]["text"] == "bob concurrent"
    with pytest.raises(ValueError, match="snapshot changed"):
        manager.commit_owner_snapshot(before, proposed, {"alice"})
    assert {row["owner"]: row["text"] for row in manager.load_all()} == {
        "alice": "alice changed", "bob": "bob concurrent"
    }
