import pytest

from src.hex_contract import evaluate_contract, find_contract, load_contract
from src.hex_contract_cli import main
from src.hex_defaults import initialize_defaults


def test_new_workspace_gets_singular_defaults_and_reference_ignore(tmp_path):
    (tmp_path / '.gitignore').write_text('*.tmp\n!/.references/\n')
    path, created = initialize_defaults(tmp_path)
    assert created
    assert path == tmp_path / '.clanker/hexes/contract.yaml'
    contract = load_contract(path)
    assert any(rule.checks.get('untracked_only') == ['.references'] for rule in contract.hexes)
    assert any('.clanker/archive/' in rule.text for rule in contract.hexes)
    assert any('mirror' in rule.text for rule in contract.hexes)
    assert evaluate_contract(path, root=tmp_path, files=[])['allowed']
    assert (tmp_path / '.gitignore').read_text().endswith('/.references/\n')
    assert not (tmp_path / '.clankers').exists()
    original = path.read_bytes()
    ignored = (tmp_path / '.gitignore').read_bytes()
    assert initialize_defaults(tmp_path) == (path, False)
    assert path.read_bytes() == original
    assert (tmp_path / '.gitignore').read_bytes() == ignored


@pytest.mark.parametrize('relative', ['.hex', '.clankers/hexes/contract.yaml', '.clanker/hexes/contract.yaml'])
def test_init_preserves_existing_custom_contract_and_ignore(tmp_path, relative):
    existing = tmp_path / relative
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text('contract: open-clank-hexes/v2\nhexes: []\n')
    before = existing.read_bytes()
    assert initialize_defaults(tmp_path) == (existing, False)
    assert existing.read_bytes() == before
    assert not (tmp_path / '.gitignore').exists()


def test_init_respects_surrounding_workspace_contract(tmp_path):
    existing, _ = initialize_defaults(tmp_path)
    child = tmp_path / 'src'
    child.mkdir()
    assert initialize_defaults(child) == (existing, False)
    assert not (child / '.clanker').exists()


def test_cli_init_creates_defaults_and_handles_file_target(tmp_path, capsys):
    assert main(['init', str(tmp_path)]) == 0
    assert 'created defaults' in capsys.readouterr().out
    assert main(['init', str(tmp_path / '.gitignore')]) == 2
    assert 'workspace must be a directory' in capsys.readouterr().err


@pytest.mark.parametrize('namespace', ['.clanker', '.clankers'])
def test_nested_cli_and_digest_use_selected_contract(tmp_path, monkeypatch, capsys, namespace):
    contract = tmp_path / namespace / 'hexes/contract.yaml'
    contract.parent.mkdir(parents=True)
    contract.write_text('contract: open-clank-hexes/v2\nhexes: []\n')
    child = tmp_path / 'src/nested'
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    assert main(['explain', 'new.py', '--json']) == 0
    assert main(['check']) == 0
    assert main(['sync']) == 0
    digest = (tmp_path / 'AGENTS.md').read_text()
    assert f'Generated from `{namespace}/hexes/contract.yaml`' in digest
    assert f'`{namespace}/hexes/checks/*.py`' in digest


def test_singular_precedence_never_falls_back_from_malformed_contract(tmp_path):
    legacy = tmp_path / '.clankers/hexes/contract.yaml'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('contract: open-clank-hexes/v2\nhexes: []\n')
    singular = tmp_path / '.clanker/hexes/contract.yaml'
    singular.parent.mkdir(parents=True)
    singular.write_text('malformed: [')
    assert find_contract(tmp_path) == singular
    assert initialize_defaults(tmp_path) == (singular, False)
    assert main(['check', str(tmp_path)]) == 2
