"""Stats: per-turn media-input counts (BQ-21 / WB09 "one receipt feeds Stats"): write, aggregate, old-row compatibility."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatMessage as DbChatMessage, Session as DbSession
from core.stats_models import StatsEvent
from routes.chat_helpers import media_input_counts
from services.stats import ledger
from services.stats.ledger import capture_message_event
from services.stats.query import parse_scope, query_summary
from types import SimpleNamespace


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stats.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _record(db, n, metrics):
    row = DbChatMessage(id=f"m{n}", session_id="chat-1", role="assistant", content="x", timestamp=datetime.utcnow() - timedelta(minutes=n))
    db.add(row)
    db.flush()
    session = db.query(DbSession).filter_by(id="chat-1").one()
    capture_message_event(db, session, row, SimpleNamespace(role="assistant", metadata={"metrics": metrics, "stats_replay_key": f"k{n}"}))
    db.commit()


def test_counts_come_from_the_receipts_and_images_not_sent_are_counted():
    meta = [{"id": "a", "media_input": {"outcome": "admit", "sent": True}},
            {"id": "b", "media_input": {"outcome": "omit", "reason": "too_many_pixels", "sent": False}},
            {"id": "c"}, {"id": "d", "media_input": {"outcome": "admit"}}]
    assert media_input_counts(meta) == {"mediaInputsEvaluated": 3, "mediaInputsNotSent": 1}
    assert media_input_counts([]) == {} and media_input_counts(None) == {} and media_input_counts([{"id": "x"}]) == {}


def test_write_aggregate_and_old_rows_count_zero(tmp_path):
    assert {"mediaInputsEvaluated", "mediaInputsNotSent"} <= ledger.EXTRA_METRICS
    db = _db(tmp_path)
    db.add(DbSession(id="chat-1", name="Chat", endpoint_url="local", model="model", owner="alice"))
    db.commit()
    _record(db, 1, {"output_tokens": 5, "mediaInputsEvaluated": 3, "mediaInputsNotSent": 1})
    _record(db, 2, {"output_tokens": 7})                                              # an older row / a turn without images
    _record(db, 3, {"output_tokens": 2, "mediaInputsEvaluated": 2, "mediaInputsNotSent": "bad"})   # a non-integer is never stored
    events = db.query(StatsEvent).order_by(StatsEvent.event_time).all()
    stored = [e.event_metadata.get("extra_metrics", {}) for e in events]
    assert {"mediaInputsEvaluated": 3, "mediaInputsNotSent": 1} in stored and {} in stored
    assert {"mediaInputsEvaluated": 2} in stored
    scope = parse_scope(owner="alice", period="7d", timezone_name="UTC")
    other = query_summary(db, scope)["other_metrics"]
    assert other["mediaInputsEvaluated"]["unit"] == "inputs" and other["mediaInputsEvaluated"]["value"] == 5   # 3 + 0 (old row) + 2
    assert other["mediaInputsNotSent"]["value"] == 1 and other["mediaInputsNotSent"]["state"] == "reported"
    assert all(other[key]["coverage"] == "preparation_only" for key in ("mediaInputsEvaluated", "mediaInputsNotSent"))
    db.close()
