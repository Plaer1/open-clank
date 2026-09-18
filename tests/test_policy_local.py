from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.policy_local import (
    PolicyClient,
    PolicyLocalError,
    PolicyWorker,
    git_policy_selection,
    issue_local_profile,
)
from src.project_hex import (
    activate_hex,
    inspect_project_policy,
    register_project,
    resolve_hex,
    transition_contract_filename,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def short_policy_socket() -> Path:
    """Keep AF_UNIX endpoints below macOS's 104-byte path limit."""
    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("policy worker peer credentials are unavailable on this platform")
    short_root = Path(
        tempfile.mkdtemp(
            prefix="oc-policy-",
            dir="/tmp" if os.name == "posix" else None,
        )
    )
    try:
        yield short_root / "worker.sock"
    finally:
        shutil.rmtree(short_root, ignore_errors=True)


def _git(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _active_project(
    tmp_path: Path,
    *,
    contract: bytes = b"{}\n",
    contract_name: str = ".hex",
    project_id: str = "project",
) -> tuple[Path, str]:
    root = tmp_path / project_id
    root.mkdir()
    _git(root)
    (root / contract_name).write_bytes(contract)
    db_path = str(tmp_path / f"{project_id}.db")
    register_project(
        root,
        owner="alice",
        workspace_id="workspace",
        project_id=project_id,
        db_path=db_path,
    )
    activate_hex(
        resolve_hex(root),
        owner="alice",
        project_id=project_id,
        db_path=db_path,
    )
    return root, db_path


@contextmanager
def _worker(db_path: str, state_dir: Path, socket_path: Path):
    stop = threading.Event()
    worker = PolicyWorker(db_path=db_path, state_dir=state_dir, socket_path=socket_path)
    failure: list[BaseException] = []

    def run() -> None:
        try:
            worker.serve_forever(stop)
        except BaseException as exc:  # returned to the test thread
            failure.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not socket_path.exists() and not failure and time.monotonic() < deadline:
        time.sleep(0.01)
    if failure:
        raise failure[0]
    if not socket_path.exists():
        raise AssertionError("policy worker did not create its socket")
    try:
        yield worker
    finally:
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        if failure:
            raise failure[0]


def _request(root: Path, *, profile: str | None, project_id: str = "project", action: str = "check"):
    return {
        "action": action,
        "profile": profile,
        "project_id": project_id,
        "project_root": str(root),
    }


def _route(router, path: str, method: str):
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(path)


def test_worker_unavailable_fails_closed_without_in_process_fallback(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sock"
    with pytest.raises(PolicyLocalError, match="worker is unavailable"):
        PolicyClient(missing).request(
            {
                "action": "check",
                "profile": "owner",
                "project_id": "project",
                "project_root": str(tmp_path),
            },
            start_worker=False,
        )


def test_worker_rejects_foreign_peer_before_reading_a_profile(tmp_path: Path) -> None:
    worker = PolicyWorker(
        db_path=str(tmp_path / "missing.db"),
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "policy.sock",
    )
    with pytest.raises(PolicyLocalError, match="different OS user"):
        worker.handle({}, peer_uid=os.getuid() + 1)


@pytest.mark.parametrize("invalid", ["expired", "wrong_uid"])
def test_worker_rejects_expired_and_wrong_uid_profiles(
    tmp_path: Path,
    short_policy_socket: Path,
    invalid: str,
) -> None:
    root, db_path = _active_project(tmp_path)
    state_dir = tmp_path / "state"
    socket_path = short_policy_socket
    issue_local_profile(
        invalid,
        owner="alice",
        project_id="project",
        db_path=db_path,
        state_dir=state_dir,
        os_uid=os.getuid() + 1 if invalid == "wrong_uid" else os.getuid(),
        now=time.time() - 7200 if invalid == "expired" else None,
        ttl_seconds=60 if invalid == "expired" else 900,
    )
    with _worker(db_path, state_dir, socket_path):
        with pytest.raises(
            PolicyLocalError,
            match="expired|different OS user",
        ):
            PolicyClient(socket_path).request(
                _request(root, profile=invalid), start_worker=False
            )


def test_worker_rejects_ambiguous_profiles(
    tmp_path: Path, short_policy_socket: Path
) -> None:
    root, db_path = _active_project(tmp_path)
    state_dir = tmp_path / "state"
    socket_path = short_policy_socket
    for profile in ("alice-one", "alice-two"):
        issue_local_profile(
            profile,
            owner="alice",
            project_id="project",
            db_path=db_path,
            state_dir=state_dir,
        )
    with _worker(db_path, state_dir, socket_path):
        with pytest.raises(PolicyLocalError, match="multiple valid policy profiles"):
            PolicyClient(socket_path).request(
                _request(root, profile=None), start_worker=False
            )


def test_web_down_worker_checks_policy_over_owner_only_socket(
    tmp_path: Path, short_policy_socket: Path
) -> None:
    root, db_path = _active_project(tmp_path)
    state_dir = tmp_path / "state"
    socket_path = short_policy_socket
    profile = issue_local_profile(
        "alice",
        owner="alice",
        project_id="project",
        db_path=db_path,
        state_dir=state_dir,
    )
    assert stat.S_IMODE(Path(profile["path"]).stat().st_mode) == 0o600
    assert stat.S_IMODE((state_dir / "profile-signing.key").stat().st_mode) == 0o600
    with _worker(db_path, state_dir, socket_path):
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        response = PolicyClient(socket_path).request(
            _request(root, profile="alice"), start_worker=False
        )
    assert response["allowed"] is True
    assert response["project_id"] == "project"
    assert response["contract_hash"] == resolve_hex(root).contract_hash


def test_hook_check_returns_real_contained_policy_finding(
    tmp_path: Path, short_policy_socket: Path
) -> None:
    contract = (
        b"henxels:\n"
        b"  - henxel: bad files stay markdown\n"
        b"    in: ./bad*\n"
        b"    allowed_filetypes: .md\n"
    )
    root, db_path = _active_project(tmp_path, contract=contract)
    (root / "bad.py").write_text("print('blocked')\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    socket_path = short_policy_socket
    issue_local_profile(
        "alice",
        owner="alice",
        project_id="project",
        db_path=db_path,
        state_dir=state_dir,
    )
    with _worker(db_path, state_dir, socket_path):
        response = PolicyClient(socket_path).request(
            _request(root, profile="alice", action="pre-commit"),
            start_worker=False,
        )
    assert response["allowed"] is False
    assert any(item["henxel"] == "bad files stay markdown" for item in response["findings"])


def test_precommit_reads_the_git_index_and_blocks_staged_deletion(
    tmp_path: Path, short_policy_socket: Path
) -> None:
    contract = b"settings:\n  confirm_before_deleting:\n    over_lines: 1\nhenxels: []\n"
    root = tmp_path / "project"
    root.mkdir()
    _git(root)
    (root / ".hex").write_bytes(contract)
    note = root / "note.md"
    note.write_text("one\ntwo\nthree\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Open Clank Test",
            "-c",
            "user.email=test@invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    db_path = str(tmp_path / "project.db")
    register_project(
        root,
        owner="alice",
        workspace_id="workspace",
        project_id="project",
        db_path=db_path,
    )
    activate_hex(
        resolve_hex(root),
        owner="alice",
        project_id="project",
        db_path=db_path,
    )
    note.unlink()
    subprocess.run(["git", "-C", str(root), "add", "-u"], check=True)
    state_dir = tmp_path / "state"
    socket_path = short_policy_socket
    issue_local_profile(
        "alice",
        owner="alice",
        project_id="project",
        db_path=db_path,
        state_dir=state_dir,
    )
    with _worker(db_path, state_dir, socket_path):
        response = PolicyClient(socket_path).request(
            _request(root, profile="alice", action="pre-commit"),
            start_worker=False,
        )
    assert response["allowed"] is False
    assert any("Information loss" in item["henxel"] for item in response["findings"])


def test_policy_use_writes_only_opaque_git_selection_then_check_uses_it(
    tmp_path: Path, short_policy_socket: Path
) -> None:
    root, db_path = _active_project(tmp_path)
    state_dir = tmp_path / "state"
    socket_path = short_policy_socket
    issue_local_profile(
        "alice-shell",
        owner="alice",
        project_id="project",
        db_path=db_path,
        state_dir=state_dir,
    )
    command = [
        str(REPO_ROOT / ".venv" / "bin" / "python"),
        str(REPO_ROOT / "scripts" / "odysseus-policy"),
        "--socket",
        str(socket_path),
    ]
    with _worker(db_path, state_dir, socket_path):
        selected = subprocess.run(
            [*command, "use", "alice-shell", "--project", "project", "--root", str(root), "--no-start"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        assert selected.returncode == 0, selected.stderr
        checked = subprocess.run(
            [*command, "check", "--root", str(root), "--no-start"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=40,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr
    assert git_policy_selection(root) == ("alice-shell", "project")
    config = (root / ".git" / "config").read_text(encoding="utf-8")
    assert "alice-shell" in config and "project" in config
    assert "owner = alice" not in config
    assert "signature" not in config and "profile-signing" not in config


def test_activation_advance_invalidates_previously_issued_profile(
    tmp_path: Path, short_policy_socket: Path
) -> None:
    original = b"{}\n"
    root, db_path = _active_project(
        tmp_path, contract=original, contract_name="henxels.yaml"
    )
    state_dir = tmp_path / "state"
    socket_path = short_policy_socket
    issue_local_profile(
        "alice",
        owner="alice",
        project_id="project",
        db_path=db_path,
        state_dir=state_dir,
    )
    transition_contract_filename(
        owner="alice",
        project_id="project",
        expected_contract_hash=hashlib.sha256(original).hexdigest(),
        target_name=".hex",
        db_path=db_path,
    )
    with _worker(db_path, state_dir, socket_path):
        with pytest.raises(PolicyLocalError, match="activation revision is stale"):
            PolicyClient(socket_path).request(
                _request(root, profile="alice"), start_worker=False
            )


def test_authenticated_project_route_issues_profile_without_accepting_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import routes.memory.memory_routes as memory_routes
    import src.auth_helpers as auth_helpers
    import src.policy_local as policy_local

    captured: dict[str, object] = {}

    def issue(profile_name, **kwargs):
        captured.update({"profile_name": profile_name, **kwargs})
        return {
            "profile": profile_name,
            "project_id": kwargs["project_id"],
            "project_root": str(tmp_path),
            "activation_revision": 7,
            "actions": list(kwargs["actions"]),
            "expires_at": 12345,
            "path": "/private/not-returned",
        }

    monkeypatch.setattr(memory_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(memory_routes, "require_user", lambda _request: "alice")
    monkeypatch.setattr(auth_helpers, "require_privilege", lambda _request, _name: None)
    monkeypatch.setattr(policy_local, "issue_local_profile", issue)
    provider = MagicMock()
    provider._fm_db_path = str(tmp_path / "frankenmemory.db")
    router = memory_routes.setup_memory_routes(
        MagicMock(), MagicMock(), memory_provider=provider
    )
    endpoint = _route(
        router, "/api/memory/projects/{project_id}/policy-profile", "POST"
    )

    class Request:
        state = SimpleNamespace(current_user="alice")

        async def json(self):
            return {
                "profile": "alice-shell",
                "actions": ["select", "check"],
                "ttl_seconds": 300,
                "os_uid": 0,
            }

    result = asyncio.run(endpoint(Request(), "project"))
    assert result == {
        "ok": True,
        "profile": "alice-shell",
        "project_id": "project",
        "project_root": str(tmp_path),
        "activation_revision": 7,
        "actions": ["select", "check"],
        "expires_at": 12345,
    }
    assert captured["owner"] == "alice"
    assert captured["db_path"] == provider._fm_db_path
    assert "os_uid" not in captured


@pytest.mark.parametrize(
    "source_contract",
    [REPO_ROOT / "henxels.yaml", REPO_ROOT / "packages" / "Copal" / "henxels.yaml"],
    ids=("open-clank-root", "copal"),
)
def test_isolated_first_party_legacy_hex_round_trip_is_exact_and_journaled(
    tmp_path: Path, source_contract: Path
) -> None:
    live_paths = (
        REPO_ROOT / "henxels.yaml",
        REPO_ROOT / ".hex",
        REPO_ROOT / "packages" / "Copal" / "henxels.yaml",
        REPO_ROOT / "packages" / "Copal" / ".hex",
    )
    before = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in live_paths
        if path.exists()
    }
    original = source_contract.read_bytes()
    project_id = source_contract.parent.name.lower().replace("-", "_") or "root"
    root, db_path = _active_project(
        tmp_path,
        contract=original,
        contract_name="henxels.yaml",
        project_id=project_id,
    )
    original_hash = hashlib.sha256(original).hexdigest()

    forward = transition_contract_filename(
        owner="alice",
        project_id=project_id,
        expected_contract_hash=original_hash,
        target_name=".hex",
        db_path=db_path,
    )
    assert not (root / "henxels.yaml").exists()
    assert (root / ".hex").read_bytes() == original
    assert resolve_hex(root).contract_path == str(root / ".hex")

    reverse = transition_contract_filename(
        owner="alice",
        project_id=project_id,
        expected_contract_hash=original_hash,
        target_name="henxels.yaml",
        db_path=db_path,
    )
    assert not (root / ".hex").exists()
    assert (root / "henxels.yaml").read_bytes() == original
    assert inspect_project_policy(project_id, owner="alice", db_path=db_path)["pending_transitions"] == []
    with sqlite3.connect(db_path) as conn:
        for operation in (forward["transition_id"], reverse["transition_id"]):
            phases = [
                row[0]
                for row in conn.execute(
                    "SELECT phase FROM fm_v2_policy_transitions WHERE operation_id=? ORDER BY sequence",
                    (operation,),
                )
            ]
            assert phases == [
                "prepared",
                "file_published",
                "activation_advanced",
                "validated",
                "projection_enqueued",
                "committed",
            ]
    after = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in live_paths
        if path.exists()
    }
    assert after == before


@pytest.mark.parametrize(
    "failed_phase",
    ["prepared", "file_published", "activation_advanced", "validated", "projection_enqueued", "committed"],
)
def test_contract_filename_transition_recovers_every_journal_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_phase: str,
) -> None:
    import src.project_hex as project_hex

    original = b"settings: {}\nhenxels: []\n"
    root, db_path = _active_project(
        tmp_path,
        contract=original,
        contract_name="henxels.yaml",
    )
    append = project_hex._append_transition_event
    failed = False

    def crash_after_event(conn, *, phase, **kwargs):
        nonlocal failed
        event_id = append(conn, phase=phase, **kwargs)
        if phase == failed_phase and not failed:
            failed = True
            raise RuntimeError(f"death after {phase}")
        return event_id

    monkeypatch.setattr(project_hex, "_append_transition_event", crash_after_event)
    with pytest.raises(RuntimeError, match=f"death after {failed_phase}"):
        transition_contract_filename(
            owner="alice",
            project_id="project",
            expected_contract_hash=hashlib.sha256(original).hexdigest(),
            target_name=".hex",
            db_path=db_path,
        )
    assert (root / "henxels.yaml").read_bytes() == original
    assert not (root / ".hex").exists()
    assert resolve_hex(root).contract_path == str(root / "henxels.yaml")
    assert inspect_project_policy("project", owner="alice", db_path=db_path)["pending_transitions"] == []


def test_contract_filename_transition_refuses_duplicate_target(tmp_path: Path) -> None:
    root, db_path = _active_project(tmp_path, contract_name="henxels.yaml")
    (root / ".hex").write_text("{}\n", encoding="utf-8")
    with pytest.raises(Exception, match="duplicate|target already exists"):
        transition_contract_filename(
            owner="alice",
            project_id="project",
            expected_contract_hash=hashlib.sha256(b"{}\n").hexdigest(),
            target_name=".hex",
            db_path=db_path,
        )
