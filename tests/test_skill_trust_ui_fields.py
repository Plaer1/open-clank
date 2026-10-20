"""The collapsed Skills UI must retain the activation/provenance evidence."""

from pathlib import Path


SKILLS_JS = Path(__file__).resolve().parents[1] / "static/js/skills.js"


def test_skill_trust_ui_shows_source_status_revision_and_last_audit():
    source = SKILLS_JS.read_text(encoding="utf-8")

    # Each field appears in both the trust-pill tooltip and expanded details.
    assert source.count("sk.source_status") >= 2
    assert source.count("sk.source_revision") >= 2
    assert source.count("sk.audited_at") >= 2
    assert "source status:" in source
    assert "source revision:" in source
    assert "last audit:" in source
