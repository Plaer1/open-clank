import base64
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.copal_routes as copal_routes
from routes.copal_routes import _note_view, _preserved_source_bytes, _require_mutable_note, setup_copal_routes
from src.openclank.copal_memes import MEMES_MIME, MemesValidationError, raw_source, validate_memes_payload


def _record(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MemesBridge:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        (self.data_dir / "assets").mkdir(parents=True)
        self.calls = []
        self.docs = {
            "OLD": {
                "id": "OLD", "kind": "wiki", "corpus": "wiki", "name": ".memes/Existing",
                "head": "old-head", "text": _record({"schemaVersion": 1, "futureField": {"keep": True}}),
                "owner": "local", "workspace_id": "default", "builtin": False,
            },
        }
        self.asset_id = "ASSET-OLD"
        old_bytes = b"old asset"
        old_hash = hashlib.sha256(old_bytes).hexdigest()
        (self.data_dir / "assets" / f"{old_hash}.png").write_bytes(old_bytes)
        self.docs[self.asset_id] = {
            "id": self.asset_id, "kind": "asset", "corpus": "system", "name": ".memes/old.png",
            "head": "asset-head", "text": None, "owner": "local", "workspace_id": "default",
            "builtin": False, "hash": old_hash,
        }
        self.counter = 0
        self.imported_assets = {}

    def is_alive(self):
        return True

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args, timeout))
        if operation == "list":
            return {"docs": [dict(doc) for doc in self.docs.values()]}
        if operation == "asset_path":
            doc = self.docs[args["id"]]
            return {"path": str(self.data_dir / "assets" / f"{doc['hash']}.png"), "name": doc["name"]}
        if operation == "get":
            return dict(self.docs[args["id"]])
        if operation == "import_vault":
            root = Path(args["path"])
            restore = args["restore_ids"]
            for archive_name, identity in restore.items():
                path = root.joinpath(*Path(archive_name).parts)
                relative = archive_name.removeprefix(".copal/wiki/")
                kind = identity["kind"]
                content = path.read_bytes()
                if kind == "asset":
                    self.imported_assets[identity["id"]] = content
                self.docs[identity["id"]] = {
                    "id": identity["id"], "kind": kind, "corpus": "wiki" if kind == "wiki" else "system",
                    "name": relative, "head": f"{identity['id']}-head", "text": content.decode("utf-8") if kind == "wiki" else None,
                    "owner": args["owner"], "workspace_id": args["workspace_id"], "builtin": False,
                }
            return {"op": "MEMES-IMPORT", "restored_identities": len(restore)}
        return {}


class ExportRaceBridge(MemesBridge):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.list_calls = 0

    async def call(self, operation, args, timeout=20):
        result = await super().call(operation, args, timeout)
        if operation == "asset_path":
            self.docs[self.asset_id]["head"] = "asset-head-raced"
        if operation == "list":
            self.list_calls += 1
            if self.list_calls == 2:
                self.docs[self.asset_id]["head"] = "asset-head-raced"
        return result


def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = MemesBridge(tmp_path)
    app.state.copal_bridge = bridge
    return TestClient(app), bridge


def envelope(*, name=".memes/Imported", record=None, asset=True, export_id="OLD-EXPORT"):
    data = b"new binary"
    assets = [{
        "assetId": "OLD-ASSET", "name": ".memes/new.bin", "mime": "application/octet-stream",
        "byteLength": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "base64": base64.b64encode(data).decode("ascii"),
    }] if asset else []
    source = record if record is not None else {"schemaVersion": 1, "unknown": {"preserve": True}}
    raw = _record(source).encode("utf-8")
    return {
        "format": "copal-memes", "schemaVersion": 1,
        "documents": [{"exportId": export_id, "name": name, "kind": "wiki", "record": source, "rawSource": raw_source(raw)}],
        "assets": assets, "extensions": {},
    }


def upload(payload, filename="roundtrip.memes", content_type=MEMES_MIME):
    return {"file": (filename, json.dumps(payload, ensure_ascii=False).encode("utf-8"), content_type)}


def test_memes_parser_rejects_schema_utf8_nan_mime_digest_and_paths():
    payload = envelope()
    payload["schemaVersion"] = 2
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
    with pytest.raises(MemesValidationError):
        validate_memes_payload(b"\xff")
    payload = envelope()
    payload["assets"][0]["mime"] = "image/png; evil=1"
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
    payload = envelope()
    payload["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
    payload = envelope()
    payload["documents"][0]["name"] = "../escape"
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
    payload = envelope()
    payload["documents"][0]["record"]["nan"] = float("nan")
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload, allow_nan=True).encode())


