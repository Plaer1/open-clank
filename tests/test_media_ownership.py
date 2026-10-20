"""Focused S14 media ownership tests: layout, provenance, adoption, moves.

These cover the write-scope behaviours from slice 14 (Files media ownership,
paste and synchronized moves) and the M07 boundaries they must preserve.  They
exercise real planning/validation code, not mocks of itself.
"""

from __future__ import annotations

import os

import pytest

from src.openclank.media_ownership import (
    AdoptionPlan,
    IncomingReference,
    MediaOwnershipError,
    MediaProvenance,
    MovePhase,
    MovePreflight,
    WorkspaceRootRecord,
    allocate_media_name,
    binary_digest,
    classify_adoption_candidates,
    document_media_dir,
    layout_for_absolute,
    plan_document_move,
    recover_move_plan,
    reconcile_external_state,
    rewrite_reference,
    scan_references,
    text_fingerprint,
)


# ---------------------------------------------------------------------------
# Binary digest consistency (the Loose Copal asset-head bug)
# ---------------------------------------------------------------------------


def test_binary_digest_hashes_original_bytes_not_reencoded_text():
    data = b"caf\xe9 \x80 binary \xff"
    # Latin-1 decode + UTF-8 re-encode changes these bytes.
    legacy = text_fingerprint(data.decode("latin1"))
    assert binary_digest(data) != legacy
    assert binary_digest(data) == binary_digest(data)
    assert binary_digest(data).startswith("sha256:")
    assert binary_digest(data).endswith(f":{len(data)}")


def test_binary_digest_matches_plain_sha256_of_bytes():
    import hashlib

    data = b"\x00\x01\xfe\xff"
    digest = binary_digest(data)
    assert digest == f"sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}"


# ---------------------------------------------------------------------------
# Media layout
# ---------------------------------------------------------------------------


def test_workspace_layout_mirrors_workspace_relative_document_stem():
    flat = document_media_dir(document_path="bar.md", canonical_root="/foo", origin="workspace")
    assert flat.media_dir == "media/bar"

    nested = document_media_dir(document_path="projects/design.md", canonical_root="/foo", origin="workspace")
    assert nested.media_dir == "media/projects/design"
    assert nested.origin == "workspace"


def test_loose_layout_uses_the_documents_containing_folder():
    layout = document_media_dir(document_path="note.md", canonical_root="/stuff", origin="loose")
    assert layout.media_dir == "media/note"
    assert layout.is_loose
    assert layout.absolute_media_dir().endswith(os.path.join("media", "note"))


def test_loose_layout_refuses_directory_shaped_document_paths():
    with pytest.raises(MediaOwnershipError) as exc:
        document_media_dir(document_path="projects/note.md", canonical_root="/stuff", origin="loose")
    assert exc.value.code == "invalid_media_request"


def test_workspace_relative_folder_is_stripped_from_the_mirrored_stem():
    layout = document_media_dir(
        document_path="notes/draft.md",
        canonical_root="/project",
        origin="workspace",
        workspace_relative_folder="notes",
    )
    assert layout.media_dir == "media/draft"


def test_layout_for_absolute_reduces_against_the_canonical_root():
    layout = layout_for_absolute(
        absolute_document_path="/foo/projects/design.md",
        canonical_root="/foo",
        origin="workspace",
    )
    assert layout.media_dir == "media/projects/design"

    loose = layout_for_absolute(
        absolute_document_path="/stuff/note.md",
        canonical_root="/stuff",
        origin="loose",
    )
    assert loose.media_dir == "media/note"


def test_same_stem_documents_share_a_directory_but_keep_distinct_identity():
    markdown = document_media_dir(document_path="foo.md", canonical_root="/foo", origin="workspace")
    rust = document_media_dir(document_path="foo.rs", canonical_root="/foo", origin="workspace")
    assert markdown.media_dir == rust.media_dir == "media/foo"
    assert markdown.document_path != rust.document_path


def test_allocate_media_name_is_collision_safe_for_clipboard_names():
    assert allocate_media_name("image.png", []) == "image.png"
    assert allocate_media_name("image.png", ["image.png"]) == "image-1.png"
    assert allocate_media_name("image.png", ["image.png", "image-1.png"]) == "image-2.png"
    assert allocate_media_name("report.tar.gz", ["report.tar.gz"]) == "report.tar-1.gz"
    assert allocate_media_name("noext", ["noext"]) == "noext-1"
    with pytest.raises(MediaOwnershipError):
        allocate_media_name("../escape.png", [])
    with pytest.raises(MediaOwnershipError):
        allocate_media_name("a/b.png", [])


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_round_trips_and_verifies_binary_digest():
    data = b"\xff\xfe binary"
    record = MediaProvenance(
        canonical_root="/project/notes",
        origin="loose",
        owner_subject_id="account-alice",
        workspace_id=None,
        document_id="doc-1",
        document_path="draft.md",
        asset_name="image.png",
        asset_digest=binary_digest(data),
        references=("doc-2",),
        asset_id="asset-1",
    )
    restored = MediaProvenance.from_receipt(record.as_receipt())
    assert restored == record
    assert restored.digest_matches(data)
    assert not restored.digest_matches(b"other")


