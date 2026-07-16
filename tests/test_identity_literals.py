"""Identity-literal guard (identity metaplan Slice 00/01, ruling R9).

Personal agent identities are user data, never product code. First-party
source must not hard-code the operator's agent name, home, or workspace.
The vendored mimo tree has its own prompt-asset guard; reference clones,
plans, and notes are exempt (not product code).
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Directories that constitute first-party product code/config.
FIRST_PARTY = [
    "app.py",
    "src",
    "routes",
    "core",
    "static",
    "templates",
    "scripts",
    "config",
    ".env.example",
    "docker-compose.yml",
    "Dockerfile",
]

# Forbidden personal-identity literals (R9). Kept as narrow path/name
# patterns so third-party model IDs like InclusionAI "Ling-*" and ordinary
# Chinese text stay out of scope.
FORBIDDEN = [
    re.compile(r"entities/ling", re.IGNORECASE),
    re.compile(r"workspace-灵"),
    re.compile(r"[~/]\.ling(?:[/\s\"']|$)"),
    re.compile(r"openclaw\.json.*ling|ling.*openclaw\.json", re.IGNORECASE),
]

TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".html", ".css", ".json", ".json5",
    ".yml", ".yaml", ".toml", ".md", ".txt", ".sh", ".example", "",
}


def _first_party_files():
    for entry in FIRST_PARTY:
        path = REPO / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            for file in path.rglob("*"):
                if not file.is_file():
                    continue
                if "__pycache__" in file.parts or "node_modules" in file.parts:
                    continue
                if file.suffix.lower() in TEXT_SUFFIXES:
                    yield file


def test_no_personal_identity_literals_in_first_party_source():
    offenders = []
    for file in _first_party_files():
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in FORBIDDEN:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{file.relative_to(REPO)}:{line}: {match.group(0)!r}")
    assert not offenders, (
        "Personal agent identity literals in first-party source (R9 — "
        "identity is user data, never product code):\n" + "\n".join(offenders)
    )


def test_supervisor_has_no_provider_config_path_fallback():
    """OPENCLAW_CONFIG_PATH must be explicit; no guessed personal default."""
    source = (REPO / "src/openclank/mimo_supervisor.py").read_text(encoding="utf-8")
    match = re.search(
        r"os\.environ\.get\(\s*[\"']OPENCLAW_CONFIG_PATH[\"']\s*,\s*([\"'])(.*?)\1",
        source,
    )
    assert match is not None, "OPENCLAW_CONFIG_PATH lookup missing from supervisor"
    assert match.group(2) == "", (
        f"OPENCLAW_CONFIG_PATH must default to empty (explicit configuration "
        f"only), found fallback {match.group(2)!r}"
    )
