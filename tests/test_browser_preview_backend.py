from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.browser_preview_routes import setup_browser_preview_routes
from src.openclank.browser_preview import BrowserPreviewError, BrowserPreviewService
from src.openclank.browser_preview_origin import BrowserPreviewOrigin, BrowserPreviewOriginError, PreviewAsset
from src.openclank.files_facade import FilesFacadeError
from src.openclank.resource_refs import issue_resource_ref


def _get_origin(port: int, host: str, path: str = "/index.html"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers={"Host": f"{host}:{port}"})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_origin_persists_stable_port_and_opaque_scopes(tmp_path: Path):
    origin = BrowserPreviewOrigin(tmp_path)
    with pytest.raises(BrowserPreviewOriginError) as unsupported:
        origin.publish(
            scope_key="a" * 64, owner_subject_id="owner-a", workspace_id="workspace-a",
            resource_key="resource-a", revision="rev-a", digest="sha256:a", html=b"<h1>one</h1>",
            app_origin="https://example.test", expires_at=4_000_000_000,
        )
    assert unsupported.value.code == "unsupported_app_origin"
    assert not (tmp_path / "browser-preview" / "origin.json").exists()
    port = origin.start()
    first = origin.artifact_id_for("a" * 64)
    assert first == origin.artifact_id_for("a" * 64)
    assert origin.artifact_id_for("b" * 64) != first
    state = tmp_path / "browser-preview" / "origin.json"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700
    origin.publish(
        scope_key="a" * 64, owner_subject_id="owner-a", workspace_id="workspace-a",
        resource_key="resource-a", revision="rev-a", digest="sha256:a", html=b"<h1>one</h1>",
        app_origin="http://127.0.0.1:7000", expires_at=4_000_000_000,
        dependencies={"deps/chart.js":PreviewAsset(b"window.chart=1", "text/javascript", "sha256:dep")},
    )
    status, headers, body = _get_origin(port, f"{first}.artifacts.localhost")
    assert status == 200 and body == b"<h1>one</h1>"
    assert "sandbox allow-scripts allow-same-origin allow-downloads" in headers["Content-Security-Policy"]
    status, _, body = _get_origin(port, f"{first}.artifacts.localhost", "/deps/chart.js")
    assert status == 200 and body == b"window.chart=1"
    assert _get_origin(port, f"{first}.artifacts.localhost", "/private/secret")[0] == 404
    assert _get_origin(port, "unknown.artifacts.localhost")[0] == 421
    expired = origin.publish(
        scope_key="c" * 64, owner_subject_id="owner-c", workspace_id="workspace-c",
        resource_key="resource-c", revision="rev-c", digest="sha256:c", html=b"expired",
        app_origin="http://127.0.0.1:7000", expires_at=0,
    )
    assert _get_origin(port, f"{expired.artifact_id}.artifacts.localhost")[0] == 410
    assert not origin.revoke(session_id=expired.session_id, owner_subject_id="owner-c")

    origin.stop()
    recovered = BrowserPreviewOrigin(tmp_path)
    assert recovered.start() == port
    assert recovered.artifact_id_for("a" * 64) == first
    assert _get_origin(port, f"{first}.artifacts.localhost")[0] == 410
    recovered.publish(
        scope_key="a" * 64, owner_subject_id="owner-a", workspace_id="workspace-a",
        resource_key="resource-a", revision="rev-a", digest="sha256:a", html=b"<h1>restored</h1>",
        app_origin="http://localhost:7000", expires_at=4_000_000_000,
    )
    assert _get_origin(port, f"{first}.artifacts.localhost")[2] == b"<h1>restored</h1>"
    recovered.stop()

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    unavailable = BrowserPreviewOrigin(tmp_path)
    with pytest.raises(BrowserPreviewOriginError) as error:
        unavailable.start()
    assert error.value.code == "origin_port_unavailable"
    assert unavailable.port is None
    blocker.close()


class _Policy:
    def __init__(self):
        self.value = 3

    def generation(self):
        return self.value


class _Auth:
    def __init__(self):
        self.tokens = {"session-a": "alice"}

    def get_username_for_token(self, token):
        return self.tokens.get(token)

    def account_id(self, username):
        return "owner-a" if username == "alice" else None