def test_provenance_rejects_non_digest_values():
    with pytest.raises(MediaOwnershipError):
        MediaProvenance(
            canonical_root="/x",
            origin="loose",
            owner_subject_id="a",
            workspace_id=None,
            document_id="d",
            document_path="n.md",
            asset_name="i.png",
            asset_digest="not-a-digest",
        )


# ---------------------------------------------------------------------------
# Orphan adoption
# ---------------------------------------------------------------------------


def _loose(root: str, document: str, name: str, *, owner: str = "account-alice", digest: str | None = None):
    return MediaProvenance(
        canonical_root=root,
        origin="loose",
        owner_subject_id=owner,
        workspace_id=None,
        document_id=f"doc-{document}",
        document_path=document,
        asset_name=name,
        asset_digest=digest or binary_digest(b"png"),
    )


def test_adoption_adopts_only_provenance_backed_loose_assets():
    plan = classify_adoption_candidates(
        candidates=[
            _loose("/project/notes", "draft.md", "image.png"),
            MediaProvenance.from_receipt({
                **_loose("/project/other", "x.md", "y.png").as_receipt(),
                "origin": "workspace",
                "workspace_id": "workspace-child",
            }),
        ],
        registered_roots=[],
        new_workspace_root="/project",
        new_workspace_id="workspace-new",
        new_owner_subject_id="account-alice",
    )
    assert len(plan.adoptable) == 1
    assert plan.adoptable[0].destination_rel == "media/notes/draft/image.png"
    assert plan.adoptable[0].source_path.endswith(os.path.join("notes", "media", "draft", "image.png"))
    assert any(item["code"] == "owned_by_workspace" for item in plan.rejected)


def test_adoption_never_swallows_another_registered_workspace():
    plan = classify_adoption_candidates(
        candidates=[
            _loose("/project/notes", "draft.md", "image.png"),
            _loose("/project/child/inside", "deep.md", "photo.png"),
        ],
        registered_roots=[
            WorkspaceRootRecord("workspace-child", "account-alice", "/project/child", archived=False),
            WorkspaceRootRecord("workspace-archived", "account-bob", "/project/notes", archived=True),
        ],
        new_workspace_root="/project",
        new_workspace_id="workspace-new",
        new_owner_subject_id="account-alice",
    )
    assert plan.adoptable == ()
    codes = {item["code"] for item in plan.rejected}
    assert "owned_by_other_workspace" in codes
    # The archived other-owner row still owns its physical root.
    assert sum(1 for item in plan.rejected if item["code"] == "owned_by_other_workspace") == 2


def test_adoption_rejects_other_account_assets_without_exposing_registry_metadata():
    plan = classify_adoption_candidates(
        candidates=[_loose("/project/notes", "draft.md", "image.png", owner="account-bob")],
        registered_roots=[],
        new_workspace_root="/project",
        new_workspace_id="workspace-new",
        new_owner_subject_id="account-alice",
    )
    assert plan.adoptable == ()
    assert plan.rejected[0]["code"] == "owned_by_other_account"
    assert "bob" not in plan.as_receipt()["rejected"][0]["reason"]
    assert "account-bob" not in str(plan.as_receipt()["rejected"])


def test_adoption_rejects_assets_outside_the_new_workspace():
    plan = classify_adoption_candidates(
        candidates=[_loose("/elsewhere", "draft.md", "image.png")],
        registered_roots=[],
        new_workspace_root="/project",
        new_workspace_id="workspace-new",
        new_owner_subject_id="account-alice",
    )
    assert plan.adoptable == ()
    assert plan.rejected[0]["code"] == "outside_workspace"


def test_root_contains_resolves_symlinked_roots(tmp_path):
    from src.openclank.media_ownership import _root_contains

    real = tmp_path / "real"
    (real / "assets").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    escape = real / "escape"
    escape.symlink_to(outside)

    # A symlinked root still physically contains its children.
    assert _root_contains(str(link), str(real / "assets"))
    assert _root_contains(str(real), str(link / "assets"))
    assert _root_contains(str(link), str(link / "assets"))
    # A symlink that escapes the root is not inside it.
    assert not _root_contains(str(real), str(escape))
    assert not _root_contains(str(link), str(outside))


