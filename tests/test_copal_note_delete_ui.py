from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_editable_note_header_exposes_a_visible_trash_action():
    feature = (ROOT / "static/js/copal/notesFeature.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "class:'copal-note-header-actions'" in feature
    assert "class:'copal-btn copal-note-trash-button danger'" in feature
    assert "'aria-label':`Move ${doc.name} to Trash`" in feature
    assert "['Move current note to Trash'" in feature
    assert ".copal-note-trash-button" in styles


def test_note_delete_saves_pending_edits_and_rejects_read_only_notes():
    app = (ROOT / "static/js/copal.js").read_text(encoding="utf-8")
    feature = (ROOT / "static/js/copal/notesFeature.js").read_text(encoding="utf-8")

    assert "if (!doc || doc.readOnly)" in app
    assert "notesFeature?.prepareDelete" in app
    assert "async function prepareDelete(docId)" in feature
    assert "if (!await saveDraft(docId))" in feature
    assert "acceptSavedDocument, prepareDelete" in feature
