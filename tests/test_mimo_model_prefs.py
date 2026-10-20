"""Per-owner visibility store for mimo provider-account models."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.mimo_model_prefs as prefs
from core.database import Base


@pytest.fixture()
def store(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(prefs, "SessionLocal", session)
    return session


def test_roundtrip_and_provider_filter(store):
    prefs.set_owner_hidden_mimo_models("e", "openai", ["openai/gpt-5.2", "openai/gpt-5.2-mini"])
    prefs.set_owner_hidden_mimo_models("e", "xiaomi", ["xiaomi/mimo-auto"])

    assert prefs.owner_hidden_mimo_model_ids("e") == {
        "openai/gpt-5.2",
        "openai/gpt-5.2-mini",
        "xiaomi/mimo-auto",
    }
    assert prefs.owner_hidden_mimo_model_ids("e", "openai") == {
        "openai/gpt-5.2",
        "openai/gpt-5.2-mini",
    }
    assert prefs.owner_hidden_mimo_model_ids("e", "xiaomi") == {"xiaomi/mimo-auto"}


def test_replace_semantics(store):
    prefs.set_owner_hidden_mimo_models("e", "openai", ["openai/a", "openai/b"])
    hidden_count = prefs.set_owner_hidden_mimo_models("e", "openai", ["openai/c"])
    assert hidden_count == 1
    assert prefs.owner_hidden_mimo_model_ids("e", "openai") == {"openai/c"}


def test_empty_list_clears(store):
    prefs.set_owner_hidden_mimo_models("e", "openai", ["openai/a"])
    assert prefs.set_owner_hidden_mimo_models("e", "openai", []) == 0
    assert prefs.owner_hidden_mimo_model_ids("e", "openai") == set()


def test_owner_isolation(store):
    prefs.set_owner_hidden_mimo_models("e", "openai", ["openai/gpt-5.2"])
    prefs.set_owner_hidden_mimo_models("bob", "openai", ["openai/o3"])

    assert prefs.owner_hidden_mimo_model_ids("e") == {"openai/gpt-5.2"}
    assert prefs.owner_hidden_mimo_model_ids("bob") == {"openai/o3"}


def test_ownerless_owner_reads_nothing(store):
    prefs.set_owner_hidden_mimo_models("e", "openai", ["openai/a"])
    assert prefs.owner_hidden_mimo_model_ids(None) == set()
    assert prefs.owner_hidden_mimo_model_ids("") == set()
    assert prefs.set_owner_hidden_mimo_models("", "openai", ["x"]) == 0


def test_blank_model_ids_are_dropped(store):
    count = prefs.set_owner_hidden_mimo_models("e", "openai", ["openai/a", "", "   ", None])
    assert count == 1
    assert prefs.owner_hidden_mimo_model_ids("e", "openai") == {"openai/a"}


def test_provider_id_is_case_normalized(store):
    prefs.set_owner_hidden_mimo_models("e", "OpenAI", ["openai/a"])
    assert prefs.owner_hidden_mimo_model_ids("e", "openai") == {"openai/a"}
