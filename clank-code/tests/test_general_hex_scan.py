import sqlite3
from pathlib import Path
from types import SimpleNamespace

from routes.general_hex_scan import scan_guidance
from src.general_hex_guidance import GeneralHexGuidance
from src.general_hex_library import ensure_general_hex_schema
from src.memery_v2 import mirror_record


def test_review_scan_reports_changed_visible_content_without_changing_the_hex(tmp_path):
    path = tmp_path / "scan.db"
    with sqlite3.connect(path) as db:
        schema = Path(__file__).resolve().parents[1] / "native/memery/crates/memery-core/src/store/current-schema.sql"
        db.executescript(schema.read_text())
        db.executemany("INSERT INTO curated(owner,id,workspace_id,kind,content,archived,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?)", [
            ("alice", "fact", "repo", "fact", "Ship on Friday.", 0, "1", "1"),
            ("bob", "other-fact", "repo", "fact", "Other account's schedule.", 0, "1", "1"),
        ])

    ensure_general_hex_schema(path)
    fact = SimpleNamespace(id="fact", text="Ship on Friday.", kind="fact", source_type="human")
    assert mirror_record(fact, owner="alice", workspace_id="repo", db_path=str(path))
    service = GeneralHexGuidance(path)
    active = service.repo.create("alice", title="Release day", body="Release on Thursday.")
    legacy = service.repo.create("alice", title="Archive", body="Use the archive.")
    proposal = service.propose("alice", account_id="a", hex_id=active["hex_id"], expected_revision=1,
                               workspace_id="repo", action="revise", reason="Release schedule changed.",
                               evidence_ids=["fact"], body="Release on Friday.")
    service.decide("alice", "a", proposal["draft_id"], accept=True)

    def scan(**page):
        return scan_guidance(service, "alice", "a", "repo", **page)

    with sqlite3.connect(path) as db:
        db.execute("UPDATE curated SET updated_at='2'")
    fact.confidence_score = 0.9
    assert mirror_record(fact, owner="alice", workspace_id="repo", db_path=str(path))
    same = scan()
    assert same["items"] == []
    assert same["coverage"]["checked_sources"] == same["coverage"]["unlinked"] == 1

    with sqlite3.connect(path) as db:
        db.execute("UPDATE curated SET content='Ship on Tuesday.' WHERE owner='alice'")
    fact.text = "Ship on Tuesday."
    assert mirror_record(fact, owner="alice", workspace_id="repo", db_path=str(path))
    changed = scan()["items"]
    assert changed == [dict(hex_id=active["hex_id"], title="Release day", expected_revision=2,
                            workspace_id="repo", evidence_id="fact", evidence_label="Ship on Tuesday.",
                            reason="source_changed")]
    assert service.repo.get("alice", active["hex_id"])["body"] == "Release on Friday."
    assert scan_guidance(service, "alice", "a", "elsewhere")["items"] == []
    assert scan_guidance(service, "alice", "different-account", "repo")["items"] == []
    assert scan_guidance(service, "bob", "b", "repo")["items"] == []
    first = scan(limit=1)
    second = scan(after=first["next_cursor"], limit=1)
    assert first["next_cursor"] == min(active["hex_id"], legacy["hex_id"]) and second["next_cursor"] is None
    assert first["items"] + second["items"] == changed

    with sqlite3.connect(path) as db:
        db.execute("UPDATE curated SET archived=1 WHERE owner='alice'")
    removed = scan()
    assert removed["items"] == [] and removed["coverage"]["unavailable_sources"] == 1
