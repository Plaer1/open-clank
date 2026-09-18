"""_owner_filter must use an explicit durable local Gallery principal.

When AUTH_ENABLED=false, get_current_user returns None and gallery routes should
see only ``local-installation`` rows. Legacy null and named-account rows remain
ambiguous. When AUTH_ENABLED=true the same None must fail closed.
"""
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import GalleryImage
from routes.gallery_helpers import _owner_filter

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False}, poolclass=NullPool)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _seed(*owners):
    db = _TS()
    try:
        db.query(GalleryImage).delete()
        for o in owners:
            db.add(GalleryImage(id=str(uuid.uuid4()), filename=f"{uuid.uuid4().hex}.png", owner=o))
        db.commit()
    finally:
        db.close()


def test_none_user_returns_only_local_installation_rows(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    _seed(None, "local-installation", "alice")
    db = _TS()
    try:
        n = _owner_filter(db.query(GalleryImage), None).count()
        assert n == 1
    finally:
        db.close()


def test_named_user_is_still_scoped():
    _seed("alice", "alice", "bob", None)
    db = _TS()
    try:
        assert _owner_filter(db.query(GalleryImage), "alice").count() == 2
        assert _owner_filter(db.query(GalleryImage), "bob").count() == 1
    finally:
        db.close()


def test_none_user_blocks_when_auth_is_enabled(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    _seed(None, "alice", "bob")
    db = _TS()
    try:
        assert _owner_filter(db.query(GalleryImage), None).count() == 0
    finally:
        db.close()
