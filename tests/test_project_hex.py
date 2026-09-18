import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import shell_policy
from src.project_hex import (
    MAX_BYTES,
    HexResolutionError,
    active_spells,
    activate_hex,
    create_spell,
    deactivate_hex,
    expire_spell,
    grant_executable_trust,
    global_policy_context,
    hex_activation_current,
    inspect_project_policy,
    inspect_registered_project_mutation_policy,
    list_projects,
    policy_executable_manifest,
    preview_spell_promotion,
    promote_spell,
    publish_contract_update,
    require_executable_trust,
    register_project,
    relocate_project,
    require_hex_activation,
    require_project_mutation_admission,
    resolve_hex,
    review_spell,
    revoke_executable_trust,
    update_spell,
    validate_registered_project_candidates,
    validate_project_file_candidates,
)


def _git(root: Path) -> None:
    (root / ".git").mkdir()


def test_hex_is_nearest_and_parse_only(tmp_path: Path) -> None:
    _git(tmp_path)
    (tmp_path / ".hex").write_text("settings: {}\nhenxels: []\n", encoding="utf-8")
    child = tmp_path / "src"
    child.mkdir()
    result = resolve_hex(child)
    assert result.contract_path == str(tmp_path / ".hex")
    assert result.state == "never_activated"
    assert result.contract["settings"] == {}


def test_canonical_v2_contract_is_discovered_before_legacy_names(tmp_path: Path) -> None:
    _git(tmp_path)
    canonical = tmp_path / ".clankers" / "hexes" / "contract.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("contract: open-clank-hexes/v2\nhexes: []\n", encoding="utf-8")
    (tmp_path / ".hex").write_text("contract: open-clank-hexes/v1\nhexes: []\n", encoding="utf-8")
    result = resolve_hex(tmp_path)
    assert result.contract_path == str(canonical)
    assert result.contract["contract"] == "open-clank-hexes/v2"
    assert any("canonical v2" in item for item in result.diagnostics)


def test_every_same_directory_contract_duplicate_fails_closed(tmp_path: Path) -> None:
    _git(tmp_path)
    (tmp_path / ".hex").write_text("{}", encoding="utf-8")
    (tmp_path / "henxels.yaml").write_text("{}", encoding="utf-8")
    with pytest.raises(HexResolutionError, match="duplicate"):
        resolve_hex(tmp_path)
    (tmp_path / "henxels.yaml").write_text("name: drifted\n", encoding="utf-8")
    with pytest.raises(HexResolutionError, match="duplicate"):
        resolve_hex(tmp_path)


def test_malformed_contract_does_not_execute_python(tmp_path: Path) -> None:
    _git(tmp_path)
    (tmp_path / ".hex").write_text("!!python/object/apply:os.system ['touch /tmp/hex-pwn']", encoding="utf-8")
    with pytest.raises(HexResolutionError):
        resolve_hex(tmp_path)
    assert not Path("/tmp/hex-pwn").exists()