class _FakeFiles:
    def __init__(self):
        self.stat_reads = 0
        self.content_reads = 0
        self.resources = {
            "html-token": {"resource_key":"rk-html", "revision":{"kind":"hostFingerprint", "value":"html-v1"}, "data":b"<!doctype html><h1>Report</h1>", "media_type":"text/html"},
            "dep-token": {"resource_key":"rk-dep", "revision":{"kind":"hostFingerprint", "value":"dep-v1"}, "data":b"window.dep=1", "media_type":"text/javascript"},
            "css-token": {"resource_key":"rk-css", "revision":{"kind":"hostFingerprint", "value":"css-v1"}, "data":b"body{color:black}", "media_type":"text/css"},
            "img-token": {"resource_key":"rk-img", "revision":{"kind":"hostFingerprint", "value":"img-v1"}, "data":b"\x89PNG\r\n\x1a\nfixture", "media_type":"image/png"},
        }

    async def stat(self, context, *, resource_ref):
        self.stat_reads += 1
        entry = self.resources[resource_ref]
        return {"id":entry["resource_key"], "policy_generation":context.policy_generation, "revision":dict(entry["revision"]), "mime_type":entry["media_type"], "capabilities":["preview","stat"]}

    async def content(self, context, *, resource_ref, capability="download"):
        self.content_reads += 1
        if capability != "preview":
            raise FilesFacadeError("preview capability required", code="resource_unavailable")
        entry = self.resources[resource_ref]
        return SimpleNamespace(data=entry["data"], path=None, stream=None, media_type=entry["media_type"], filename="fixture.html", size=len(entry["data"]))


def _ref(owner: str, origin: str, workspace: str = "workspace-a", capabilities=("preview", "stat")):
    return issue_resource_ref(
        owner_subject_id=owner, provider="host", origin_id=origin, kind="file",
        capabilities=capabilities, policy_generation=3, workspace_id=workspace,
    ).token


def test_routes_bind_owner_revision_dependencies_and_revoke(tmp_path: Path):
    policy = _Policy()
    files = _FakeFiles()
    principal = ["owner-a", "alice", False]
    refs = {key:_ref("owner-a", f"origin-{key}") for key in ("html-token", "dep-token", "css-token", "img-token")}
    files.resources = {refs[key]: files.resources[key] for key in refs}
    router = setup_browser_preview_routes(
        facade_factory=lambda _request, _context: files,
        policy_repository=policy,
        data_dir=tmp_path,
        principal_resolver=lambda _request, repository=None: tuple(principal),
    )
    app = FastAPI()
    app.state.auth_manager = _Auth()
    app.include_router(router)
    service: BrowserPreviewService = router.state.preview_service
    try:
        with TestClient(app, base_url="http://127.0.0.1:7777") as client:
            client.cookies.set("open_clank_session", "session-a")
            cancelled = client.post("/api/browser-previews/cancel", json={"client_id":"b" * 32, "client_generation":1})
            assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True
            replayed = client.post("/api/browser-previews/cancel", json={"client_id":"b" * 32, "client_generation":1})
            assert replayed.status_code == 200 and replayed.json()["cancelled"] is True
            payload = {
                "resource_ref":refs["html-token"],
                "expected_revision":{"kind":"hostFingerprint", "value":"html-v1"},
                "dependencies":[
                    {"path":"deps/chart.js", "resource_ref":refs["dep-token"], "expected_revision":{"kind":"hostFingerprint", "value":"dep-v1"}},
                    {"path":"deps/report.css", "resource_ref":refs["css-token"], "expected_revision":{"kind":"hostFingerprint", "value":"css-v1"}},
                    {"path":"images/chart.png", "resource_ref":refs["img-token"], "expected_revision":{"kind":"hostFingerprint", "value":"img-v1"}},
                ],
            }
            response = client.post("/api/browser-previews", json=payload)
            assert response.status_code == 200, response.text
            session = response.json()
            assert session["resource_key"] == "rk-html"
            assert session["workspace_id"] == "workspace-a"
            assert "owner-a" not in json.dumps(session)
            artifact_host = session["url"].split("//",1)[1].split("/",1)[0]
            port = int(artifact_host.rsplit(":",1)[1])
            assert session["origin_port"] == port
            status, headers, body = _get_origin(port, artifact_host.rsplit(":",1)[0])
            assert status == 200 and body == files.resources[refs["html-token"]]["data"]
            assert headers["Content-Type"].startswith("text/html")
            assert files.content_reads == 4
            assert _get_origin(port, artifact_host.rsplit(":",1)[0], "/deps/chart.js")[2] == b"window.dep=1"
            assert _get_origin(port, artifact_host.rsplit(":",1)[0], "/deps/report.css")[2] == b"body{color:black}"
            assert _get_origin(port, artifact_host.rsplit(":",1)[0], "/images/chart.png")[2] == files.resources[refs["img-token"]]["data"]
            assert _get_origin(port, artifact_host.rsplit(":",1)[0], "/deps/private.js")[0] == 404
            assert files.content_reads == 4
            assert files.stat_reads == 4 * 2 + 1 + 3 * 2
            assert client.get(f"/api/browser-previews/{session['session_id']}/status").json()["status"] == "delivered"

            stale = client.post("/api/browser-previews", json={**payload, "expected_revision":{"kind":"hostFingerprint", "value":"old"}})
            assert stale.status_code == 409
            invalid_owner = client.post("/api/browser-previews", json={**payload, "owner_id":"owner-b"})
            assert invalid_owner.status_code == 422
            forbidden = client.post("/api/browser-previews", json={"resource_ref":_ref("owner-a", "no-preview", capabilities=("stat",))})
            assert forbidden.status_code == 404
            policy.value += 1
            status, _, _ = _get_origin(port, artifact_host.rsplit(":",1)[0])
            assert status == 410
            stale_status = client.get(f"/api/browser-previews/{session['session_id']}/status")
            assert stale_status.status_code == 200 and stale_status.json()["status"] == "stale"

            policy.value = 3
            fresh = client.post("/api/browser-previews", json=payload).json()
            assert fresh["url"].split("/")[2] == session["url"].split("/")[2]
            principal[:] = ["owner-b", "bob", False]
            assert client.delete(f"/api/browser-previews/{fresh['session_id']}").status_code == 404
            principal[:] = ["owner-a", "alice", False]
            assert client.delete(f"/api/browser-previews/{fresh['session_id']}").json()["revoked"] is True
            assert _get_origin(port, artifact_host.rsplit(":",1)[0])[0] == 410

            still_active = client.post("/api/browser-previews", json=payload).json()
            app.state.auth_manager.tokens.pop("session-a")
            fresh_host = still_active["url"].split("//",1)[1].split("/",1)[0]
            assert _get_origin(port, fresh_host.rsplit(":",1)[0])[0] == 410
            assert client.get(f"/api/browser-previews/{still_active['session_id']}/status").status_code == 401
            assert client.post("/api/browser-previews", json=payload).status_code == 401
    finally:
        service.close()


