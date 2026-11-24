"""Engine separation from a real MiMo Code install: rename table, apply script, leftover check."""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "engine_separation"


def _load(name):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(f"engine_separation_{name}", SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


apply_mod = _load("apply")
check_mod = _load("check_leftovers")

SOURCE = {
    "packages/shared/src/global.ts": (
        'const APP = "mimocode"\n'
        "export function resolveMimocodeHome(env = process.env) {\n"
        "  const home = env.MIMOCODE_HOME\n"
        '  return { mode: "mimocode_home", db: path.join(home, "mimocode.db") }\n'
        "}\n"
    ),
    "packages/cli/src/flag.ts": (
        'export const MIMOCODE_DB = process.env["MIMOCODE_DB"]\n'
        "const python = process.env.MIMO_PYTHON\n"
        'const project = path.join(dir, ".mimocode", "skills")\n'
        'const id = path.join(dir, ".mimocode-project-id")\n'
        "// config lives in ~/.config/mimocode/mimocode.jsonc\n"
        'headers["x-mimocode-directory"] = dir\n'
    ),
    "packages/cli/src/index.ts": (
        'const cli = yargs(args).scriptName("mimo")\n'
        'console.log("Run `mimo serve` to start MiMoCode; MiMo Code reads mimocode.json")\n'
        'const exe = path.join(dir, "mimo.exe")\n'
    ),
    "packages/cli/mimo.js": "#!/usr/bin/env node\n",
    "packages/cli/src/theme/mimocode.json": "{}\n",
    "packages/cli/test/.mimocode/agent/a.md": "agent\n",
    "packages/cli/src/provider.ts": (
        'import { Flock } from "@mimo-ai/shared/util/flock"\n'
        'const api = "https://api.xiaomimimo.com/v1"\n'
        'const plan = "https://token-plan-cn.xiaomimimo.com/v1"\n'
        'const schema = "https://mimo.xiaomi.com/mimocode/config.json"\n'
        'const models = ["xiaomi/mimo-v2.5-pro", "mimo-v2-flash", "XiaomiMiMo/MiMo-V2-Flash"]\n'
        'const providers = ["xiaomi", "xiaomi-token-plan-cn", "mimo"]\n'
        'headers["X-Mimo-Source"] = "mimocode-cli"\n'
        'headers["User-Agent"] = `mimocode/${InstallationVersion}`\n'
        'const label = "MiMo"\n'
        'const capability = "com.xiaomi.mimo/turn-lifecycle"\n'
    ),
    "LICENSE": "MIT, Xiaomi MiMoCode\n",
    "USE_RESTRICTIONS.md": "# Xiaomi MiMoCode Use Restrictions\n",
    "docs/spec.md": "MIMOCODE_HOME and .mimocode are described here\n",
    "packages/cli/assets/logo.bin": b"\x00\x01mimocode\x00",
}


@pytest.fixture
def tree(tmp_path):
    for rel, content in SOURCE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return tmp_path


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _table():
    return apply_mod.Table(apply_mod.DEFAULT_TABLE)


def test_each_class_is_renamed(tree):
    apply_mod.apply(tree, _table())
    shared = (tree / "packages/shared/src/global.ts").read_text()
    assert 'const APP = "openclank-engine"' in shared  # XDG fallback folder
    assert "env.OPENCLANK_HOME" in shared and "resolveOpenclankHome" in shared
    assert '"openclank.db"' in shared and '"openclank_home"' in shared
    flag = (tree / "packages/cli/src/flag.ts").read_text()
    assert 'OPENCLANK_DB = process.env["OPENCLANK_DB"]' in flag  # environment
    assert "process.env.OPENCLANK_PYTHON" in flag
    assert '".openclank", "skills"' in flag and ".openclank-project-id" in flag  # project folder
    assert "~/.config/openclank-engine/openclank.jsonc" in flag
    assert '"x-openclank-directory"' in flag
    index = (tree / "packages/cli/src/index.ts").read_text()
    assert 'scriptName("openclank-engine")' in index  # binary
    assert "`openclank-engine serve`" in index and '"openclank-engine.exe"' in index
    assert "start Open Clank; Open Clank reads openclank.json" in index  # product
    assert (tree / "packages/cli/openclank-engine.js").is_file()  # path names
    assert (tree / "packages/cli/src/theme/openclank.json").is_file()
    assert (tree / "packages/cli/test/.openclank/agent/a.md").is_file()
    assert not (tree / "packages/cli/mimo.js").exists()
    assert not (tree / "packages/cli/test/.mimocode").exists()


def test_apply_is_idempotent(tree):
    assert apply_mod.apply(tree, _table()) != (0, 0)
    first = _snapshot(tree)
    assert apply_mod.apply(tree, _table()) == (0, 0)
    assert _snapshot(tree) == first


def test_protocol_names_and_allow_listed_files_are_untouched(tree):
    before = _snapshot(tree)
    apply_mod.apply(tree, _table())
    after = _snapshot(tree)
    for rel in ("packages/cli/src/provider.ts", "LICENSE", "USE_RESTRICTIONS.md", "docs/spec.md",
                "packages/cli/assets/logo.bin"):
        assert after[rel] == before[rel], rel


def test_check_passes_on_a_renamed_tree_and_fails_on_a_planted_leftover(tree, capsys):
    assert check_mod.main([str(tree)]) == 1  # untouched tree is full of leftovers
    apply_mod.apply(tree, _table())
    capsys.readouterr()
    assert check_mod.main([str(tree)]) == 0
    planted = tree / "packages/cli/src/planted.ts"
    planted.write_text('const ok = 1\nconst home = process.env.MIMOCODE_HOME ?? "~/.mimocode"\n', encoding="utf-8")
    (tree / "packages/cli/src/mimocode-notes.txt").write_text("fine\n", encoding="utf-8")
    capsys.readouterr()
    assert check_mod.main([str(tree)]) == 1
    out = capsys.readouterr().out
    assert "packages/cli/src/planted.ts:2: environment:" in out
    assert "packages/cli/src/mimocode-notes.txt:0: path name:" in out
    assert "provider.ts" not in out and "LICENSE" not in out
