"""Chat-specific personas (identity ruling, e 2026-07-16).

The in-chat persona menu binds a persona to ONE session row; the global
default persona keeps branding and new chats. session_persona_override()
is the seam: it applies the session's record over whatever preset/default
resolution produced, and fails open.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as core_db
from core.database import Base, Session as DbSession
from routes.chat_helpers import PresetInfo, session_persona_override


@pytest.fixture
def db_session_row(tmp_path, monkeypatch):
    # Hermetic engine: the shared in-memory DB gets rebuilt/patched by other
    # tests in the full run; session_persona_override resolves SessionLocal
    # from core.database at call time, so patching it here isolates us.
    engine = create_engine(f"sqlite:///{tmp_path}/persona.db")
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(core_db, "SessionLocal", maker)
    db = maker()
    row = DbSession(
        id="sess-persona-test",
        name="t",
        endpoint_url="http://x",
        model="m",
        owner="alice",
    )
    db.merge(row)
    db.commit()
    yield db
    db.close()


def _base_preset():
    return PresetInfo(
        temperature=1.0,
        max_tokens=0,
        system_prompt="Your name is Odysseus. Default prompt.",
        character_name="Odysseus",
    )


def _set_persona(db, record):
    row = db.query(DbSession).filter(DbSession.id == "sess-persona-test").first()
    row.persona = json.dumps(record) if record is not None else None
    db.commit()


def test_no_session_persona_keeps_preset(db_session_row):
    out = session_persona_override("sess-persona-test", _base_preset())
    assert out.character_name == "Odysseus"
    assert out.system_prompt.endswith("Default prompt.")


def test_session_persona_overrides_default(db_session_row):
    _set_persona(db_session_row, {
        "character_name": "Nyx",
        "system_prompt": "Speak in riddles.",
        "temperature": 0.7,
        "max_tokens": 2048,
    })
    out = session_persona_override("sess-persona-test", _base_preset())
    assert out.character_name == "Nyx"
    assert out.system_prompt == "Your name is Nyx. Speak in riddles."
    assert out.temperature == 0.7
    assert out.max_tokens == 2048


def test_session_persona_partial_fields_inherit_preset(db_session_row):
    _set_persona(db_session_row, {"character_name": "Nyx", "system_prompt": "Riddles."})
    base = _base_preset()
    out = session_persona_override("sess-persona-test", base)
    assert out.temperature == base.temperature
    assert out.max_tokens == base.max_tokens


def test_malformed_record_fails_open(db_session_row):
    row = db_session_row.query(DbSession).filter(DbSession.id == "sess-persona-test").first()
    row.persona = "{not json"
    db_session_row.commit()
    out = session_persona_override("sess-persona-test", _base_preset())
    assert out.character_name == "Odysseus"


def test_unknown_session_fails_open():
    out = session_persona_override("no-such-session", _base_preset())
    assert out.character_name == "Odysseus"