def test_memes_route_preview_export_import_remaps_and_preserves_unknowns(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    exported = http.get("/api/copal/export/memes")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith(MEMES_MIME)
    exported_payload = json.loads(exported.content)
    assert exported_payload["format"] == "copal-memes"
    exported_source = base64.b64decode(exported_payload["documents"][0]["rawSource"]["base64"])
    assert exported_payload["documents"][0]["rawSource"]["sha256"] == hashlib.sha256(exported_source).hexdigest()
    restore = dict(exported_payload)
    restore["extensions"] = {**exported_payload["extensions"], "restore": True}
    restored = http.post("/api/copal/import/memes?mode=restore", files=upload(restore))
    assert restored.status_code == 200
    assert restored.json()["imported"]["restore"] is True

    payload = envelope(record={
        "schemaVersion": 1,
        "owner": "attacker",
        "unknown": {"preserve": [1, "two"]},
        "relations": [{"kind": "link", "targetDocumentId": "OLD-EXPORT", "targetAssetId": "OLD-ASSET"}],
    })
    payload["documents"][0]["rawSource"] = raw_source(_record({"schemaVersion": 1, "unknown": {"preserve": "raw provenance"}}).encode())
    calls_before_preview = len(bridge.calls)
    preview = http.post("/api/copal/preview/memes", files=upload(payload))
    assert preview.status_code == 200
    assert preview.json()["preview"] is True
    assert not any(call[0] == "import_vault" for call in bridge.calls[calls_before_preview:])
    imported = http.post("/api/copal/import/memes", files=upload(payload))
    assert imported.status_code == 200
    result = imported.json()["imported"]
    new_doc = bridge.docs[result["ids"]["OLD-EXPORT"]]
    stored = json.loads(new_doc["text"])
    assert stored["owner"] == "attacker"  # inert content is preserved, never trusted as scope
    assert stored["unknown"] == {"preserve": [1, "two"]}
    assert stored["relations"][0]["targetDocumentId"] == result["ids"]["OLD-EXPORT"]
    assert stored["relations"][0]["targetAssetId"] == result["assetIds"]["OLD-ASSET"]
    assert new_doc["owner"] == "local"
    imported_asset_id = result["assetIds"]["OLD-ASSET"]
    assert bridge.imported_assets[imported_asset_id] == b"new binary"
    assert bridge.docs[imported_asset_id]["owner"] == "local"


def test_raw_only_native_record_is_staged_from_source_and_relations_are_remapped(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    native = {
        "schemaVersion": 1,
        "body": {"type": "doc", "blocks": [{"id": "block-1", "type": "heading", "level": 1, "text": "Imported"}]},
        "relations": [{"kind": "link", "targetDocumentId": "RAW-EXPORT"}],
    }
    payload = envelope(name=".memes/Raw-only", record={}, asset=False, export_id="RAW-EXPORT")
    payload["documents"][0]["rawSource"] = raw_source(_record(native).encode("utf-8"))
    response = http.post("/api/copal/import/memes", files=upload(payload))
    assert response.status_code == 200
    imported_id = response.json()["imported"]["ids"]["RAW-EXPORT"]
    stored = json.loads(bridge.docs[imported_id]["text"])
    assert stored["body"]["blocks"][0]["text"] == "Imported"
    assert stored["relations"][0]["targetDocumentId"] == imported_id
    preserved = stored["extensions"]["rawSource"]
    assert base64.b64decode(preserved["base64"]) == _record(native).encode("utf-8")
    assert preserved["sha256"] == hashlib.sha256(_record(native).encode("utf-8")).hexdigest()
    assert bridge.docs[imported_id]["text"] != "{}"


def test_supported_native_provenance_is_preserved_by_export_and_download(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    raw = b'{  "schemaVersion": 1,\n  "body": {"type": "doc", "blocks": []}\n}\n'
    bridge.docs["PROVENANCE"] = {
        "id": "PROVENANCE", "kind": "wiki", "corpus": "wiki", "name": ".memes/Provenance",
        "head": "provenance-head", "text": _record({"schemaVersion": 1, "body": {"type": "doc", "blocks": []}}),
        "format": "copal-note-v1", "storage": "database", "extensions": {"rawSource": raw_source(raw)},
        "owner": "local", "workspace_id": "default", "builtin": False,
    }
    exported = http.get("/api/copal/export/memes")
    assert exported.status_code == 200
    exported_doc = next(item for item in exported.json()["documents"] if item["exportId"] == "PROVENANCE")
    assert base64.b64decode(exported_doc["rawSource"]["base64"]) == raw
    assert exported_doc["rawSource"]["sha256"] == hashlib.sha256(raw).hexdigest()
    downloaded = http.get("/api/copal/documents/PROVENANCE/download")
    assert downloaded.status_code == 200
    assert downloaded.content == raw


def test_nonempty_future_raw_source_is_byte_exact_and_record_is_not_authority(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    raw = b'{  "schemaVersion": 99,\n  "future": [1, 2],\n  "body": null\n}\n'
    payload = envelope(name=".memes/Future", record={"schemaVersion": 99, "future": ["projection"]}, asset=False, export_id="FUTURE")
    payload["documents"][0]["rawSource"] = raw_source(raw)
    response = http.post("/api/copal/import/memes", files=upload(payload))
    assert response.status_code == 200
    imported_id = response.json()["imported"]["ids"]["FUTURE"]
    assert bridge.docs[imported_id]["text"].encode("utf-8") == raw
    assert _preserved_source_bytes(_note_view(bridge.docs[imported_id])) == raw


def test_malformed_projection_retains_validated_recovery_bytes():
    source = b'{"schemaVersion":99,"body":null}'
    view = _note_view({"id": "broken", "kind": "wiki", "corpus": "wiki", "text": source.decode()})
    assert view["recoveryState"] == "malformed-preserved"
    assert _preserved_source_bytes(view) == source


def test_future_native_projection_has_explicit_versioned_read_only_state():
    source = b'{  "schemaVersion": 9,\n  "futureField": true,\n  "body": {"type": "doc", "blocks": []}\n}\n'
    view = _note_view({
        "id": "future", "kind": "wiki", "corpus": "wiki", "text": source.decode("utf-8"),
    })
    assert view["recoveryState"] == "unsupported-future"
    assert view["sourceSchemaVersion"] == 9
    assert view["sourceFormat"] == "native"
    assert _preserved_source_bytes(view) == source
    with pytest.raises(HTTPException) as error:
        _require_mutable_note(view)
    assert error.value.status_code == 409


def test_future_bridge_projection_keeps_version_and_exact_nested_source():
    source = b'{"schemaVersion":12,"body":{"unknown":true}}\n'
    view = _note_view({
        "id": "future-bridge", "kind": "wiki", "corpus": "wiki", "format": "copal-note-v1", "storage": "database",
        "rawPreserved": True, "recoveryState": "unsupported-future", "note_error": "newer schema",
        "rawSource": raw_source(source), "text": "",
    })
    assert view["recoveryState"] == "unsupported-future"
    assert view["sourceSchemaVersion"] == 12
    assert _preserved_source_bytes(view) == source


def test_bridge_projection_downgrades_future_marker_with_malformed_body():
    source = b'{"schemaVersion":12,"body":null}\n'
    view = _note_view({
        "id": "future-malformed", "kind": "wiki", "corpus": "wiki", "format": "copal-note-v1", "storage": "database",
        "rawPreserved": True, "recoveryState": "unsupported-future", "note_error": "newer schema",
        "rawSource": raw_source(source), "text": "",
    })
    assert view["recoveryState"] == "malformed-preserved"
    assert view["sourceFormat"] == "unknown"
    assert _preserved_source_bytes(view) == source


def test_memes_export_fails_if_scoped_head_changes_while_reading_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = ExportRaceBridge(tmp_path)
    app.state.copal_bridge = bridge
    http = TestClient(app)
    response = http.get("/api/copal/export/memes")
    assert response.status_code == 409
    assert "retry" in response.json()["detail"]


def test_memes_rejects_collision_stale_and_malformed_without_mutation(tmp_path, monkeypatch):
    http, bridge = client(tmp_path, monkeypatch)
    before_docs = {key: dict(value) for key, value in bridge.docs.items()}
    forged_mode = envelope(name=".memes/Mode")
    forged_mode["extensions"] = {"restore": True}
    response = http.post("/api/copal/import/memes", files=upload(forged_mode))
    assert response.status_code == 400
    assert not any(call[0] == "import_vault" for call in bridge.calls)

    collision = envelope(name=".memes/Existing")
    response = http.post("/api/copal/import/memes", files=upload(collision))
    assert response.status_code == 409
    assert not any(call[0] == "import_vault" for call in bridge.calls)

    stale = envelope(name=".memes/New")
    stale["extensions"] = {"expectedHeads": {"OLD": "wrong-head"}}
    response = http.post("/api/copal/import/memes", files=upload(stale))
    assert response.status_code == 409
    assert not any(call[0] == "import_vault" for call in bridge.calls)

    malformed = envelope(name=".memes/Malformed")
    malformed["documents"][0]["rawSource"] = {"encoding": "utf-8", "base64": "%%%", "sha256": "0" * 64}
    response = http.post("/api/copal/import/memes", files=upload(malformed))
    assert response.status_code == 400
    assert not any(call[0] == "import_vault" for call in bridge.calls)
    assert bridge.docs == before_docs


def test_memes_case_collision_and_duplicate_asset_ids_are_rejected():
    payload = envelope()
    payload["documents"].append(dict(payload["documents"][0], exportId="SECOND", name=".MEMES/imported"))
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
    payload = envelope()
    payload["assets"].append(dict(payload["assets"][0], name=".memes/other.bin"))
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
    payload = envelope(name=".memes/shared-name")
    payload["assets"][0]["name"] = ".MEMES/SHARED-NAME"
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
    payload = envelope(name=".memes/Straße")
    payload["assets"][0]["name"] = ".memes/STRASSE"
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
    payload = envelope()
    payload["assets"][0]["assetId"] = payload["documents"][0]["exportId"]
    with pytest.raises(MemesValidationError):
        validate_memes_payload(json.dumps(payload).encode())
