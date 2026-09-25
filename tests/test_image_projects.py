"""Managed Imps image project records — versioned state, Lore recovery, export.

These cover the S19 managed project substrate: creation bound to a stable
image resource identity, optimistic-concurrency state commits, the recoverable
Save operation (Lore preimages before mutation), Save-a-copy allocation, and
the explicit portable export/import round-trip including blank canvases.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ManagedImageProject
from src.openclank.image_projects import (
    EXPORT_KIND,
    EXPORT_SCHEMA_VERSION,
    ImageProjectError,
    ImageProjectRepository,
    ImageResourceIdentity,
    LoreCaptureFailed,
    ProjectNotFound,
    StaleImageRevision,
    StaleProjectRevision,
)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'projects.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


IDENTITY = ImageResourceIdentity("gallery", "image-1")


def test_create_project_binds_owner_and_stable_image_identity(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    record = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        name="Sunset",
        width=640,
        height=480,
        state={"layers": [{"id": 1}]},
        expected_image_revision="rev-a",
    )
    assert record.owner == "alice"
    assert record.image_identity == IDENTITY
    assert record.expected_image_revision == "rev-a"
    assert record.project_revision == 1
    assert record.state["layers"] == [{"id": 1}]
    assert record.state["v"] >= 2
    assert record.is_active is True


def test_stable_identity_survives_rename_and_matches_across_calls():
    a = ImageResourceIdentity("gallery", "image-1").stable_id(owner="alice")
    b = ImageResourceIdentity("gallery", "image-1").stable_id(owner="alice")
    assert a == b
    # A different resource is a different identity.
    assert ImageResourceIdentity("gallery", "image-2").stable_id(owner="alice") != a
    # A different owner is a different identity.
    assert ImageResourceIdentity("gallery", "image-1").stable_id(owner="bob") != a


def test_blank_canvas_project_persists_and_round_trips(tmp_path):
    """A blank-canvas draft (no base image layer) is first-class state."""
    repo = ImageProjectRepository(_factory(tmp_path))
    blank_state = {
        "v": 2,
        "imageId": None,
        "imgWidth": 800,
        "imgHeight": 600,
        "layers": [],
        "activeLayerId": None,
        "nextLayerId": 1,
    }
    record = repo.create_project(
        owner="alice",
        image_identity=ImageResourceIdentity("gallery", "draft-blank"),
        name="Untitled",
        width=800,
        height=600,
        state=blank_state,
    )
    assert record.state["layers"] == []
    fetched = repo.get_project(project_id=record.id, owner="alice")
    assert fetched.state["layers"] == []
    assert fetched.width == 800
    assert fetched.height == 600


def test_update_state_requires_matching_project_revision(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    record = repo.create_project(owner="alice", image_identity=IDENTITY)

    updated = repo.update_state(
        project_id=record.id,
        owner="alice",
        state={"layers": [{"id": 1}, {"id": 2}]},
        expected_project_revision=1,
    )
    assert updated.project_revision == 2
    assert len(updated.state["layers"]) == 2

    with pytest.raises(StaleProjectRevision):
        repo.update_state(
            project_id=record.id,
            owner="alice",
            state={"layers": []},
            expected_project_revision=1,  # stale
        )


def test_update_state_rejects_stale_image_revision(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    record = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        expected_image_revision="rev-a",
    )
    with pytest.raises(StaleImageRevision):
        repo.update_state(
            project_id=record.id,
            owner="alice",
            state={"layers": []},
            expected_project_revision=1,
            expected_image_revision="rev-b",  # does not match bound revision
        )


def test_owner_isolation_for_read_and_write(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    record = repo.create_project(owner="alice", image_identity=IDENTITY)

    with pytest.raises(ProjectNotFound):
        repo.get_project(project_id=record.id, owner="bob")
    with pytest.raises(ProjectNotFound):
        repo.update_state(
            project_id=record.id,
            owner="bob",
            state={},
            expected_project_revision=1,
        )


def test_find_for_image_returns_newest_and_none_for_unbound(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    other = ImageResourceIdentity("gallery", "image-other")
    repo.create_project(owner="alice", image_identity=other)
    assert repo.find_for_image(owner="alice", image_identity=IDENTITY) is None

    first = repo.create_project(owner="alice", image_identity=IDENTITY, name="One")
    second = repo.create_project(owner="alice", image_identity=IDENTITY, name="Two")
    found = repo.find_for_image(owner="alice", image_identity=IDENTITY)
    assert found is not None
    assert {first.id, second.id} >= {found.id}


# ── recoverable Save ─────────────────────────────────────────────────────


def test_save_captures_preimages_before_mutating(tmp_path):
    """Lore sees the original image revision and original project state."""
    events = []

    def lore_capture(**kwargs):
        events.append(kwargs)

    repo = ImageProjectRepository(_factory(tmp_path), lore_capture=lore_capture)
    record = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        state={"layers": [{"id": "base"}]},
        expected_image_revision="rev-old",
    )

    writes = []

    def image_writer():
        writes.append("write")
        return "rev-new"

    outcome = repo.save_image_and_project(
        owner="alice",
        project_id=record.id,
        expected_project_revision=1,
        expected_image_revision="rev-old",
        new_image_revision="rev-new",
        image_writer=image_writer,
        state={"layers": [{"id": "base"}, {"id": "paint"}]},
    )

    assert outcome.allocated is False
    assert outcome.image_identity == IDENTITY  # identity preserved
    assert outcome.project_revision == 2
    assert writes == ["write"]

    # Preimage captured before the image write.
    assert len(events) == 1
    captured = json.loads(events[0]["preimage"])
    assert captured["kind"] == "imps_project_preimage"
    assert captured["image_revision"] == "rev-old"
    assert captured["project_revision"] == 1
    assert captured["state"]["layers"] == [{"id": "base"}]
    assert events[0]["operation_id"] == outcome.action_id


def test_save_refuses_when_preimage_capture_fails(tmp_path):
    """A Save that cannot be recovered is refused before any mutation."""
    calls = []

    def lore_capture(**kwargs):
        raise RuntimeError("history worker unavailable")

    repo = ImageProjectRepository(_factory(tmp_path), lore_capture=lore_capture)
    record = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        state={"layers": [{"id": "base"}]},
        expected_image_revision="rev-old",
    )

    def image_writer():
        calls.append("write")
        return "rev-new"

    with pytest.raises(LoreCaptureFailed):
        repo.save_image_and_project(
            owner="alice",
            project_id=record.id,
            expected_project_revision=1,
            expected_image_revision="rev-old",
            new_image_revision="rev-new",
            image_writer=image_writer,
            state={"layers": []},
        )
    # The image was never written and the project state is unchanged.
    assert calls == []
    unchanged = repo.get_project(project_id=record.id, owner="alice")
    assert unchanged.project_revision == 1
    assert unchanged.state["layers"] == [{"id": "base"}]
    assert unchanged.expected_image_revision == "rev-old"


def test_save_rejects_stale_project_or_image_revision_before_capture(tmp_path):
    events = []

    def lore_capture(**kwargs):
        events.append(kwargs)

    repo = ImageProjectRepository(_factory(tmp_path), lore_capture=lore_capture)
    record = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        expected_image_revision="rev-old",
    )

    with pytest.raises(StaleProjectRevision):
        repo.save_image_and_project(
            owner="alice",
            project_id=record.id,
            expected_project_revision=99,
            expected_image_revision="rev-old",
            new_image_revision="rev-new",
            image_writer=lambda: "rev-new",
        )
    with pytest.raises(StaleImageRevision):
        repo.save_image_and_project(
            owner="alice",
            project_id=record.id,
            expected_project_revision=1,
            expected_image_revision="rev-wrong",
            new_image_revision="rev-new",
            image_writer=lambda: "rev-new",
        )
    # No Lore capture for a refused save.
    assert events == []


def test_save_rebinds_project_to_new_image_revision(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    record = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        expected_image_revision="rev-old",
    )
    outcome = repo.save_image_and_project(
        owner="alice",
        project_id=record.id,
        expected_project_revision=1,
        expected_image_revision="rev-old",
        new_image_revision="rev-new",
        image_writer=lambda: "rev-new",
    )
    assert outcome.refresh_receipt["image_revision"] == "rev-new"
    bound = repo.get_project(project_id=record.id, owner="alice")
    assert bound.expected_image_revision == "rev-new"
    assert bound.project_revision == 2


# ── Save a copy ──────────────────────────────────────────────────────────


def test_save_copy_allocates_separate_resource_and_project(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    source = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        name="Original",
        state={"layers": [{"id": "base"}]},
        expected_image_revision="rev-a",
    )
    copy_identity = ImageResourceIdentity("gallery", "image-2")

    outcome = repo.save_copy(
        owner="alice",
        project_id=source.id,
        image_identity=copy_identity,
        expected_image_revision="rev-copy",
        image_writer=lambda: "rev-copy",
    )

    assert outcome.allocated is True
    assert outcome.image_identity == copy_identity
    assert outcome.project_id != source.id

    # Source is untouched.
    untouched = repo.get_project(project_id=source.id, owner="alice")
    assert untouched.expected_image_revision == "rev-a"
    assert untouched.name == "Original"
    assert untouched.project_revision == 1

    # Copy is independent.
    copied = repo.get_project(project_id=outcome.project_id, owner="alice")
    assert copied.image_identity == copy_identity
    assert copied.expected_image_revision == "rev-copy"
    assert copied.name.endswith("copy")


# ── portable export / import ─────────────────────────────────────────────


def test_export_import_round_trips_layers_text_and_masks(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    state = {
        "v": 2,
        "layers": [
            {"id": 1, "name": "base", "visible": True, "opacity": 1, "isBase": True},
            {"id": 2, "name": "paint", "visible": True, "opacity": 0.5},
        ],
        "masks": {"2": {"op": "add", "d": "AAAA"}},
        "text": {"3": {"str": "hello", "x": 10, "y": 20}},
    }
    record = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        name="Layers",
        width=100,
        height=50,
        state=state,
        expected_image_revision="rev-a",
    )

    bundle = repo.export_project(
        project_id=record.id,
        owner="alice",
        assets=[{"id": "blob-1", "sha256": "aa", "data_base64": "YQ=="}],
    )
    assert bundle["kind"] == EXPORT_KIND
    assert bundle["schema_version"] == EXPORT_SCHEMA_VERSION
    assert bundle["image"] == {**IDENTITY.as_key(), "revision": "rev-a"}
    assert bundle["state"]["layers"] == state["layers"]
    assert bundle["state"]["masks"] == state["masks"]
    assert bundle["state"]["text"] == state["text"]
    assert bundle["assets"][0]["id"] == "blob-1"

    restored = repo.import_project(
        owner="alice",
        image_identity=ImageResourceIdentity("gallery", "image-restored"),
        bundle=bundle,
    )
    assert restored.name == "Layers"
    assert restored.state["layers"] == state["layers"]
    assert restored.state["masks"] == state["masks"]
    assert restored.state["text"] == state["text"]
    assert restored.image_identity.resource_id == "image-restored"


def test_export_import_round_trips_blank_canvas(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    blank = {"v": 2, "layers": [], "imgWidth": 32, "imgHeight": 32}
    record = repo.create_project(
        owner="alice",
        image_identity=ImageResourceIdentity("gallery", "draft-blank"),
        width=32,
        height=32,
        state=blank,
    )
    bundle = repo.export_project(project_id=record.id, owner="alice")
    assert bundle["state"]["layers"] == []
    restored = repo.import_project(
        owner="alice",
        image_identity=ImageResourceIdentity("gallery", "draft-blank-2"),
        bundle=bundle,
    )
    assert restored.state["layers"] == []
    assert restored.width == 32


def test_import_rejects_foreign_bundle_and_preserves_unknown_keys(tmp_path):
    repo = ImageProjectRepository(_factory(tmp_path))
    with pytest.raises(ImageProjectError):
        repo.import_project(
            owner="alice",
            image_identity=IDENTITY,
            bundle={"kind": "not-imps", "state": {}},
        )

    # Unknown future keys survive the round-trip.
    bundle = {
        "kind": EXPORT_KIND,
        "schema_version": 99,
        "name": "Future",
        "state": {"v": 2, "layers": [], "futureKey": {"x": 1}},
        "image": {"provider": "gallery", "resource_id": "r", "revision": "rev"},
    }
    restored = repo.import_project(owner="alice", image_identity=IDENTITY, bundle=bundle)
    assert restored.state["futureKey"] == {"x": 1}


# ── storage shape ────────────────────────────────────────────────────────


def test_state_is_stored_as_json_text(tmp_path):
    factory = _factory(tmp_path)
    repo = ImageProjectRepository(factory)
    record = repo.create_project(
        owner="alice",
        image_identity=IDENTITY,
        state={"layers": [{"id": 1}]},
    )
    db = factory()
    try:
        row = db.query(ManagedImageProject).filter(ManagedImageProject.id == record.id).one()
        assert isinstance(row.state, str)
        parsed = json.loads(row.state)
        assert parsed["layers"] == [{"id": 1}]
    finally:
        db.close()
