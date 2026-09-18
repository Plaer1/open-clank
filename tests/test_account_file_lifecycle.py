"""Focused closure for crash-replayable file-backed account owners."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.openclank.account_file_lifecycle import (
    AccountFileLifecycleError,
    AccountFileLifecyclePaths,
    AccountFileOwnerLifecycle,
)


SKILL = """---
name: private-procedure
description: private
owner: {owner}
---

# Procedure

{secret}
"""


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _adapter(tmp_path: Path) -> AccountFileOwnerLifecycle:
    return AccountFileOwnerLifecycle(
        AccountFileLifecyclePaths(
            preferences_file=tmp_path / "user_prefs.json",
            completed_research_dir=tmp_path / "deep_research",
            legacy_memory_file=tmp_path / "memory.json",
            skills_dir=tmp_path / "skills",
            lock_marker=tmp_path / "lifecycle" / "state",
        )
    )


def _seed(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "user_prefs.json",
        {
            "global": "stable",
            "_users": {
                "Alice": {"private": "prefs-secret"},
                "bob": {"private": "bob-prefs"},
            },
        },
    )
    _write_json(
        tmp_path / "deep_research" / "alice.json",
        {"owner": "Alice", "result": "research-secret"},
    )
    _write_json(
        tmp_path / "deep_research" / "bob.json",
        {"owner": "bob", "result": "bob-research"},
    )
    _write_json(
        tmp_path / "memory.json",
        [
            {"owner": "alice", "text": "memory-secret"},
            {"owner": "bob", "text": "bob-memory"},
        ],
    )
    skill = tmp_path / "skills" / "general" / "private" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(SKILL.format(owner="'Alice'", secret="skill-secret"), encoding="utf-8")
    bob_skill = tmp_path / "skills" / "general" / "bob" / "SKILL.md"
    bob_skill.parent.mkdir(parents=True, exist_ok=True)
    bob_skill.write_text(SKILL.format(owner="bob", secret="bob-skill"), encoding="utf-8")
    _write_json(
        tmp_path / "skills" / "_usage.json",
        {
            "Alice::private-procedure": {"uses": 7},
            "bob::bob-procedure": {"uses": 2},
        },
    )


def _owners(tmp_path: Path) -> dict[str, object]:
    prefs = json.loads((tmp_path / "user_prefs.json").read_text(encoding="utf-8"))
    research = {
        path.name: json.loads(path.read_text(encoding="utf-8"))["owner"]
        for path in sorted((tmp_path / "deep_research").glob("*.json"))
    }
    memory = json.loads((tmp_path / "memory.json").read_text(encoding="utf-8"))
    usage = json.loads((tmp_path / "skills" / "_usage.json").read_text(encoding="utf-8"))
    return {
        "prefs": prefs,
        "research": research,
        "memory": memory,
        "usage": usage,
    }


def test_preview_is_exact_and_contains_no_user_content(tmp_path):
    _seed(tmp_path)
    lifecycle = _adapter(tmp_path)

    manifest = lifecycle.preview_rename("alice", "deleted:subject-1")

    assert manifest["source"]["counts"] == {
        "preferences": 1,
        "completed_research": 1,
        "legacy_memory": 1,
        "skill_documents": 1,
        "skill_usage": 1,
    }
    assert manifest["source"]["count"] == 5
    encoded = json.dumps(manifest)
    for secret in (
        "prefs-secret",
        "research-secret",
        "memory-secret",
        "skill-secret",
        "private-procedure",
        "alice.json",
    ):
        assert secret not in encoded
    assert all(
        token.startswith("sha256:") and len(token) == 71
        for token in manifest["closure"]["items"]
    )


def test_stage_verify_compensate_and_replay_preserve_other_owner(tmp_path):
    _seed(tmp_path)
    lifecycle = _adapter(tmp_path)
    tombstone = "deleted:subject-1"
    manifest = lifecycle.preview_rename("alice", tombstone)
    bob_before = lifecycle.owner_inventory("bob")

    staged = lifecycle.stage_to_tombstone("alice", tombstone, manifest)
    assert staged["state"] == "staged"
    assert lifecycle.verify("alice", tombstone, manifest, expected="staged") == staged
    # Applying the same durable operation after restart is a no-op.
    assert lifecycle.stage_to_tombstone("alice", tombstone, manifest) == staged
    assert lifecycle.owner_inventory("bob") == bob_before

    owners = _owners(tmp_path)
    assert "alice" not in {str(key).lower() for key in owners["prefs"]["_users"]}
    assert tombstone in owners["prefs"]["_users"]
    assert owners["research"]["alice.json"] == tombstone
    assert owners["memory"][0]["owner"] == tombstone
    assert tombstone + "::private-procedure" in owners["usage"]
    skill_text = (tmp_path / "skills" / "general" / "private" / "SKILL.md").read_text()
    assert "owner: 'deleted:subject-1'" in skill_text
    assert "bob-skill" in (tmp_path / "skills" / "general" / "bob" / "SKILL.md").read_text()

    restored = lifecycle.compensate("alice", tombstone, manifest)
    assert restored["state"] == "restored"
    assert lifecycle.compensate("alice", tombstone, manifest) == restored
    assert lifecycle.verify("alice", tombstone, manifest, expected="restored") == restored
    assert lifecycle.owner_inventory("bob") == bob_before


def test_reconcile_accepts_a_valid_partial_crash_split(tmp_path):
    _seed(tmp_path)
    lifecycle = _adapter(tmp_path)
    tombstone = "deleted:subject-1"
    manifest = lifecycle.preview_rename("alice", tombstone)

    # Simulate a process death after one standalone report was atomically
    # rewritten but before the remaining authorities were staged.
    report = tmp_path / "deep_research" / "alice.json"
    value = json.loads(report.read_text(encoding="utf-8"))
    value["owner"] = tombstone
    _write_json(report, value)

    result = lifecycle.reconcile_rename("alice", tombstone, manifest)
    assert result["state"] == "staged"
    assert result["source"]["count"] == 0
    assert result["target"]["fingerprint"] == manifest["closure"]["fingerprint"]


def test_completed_research_inventory_uses_the_authoritative_top_level_only(tmp_path):
    _write_json(
        tmp_path / "deep_research" / "top.json",
        {"owner": "alice", "result": "report"},
    )
    _write_json(
        tmp_path / "deep_research" / "internal" / "metadata.json",
        {"owner": "alice", "result": "not a completed report"},
    )
    lifecycle = _adapter(tmp_path)

    manifest = lifecycle.preview_rename("alice", "renamed")
    lifecycle.rename_owner("alice", "renamed", manifest)

    assert json.loads(
        (tmp_path / "deep_research" / "top.json").read_text(encoding="utf-8")
    )["owner"] == "renamed"
    assert json.loads(
        (tmp_path / "deep_research" / "internal" / "metadata.json").read_text(
            encoding="utf-8"
        )
    )["owner"] == "alice"


def test_target_content_or_post_preview_mutation_fails_closed(tmp_path):
    _seed(tmp_path)
    lifecycle = _adapter(tmp_path)
    tombstone = "deleted:subject-1"
    manifest = lifecycle.preview_rename("alice", tombstone)

    report = tmp_path / "deep_research" / "collision.json"
    _write_json(report, {"owner": tombstone, "result": "unrelated"})
    before = _owners(tmp_path)

    with pytest.raises(AccountFileLifecycleError, match="conflicts"):
        lifecycle.stage_to_tombstone("alice", tombstone, manifest)
    assert _owners(tmp_path) == before

    with pytest.raises(AccountFileLifecycleError, match="already contains"):
        lifecycle.preview_rename("alice", tombstone)


def test_purge_replays_from_an_expected_subset_and_leaves_no_owner_files(tmp_path):
    _seed(tmp_path)
    lifecycle = _adapter(tmp_path)
    tombstone = "deleted:subject-1"
    manifest = lifecycle.preview_rename("alice", tombstone)
    lifecycle.stage_to_tombstone("alice", tombstone, manifest)
    bob_before = lifecycle.owner_inventory("bob")

    # Simulate a crash after one owned standalone file was deleted.
    (tmp_path / "deep_research" / "alice.json").unlink()
    result = lifecycle.purge_owner(tombstone, manifest)

    assert result["state"] == "purged"
    assert result["after"]["count"] == 0
    assert result["deleted_now_counts"]["completed_research"] == 0
    assert result["deleted_total_counts"] == manifest["closure"]["counts"]
    assert lifecycle.purge_owner(tombstone, manifest)["after"]["count"] == 0
    assert lifecycle.owner_inventory("bob") == bob_before
    assert not (tmp_path / "skills" / "general" / "private" / "SKILL.md").exists()
    owners = _owners(tmp_path)
    assert all(row["owner"] == "bob" for row in owners["memory"])
    assert set(owners["prefs"]["_users"]) == {"bob"}
    assert set(owners["usage"]) == {"bob::bob-procedure"}


def test_purge_rejects_new_owner_state_after_preview(tmp_path):
    _seed(tmp_path)
    lifecycle = _adapter(tmp_path)
    expected = lifecycle.owner_inventory("alice")
    _write_json(
        tmp_path / "deep_research" / "new.json",
        {"owner": "alice", "result": "arrived after preview"},
    )

    with pytest.raises(AccountFileLifecycleError, match="conflicts"):
        lifecycle.purge_owner("alice", expected)
    assert (tmp_path / "deep_research" / "new.json").exists()


@pytest.mark.parametrize(
    ("relative", "value", "message"),
    [
        ("user_prefs.json", {"legacy": "flat"}, "no exact owner"),
        ("memory.json", {"owner": "alice"}, "wrong schema"),
        ("skills/_usage.json", [], "wrong schema"),
    ],
)
def test_malformed_authorities_fail_closed(tmp_path, relative, value, message):
    _write_json(tmp_path / relative, value)
    lifecycle = _adapter(tmp_path)

    with pytest.raises(AccountFileLifecycleError, match=message):
        lifecycle.owner_inventory("alice")


def test_duplicate_preference_aliases_fail_closed(tmp_path):
    _write_json(
        tmp_path / "user_prefs.json",
        {"_users": {"alice": {"a": 1}, "Alice": {"a": 2}}},
    )

    with pytest.raises(AccountFileLifecycleError, match="duplicate owner"):
        _adapter(tmp_path).owner_inventory("alice")