def test_route_rejects_non_loopback_app_origin_and_dependency_escape(tmp_path: Path):
    policy = _Policy()
    files = _FakeFiles()
    html_ref = _ref("owner-a", "origin-html")
    files.resources = {html_ref: files.resources["html-token"]}
    payload = {"resource_ref":html_ref}
    router = setup_browser_preview_routes(
        facade_factory=lambda _request, _context: files,
        policy_repository=policy,
        data_dir=tmp_path,
        principal_resolver=lambda _request, repository=None: ("owner-a", "alice", False),
    )
    app = FastAPI(); app.state.auth_manager = _Auth(); app.include_router(router)
    service: BrowserPreviewService = router.state.preview_service
    try:
        with TestClient(app, base_url="http://example.test") as client:
            client.cookies.set("open_clank_session", "session-a")
            response = client.post("/api/browser-previews", json=payload)
            assert response.status_code == 503
        with TestClient(app, base_url="http://127.0.0.1:7777") as client:
            client.cookies.set("open_clank_session", "session-a")
            response = client.post("/api/browser-previews", json={**payload, "dependencies":[{"path":"../outside.js", "resource_ref":_ref("owner-a", "origin-dep")}]})
            assert response.status_code == 422
    finally:
        service.close()


def test_superseded_client_generation_cannot_publish(tmp_path: Path):
    service = BrowserPreviewService(tmp_path)
    client_id = "a" * 32
    assert service.note_client_generation(owner_subject_id="owner-a", client_id=client_id, generation=1)
    assert service.note_client_generation(owner_subject_id="owner-a", client_id=client_id, generation=2)
    with pytest.raises(BrowserPreviewError) as stale:
        service.create(
            owner_subject_id="owner-a", workspace_id="workspace-a", resource_key="resource-a",
            revision={"kind":"hostFingerprint", "value":"old"}, html=b"<h1>old</h1>",
            app_origin="http://127.0.0.1:7777", client_id=client_id, client_generation=1,
        )
    assert stale.value.code == "stale_preview_request"
    assert service.origin.port is None, "a rejected stale operation does not open the listener"
    service.close()


