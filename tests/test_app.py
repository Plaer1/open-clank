"""Checkout requirements for native setup."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_file_is_optional_and_ignored():
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_env_example_exists():
    assert (ROOT / ".env.example").is_file()