def test_non_git_workspace_root_is_honored(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    child = workspace / "src"
    child.mkdir(parents=True)
    (workspace / ".hex").write_text("name: demo\n", encoding="utf-8")
    result = resolve_hex(child, workspace_root=workspace)
    assert result.project_root == str(workspace.resolve())
    assert result.contract_path == str((workspace / ".hex").resolve())


def test_contract_symlink_cannot_escape_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.hex"
    outside.write_text("name: outside\n", encoding="utf-8")
    (project / ".hex").symlink_to(outside)
    with pytest.raises(HexResolutionError, match="cannot be symlinks"):
        resolve_hex(project, workspace_root=project)


@pytest.mark.skipif(
    shell_policy._working_network_bwrap() is None,
    reason="active Open Clank Hexes mutation checks require parser containment",
)
def test_tracked_file_symlink_is_recreated_inside_policy_shadow(tmp_path: Path) -> None:
    _git(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("safe\n", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(target.name)
    (tmp_path / ".hex").write_text("henxels: []\n", encoding="utf-8")
    db = str(tmp_path / "fm.db")
    resolution = resolve_hex(tmp_path)
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)
    result = validate_project_file_candidates(
        resolution,
        owner="alice",
        project_id="project",
        db_path=db,
        candidates={},
    )
    assert result["allowed"] is True

    outside = tmp_path.parent / "outside-policy-target.txt"
    outside.write_text("outside\n", encoding="utf-8")
    alias.unlink()
    alias.symlink_to(outside)
    with pytest.raises(HexResolutionError, match="resolve inside the project"):
        validate_project_file_candidates(
            resolution,
            owner="alice",
            project_id="project",
            db_path=db,
            candidates={},
        )


def test_oversized_contract_is_rejected(tmp_path: Path) -> None:
    _git(tmp_path)
    (tmp_path / ".hex").write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(HexResolutionError, match="exceeds the 1 MiB limit"):
        resolve_hex(tmp_path)


def test_oversized_sparse_contract_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git(tmp_path)
    contract = tmp_path / ".hex"
    with contract.open("wb") as handle:
        handle.truncate(MAX_BYTES * 8)

    def unexpected_read(*_args: object) -> bytes:
        raise AssertionError("oversized contract payload was read")

    monkeypatch.setattr(os, "read", unexpected_read)
    with pytest.raises(HexResolutionError, match="exceeds the 1 MiB limit"):
        resolve_hex(tmp_path)


def test_activation_pins_exact_contract_and_detects_drift(tmp_path: Path) -> None:
    _git(tmp_path)
    contract = tmp_path / ".hex"
    contract.write_text("name: demo\n", encoding="utf-8")
    result = resolve_hex(tmp_path)
    db = tmp_path / "fm.db"
    with __import__("sqlite3").connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE fm_v2_policy_projections(owner_id TEXT, project_id TEXT, contract_path TEXT, contract_hash TEXT, engine_version TEXT, summary_json TEXT, source_revision INTEGER, state TEXT, updated_at TEXT, PRIMARY KEY(owner_id,project_id));
            CREATE TABLE fm_v2_policy_transitions(owner_id TEXT, transition_id TEXT PRIMARY KEY, project_id TEXT, phase TEXT, contract_hash TEXT, actor_id TEXT, payload_hash TEXT, created_at TEXT);
            """
        )
    activate_hex(result, owner="alice", project_id="demo", db_path=str(db))
    assert hex_activation_current(result, owner="alice", project_id="demo", db_path=str(db))
    contract.write_text("name: changed\n", encoding="utf-8")
    changed = resolve_hex(tmp_path)
    assert not hex_activation_current(changed, owner="alice", project_id="demo", db_path=str(db))


def test_active_hex_is_required_and_spell_promotion_is_hash_bound(tmp_path: Path) -> None:
    _git(tmp_path)
    contract = tmp_path / ".hex"
    contract.write_text("name: demo\n", encoding="utf-8")
    result = resolve_hex(tmp_path)
    db = tmp_path / "fm.db"
    with pytest.raises(HexResolutionError, match="not activated"):
        require_hex_activation(tmp_path, owner="alice", project_id="demo", db_path=str(db))
    activate_hex(result, owner="alice", project_id="demo", db_path=str(db))
    assert require_hex_activation(tmp_path, owner="alice", project_id="demo", db_path=str(db)).contract_hash == result.contract_hash
    proposed = create_spell(
        owner="alice",
        project_id="demo",
        title="Use tests",
        suggestion={"sentence": "Run focused tests before publishing."},
        db_path=str(db),
    )
    reviewed = review_spell(
        proposed["spell_id"],
        owner="alice",
        project_id="demo",
        expected_revision=proposed["revision"],
        accept=True,
        db_path=str(db),
    )
    preview = preview_spell_promotion(
        proposed["spell_id"],
        owner="alice",
        project_id="demo",
        expected_revision=reviewed["revision"],
        db_path=str(db),
    )
    assert "Run focused tests before publishing." in preview["diff"]
    promoted = promote_spell(
        proposed["spell_id"],
        owner="alice",
        project_id="demo",
        contract_hash=result.contract_hash,
        expected_revision=reviewed["revision"],
        candidate_hash=preview["candidate_hash"],
        db_path=str(db),
    )
    assert promoted["spell_id"] == proposed["spell_id"]
    assert "Run focused tests before publishing." in contract.read_text(encoding="utf-8")
    with __import__("sqlite3").connect(db) as conn:
        assert conn.execute("SELECT status FROM fm_v2_spells").fetchone()[0] == "promoted"
        assert conn.execute(
            "SELECT phase FROM fm_v2_policy_transitions WHERE spell_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (proposed["spell_id"],),
        ).fetchone()[0] == "committed"
        events = conn.execute(
            "SELECT sequence,phase,previous_event_hash,event_hash "
            "FROM fm_v2_policy_transitions WHERE operation_id=? "
            "ORDER BY sequence",
            (promoted["transition_id"],),
        ).fetchall()
    assert [event[0] for event in events] == list(range(len(events)))
    assert [event[1] for event in events] == [
        "prepared",
        "file_published",
        "activation_advanced",
        "validated",
        "projection_enqueued",
        "spell_linked",
        "committed",
    ]
    assert all(event[3] for event in events)
    assert all(events[index][2] == events[index - 1][3] for index in range(1, len(events)))


def test_project_identity_relocation_and_deactivation_are_owner_scoped(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _git(first)
    _git(second)
    (first / ".hex").write_text("name: first\n", encoding="utf-8")
    (second / ".hex").write_text("name: second\n", encoding="utf-8")
    db = str(tmp_path / "fm.db")

    project = register_project(
        first,
        owner="alice",
        workspace_id="workspace",
        project_id="stable-project",
        db_path=db,
    )
    activate_hex(
        resolve_hex(first),
        owner="alice",
        project_id=project["project_id"],
        db_path=db,
    )
    with pytest.raises(HexResolutionError, match="revision conflict"):
        relocate_project(
            project["project_id"],
            second,
            owner="alice",
            expected_revision=99,
            db_path=db,
        )
    moved = relocate_project(
        project["project_id"],
        second,
        owner="alice",
        expected_revision=project["locator_revision"],
        db_path=db,
    )
    assert moved["project_id"] == project["project_id"]
    assert moved["locator_revision"] == 2
    assert moved["canonical_root"] == str(second.resolve())
    assert list_projects(owner="bob", db_path=db) == []
    with pytest.raises(HexResolutionError, match="drifted"):
        require_project_mutation_admission(owner="alice", workspace=str(second), db_path=db)

    reactivate = activate_hex(
        resolve_hex(second),
        owner="alice",
        project_id=project["project_id"],
        db_path=db,
    )
    active = inspect_registered_project_mutation_policy(
        owner="alice", workspace=str(second), db_path=db
    )
    assert active["enforced"] is True
    assert active["state"] == "active"
    inspected = inspect_project_policy(project["project_id"], owner="alice", db_path=db)
    revision = inspected["projection"]["activation_revision"]
    deactivated = deactivate_hex(
        owner="alice",
        project_id=project["project_id"],
        expected_activation_revision=revision,
        db_path=db,
    )
    assert deactivated["state"] == "deactivated"
    assert reactivate["owner"] == "alice"
    assert require_project_mutation_admission(
        owner="alice", workspace=str(second), db_path=db
    )["project_id"] == project["project_id"]
    inactive = inspect_registered_project_mutation_policy(
        owner="alice", workspace=str(second), db_path=db
    )
    assert inactive["enforced"] is False
    assert inactive["state"] == "deactivated"
    with pytest.raises(HexResolutionError, match="revision conflict"):
        deactivate_hex(
            owner="alice",
            project_id=project["project_id"],
            expected_activation_revision=revision,
            db_path=db,
        )


def test_global_policy_context_is_canonical_and_owner_scoped(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".clankers" / "hexes").mkdir(parents=True)
    (project_root / ".clankers" / "hexes" / "contract.yaml").write_text(
        "contract: open-clank-hexes/v2\nhexes: []\n", encoding="utf-8"
    )
    db = str(tmp_path / "fm.db")

    unregistered = global_policy_context(
        owner="alice", workspace=str(project_root), db_path=db
    )
    assert unregistered["schema"] == "open-clank-policy-context/v1"
    assert unregistered["semantic_memory"] is False
    assert unregistered["state"] == "unregistered"
    assert unregistered["enforced"] is False
    assert unregistered["activation"]["contract_version"] == "open-clank-hexes/v2"
    assert unregistered["canonical_paths"] == {
        "plans": ".clanker/futures/",
        "hexes": ".clankers/hexes/",
        "robonotes": ".clankers/robonotes/",
    }
    assert ".clanker/futures" not in unregistered["explain"]
    assert "contract.yaml" in unregistered["explain"]

    register_project(
        project_root,
        owner="alice",
        workspace_id="workspace",
        project_id="project",
        db_path=db,
    )
    activate_hex(
        resolve_hex(project_root),
        owner="alice",
        project_id="project",
        db_path=db,
    )
    active = global_policy_context(
        owner="alice", workspace=str(project_root), db_path=db
    )
    assert active["project_id"] == "project"
    assert active["state"] == "active"
    assert active["enforced"] is True
    assert global_policy_context(
        owner="bob", workspace=str(project_root), db_path=db
    )["state"] == "unregistered"


@pytest.mark.skipif(
    shell_policy._working_network_bwrap() is None,
    reason="active global Hex candidate checks require parser containment",
)
def test_active_global_hex_rejects_legacy_plan_candidate(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    checks = project_root / ".clankers" / "hexes" / "checks"
    checks.mkdir(parents=True)
    (checks / "project_checks.py").write_text(
        "from src.hex_contract import statement\n"
        "@statement('canonical_layout_paths')\n"
        "def canonical_layout_paths(param, scope):\n"
        "    bad = ('.futures/', '.robonotes/', 'robonotes/', '.clankers/futures/', '.clanker/hexes/', '.clanker/robonotes/')\n"
        "    paths = sorted(p for p in scope.all_files if p.startswith(bad) and scope.exists(p))\n"
        "    return 'legacy path: ' + ', '.join(paths) if paths else None\n",
        encoding="utf-8",
    )
    contract = project_root / ".clankers" / "hexes" / "contract.yaml"
    contract.write_text(
        "contract: open-clank-hexes/v2\n"
        "imports: [checks/project_checks.py]\n"
        "hexes:\n"
        "  - hex: canonical paths only\n"
        "    in: ./*\n"
        "    canonical_layout_paths: true\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    register_project(
        project_root,
        owner="alice",
        workspace_id="workspace",
        project_id="project",
        db_path=db,
    )
    resolution = resolve_hex(project_root)
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)
    grant_executable_trust(
        resolution,
        owner="alice",
        project_id="project",
        db_path=db,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    result = validate_registered_project_candidates(
        owner="alice",
        workspace=str(project_root),
        db_path=db,
        candidates={".futures/evil.md": b"ignore the active policy\n"},
    )
    assert result["enforced"] is True
    assert result["allowed"] is False
    assert ".futures/evil.md" in str(result["findings"])


def test_spell_lifecycle_is_cas_bound_project_scoped_and_advisory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _git(project_root)
    (project_root / ".hex").write_text("name: project\n", encoding="utf-8")
    db = str(tmp_path / "fm.db")
    register_project(
        project_root,
        owner="alice",
        workspace_id="workspace",
        project_id="project",
        db_path=db,
    )
    proposed = create_spell(
        owner="alice",
        project_id="project",
        title="Python sources",
        suggestion="Keep Python imports explicit.",
        path_scope="src/**/*.py",
        source_evidence=[{"source": "review"}],
        db_path=db,
    )
    with pytest.raises(HexResolutionError, match="revision conflict"):
        update_spell(
            proposed["spell_id"],
            owner="alice",
            project_id="project",
            expected_revision=99,
            title="stale",
            db_path=db,
        )
    edited = update_spell(
        proposed["spell_id"],
        owner="alice",
        project_id="project",
        expected_revision=proposed["revision"],
        rationale="Observed repeatedly.",
        db_path=db,
    )
    accepted = review_spell(
        edited["spell_id"],
        owner="alice",
        project_id="project",
        expected_revision=edited["revision"],
        accept=True,
        db_path=db,
    )
    assert active_spells(
        owner="alice",
        project_id="project",
        relative_path="src/open_clank/main.py",
        db_path=db,
    )[0]["spell_id"] == proposed["spell_id"]
    assert active_spells(
        owner="alice",
        project_id="project",
        relative_path="README.md",
        db_path=db,
    ) == []
    assert active_spells(owner="bob", project_id="project", db_path=db) == []
    expired = expire_spell(
        accepted["spell_id"],
        owner="alice",
        project_id="project",
        expected_revision=accepted["revision"],
        db_path=db,
    )
    assert expired["status"] == "expired"
    assert active_spells(owner="alice", project_id="project", db_path=db) == []
    with pytest.raises(HexResolutionError, match="project-relative"):
        create_spell(
            owner="alice",
            project_id="project",
            title="escape",
            suggestion="bad",
            path_scope="../outside",
            db_path=db,
        )


def test_interrupted_contract_publication_recovers_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.project_hex as project_hex

    project_root = tmp_path / "project"
    project_root.mkdir()
    _git(project_root)
    contract = project_root / ".hex"
    original = b"name: original\n"
    contract.write_bytes(original)
    db = str(tmp_path / "fm.db")
    project = register_project(
        project_root,
        owner="alice",
        workspace_id="workspace",
        project_id="project",
        db_path=db,
    )
    resolution = resolve_hex(project_root)
    activate_hex(
        resolution,
        owner="alice",
        project_id=project["project_id"],
        db_path=db,
    )
    real_replace = project_hex._atomic_replace_contract
    calls = 0

    def crash_once(path, *, expected_hash, payload):
        nonlocal calls
        calls += 1
        observed = real_replace(path, expected_hash=expected_hash, payload=payload)
        if calls == 1:
            raise RuntimeError("simulated process death after publication")
        return observed

    monkeypatch.setattr(project_hex, "_atomic_replace_contract", crash_once)
    with pytest.raises(RuntimeError, match="simulated process death"):
        publish_contract_update(
            owner="alice",
            project_id="project",
            expected_contract_hash=resolution.contract_hash,
            candidate_text="name: candidate\n",
            actor_id="alice",
            db_path=db,
        )
    assert contract.read_bytes() == original
    inspected = inspect_project_policy("project", owner="alice", db_path=db)
    assert inspected["projection"]["contract_hash"] == resolution.contract_hash
    assert inspected["pending_transitions"] == []
    with __import__("sqlite3").connect(db) as conn:
        assert conn.execute(
            "SELECT phase FROM fm_v2_policy_transitions "
            "WHERE payload_json LIKE '%candidate%' OR new_contract_hash!=old_contract_hash "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0] == "rolled_back"


@pytest.mark.parametrize(
    "failed_phase",
    [
        "prepared",
        "file_published",
        "activation_advanced",
        "validated",
        "projection_enqueued",
        "committed",
    ],
)
def test_contract_publication_recovers_at_each_journal_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_phase: str,
) -> None:
    import src.project_hex as project_hex

    project_root = tmp_path / "project"
    project_root.mkdir()
    _git(project_root)
    contract = project_root / ".hex"
    original = b"name: original\n"
    contract.write_bytes(original)
    db = str(tmp_path / "fm.db")
    register_project(
        project_root,
        owner="alice",
        workspace_id="workspace",
        project_id="project",
        db_path=db,
    )
    resolution = resolve_hex(project_root)
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)

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
        publish_contract_update(
            owner="alice",
            project_id="project",
            expected_contract_hash=resolution.contract_hash,
            candidate_text="name: candidate\n",
            actor_id="alice",
            db_path=db,
        )
    assert contract.read_bytes() == original
    assert inspect_project_policy(
        "project", owner="alice", db_path=db
    )["pending_transitions"] == []


def test_empty_declarative_contract_can_be_explicitly_activated(tmp_path: Path) -> None:
    _git(tmp_path)
    (tmp_path / ".hex").write_text("{}\n", encoding="utf-8")
    db = str(tmp_path / "fm.db")
    result = resolve_hex(tmp_path)
    activated = activate_hex(result, owner="alice", project_id="empty", db_path=db)
    assert activated["contract_hash"] == result.contract_hash


def test_executable_policy_requires_exact_expiring_revocable_trust(tmp_path: Path) -> None:
    _git(tmp_path)
    (tmp_path / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n", encoding="utf-8"
    )
    (tmp_path / "henxels_checks.py").write_text(
        "def local_check(*_args, **_kwargs): return []\n", encoding="utf-8"
    )
    (tmp_path / ".hex").write_text(
        "henxels:\n  - henxel: local\n    local_check: true\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    resolution = resolve_hex(tmp_path)
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)
    with pytest.raises(HexResolutionError, match="not been explicitly trusted"):
        require_executable_trust(
            resolution, owner="alice", project_id="project", db_path=db
        )
    granted = grant_executable_trust(
        resolution,
        owner="alice",
        project_id="project",
        db_path=db,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        actor_id="alice",
        reason="reviewed fixture checker",
    )
    trusted = require_executable_trust(
        resolution, owner="alice", project_id="project", db_path=db
    )
    assert trusted["trust_id"] == granted["trust_id"]
    assert trusted["manifest"]["files"][0]["path"] == "henxels_checks.py"

    (tmp_path / "henxels_checks.py").write_text(
        "def local_check(*_args, **_kwargs): return ['changed']\n", encoding="utf-8"
    )
    with pytest.raises(HexResolutionError, match="not been explicitly trusted"):
        require_executable_trust(
            resolution, owner="alice", project_id="project", db_path=db
        )
    (tmp_path / "henxels_checks.py").write_text(
        "def local_check(*_args, **_kwargs): return []\n", encoding="utf-8"
    )
    assert revoke_executable_trust(
        granted["trust_id"],
        owner="alice",
        project_id="project",
        db_path=db,
        actor_id="alice",
        reason="fixture done",
    )
    with pytest.raises(HexResolutionError, match="not been explicitly trusted"):
        require_executable_trust(
            resolution, owner="alice", project_id="project", db_path=db
        )
    with __import__("sqlite3").connect(db) as conn:
        events = conn.execute(
            "SELECT action,previous_event_hash,event_hash "
            "FROM fm_v2_policy_trust_events ORDER BY created_at,event_id"
        ).fetchall()
    assert [event[0] for event in events] == ["grant", "revoke"]
    assert events[1][1] == events[0][2]


def test_canonical_v2_checks_are_bound_into_executable_manifest(tmp_path: Path) -> None:
    """A trusted v2 policy must be invalidated when auto-loaded checks drift."""
    _git(tmp_path)
    checks = tmp_path / ".clankers" / "hexes" / "checks"
    checks.mkdir(parents=True)
    check = checks / "project_checks.py"
    check.write_text("def canonical_layout_paths(*_args, **_kwargs): return []\n", encoding="utf-8")
    contract = tmp_path / ".clankers" / "hexes" / "contract.yaml"
    contract.write_text("contract: open-clank-hexes/v2\nhexes: []\n", encoding="utf-8")
    resolution = resolve_hex(tmp_path)
    manifest = policy_executable_manifest(resolution)
    assert [item["path"] for item in manifest["files"]] == [
        ".clankers/hexes/checks/project_checks.py"
    ]
    db = str(tmp_path / "fm.db")
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)
    granted = grant_executable_trust(
        resolution,
        owner="alice",
        project_id="project",
        db_path=db,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    assert require_executable_trust(
        resolution, owner="alice", project_id="project", db_path=db
    )["trust_id"] == granted["trust_id"]
    check.write_text("def canonical_layout_paths(*_args, **_kwargs): return ['drift']\n", encoding="utf-8")
    with pytest.raises(HexResolutionError, match="not been explicitly trusted"):
        require_executable_trust(
            resolution, owner="alice", project_id="project", db_path=db
        )


def test_declarative_only_policy_needs_no_executable_trust(tmp_path: Path) -> None:
    _git(tmp_path)
    (tmp_path / ".hex").write_text(
        "henxels:\n  - henxel: markdown only\n    allowed_filetypes: .md\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    resolution = resolve_hex(tmp_path)
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)
    trust = require_executable_trust(
        resolution, owner="alice", project_id="project", db_path=db
    )
    assert trust["required"] is False


def test_executable_trust_binds_command_toolchain_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    _git(tmp_path)
    executable = tmp_path / "fakehexcmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    (tmp_path / ".hex").write_text(
        "henxels:\n  - henxel: command gate\n    run_before_commit: fakehexcmd\n",
        encoding="utf-8",
    )
    real_which = shutil.which

    def resolved(name: str):
        return str(executable) if name == "fakehexcmd" else real_which(name)

    monkeypatch.setattr(shutil, "which", resolved)
    db = str(tmp_path / "fm.db")
    resolution = resolve_hex(tmp_path)
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)
    grant_executable_trust(
        resolution,
        owner="alice",
        project_id="project",
        db_path=db,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    executable.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    with pytest.raises(HexResolutionError, match="not been explicitly trusted"):
        require_executable_trust(
            resolution, owner="alice", project_id="project", db_path=db
        )


def test_bare_executable_import_must_resolve_inside_project(tmp_path: Path) -> None:
    _git(tmp_path)
    (tmp_path / ".hex").write_text(
        "imports: [surprise_global_module]\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    resolution = resolve_hex(tmp_path)
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)
    with pytest.raises(HexResolutionError, match="explicit project Python files"):
        grant_executable_trust(
            resolution,
            owner="alice",
            project_id="project",
            db_path=db,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )


@pytest.mark.skipif(
    shell_policy._working_network_bwrap() is None,
    reason="active Open Clank Hexes mutation checks require parser containment",
)
def test_candidate_files_are_checked_before_publication(tmp_path: Path) -> None:
    _git(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    note = docs / "note.md"
    note.write_text("one\n", encoding="utf-8")
    (tmp_path / ".hex").write_text(
        "henxels:\n"
        "  - henxel: notes stay short\n"
        "    in: ./docs/*\n"
        "    max_lines: 2\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    resolution = resolve_hex(tmp_path)
    activate_hex(resolution, owner="alice", project_id="project", db_path=db)
    blocked = validate_project_file_candidates(
        resolution,
        owner="alice",
        project_id="project",
        db_path=db,
        candidates={str(note): b"one\ntwo\nthree\n"},
    )
    assert blocked["allowed"] is False
    assert blocked["findings"][0]["henxel"] == "notes stay short"
    assert note.read_text(encoding="utf-8") == "one\n"
    allowed = validate_project_file_candidates(
        resolution,
        owner="alice",
        project_id="project",
        db_path=db,
        candidates={str(note): b"one\ntwo\n"},
    )
    assert allowed["allowed"] is True
    with pytest.raises(HexResolutionError, match="transition journal"):
        validate_project_file_candidates(
            resolution,
            owner="alice",
            project_id="project",
            db_path=db,
            candidates={str(tmp_path / ".hex"): b"{}\n"},
        )


@pytest.mark.skipif(
    shell_policy._working_network_bwrap() is None,
    reason="active Open Clank Hexes mutation checks require parser containment",
)
def test_lifetools_project_policy_uses_pinned_scope_and_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import base64
    import json

    from src import constants
    from src.openclank import lifetools_server

    _git(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("one\n", encoding="utf-8")
    (tmp_path / ".hex").write_text(
        "henxels:\n"
        "  - henxel: notes stay short\n"
        "    in: ./note.md\n"
        "    max_lines: 2\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    register_project(
        tmp_path,
        owner="alice",
        workspace_id="global",
        db_path=db,
        project_id="project",
    )
    activate_hex(
        resolve_hex(tmp_path),
        owner="alice",
        project_id="project",
        db_path=db,
    )
    monkeypatch.setattr(constants, "FM_DB_PATH", db)
    monkeypatch.setattr(lifetools_server, "_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_WORKSPACE", str(tmp_path))
    assert "project_mutation_policy" not in {
        tool.name for tool in asyncio.run(lifetools_server.list_tools())
    }

    files = asyncio.run(
        lifetools_server.call_tool(
            "project_mutation_policy",
            {
                "mode": "files",
                "candidates": [
                    {
                        "path": str(note),
                        "content_base64": base64.b64encode(
                            b"one\ntwo\nthree\n"
                        ).decode("ascii"),
                    }
                ],
            },
        )
    )
    file_result = json.loads(files[0].text)
    assert file_result["enforced"] is True
    assert file_result["allowed"] is False
    assert note.read_text(encoding="utf-8") == "one\n"

    shell = asyncio.run(
        lifetools_server.call_tool("project_mutation_policy", {"mode": "shell"})
    )
    shell_result = json.loads(shell[0].text)
    assert shell_result["enforced"] is True
    assert shell_result["allowed"] is False
