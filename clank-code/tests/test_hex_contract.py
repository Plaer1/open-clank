from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.hex_contract import (
    CandidateDiff,
    HexContractError,
    evaluate_contract,
    explain_contract,
    load_contract,
    render_agent_digest,
    replace_agent_digest,
    find_contract,
)
from src.hex_contract_cli import main
from src.project_hex import resolve_hex


def _write_contract(root: Path, body: str) -> Path:
    contract = root / ".hex"
    contract.write_text("contract: open-clank-hexes/v1\n" + body, encoding="utf-8")
    return contract


def test_first_party_vocabulary_and_hidden_path_explain(tmp_path: Path) -> None:
    contract = _write_contract(
        tmp_path,
        "hexes:\n"
        "  - hex: plans stay markdown\n"
        "    in: ./.futures/*\n"
        "    allowed_filetypes: .md\n",
    )
    parsed = load_contract(contract)
    assert parsed.vocabulary == "hexes"
    explained = explain_contract(parsed, ".futures/plan.md")
    assert [item["hex"] for item in explained] == ["plans stay markdown"]


def test_new_contract_rejects_mixed_legacy_vocabulary(tmp_path: Path) -> None:
    contract = _write_contract(tmp_path, "hexes: []\nhenxels: []\n")
    with pytest.raises(HexContractError, match="cannot mix"):
        load_contract(contract)


def test_legacy_contract_is_a_reader_not_an_external_dependency(tmp_path: Path) -> None:
    contract = tmp_path / "henxels.yaml"
    contract.write_text(
        "henxels:\n  - henxel: markdown only\n    allowed_filetypes: .md\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.txt").write_text("bad\n", encoding="utf-8")
    result = evaluate_contract(contract, root=tmp_path, files=["bad.txt"])
    assert result["vocabulary"] == "legacy_henxels"
    assert result["allowed"] is False
    assert result["findings"][0]["hex"] == "markdown only"


def test_internal_custom_check_import_uses_hexes_alias(tmp_path: Path) -> None:
    contract = _write_contract(
        tmp_path,
        "hexes:\n  - hex: local check\n    local_check: true\n",
    )
    (tmp_path / "hexes_checks.py").write_text(
        "from hexes import statement\n"
        "@statement('local_check')\n"
        "def local_check(param):\n"
        "    return None if param else 'enable it'\n",
        encoding="utf-8",
    )
    assert evaluate_contract(contract, root=tmp_path, files=[])["allowed"] is True


def test_unknown_check_fails_closed(tmp_path: Path) -> None:
    contract = _write_contract(
        tmp_path,
        "hexes:\n  - hex: known contract only\n    mystery_check: true\n",
    )
    result = evaluate_contract(contract, root=tmp_path, files=[])
    assert result["allowed"] is False
    assert "unknown Hexes check" in result["findings"][0]["details"][0]


def test_candidate_diff_drives_changed_with_and_delete_guard(tmp_path: Path) -> None:
    contract = _write_contract(
        tmp_path,
        "settings:\n  confirm_before_deleting: {over_lines: 1}\n"
        "hexes:\n"
        "  - hex: glue changes carry notes\n"
        "    changed_with: {when: ['src/**'], expect: ['.robonotes/**']}\n",
    )
    source = tmp_path / "source"
    candidate = tmp_path / "candidate"
    source.mkdir()
    candidate.mkdir()
    diff = CandidateDiff(
        candidate,
        source,
        modified=frozenset({"src/a.py"}),
        deleted=frozenset({"old.txt"}),
        old_bytes={"src/a.py": b"a\nb\n", "old.txt": b"x\n"},
        new_bytes={"src/a.py": b"a\n", "old.txt": None},
    )
    result = evaluate_contract(
        contract,
        root=candidate,
        files=["src/a.py"],
        diff=diff,
        stage="pre-commit",
        command_root=tmp_path,
    )
    sentences = {item["hex"] for item in result["findings"]}
    assert "glue changes carry notes" in sentences
    assert "Destructive changes require an explicit Open Clank Hexes blessing" in sentences


def test_agent_digest_replaces_legacy_generated_block(tmp_path: Path) -> None:
    contract = load_contract(
        _write_contract(tmp_path, "hexes:\n  - hex: one rule\n    max_lines: 2\n")
    )
    old = "<!-- henxels:begin -->\nold\n<!-- henxels:end -->\nTAIL\n"
    updated = replace_agent_digest(old, render_agent_digest(contract))
    assert "openclank-hexes:begin" in updated
    assert "henxels:begin" not in updated
    assert updated.endswith("TAIL\n")


def test_first_party_dot_hex_precedes_declared_legacy_file(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    primary = _write_contract(tmp_path, "hexes: []\n")
    (tmp_path / "henxels.yaml").write_text("henxels: []\n", encoding="utf-8")
    resolved = resolve_hex(tmp_path)
    assert resolved.contract_path == str(primary)
    assert resolved.diagnostics and "ignored legacy contract" in resolved.diagnostics[0]


def test_canonical_v2_contract_precedes_root_dot_hex(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    canonical = tmp_path / ".clankers" / "hexes" / "contract.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("contract: open-clank-hexes/v2\nhexes: []\n", encoding="utf-8")
    (tmp_path / ".hex").write_text("contract: open-clank-hexes/v1\nhexes: []\n", encoding="utf-8")
    assert find_contract(tmp_path / "src") == canonical
    resolved = resolve_hex(tmp_path)
    assert resolved.contract_path == str(canonical)
    assert any("canonical v2" in item for item in resolved.diagnostics)


def test_cli_check_and_explain_are_first_party(tmp_path: Path, capsys) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    contract = _write_contract(tmp_path, "hexes: []\n")
    assert main(["check", str(tmp_path), "--config", str(contract)]) == 0
    assert "open-clank-hexes/1" in capsys.readouterr().out
    assert main(["explain", str(tmp_path / ".hidden"), "--config", str(contract), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"hexes": [], "path": ".hidden"}


def test_cli_discovers_canonical_v2_contract_without_config(tmp_path: Path, capsys) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    canonical = tmp_path / ".clankers" / "hexes" / "contract.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("contract: open-clank-hexes/v2\nhexes: []\n", encoding="utf-8")
    assert main(["check", str(tmp_path)]) == 0
    assert "open-clank-hexes/2" in capsys.readouterr().out


def test_policy_worker_source_has_no_external_henxels_import() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "hex_policy_worker.py").read_text(encoding="utf-8")
    assert "from henxels" not in source
    assert "import henxels" not in source
