"""Lore stores references to reviewed guidance; the Hex library owns every revision."""
from services.memory.memery_history import _query_batch, read_guidance_revision
from src.general_hex_library import GeneralHexRepository, ensure_general_hex_schema


def test_guidance_history_follows_review_retirement_and_owner_erasure(tmp_path):
    path = tmp_path / "memery.sqlite"
    ensure_general_hex_schema(path)
    repo = GeneralHexRepository(path)
    active = repo.create("alice", title="Publish", body="Publish on Friday", tags=[])
    hex_id = active["hex_id"]
    retired = repo.update("alice", hex_id, expected_revision=1, enabled=False)
    repo.create("other", title="Private", body="Other owner's rule", tags=[])
    proposed = repo.create("alice", authorship="agent", title="Draft", body="Unreviewed", tags=[])
    cursor = {"position": 0, "witness": "", "entries": []}
    position, witness, entries, removed = _query_batch(path, "alice", "", cursor, guidance=True)
    assert [(item["block"], item["revision"]) for item in entries] == [(hex_id, 1), (hex_id, 2)]
    assert not removed
    assert read_guidance_revision(path, "alice", hex_id, 2) == (
        entries[1], {"title": retired["title"], "body": retired["body"], "revision": 2, "enabled": False})
    assert read_guidance_revision(path, "other", hex_id, 1) is None
    assert read_guidance_revision(path, "alice", proposed["hex_id"], 1) is None
    assert "body" not in entries[1]

    cursor = dict(position=position, witness=witness, entries=entries)
    restored = repo.restore("alice", hex_id, revision=1, expected_revision=2)
    assert restored["revision"] == 3 and restored["enabled"]
    position, witness, added, removed = _query_batch(path, "alice", "", cursor, guidance=True)
    assert [item["revision"] for item in added] == [3]
    assert not removed
    cursor = dict(position=position, witness=witness, entries=entries + added)
    repo.delete("alice", hex_id, expected_revision=3)
    _, _, added, removed = _query_batch(path, "alice", "", cursor, guidance=True)
    assert not added
    assert set(removed) == {item["change"] for item in cursor["entries"]}
    assert read_guidance_revision(path, "alice", hex_id, 1) is None
