"""Current-rule admission and independent executable trust on isolated stores."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from src import project_hex as policy
from src.policy_local import PolicyLocalError, _verify_profile_file, issue_local_profile


def enabled(tmp_path, text="hexes: []\n"):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    contract = root / ".hex"
    contract.write_text(text)
    db = str(tmp_path / "policy.sqlite3")
    policy.activate_hex(policy.resolve_hex(root), owner="alice", project_id="project", db_path=db)
    return root, contract, db


def resolve(root, db):
    return policy.require_hex_activation(root, owner="alice", project_id="project", db_path=db)


def projection(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM fm_v2_policy_projections").fetchone())


def approve(root, db, **kwargs):
    return policy.grant_executable_trust(policy.resolve_hex(root), owner="alice", project_id="project", db_path=db,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), **kwargs)


def trusted(root, db, **kwargs):
    return policy.require_executable_trust(resolve(root, db), owner="alice", project_id="project", db_path=db, **kwargs)


def direct_fixture_worker(monkeypatch):
    """Exercise actual worker semantics; OS confinement is qualified separately.

    This subprocess only receives synthetic temporary files and no credentials.
    macOS cannot execute the Linux parser-containment adapter.
    """
    from src import shell_policy
    def fixture(argv, *, input_descriptor, **kwargs):
        return [sys.executable, "-B", "-c",
            "import sys;sys.path.insert(0," + repr(str(Path(__file__).resolve().parents[1]))
            + ");from src.hex_policy_worker import main;main(" + repr(f"/dev/fd/{input_descriptor}") + ")"], "fixture"
    monkeypatch.setattr(shell_policy, "contained_parser_argv", fixture)


def validate(root, db, candidates):
    return policy.validate_registered_project_candidates(owner="alice", workspace=str(root), db_path=db, candidates=candidates)


def test_current_rules_enforced_without_new_authority_revision(tmp_path, monkeypatch):
    root, contract, db = enabled(tmp_path, "hexes:\n- hex: notes only\n  in: './notes/*'\n  allowed_filetypes: .md\n")
    direct_fixture_worker(monkeypatch)
    assert validate(root, db, {"notes/test.md": b"allowed"})["allowed"]
    before = projection(db)
    contract.write_text("hexes:\n- hex: text only\n  in: './notes/*'\n  allowed_filetypes: .txt\n")
    result = validate(root, db, {"notes/test.md": b"blocked by current rule"})
    assert not result["allowed"] and result["findings"][0]["hex"] == "text only"
    after = projection(db)
    assert after["activation_revision"] == before["activation_revision"]
    assert after["source_revision"] == before["source_revision"] + 1
    assert after["contract_hash"] == policy.resolve_hex(root).contract_hash


def test_hash_only_drift_recovers_but_stale_operation_snapshot_does_not(tmp_path):
    root, contract, db = enabled(tmp_path)
    old = resolve(root, db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE fm_v2_policy_projections SET state='drifted'")
    contract.write_text("# formatting revision\nhexes: []\n")
    current = resolve(root, db)
    assert current.contract_hash != old.contract_hash
    assert projection(db)["state"] == "active"
    assert not policy.hex_activation_current(old, owner="alice", project_id="project", db_path=db)
    with pytest.raises(policy.HexResolutionError, match="changed after activation"):
        with policy.verified_contract_snapshot(old):
            pytest.fail("stale snapshot yielded")


@pytest.mark.parametrize("axis", ["owner", "root", "worktree", "git", "engine", "path", "missing"])
def test_identity_and_missing_contract_still_fail_closed(tmp_path, axis):
    root, contract, db = enabled(tmp_path)
    if axis == "missing":
        contract.unlink()
    elif axis != "owner":
        field = {"root": "root_identity", "worktree": "worktree_identity", "git": "git_identity",
                 "engine": "engine_version", "path": "contract_path"}[axis]
        with sqlite3.connect(db) as conn:
            conn.execute(f"UPDATE fm_v2_policy_projections SET {field}='different'")
    with pytest.raises(policy.HexResolutionError):
        policy.require_hex_activation(root, owner="bob" if axis == "owner" else "alice", project_id="project", db_path=db)


def test_unfinished_transition_cannot_auto_admit_new_bytes(tmp_path):
    root, contract, db = enabled(tmp_path)
    old = projection(db)
    with sqlite3.connect(db) as conn:
        policy._append_transition_event(conn, owner="alice", project_id="project", operation_id="pending",
            phase="prepared", values={"contract_hash": old["contract_hash"]})
    contract.write_text("# not committed\nhexes: []\n")
    with pytest.raises(policy.HexResolutionError):
        resolve(root, db)
    assert projection(db)["contract_hash"] == old["contract_hash"]
    assert projection(db)["source_revision"] == old["source_revision"]


def test_initial_enable_and_explicit_disable_remain_deliberate(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".hex").write_text("hexes: []\n")
    db = str(tmp_path / "policy.db")
    policy.register_project(root, owner="alice", workspace_id="ws", project_id="project", db_path=db)
    assert not policy.inspect_registered_project_mutation_policy(owner="alice", workspace=str(root), db_path=db)["enforced"]
    with pytest.raises(policy.HexResolutionError):
        resolve(root, db)
    policy.activate_hex(policy.resolve_hex(root), owner="alice", project_id="project", db_path=db)
    policy.deactivate_hex(owner="alice", project_id="project", expected_activation_revision=projection(db)["activation_revision"], db_path=db)
    (root / ".hex").write_text("# disabled stays disabled\nhexes: []\n")
    assert not policy.inspect_registered_project_mutation_policy(owner="alice", workspace=str(root), db_path=db)["enforced"]
    assert projection(db)["state"] == "deactivated"


def test_profile_survives_rule_revision_but_not_explicit_disable(tmp_path):
    root, contract, db = enabled(tmp_path)
    state = tmp_path / "profiles"
    profile = issue_local_profile("alice", owner="alice", project_id="project", db_path=db, state_dir=state)
    contract.write_text("# current declarative version\nhexes: []\n")
    kwargs = dict(state_dir=state, peer_uid=os.getuid(), action="check", project_id="project", project_root=root.resolve(), db_path=db)
    assert _verify_profile_file(Path(profile["path"]), **kwargs)["owner"] == "alice"
    policy.deactivate_hex(owner="alice", project_id="project", expected_activation_revision=projection(db)["activation_revision"], db_path=db)
    with pytest.raises(PolicyLocalError):
        _verify_profile_file(Path(profile["path"]), **kwargs)


def checker(tmp_path):
    root, contract, db = enabled(tmp_path, "hexes:\n- hex: custom\n  custom_check: {mode: strict}\n")
    (root / "hexes_checks.py").write_text("def custom_check(param, scope): return []\n")
    return root, contract, db


def test_custom_approval_survives_formatting_and_unrelated_rules(tmp_path):
    root, contract, db = checker(tmp_path)
    grant = approve(root, db)
    old = policy.policy_executable_manifest(policy.resolve_hex(root))
    contract.write_text("# harmless formatting\nhexes:\n- hex: unrelated declarative rule\n  allowed_filetypes: .md\n- hex: custom\n  custom_check:\n    mode: strict\n")
    new = policy.policy_executable_manifest(resolve(root, db))
    assert old["contract_hash"] != new["contract_hash"]
    assert old["manifest_hash"] == new["manifest_hash"]
    assert trusted(root, db)["trust_id"] == grant["trust_id"]


@pytest.mark.parametrize("change", ["parameter", "scope", "script", "capability", "hook", "hook-order", "helper"])
def test_changed_executable_inputs_require_new_approval(tmp_path, change):
    root, contract, db = checker(tmp_path)
    if change == "helper":
        (root / "hexes_checks.py").write_text("import helper\ndef custom_check(param, scope): return helper.check()\n")
        (root / "helper.py").write_text("def check(): return []\n")
    if change == "hook-order":
        contract.write_text(contract.read_text() + "- hex: hooks\n  run_before_commit: ['true', 'false']\n")
    approve(root, db)
    if change == "parameter":
        contract.write_text(contract.read_text().replace("strict", "unsafe"))
    elif change == "scope":
        contract.write_text(contract.read_text() + "  in: './other/*'\n")
    elif change == "script":
        (root / "hexes_checks.py").write_text("def custom_check(param, scope): return ['different']\n")
    elif change == "helper":
        (root / "helper.py").write_text("def check(): return ['different helper']\n")
    elif change == "hook":
        contract.write_text(contract.read_text() + "- hex: hook\n  run_before_commit: 'true'\n")
    elif change == "hook-order":
        contract.write_text(contract.read_text().replace("['true', 'false']", "['false', 'true']"))
    with pytest.raises(policy.HexResolutionError, match="not been explicitly trusted"):
        trusted(root, db, **({"capabilities": ["extra"]} if change == "capability" else {}))
    # A deliberate owner edit can be saved, reviewed, and explicitly re-trusted.
    approve(root, db, **({"capabilities": ["extra"]} if change == "capability" else {}))
    assert trusted(root, db, **({"capabilities": ["extra"]} if change == "capability" else {}))["required"]


def test_hook_script_dependency_and_legacy_grant_fail_closed(tmp_path):
    root, contract, db = enabled(tmp_path, "hexes:\n- hex: hook\n  run_before_commit: 'python check.py'\n")
    script = root / "check.py"
    script.write_text("print('fixture')\n")
    approve(root, db)
    script.write_text("print('changed fixture')\n")
    with pytest.raises(policy.HexResolutionError, match="not been explicitly trusted"):
        trusted(root, db)
    approve(root, db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE fm_v2_policy_trust SET contract_hash=?", (policy.resolve_hex(root).contract_hash,))
    with pytest.raises(policy.HexResolutionError, match="not been explicitly trusted"):
        trusted(root, db)


@pytest.mark.parametrize("text", ["import importlib\nimportlib.import_module('helper')\n", "exec('pass')\n"])
def test_unsupported_dynamic_python_is_explicitly_untrusted(tmp_path, text):
    root, contract, db = checker(tmp_path)
    (root / "hexes_checks.py").write_text(text)
    with pytest.raises(policy.HexResolutionError, match="dynamic executable policy"):
        approve(root, db)


def test_rule_and_checker_edits_stay_outside_generic_candidates(tmp_path):
    root, contract, db = checker(tmp_path)
    approve(root, db)
    for candidate, message in [(".hex", "transition journal"), ("hexes_checks.py", "owner-approved edit")]:
        with pytest.raises(policy.HexResolutionError, match=message):
            validate(root, db, {candidate: b"changed"})


def test_current_status_and_general_composition_agree(tmp_path):
    from src.general_hex_composition import workspace_source
    root, contract, db = enabled(tmp_path)
    contract.write_text("hexes:\n- hex: current guidance\n")
    state = policy.global_policy_context(owner="alice", workspace=str(root), db_path=db)
    inspected = policy.inspect_project_policy("project", owner="alice", db_path=db)
    source = workspace_source("alice", str(root), db)
    assert state["state"] == inspected["policy"]["state"] == source["state"] == "active"
    assert source["rules"] == [{"hex": "current guidance"}]


def test_actual_worker_uses_trusted_checker_snapshot(tmp_path, monkeypatch):
    root, contract, db = checker(tmp_path)
    (root / "hexes_checks.py").write_text("from src.hex_contract import statement\n@statement('custom_check')\ndef check(param, scope): return []\n")
    approve(root, db)
    direct_fixture_worker(monkeypatch)
    assert validate(root, db, {"notes.md": b"fixture"})["allowed"]


@pytest.mark.parametrize("command", ["python check.py; true", "python check.py|cat", "python check"])
def test_compound_and_extensionless_hook_dependencies_are_bound(tmp_path, command):
    root, contract, db = enabled(tmp_path, "hexes:\n- hex: hook\n  run_before_commit: " + json.dumps(command) + "\n")
    script = root / ("check" if command == "python check" else "check.py")
    script.write_text("import helper\n")
    helper = root / "helper.py"
    helper.write_text("value = 1\n")
    approve(root, db)
    helper.write_text("value = 2\n")
    with pytest.raises(policy.HexResolutionError, match="not been explicitly trusted"):
        trusted(root, db)


@pytest.mark.parametrize("change", ["import-order", "check-order", "parameter-order"])
def test_executable_import_and_invocation_order_are_bound(tmp_path, change):
    root, contract, db = checker(tmp_path)
    (root / "first.py").write_text("first = True\n")
    (root / "second.py").write_text("second = True\n")
    text = "imports: [first.py, second.py]\nhexes:\n- hex: ordered custom calls\n  alpha: {first: 1, second: 2}\n  beta: true\n"
    contract.write_text(text)
    approve(root, db)
    if change == "import-order":
        text = text.replace("[first.py, second.py]", "[second.py, first.py]")
    elif change == "check-order":
        text = text.replace("  alpha: {first: 1, second: 2}\n  beta: true", "  beta: true\n  alpha: {first: 1, second: 2}")
    else:
        text = text.replace("{first: 1, second: 2}", "{second: 2, first: 1}")
    contract.write_text(text)
    with pytest.raises(policy.HexResolutionError, match="not been explicitly trusted"):
        trusted(root, db)


@pytest.mark.parametrize("command", ["./check", "sh tools/check.sh"])
def test_unsupported_shell_script_closure_is_explicitly_untrusted(tmp_path, command):
    root, contract, db = enabled(tmp_path, "hexes:\n- hex: hook\n  run_before_commit: " + json.dumps(command) + "\n")
    (root / "tools").mkdir()
    (root / "check").write_text(". helper.sh\n")
    (root / "tools/check.sh").write_text(". helper.sh\n")
    with pytest.raises(policy.HexResolutionError, match="policy (scripts|dependencies)"):
        approve(root, db)


def test_aliased_dynamic_import_is_explicitly_untrusted(tmp_path):
    root, contract, db = checker(tmp_path)
    (root / "hexes_checks.py").write_text("from importlib import import_module as load\nload('helper')\n")
    with pytest.raises(policy.HexResolutionError, match="dynamic executable policy"):
        approve(root, db)


def test_live_hook_change_never_executes_unapproved_bytes(tmp_path, monkeypatch):
    import subprocess
    root, contract, db = enabled(tmp_path, "hexes:\n- hex: hook\n  run_before_commit: 'python3 check.py'\n")
    script = root / "check.py"
    approved = tmp_path / "approved-marker"
    unapproved = tmp_path / "unapproved-marker"
    script.write_text("from pathlib import Path\nPath(" + repr(str(approved)) + ").touch()\n")
    approve(root, db)
    direct_fixture_worker(monkeypatch)
    original = subprocess.run
    def change_before_worker(argv, **kwargs):
        if argv[0] == sys.executable and "src.hex_policy_worker" in str(argv):
            script.write_text("from pathlib import Path\nPath(" + repr(str(unapproved)) + ").touch()\n")
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", change_before_worker)
    with pytest.raises(policy.HexResolutionError, match="not been explicitly trusted"):
        policy.validate_project_file_candidates(resolve(root, db), owner="alice", project_id="project", db_path=db,
            candidates={"note.md": b"fixture"}, stage="pre-commit")
    assert approved.exists()
    assert not unapproved.exists()


def test_workspace_routes_follow_current_rules_and_separate_trust(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import general_hex_exchange_routes as routes
    root, contract, db = checker(tmp_path)
    monkeypatch.setattr(routes, "prepare_hex_authority", lambda value: value)
    app = FastAPI()
    app.include_router(routes.setup_general_hex_exchange_routes(db_path_resolver=lambda: db,
        owner_resolver=lambda request: "alice", workspace_resolver=lambda request, workspace: str(root)))
    client = TestClient(app)
    contract.write_text("# current\n" + contract.read_text())
    result = client.post("/api/hexes/workspace/inspect", json={"workspace_id": "workspace"})
    assert result.status_code == 200
    assert result.json()["state"] == "active"
    assert result.json()["executable_policy"]["state"] == "untrusted"
    contexts = client.get("/api/hexes/contexts").json()
    assert contexts["workspaces"][0]["activation_state"] == "active"
    approve(root, db)
    assert client.post("/api/hexes/workspace/inspect", json={"workspace_id": "workspace"}).json()["executable_policy"]["state"] == "trusted"


@pytest.mark.parametrize("command,filename", [("python3 -B check.txt", "check.txt"), ("python3 -u check.pyw", "check.pyw")])
def test_unsupported_flagged_python_form_cannot_omit_helper(tmp_path, command, filename):
    root, contract, db = enabled(tmp_path, "hexes:\n- hex: hook\n  run_before_commit: " + json.dumps(command) + "\n")
    (root / filename).write_text("import helper\n")
    (root / "helper.py").write_text("value = 1\n")
    with pytest.raises(policy.HexResolutionError, match="supported .py form"):
        approve(root, db)


def test_auto_refresh_keeps_summary_bounded_for_recursive_declarative_yaml(tmp_path):
    root, contract, db = enabled(tmp_path)
    contract.write_text("hexes: []\nsettings: &loop\n  self: *loop\n")
    assert resolve(root, db).contract["settings"]["self"] is not None
    assert set(json.loads(projection(db)["summary_json"])) == {"project_root", "diagnostics"}


@pytest.mark.parametrize("kind", ["cycle", "expansion"])
def test_executable_argument_aliases_fail_with_bounded_diagnostic(tmp_path, kind):
    root, contract, db = checker(tmp_path)
    if kind == "cycle":
        contract.write_text("hexes:\n- hex: custom\n  custom_check: &cycle\n    nested: *cycle\n")
    else:
        aliases = ["settings:", "  a0: &a0 [x]"]
        aliases += [f"  a{index}: &a{index} [*a{index-1}, *a{index-1}]" for index in range(1, 25)]
        contract.write_text("hexes: []\n" + "\n".join(aliases) + "\n")
    with pytest.raises(policy.HexResolutionError, match="cyclic|bounded serialization"):
        approve(root, db)