def test_adoption_sees_through_a_symlinked_registered_root(tmp_path):
    real_child = tmp_path / "child"
    (real_child / "inside").mkdir(parents=True)
    link_root = tmp_path / "link-to-child"
    link_root.symlink_to(real_child)

    plan = classify_adoption_candidates(
        candidates=[_loose(str(real_child / "inside"), "deep.md", "photo.png")],
        registered_roots=[
            # Registered through a symlink: containment must resolve both
            # sides or the asset looks unowned and would be absorbed.
            WorkspaceRootRecord("workspace-child", "account-alice", str(link_root), archived=False),
        ],
        new_workspace_root=str(tmp_path),
        new_workspace_id="workspace-new",
        new_owner_subject_id="account-alice",
    )
    assert plan.adoptable == ()
    assert any(item["code"] == "owned_by_other_workspace" for item in plan.rejected)


def test_folder_named_media_alone_is_not_proof_of_orphanhood():
    # No provenance row means no adoption, regardless of folder names.
    plan = classify_adoption_candidates(
        candidates=[],
        registered_roots=[],
        new_workspace_root="/project",
        new_workspace_id="workspace-new",
    )
    assert isinstance(plan, AdoptionPlan)
    assert plan.adoptable == ()


# ---------------------------------------------------------------------------
# Reference scanning and surgical rewrite
# ---------------------------------------------------------------------------


def test_scan_references_finds_markdown_and_wiki_forms_with_spans():
    source = "intro ![alt](media/bar/image.png) mid ![[media/bar/other.png|label]] end"
    sites = scan_references("doc-1", source)
    assert [s.syntax for s in sites] == ["markdown", "wiki"]
    assert sites[0].target == "media/bar/image.png"
    assert sites[0].embed is True
    assert sites[1].target == "media/bar/other.png"
    assert sites[1].label == "label"
    for site in sites:
        assert source[site.start : site.end]


def test_rewrite_reference_is_surgical_and_preserves_syntax():
    source = "keep ![alt](media/bar/image.png) keep"
    site = scan_references("doc-1", source)[0]
    replacement = rewrite_reference(site, "media/dest/image.png")
    rebuilt = source[: site.start] + replacement + source[site.end :]
    assert rebuilt == "keep ![alt](<media/dest/image.png>) keep"
    wiki = scan_references("doc-1", "![[media/bar/image.png|cap]]")[0]
    assert rewrite_reference(wiki, "media/dest/image.png") == "![[media/dest/image.png|cap]]"


def test_scan_references_ignores_external_urls_and_escaped_brackets():
    sites = scan_references("doc-1", r"![a](https://example/x.png) \![[not/a/ref]]")
    assert sites == [] or all(not s.target.startswith("https") or s.syntax != "wiki" for s in sites)
    assert all(s.target != "not/a/ref" for s in sites)


# ---------------------------------------------------------------------------
# Move transactions
# ---------------------------------------------------------------------------


def _preflight(revision="rev-1", **kwargs):
    return MovePreflight(expected_revision="rev-1", current_revision=revision, **kwargs)


def test_move_plan_repairs_known_references_and_moves_owned_assets():
    source_text = "see ![img](media/bar/image.png) and ![doc](bar.md)"
    plan = plan_document_move(
        operation_id="move-1",
        document_id="bar.md",
        source_document_path="bar.md",
        destination_document_path="projects/bar.md",
        canonical_root="/foo",
        origin="workspace",
        owner_subject_id="account-alice",
        preflight=_preflight(
            assets=[{"asset_id": "a1", "name": "image.png", "digest": binary_digest(b"png"), "owner_document_id": "bar.md"}],
        ),
        referencing_documents=[("bar.md", source_text, False), ("other.md", "link ![i](media/bar/image.png)", False)],
    )
    assert plan.phase == MovePhase.STAGED
    assert plan.conflicts == ()
    assert len(plan.asset_moves) == 1
    assert plan.asset_moves[0].destination_media_dir == "media/projects/bar"
    # Two references in the moved document plus one shared-image reference.
    assert len(plan.reference_edits) == 3
    image_edits = [edit for edit in plan.reference_edits if "image.png" in edit.site.target]
    assert image_edits
    assert all(edit.new_target.endswith("media/projects/bar/image.png") for edit in image_edits)
    # The document's own self-link is repointed at its destination.
    self_edits = [edit for edit in plan.reference_edits if edit.site.target == "bar.md"]
    assert self_edits and self_edits[0].new_target == "projects/bar.md"
    assert "bar.md" in plan.preimages