def test_cancel_confirms_lost_response_publication_without_touching_newer_lease(tmp_path: Path):
    service = BrowserPreviewService(tmp_path)
    owner = "owner-a"
    client_id = "d" * 32
    try:
        assert service.note_client_generation(owner_subject_id=owner, client_id=client_id, generation=1)
        first = service.create(
            owner_subject_id=owner, workspace_id="workspace-a", resource_key="resource-a",
            revision={"kind":"hostFingerprint", "value":"r1"}, html=b"<h1>first</h1>",
            app_origin="http://127.0.0.1:7777", client_id=client_id, client_generation=1,
        )
        assert service.origin.session_status(session_id=first["session_id"], owner_subject_id=owner)["status"] == "active"

        assert service.note_client_generation(owner_subject_id=owner, client_id=client_id, generation=2)
        assert service.origin.session_status(session_id=first["session_id"], owner_subject_id=owner)["status"] == "revoked"
        second = service.create(
            owner_subject_id=owner, workspace_id="workspace-a", resource_key="resource-a",
            revision={"kind":"hostFingerprint", "value":"r2"}, html=b"<h1>second</h1>",
            app_origin="http://127.0.0.1:7777", client_id=client_id, client_generation=2,
        )
        assert service.cancel_client_generation(owner_subject_id=owner, client_id=client_id, generation=1) is True
        assert service.origin.current_lease(session_id=second["session_id"], owner_subject_id=owner) is not None

        assert service.cancel_client_generation(owner_subject_id=owner, client_id=client_id, generation=3) is True
        assert service.origin.current_lease(session_id=second["session_id"], owner_subject_id=owner) is None
        assert service.origin.session_status(session_id=second["session_id"], owner_subject_id=owner)["status"] == "revoked"
        assert service.cancel_client_generation(owner_subject_id=owner, client_id=client_id, generation=3) is True
    finally:
        service.close()


def test_cancel_tombstone_blocks_a_late_publication_but_allows_next_generation(tmp_path: Path):
    service = BrowserPreviewService(tmp_path)
    owner = "owner-a"
    client_id = "e" * 32
    try:
        assert service.note_client_generation(owner_subject_id=owner, client_id=client_id, generation=1)
        assert service.cancel_client_generation(owner_subject_id=owner, client_id=client_id, generation=1)
        with pytest.raises(BrowserPreviewError) as cancelled:
            service.create(
                owner_subject_id=owner, workspace_id="workspace-a", resource_key="resource-a",
                revision={"kind":"hostFingerprint", "value":"r1"}, html=b"<h1>late</h1>",
                app_origin="http://127.0.0.1:7777", client_id=client_id, client_generation=1,
            )
        assert cancelled.value.code == "stale_preview_request"
        assert service.note_client_generation(owner_subject_id=owner, client_id=client_id, generation=2)
        current = service.create(
            owner_subject_id=owner, workspace_id="workspace-a", resource_key="resource-a",
            revision={"kind":"hostFingerprint", "value":"r2"}, html=b"<h1>current</h1>",
            app_origin="http://127.0.0.1:7777", client_id=client_id, client_generation=2,
        )
        assert service.origin.current_lease(session_id=current["session_id"], owner_subject_id=owner) is not None
    finally:
        service.close()


def test_equal_generation_cancel_after_publish_wins_before_response(tmp_path: Path):
    service = BrowserPreviewService(tmp_path)
    owner = "owner-a"
    client_id = "f" * 32
    entered_publish = threading.Event()
    release_publish = threading.Event()
    outcomes = {}
    original_publish = service.origin.publish

    def blocked_publish(**kwargs):
        entered_publish.set()
        assert release_publish.wait(2), "fixture publish gate timed out"
        return original_publish(**kwargs)

    service.origin.publish = blocked_publish
    try:
        assert service.note_client_generation(owner_subject_id=owner, client_id=client_id, generation=1)

        def create_request():
            outcomes["created"] = service.create(
                owner_subject_id=owner, workspace_id="workspace-a", resource_key="resource-a",
                revision={"kind":"hostFingerprint", "value":"r1"}, html=b"<h1>published</h1>",
                app_origin="http://127.0.0.1:7777", client_id=client_id, client_generation=1,
            )

        creator = threading.Thread(target=create_request)
        creator.start()
        assert entered_publish.wait(2), "create never reached the publication boundary"
        cancel_result = {}

        def cancel_request():
            cancel_result["cancelled"] = service.cancel_client_generation(
                owner_subject_id=owner, client_id=client_id, generation=1,
            )

        canceller = threading.Thread(target=cancel_request)
        canceller.start()
        release_publish.set()
        creator.join(2)
        canceller.join(2)
        assert not creator.is_alive() and not canceller.is_alive(), "generation race did not settle"
        session_id = outcomes["created"]["session_id"]
        assert cancel_result["cancelled"] is True
        assert service.origin.current_lease(session_id=session_id, owner_subject_id=owner) is None
        assert service.origin.session_status(session_id=session_id, owner_subject_id=owner)["status"] == "revoked"
    finally:
        release_publish.set()
        service.close()
