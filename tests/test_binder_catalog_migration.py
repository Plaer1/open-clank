import json

import pytest

from scripts.binder_catalog_migration import PreflightError, inventory_vault, propose_catalog
from src.openclank.binder_catalog import BinderCatalogError, apply_conversion, prepare_storage_update, public_catalog, rollback_conversion, store_catalog


def test_offline_inventory_and_catalog_proposal_are_content_free(tmp_path):
    (tmp_path / ".copal").mkdir()
    (tmp_path / "section").mkdir()
    (tmp_path / "section" / "note.md").write_text("private content", encoding="utf-8")
    manifest = {"schemaVersion":1, "documents":{"note-1":{"owner":"alice", "name":"section/note.md", "path":"section/note.md", "kind":"note", "head":"h1"}}}
    (tmp_path / ".copal" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = inventory_vault(tmp_path, expected_owner="alice")
    assert "private content" not in json.dumps(result)
    assert not result["missingIds"] and result["complete"] is True
    (tmp_path / "section" / "note.md").write_text("[[missing]]", encoding="utf-8")
    assert inventory_vault(tmp_path, expected_owner="alice")["complete"] is False
    source = {"workspaceId":"space", "corpus":"notes", "root":["section"]}
    proposal = propose_catalog(account_id="account-a", catalog=None, binder_id="binder-a", display_name="Example", source=source, expected_revision=0)
    assert proposal["request"]["defaultId"] == "binder-a" and proposal["applyReady"] is False
    with pytest.raises(BinderCatalogError):
        propose_catalog(account_id="account-a", catalog=None, binder_id="binder-a", display_name="Example", source=source, expected_revision=1)
    with pytest.raises(PreflightError):
        inventory_vault(tmp_path, expected_owner="bob")


def test_conversion_receipt_replay_conflict_and_exact_rollback():
    source = {"workspaceId":"space", "corpus":"notes", "root":["section"]}
    incoming = propose_catalog(account_id="account-a", catalog=None, binder_id="binder-a", display_name="Example", source=source, expected_revision=0)["request"]
    request = {"accountId":"account-a", "expectedRevision":0, "catalog":incoming, "sourceFingerprint":"abc"}
    operation = "991c5551-ecef-4b3a-bb68-e244a7b91eb1"
    stored, receipt = apply_conversion(None, account_id="account-a", operation_id=operation, request=request, incoming=incoming, expected_revision=0)
    assert public_catalog(stored, "account-a")["revision"] == 1
    replay, repeated = apply_conversion(stored, account_id="account-a", operation_id=operation, request=request, incoming=incoming, expected_revision=0)
    assert replay == stored and repeated == receipt
    with pytest.raises(BinderCatalogError):
        apply_conversion(stored, account_id="account-a", operation_id=operation, request={**request,"sourceFingerprint":"changed"}, incoming=incoming, expected_revision=0)
    changed = dict(receipt["after"], revision=2)
    with pytest.raises(BinderCatalogError):
        rollback_conversion(store_catalog(stored, changed, "account-a"), account_id="account-a", operation_id=operation)
    restored, rolled = rollback_conversion(stored, account_id="account-a", operation_id=operation)
    assert restored["catalog"] is None and rolled["state"] == "rolled_back"
    assert public_catalog(restored, "account-a")["revision"] == 2
    reused = propose_catalog(account_id="account-a", catalog=public_catalog(restored, "account-a"), binder_id="binder-a", display_name="Again", source=source, expected_revision=2)["request"]
    with pytest.raises(BinderCatalogError):
        prepare_storage_update(restored, reused, "account-a", 2)
    fresh = propose_catalog(account_id="account-a", catalog=public_catalog(restored, "account-a"), binder_id="binder-b", display_name="Next", source=source, expected_revision=2)["request"]
    request2 = {"accountId":"account-a", "expectedRevision":2, "catalog":fresh}
    later, _ = apply_conversion(restored, account_id="account-a", operation_id="ab1bf24b-b6be-45c2-9fd4-73f1c3d43ab3", request=request2, incoming=fresh, expected_revision=2)
    assert public_catalog(later, "account-a")["revision"] == 3