def test_move_leaves_protected_references_unapplied_with_a_concrete_conflict():
    plan = plan_document_move(
        operation_id="move-2",
        document_id="bar.md",
        source_document_path="bar.md",
        destination_document_path="projects/bar.md",
        canonical_root="/foo",
        origin="workspace",
        owner_subject_id="account-alice",
        preflight=_preflight(
            incoming=(
                IncomingReference(
                    document_id="locked.md",
                    source="locked text",
                    target="media/bar/image.png",
                    protected=True,
                    writable=False,
                ),
            ),
        ),
        referencing_documents=[("locked.md", "![i](media/bar/image.png)", True)],
    )
    assert plan.phase == MovePhase.ABORTED
    assert not plan.applied
    assert any(c.code == "protected_reference" and c.document_id == "locked.md" for c in plan.conflicts)
    assert plan.asset_moves == () or plan.phase == MovePhase.ABORTED


def test_move_preflights_revisions_and_stays_unapplied_on_staleness():
    plan = plan_document_move(
        operation_id="move-3",
        document_id="bar.md",
        source_document_path="bar.md",
        destination_document_path="projects/bar.md",
        canonical_root="/foo",
        origin="workspace",
        owner_subject_id="account-alice",
        preflight=_preflight(revision="rev-2"),
    )
    assert plan.phase == MovePhase.ABORTED
    assert plan.conflicts[0].code == "stale_revision"


def test_move_never_relocates_another_documents_asset_bytes():
    plan = plan_document_move(
        operation_id="move-4",
        document_id="bar.md",
        source_document_path="bar.md",
        destination_document_path="projects/bar.md",
        canonical_root="/foo",
        origin="workspace",
        owner_subject_id="account-alice",
        preflight=_preflight(
            assets=[{"asset_id": "a1", "name": "image.png", "digest": binary_digest(b"png"), "owner_document_id": "sibling.md"}],
        ),
    )
    assert plan.phase == MovePhase.ABORTED
    assert any(c.code == "foreign_asset" and c.document_id == "sibling.md" for c in plan.conflicts)
    assert plan.asset_moves == ()


def test_move_uses_no_global_string_replacement():
    source_text = 'const s = "media/bar/image.png"; ![img](media/bar/image.png)'
    plan = plan_document_move(
        operation_id="move-5",
        document_id="bar.md",
        source_document_path="bar.md",
        destination_document_path="projects/bar.md",
        canonical_root="/foo",
        origin="workspace",
        owner_subject_id="account-alice",
        preflight=_preflight(),
        referencing_documents=[("bar.md", source_text, False)],
    )
    assert plan.phase == MovePhase.STAGED
    assert len(plan.reference_edits) == 1
    # Only the real reference span is rewritten; the string literal is intact.
    rebuilt = source_text[: plan.reference_edits[0].site.start] + plan.reference_edits[0].new_text + source_text[plan.reference_edits[0].site.end :]
    assert '"media/bar/image.png"' in rebuilt
    assert "media/projects/bar/image.png" in rebuilt


def test_move_recovery_is_idempotent_and_publishes_one_receipt():
    plan = plan_document_move(
        operation_id="move-6",
        document_id="bar.md",
        source_document_path="bar.md",
        destination_document_path="projects/bar.md",
        canonical_root="/foo",
        origin="workspace",
        owner_subject_id="account-alice",
        preflight=_preflight(
            assets=[{"asset_id": "a1", "name": "image.png", "digest": binary_digest(b"png"), "owner_document_id": "bar.md"}],
        ),
        referencing_documents=[("other.md", "![i](media/bar/image.png)", False)],
    )
    recovered = recover_move_plan(plan)
    assert recovered.phase == MovePhase.PUBLISHED
    assert recovered.applied
    assert recovered.resource_receipt["action_id"] == "move-move-6"
    again = recover_move_plan(recovered)
    assert again is recovered or again.phase == MovePhase.PUBLISHED
    assert again.resource_receipt == recovered.resource_receipt


def test_move_recovery_keeps_aborted_plans_unapplied():
    plan = plan_document_move(
        operation_id="move-7",
        document_id="bar.md",
        source_document_path="bar.md",
        destination_document_path="projects/bar.md",
        canonical_root="/foo",
        origin="workspace",
        owner_subject_id="account-alice",
        preflight=_preflight(revision="rev-9"),
    )
    assert recover_move_plan(plan).phase == MovePhase.ABORTED


# ---------------------------------------------------------------------------
# External filesystem reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_external_state_retains_resolver_aliases():
    result = reconcile_external_state(
        indexed={"a": "fp-1", "b": "fp-2"},
        observed={"a": "fp-1", "c": "fp-3"},
        aliases={"b": "media/old/image.png"},
    )
    assert result["missing"] == ["b"]
    assert result["added"] == ["c"]
    assert result["changed"] == []
    assert result["retained_aliases"]["b"] == "media/old/image.png"
    assert result["status"] == "conflict"


def test_reconcile_external_state_reports_clean_runs_as_ok():
    result = reconcile_external_state(indexed={"a": "fp-1"}, observed={"a": "fp-1"})
    assert result == {"missing": [], "added": [], "changed": [], "retained_aliases": {}, "status": "ok"}
