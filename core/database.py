import os
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse
from sqlalchemy import event, create_engine, Column, String, Text, Boolean, DateTime, Integer, ForeignKey, JSON, Index, UniqueConstraint, func, inspect, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.orm import Session as ORMSession, relationship, sessionmaker, backref

from src.runtime_paths import get_app_root
from core.platform_compat import safe_chmod, IS_WINDOWS

logger = logging.getLogger(__name__)

# Create base class for declarative models
Base = declarative_base()


def utcnow_naive() -> datetime:
    """Return naive UTC for existing DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    """Mixin that adds timestamp fields to models"""
    @declared_attr
    def created_at(cls):
        return Column(DateTime, default=utcnow_naive, nullable=False)
    
    @declared_attr
    def updated_at(cls):
        return Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

# Ensure the writable data directory exists before SQLite connects.
from src.constants import DATA_DIR, AUTH_FILE, MEMORY_FILE, USER_PREFS_FILE, SETTINGS_FILE
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)


def _default_database_url() -> str:
    return f"sqlite:///{Path(DATA_DIR) / 'app.db'}"


def _normalize_sqlite_url(url: str) -> str:
    """Resolve relative ordinary SQLite paths without rewriting URI filenames."""
    try:
        parsed = make_url(url)
    except Exception:
        return url

    if parsed.get_backend_name() != "sqlite":
        return url

    db_path = parsed.database
    if (
        not db_path
        or db_path == ":memory:"
        or str(db_path).lower().startswith("file:")
        or os.path.isabs(str(db_path))
    ):
        return url

    absolute_path = (Path(get_app_root()) / str(db_path)).resolve().as_posix()
    return parsed.set(database=absolute_path).render_as_string(
        hide_password=False
    )


# Get database URL from environment, default to SQLite in DATA_DIR
DATABASE_URL = _normalize_sqlite_url(os.getenv("DATABASE_URL", _default_database_url()))

# Create engine
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)


# Sidecar files SQLite can create next to the main DB. -journal is the default
# rollback journal; -wal/-shm appear once WAL is enabled. Each can hold copies of
# secret-bearing pages, so they get the same 0o600 lockdown as the DB itself.
_SQLITE_SIDECARS = ("-journal", "-wal", "-shm")


_RETIRED_PROVIDER_TABLES = frozenset({
    "model_endpoints",
    "model_capabilities",
    "provider_auth_sessions",
    "mimo_auth_store",
    "mimo_model_prefs",
    "model_shares",
    "model_share_subscriptions",
    "mimo_projection_states",
})


def _sqlite_db_path(url) -> Optional[str]:
    """Return the filesystem path for a file-backed SQLite URL.

    SQLite query parameters such as ``mode=memory`` only affect filename
    semantics when SQLAlchemy enables URI handling with ``uri=true``. Ordinary
    file URLs must therefore remain file-backed even when they contain a query
    parameter named ``mode``.

    For SQLite ``file:`` URIs, an empty authority or ``localhost`` identifies a
    local path. Other authorities are retained as UNC-style paths.
    """
    if url.get_backend_name() != "sqlite":
        return None

    db_path = url.database
    if not db_path or db_path == ":memory:":
        return None

    db_path = str(db_path)
    query = {
        str(key).lower(): str(value).strip().lower()
        for key, value in dict(getattr(url, "query", {}) or {}).items()
    }
    uri_enabled = query.get("uri") in {"1", "true", "yes", "on"}
    is_file_uri = db_path.lower().startswith("file:")

    if not uri_enabled or not is_file_uri:
        return db_path

    if (
        db_path.lower().startswith("file::memory:")
        or query.get("mode") == "memory"
    ):
        return None

    parsed = urlparse(db_path)
    fs_path = parsed.path or ""
    if not fs_path or fs_path == ":memory:":
        return None

    authority = parsed.netloc
    if authority and authority.lower() != "localhost":
        fs_path = f"//{authority}{fs_path}"

    return unquote(fs_path)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Listening on the Engine class ensures this listener fires for all Engine
# instances created within the process, not just the primary application engine.
# The isinstance(sqlite3.Connection) check ensures that this PRAGMA foreign_keys=ON
# configuration remains a no-op when using non-SQLite database backends.
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class EncryptedText(TypeDecorator):
    """Text column transparently encrypted at rest via src.secret_storage.

    Writes are Fernet-encrypted (`enc:` prefix); reads decrypt back to
    plaintext, so all consumers use the column normally. Legacy plaintext
    rows pass through unchanged until their next write (an explicit workspace migration
    encrypts them). Protects the SQLite file at rest (stolen backup / leaked
    image), not a live process that can read the key.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import encrypt
        return encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import decrypt
        return decrypt(value)


class Session(TimestampMixin, Base):
    """
    SQLAlchemy model for Session table.
    Represents a chat session with its configuration and metadata.
    """
    __tablename__ = "sessions"
    
    # Primary key
    id = Column(String, primary_key=True, index=True)
    
    # Session metadata
    name = Column(String, nullable=False)
    endpoint_url = Column(String, nullable=False)
    endpoint_id = Column(String, nullable=True, index=True)
    # Stable normalized execution identity. endpoint_* remains nullable,
    # display-only compatibility after the provider hard cut.
    provider_model_route_id = Column(String, nullable=True, index=True)
    model = Column(String, nullable=False)
    owner = Column(String, nullable=True, index=True)  # username; null = legacy/shared
    # Opaque reference into the canonical file-policy Workspace catalog. This
    # is deliberately not a SQL foreign key: that catalog uses its own raw
    # SQLite schema and may be configured separately from this ORM database.
    workspace_id = Column(String, nullable=True, index=True)
    
    # Configuration flags
    rag = Column(Boolean, default=False)
    archived = Column(Boolean, default=False)

    # Organization
    folder = Column(String, nullable=True, default=None)
    
    # Headers stored as JSON
    headers = Column(JSON, default=dict)
    
    # Timestamps are provided by TimestampMixin
    last_accessed = Column(DateTime, default=func.now(), onupdate=func.now())
    # Timestamp of the last actual MESSAGE in this session. Set explicitly
    # only when a message is persisted (NOT onupdate) — so it's a clean
    # "last conversation" signal, immune to renames / model swaps / merely
    # opening the chat (all of which bump updated_at and last_accessed).
    # The "Last active" sort uses this.
    last_message_at = Column(DateTime, nullable=True, default=None)
    
    
    # Indexes - optimized composites
    __table_args__ = (
        Index('ix_sessions_active', 'archived', 'last_accessed'),
        Index('ix_sessions_search', 'name', 'archived'),
    )
    
    # Properties
    is_important = Column(Boolean, default=False)
    message_count = Column(Integer, default=0)
    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)
    mode = Column(String, nullable=True)  # 'agent', 'chat', or 'research'
    # Per-chat persona override (identity ruling: the in-chat persona menu is
    # chat-specific; the global default persona covers branding + new chats).
    # JSON: {"character_name", "system_prompt", "temperature", "max_tokens"}.
    persona = Column(Text, nullable=True)
    crew_member_id = Column(String, nullable=True)  # links to crew_members.id
    mimo_session_id = Column(String, nullable=True)  # durable lazy-cache for pre-migration uuid4 rows
    transcript_revision = Column(Integer, nullable=False, default=0)
    mimo_state = Column(JSON, nullable=False, default=dict)

    # Relationship to chat messages
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    
    @property
    def is_active(self):
        """Check if session is active (not archived)"""
        return not self.archived
    
    def to_dict(self):
        """Convert session to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'name': self.name,
            'model': self.model,
            'endpoint_url': self.endpoint_url,
            'endpoint_id': self.endpoint_id,
            'rag': self.rag,
            'archived': self.archived,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'last_accessed': self.last_accessed.isoformat() if self.last_accessed else None,
            'last_message_at': self.last_message_at.isoformat() if self.last_message_at else None,
            'message_count': self.message_count,
            'is_important': self.is_important,
            'folder': self.folder,
            'total_input_tokens': self.total_input_tokens or 0,
            'total_output_tokens': self.total_output_tokens or 0,
            'crew_member_id': self.crew_member_id,
            'workspace_id': self.workspace_id,
        }

class ChatMessage(Base):
    """
    SQLAlchemy model for ChatMessage table.
    Represents individual chat messages within a session.
    """
    __tablename__ = "chat_messages"
    
    # Primary key - using String to support UUIDs
    id = Column(String, primary_key=True, index=True)
    
    # Foreign key to Session
    session_id = Column(String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Message content
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    meta_data = Column("metadata", Text, nullable=True)  # JSON string for metrics etc.

    # Timestamp
    timestamp = Column(DateTime, default=utcnow_naive)
    
    # Relationship to Session
    session = relationship("Session", back_populates="messages")
    
    # Indexes - optimized composite
    __table_args__ = (
        Index('ix_messages_session_time', 'session_id', 'timestamp'),  # Composite for efficient message retrieval
    )


class AgentTurn(Base):
    """Durable actor-accounting state for one persisted Agent turn."""

    __tablename__ = "agent_turns"

    root_turn_id = Column(
        String,
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    assistant_message_id = Column(
        String,
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    mimo_session_id = Column(String, nullable=False, index=True)
    actor_accounting_version = Column(Integer, nullable=False, default=1)
    actor_accounting_state = Column(String, nullable=False, default="pending", index=True)
    last_source_revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow_naive)
    updated_at = Column(DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)
    closed_at = Column(DateTime, nullable=True)


class TurnActor(Base):
    """One MiMo actor projected into the Agent turn that spawned it."""

    __tablename__ = "turn_actors"

    root_turn_id = Column(
        String,
        ForeignKey("agent_turns.root_turn_id", ondelete="CASCADE"),
        primary_key=True,
    )
    actor_id = Column(String, primary_key=True)
    mimo_session_id = Column(String, nullable=False, index=True)
    parent_actor_id = Column(String, nullable=True)
    mode = Column(String, nullable=False)
    agent = Column(String, nullable=False)
    description = Column(Text, nullable=False, default="")
    # Keep the pre-resolution request and the provider-resolved destination
    # separately for truthful actor accounting and audit display.
    requested_model = Column(String, nullable=True)
    effective_model = Column(String, nullable=True)
    background = Column(Boolean, nullable=False, default=False)
    lifecycle = Column(String, nullable=False, default="ephemeral")
    counts_toward_total = Column(Boolean, nullable=False, default=False)
    status = Column(String, nullable=False, default="pending", index=True)
    outcome = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)
    source_revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow_naive)
    updated_at = Column(DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_turn_actors_session_actor", "mimo_session_id", "actor_id"),
        Index("ix_turn_actors_root_parent", "root_turn_id", "parent_actor_id"),
    )


class MimoProjection(TimestampMixin, Base):
    """Active MiMo execution projection of one canonical Odysseus transcript."""

    __tablename__ = "mimo_projections"

    odysseus_session_id = Column(
        String,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mimo_session_id = Column(String, nullable=False, unique=True, index=True)
    owner = Column(String, nullable=False, index=True)
    workspace = Column(String, nullable=False)
    endpoint_url = Column(String, nullable=False)
    model = Column(String, nullable=False)
    transcript_revision = Column(Integer, nullable=False, default=0)
    covered_message_ids = Column(Text, nullable=False, default="[]")
    canonical_digest = Column(String, nullable=False)
    mode_config_revision = Column(Integer, nullable=False, default=0)
    lifecycle_state = Column(String, nullable=False, default="active", index=True)
    active_turn_id = Column(String, nullable=False, index=True)


@event.listens_for(ORMSession, "before_flush")
def _collect_transcript_revision_changes(session, _flush_context, _instances):
    changed = session.info.setdefault("_transcript_revision_ids", set())
    for obj in session.new.union(session.dirty).union(session.deleted):
        if isinstance(obj, ChatMessage) and obj.session_id:
            changed.add(obj.session_id)
        elif isinstance(obj, Session) and obj.id and obj in session.dirty:
            state = inspect(obj)
            if state.attrs.endpoint_url.history.has_changes() or state.attrs.model.history.has_changes():
                changed.add(obj.id)


@event.listens_for(ORMSession, "after_flush_postexec")
def _bump_transcript_revisions(session, _flush_context):
    changed = session.info.pop("_transcript_revision_ids", set())
    if not changed:
        return
    session.execute(
        update(Session)
        .where(Session.id.in_(changed))
        .values(transcript_revision=func.coalesce(Session.transcript_revision, 0) + 1)
        .execution_options(synchronize_session=False)
    )

class Document(TimestampMixin, Base):
    """Living document that the AI can create and edit in-place."""
    __tablename__ = "documents"

    id              = Column(String, primary_key=True, index=True)
    session_id      = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    title           = Column(String, nullable=False, default="Untitled")
    language        = Column(String, nullable=True)          # "python", "markdown", "text", etc.
    current_content = Column(Text, nullable=False, default="")
    version_count   = Column(Integer, default=1)
    is_active       = Column(Boolean, default=True)
    # Soft-archive: hidden from the Library's Documents list/search/Tidy until
    # restored. Distinct from is_active (which tracks "open in a session").
    archived        = Column(Boolean, default=False)
    # Owner of this document. Documents used to derive ownership from their
    # linked chat session, but a session can be deleted (session_id → NULL via
    # SET NULL), orphaning the doc and making it vanish from the owner's
    # Library + search. Owning the row directly is robust against that.
    owner           = Column(String, nullable=True, index=True)
    tidy_verdict    = Column(String, nullable=True)        # "keep", "junk", or None (not yet reviewed)
    # Provenance: if this document was created by opening an email attachment,
    # these point back to the source email so the "Sign and reply" flow can
    # thread a response on the original conversation.
    source_email_uid         = Column(String, nullable=True)
    source_email_folder      = Column(String, nullable=True)
    source_email_account_id  = Column(String, nullable=True)
    source_email_message_id  = Column(String, nullable=True, index=True)

    session  = relationship("Session", backref=backref("documents", cascade="save-update, merge"))
    versions = relationship("DocumentVersion", back_populates="document",
                           cascade="all, delete-orphan", order_by="DocumentVersion.version_number")


class DocumentVersion(Base):
    """Immutable snapshot of a document at a point in time."""
    __tablename__ = "document_versions"

    id             = Column(String, primary_key=True, index=True)
    document_id    = Column(String, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    content        = Column(Text, nullable=False)
    summary        = Column(String, nullable=True)     # Edit description
    source         = Column(String, default="ai")      # "ai" or "user"
    created_at     = Column(DateTime, default=utcnow_naive)

    document = relationship("Document", back_populates="versions")


class PublishedFile(Base):
    """Managed bytes an agent deliberately exposed through a download grant."""
    __tablename__ = "published_files"

    id          = Column(String, primary_key=True, index=True)
    owner       = Column(String, nullable=False, index=True)
    filename    = Column(String, nullable=False)
    mime_type   = Column(String, nullable=False, default="application/octet-stream")
    size        = Column(Integer, nullable=False)
    sha256      = Column(String, nullable=False, index=True)
    source      = Column(String, nullable=False, default="agent")
    created_at  = Column(DateTime, default=utcnow_naive, nullable=False)

    grants = relationship(
        "PublishedFileGrant",
        back_populates="file",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PublishedFileGrant(Base):
    """Revocable owner-only or expiring-public link for a published file."""
    __tablename__ = "published_file_grants"

    id          = Column(String, primary_key=True, index=True)
    file_id     = Column(
        String,
        ForeignKey("published_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash  = Column(String, nullable=False, unique=True, index=True)
    audience    = Column(String, nullable=False, default="owner")
    created_at  = Column(DateTime, default=utcnow_naive, nullable=False)
    expires_at  = Column(DateTime, nullable=True, index=True)
    revoked_at  = Column(DateTime, nullable=True, index=True)

    file = relationship("PublishedFile", back_populates="grants")


class FilesImageResource(TimestampMixin, Base):
    """Files-owned immutable image/folder metadata.

    This table owns stable Files identity, hierarchy and byte locators.
    """
    __tablename__ = "files_image_resources"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)  # folder or image
    parent_id = Column(String, nullable=True, index=True)
    display_name = Column(String, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    digest = Column(String(64), nullable=True, index=True)
    size = Column(Integer, nullable=False, default=0)
    mime_type = Column(String, nullable=True)
    locator = Column(String, nullable=True)
    provenance = Column(JSON, nullable=True)
    operation_key = Column(String, nullable=True, index=True)
    source_provider = Column(String, nullable=True)
    source_resource_id = Column(String, nullable=True)
    favorite = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)

    __table_args__ = (
        Index("ix_files_image_owner_parent", "owner", "parent_id", "is_active"),
        UniqueConstraint("owner", "operation_key", name="uq_files_image_owner_operation"),
    )


class EmailAccount(TimestampMixin, Base):
    """A configured IMAP/SMTP account. Supports multiple accounts per user —
    exactly one row per owner has is_default=True.

    Security note: imap_password / smtp_password are stored Fernet-encrypted
    via src/secret_storage.py. The key lives at data/.app_key (mode 0o600,
    gitignored). Anyone with read access to that file can decrypt every
    row, so the threat model is "stolen SQLite backup" rather than
    "process compromise". On first start any legacy plaintext rows are
    converted only by explicit workspace migration scripts.
    """
    __tablename__ = "email_accounts"

    id             = Column(String, primary_key=True, index=True)
    owner          = Column(String, nullable=True, index=True)
    name           = Column(String, nullable=False)  # Display name: "Work", "Personal", etc.
    is_default     = Column(Boolean, default=False, nullable=False)
    enabled        = Column(Boolean, default=True, nullable=False)

    # IMAP (receiving)
    imap_host      = Column(String, default="")
    imap_port      = Column(Integer, default=993)
    imap_user      = Column(String, default="")
    imap_password  = Column(String, default="")
    imap_starttls  = Column(Boolean, default=True)

    # SMTP (sending)
    smtp_host      = Column(String, default="")
    smtp_port      = Column(Integer, default=465)
    smtp_security  = Column(String, default="ssl")  # ssl | starttls | none
    smtp_user      = Column(String, default="")
    smtp_password  = Column(String, default="")

    from_address   = Column(String, default="")
    display_name   = Column(String, nullable=True)   # "Hriday Ranka" — used in From: header

    # OAuth2 (Google / Google Workspace). Tokens stored encrypted via secret_storage.
    oauth_provider      = Column(String, nullable=True)   # "google" or None
    oauth_access_token  = Column(String, nullable=True)   # encrypted
    oauth_refresh_token = Column(String, nullable=True)   # encrypted
    oauth_token_expiry  = Column(String, nullable=True)   # unix timestamp string

    __table_args__ = (
        Index('ix_email_accounts_owner_default', 'owner', 'is_default'),
    )


class ModelEndpoint(TimestampMixin, Base):
    """Admin-configured model endpoints. Models are auto-discovered via /v1/models."""
    __tablename__ = "model_endpoints"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)          # Display label, e.g. "Local vLLM", "OpenRouter"
    base_url = Column(String, nullable=False)      # Base URL, e.g. "http://localhost:8002/v1"
    api_key = Column(EncryptedText, nullable=True)  # Optional provider API key, encrypted at rest
    is_enabled = Column(Boolean, default=True)
    hidden_models = Column(Text, nullable=True)    # JSON list of model IDs that failed probing
    cached_models = Column(Text, nullable=True)    # JSON list of last-known model IDs (avoids probe on list)
    pinned_models = Column(Text, nullable=True)    # JSON list of admin-pinned model IDs (manual, may not appear in /v1/models)
    model_type = Column(String, nullable=True, default="llm")  # "llm" or "image"
    # auto = classify by URL; local = self-hosted server; api/proxy = external
    # OpenAI-compatible API even when reachable through a private/tailnet IP.
    endpoint_kind = Column(String, nullable=True, default="auto")
    # auto = background refresh with TTL/backoff; manual/disabled = cached-first
    # only unless an explicit endpoint probe is requested.
    model_refresh_mode = Column(String, nullable=True, default="auto")
    model_refresh_interval = Column(Integer, nullable=True, default=None)
    model_refresh_timeout = Column(Integer, nullable=True, default=None)
    # Last model-catalog probe is kept separate from per-model tool evidence.
    # A catalog 401 must not erase a previously certified tool capability, and
    # an unknown capability must not be misreported as an authentication error.
    catalog_probe_status = Column(String, nullable=True)
    catalog_probe_http_status = Column(Integer, nullable=True)
    catalog_probed_at = Column(DateTime, nullable=True)
    # Dormant compatibility column. Explicit workspace migration converts any old true/false value
    # into ModelCapability rows and clears it; new authority is per model.
    supports_tools = Column(Boolean, nullable=True, default=None)
    # Per-user ownership. NULL is legacy/unclaimed state and is never visible
    # in authenticated catalogues; startup claims such rows to the first admin.
    owner = Column(String, nullable=True, index=True)
    # Optional OAuth/session-backed credential row. Used by subscription-backed
    # providers that need refresh tokens instead of a static API key.
    provider_auth_id = Column(String, nullable=True, index=True)


class ModelCapability(Base):
    """Per-model tool declaration and verification evidence."""

    __tablename__ = "model_capabilities"

    endpoint_id = Column(
        String,
        ForeignKey("model_endpoints.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_id = Column(String, primary_key=True)
    tools_declared = Column(Boolean, nullable=True)
    declaration_fingerprint = Column(String, nullable=True)
    tools_verified = Column(Boolean, nullable=True)
    tools_verified_at = Column(DateTime, nullable=True)
    verification_fingerprint = Column(String, nullable=True)


class MimoProjectionState(Base):
    """Committed desired MiMo provider projection for one owner."""

    __tablename__ = "mimo_projection_states"

    owner_id = Column(String, primary_key=True, default="")
    desired_fingerprint = Column(String, nullable=False, default="")
    generation = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default="not_materialized")
    last_error_code = Column(String, nullable=True)
    requested_at = Column(DateTime, nullable=True)
    installed_at = Column(DateTime, nullable=True)


class ProviderAuthSession(TimestampMixin, Base):
    """Encrypted OAuth/session credentials for refresh-aware model providers."""
    __tablename__ = "provider_auth_sessions"

    id = Column(String, primary_key=True, index=True)
    provider = Column(String, nullable=False, index=True)
    owner = Column(String, nullable=True, index=True)
    label = Column(String, nullable=True)
    base_url = Column(String, nullable=False)
    access_token = Column(EncryptedText, nullable=True)
    refresh_token = Column(EncryptedText, nullable=True)
    last_refresh = Column(DateTime, nullable=True)
    auth_mode = Column(String, nullable=True)
    account_id = Column(String, nullable=True, index=True)


class MimoAuthStore(TimestampMixin, Base):
    """Odysseus-owned provider connection credentials (per owner, encrypted).

    Source of truth for the agent runtime's provider auth: the runtime's
    on-disk auth.json is a regenerable cache seeded from this row at spawn
    and mirrored back after connect/disconnect and OAuth token refreshes."""
    __tablename__ = "mimo_auth_store"

    owner = Column(String, primary_key=True, default="")
    payload = Column(EncryptedText, nullable=True)  # JSON auth store contents


class MimoModelPref(Base):
    """Per-owner visibility for mimo provider-account models.

    Row present = the owner hid that model from their own chat lists.
    Opt-out hide-list (vanilla semantics): provider inventories fluctuate,
    so newly appearing models default to visible until the owner hides them.
    Provider-account connections have no ModelEndpoint row, so their
    visibility state lives here instead of hidden_models/pinned_models."""
    __tablename__ = "mimo_model_prefs"

    owner = Column(String, primary_key=True, nullable=False, index=True)
    provider_id = Column(String, primary_key=True)
    model_id = Column(String, primary_key=True)


class ModelShare(TimestampMixin, Base):
    """One exact model route an owner has deliberately published."""

    __tablename__ = "model_shares"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    source_kind = Column(String, nullable=False)  # endpoint | native
    source_id = Column(String, nullable=False)
    model_id = Column(String, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index(
            "uq_model_shares_active_route",
            "owner",
            "source_kind",
            "source_id",
            "model_id",
            unique=True,
        ),
    )


class ModelShareSubscription(TimestampMixin, Base):
    """One owner grant plus the named recipient's explicit opt-in."""

    __tablename__ = "model_share_subscriptions"

    share_id = Column(
        String,
        ForeignKey("model_shares.id", ondelete="CASCADE"),
        primary_key=True,
    )
    subscriber = Column(String, primary_key=True, index=True)
    enabled = Column(Boolean, nullable=False, default=False)


class McpServer(TimestampMixin, Base):
    """Admin-configured MCP (Model Context Protocol) tool servers."""
    __tablename__ = "mcp_servers"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    transport = Column(String, nullable=False, default="stdio")  # "stdio" or "sse"
    command = Column(String, nullable=True)      # For stdio: executable path
    args = Column(Text, nullable=True)           # JSON array of command args
    env = Column(Text, nullable=True)            # JSON object of env vars
    url = Column(String, nullable=True)          # For SSE: server URL
    is_enabled = Column(Boolean, default=True)
    oauth_config = Column(Text, nullable=True)   # JSON: provider, keys_file, token_file, scopes
    disabled_tools = Column(Text, nullable=True)  # JSON array of tool names to hide from LLM
    oauth_tokens = Column(EncryptedText, nullable=True)  # JSON {tokens, client_info} for generic MCP OAuth, encrypted at rest


class Comparison(TimestampMixin, Base):
    """Stores A/B model comparison results."""
    __tablename__ = "comparisons"

    id = Column(String, primary_key=True, index=True)
    session_id = Column(String, nullable=True)     # Parent session context (optional)
    owner = Column(String, nullable=True, index=True)  # username
    prompt = Column(Text, nullable=False)
    model_a = Column(String, nullable=False)
    model_b = Column(String, nullable=False)
    endpoint_a = Column(String, nullable=False)
    endpoint_b = Column(String, nullable=False)
    provider_model_route_a_id = Column(String, nullable=True, index=True)
    provider_model_route_b_id = Column(String, nullable=True, index=True)
    response_a = Column(Text, nullable=True)
    response_b = Column(Text, nullable=True)
    metrics_a = Column(Text, nullable=True)         # JSON string
    metrics_b = Column(Text, nullable=True)         # JSON string
    winner = Column(String, nullable=True)           # "a", "b", "tie", or null
    is_blind = Column(Boolean, default=True)
    blind_mapping = Column(Text, nullable=True)      # JSON: {"left": "a"/"b", "right": "a"/"b"}
    voted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_comparisons_voted_at', 'voted_at'),
    )


class Signature(TimestampMixin, Base):
    """User-saved visual signatures (image stamps).

    Reusable across PDF form filling, email composition, and document editing.
    `data_png` is a base64-encoded PNG (no `data:` prefix). The SVG vector
    column is reserved for future smooth vector storage. Both are stored
    Fernet-encrypted at rest (see EncryptedText / src.secret_storage); a
    handwritten signature is sensitive, so it must never sit plaintext in the
    DB file. Existing rows are converted only by explicit workspace migration scripts.
    """
    __tablename__ = "signatures"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False, default="Signature")
    data_png = Column(EncryptedText, nullable=False)   # base64 PNG, encrypted at rest
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    svg = Column(EncryptedText, nullable=True)         # vector signature, encrypted at rest


class ApiToken(TimestampMixin, Base):
    """API tokens for external integrations (n8n, Make, etc.)."""
    __tablename__ = "api_tokens"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)
    token_hash = Column(String, nullable=False)
    token_prefix = Column(String, nullable=False)  # first 8 chars for display
    scopes = Column(String, nullable=False, default="chat")
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime, nullable=True)
    # Client classification keeps TUI device credentials inside the same
    # Open Clank application-token authority without granting them access to
    # ordinary API routes.  Existing rows migrate as ``api``.
    client_kind = Column(String, nullable=False, default="api")
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    device_label = Column(String, nullable=True)


class TuiTurnSubmission(TimestampMixin, Base):
    """Durable idempotency fence for a TUI-submitted canonical turn."""

    __tablename__ = "tui_turn_submissions"

    id = Column(String(64), primary_key=True)
    owner = Column(String, nullable=False, index=True)
    session_id = Column(
        String,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    state = Column(String, nullable=False, default="submitting", index=True)
    error_code = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "owner",
            "idempotency_key",
            name="uq_tui_turn_owner_idempotency",
        ),
    )


class Webhook(TimestampMixin, Base):
    """Outgoing webhooks fired on events."""
    __tablename__ = "webhooks"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    secret = Column(String, nullable=True)  # HMAC-SHA256 signing secret
    events = Column(String, nullable=False)  # comma-separated event types
    is_active = Column(Boolean, default=True)
    last_triggered_at = Column(DateTime, nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)


class UserTool(TimestampMixin, Base):
    """User-created sandboxed mini-apps/tools."""
    __tablename__ = "user_tools"

    id            = Column(String, primary_key=True, index=True)
    name          = Column(String, nullable=False)
    description   = Column(Text, nullable=True)
    icon          = Column(String, nullable=True, default="")
    html_content  = Column(Text, nullable=False)
    scope         = Column(String, nullable=False, default="global")  # "global" or session_id
    session_id    = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    owner         = Column(String, nullable=True, index=True)      # username
    is_pinned     = Column(Boolean, default=False)
    is_active     = Column(Boolean, default=True)
    version       = Column(Integer, default=1)
    author        = Column(String, nullable=True, default="ai")

    session = relationship("Session", backref=backref("user_tools", cascade="all, delete-orphan"))

    __table_args__ = (
        Index('ix_user_tools_scope', 'scope'),
        Index('ix_user_tools_active', 'is_active'),
    )


class UserToolData(Base):
    """Key-value storage for user tool persistent data."""
    __tablename__ = "user_tool_data"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    tool_id    = Column(String, ForeignKey("user_tools.id", ondelete="CASCADE"), nullable=False)
    key        = Column(String, nullable=False)
    value      = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    tool = relationship("UserTool", backref=backref("data_entries", cascade="all, delete-orphan"))

    __table_args__ = (
        Index('ix_user_tool_data_tool_key', 'tool_id', 'key', unique=True),
    )


class CrewMember(TimestampMixin, Base):
    """A custom AI persona ('crew member') with its own personality, model, tools, and memory scope."""
    __tablename__ = "crew_members"

    id            = Column(String, primary_key=True, index=True)
    owner         = Column(String, nullable=True, index=True)
    name          = Column(String, nullable=False)
    avatar        = Column(String, nullable=True)
    user_name     = Column(String, nullable=True)          # what they call the user
    personality   = Column(Text, nullable=True)             # system prompt
    model         = Column(String, nullable=True)
    endpoint_url  = Column(String, nullable=True)
    endpoint_id   = Column(String, nullable=True)  # display compatibility; executable selector is provider_model_route_id
    provider_model_route_id = Column(String, nullable=True, index=True)
    greeting      = Column(Text, nullable=True)
    enabled_tools = Column(Text, nullable=True)             # JSON array or "all"
    session_id    = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    is_active     = Column(Boolean, default=True)
    sort_order    = Column(Integer, default=0)
    is_default_assistant = Column(Boolean, default=False)   # singleton per-owner "personal assistant"
    timezone      = Column(String, nullable=True)           # IANA tz name (e.g. "America/New_York") for scheduled check-ins

    session = relationship("Session", foreign_keys=[session_id],
                           backref=backref("crew_member", uselist=False))


class ScheduledTask(TimestampMixin, Base):
    """A recurring or one-off task — LLM-powered or direct action, time or event triggered."""
    __tablename__ = "scheduled_tasks"

    id             = Column(String, primary_key=True, index=True)
    owner          = Column(String, nullable=True, index=True)
    name           = Column(String, nullable=False, default="Untitled Task")
    prompt         = Column(Text, nullable=True)              # LLM prompt (for task_type="llm")
    task_type      = Column(String, default="llm")            # "llm" | "action"
    action         = Column(String, nullable=True)            # builtin action name (for task_type="action")
    schedule       = Column(String, nullable=True)            # "once", "daily", "weekly", "monthly"
    scheduled_time = Column(String, nullable=True)            # "HH:MM" (24h, stored UTC)
    scheduled_day  = Column(Integer, nullable=True)           # day-of-week 0=Mon for weekly, day-of-month for monthly
    scheduled_date = Column(DateTime, nullable=True)          # exact datetime for "once"
    trigger_type   = Column(String, default="schedule")       # "schedule" | "event"
    trigger_event  = Column(String, nullable=True)            # e.g. "session_created", "message_sent"
    trigger_count  = Column(Integer, nullable=True)           # fire every N events
    trigger_counter = Column(Integer, default=0)              # current count toward trigger_count
    next_run       = Column(DateTime, nullable=True, index=True)
    last_run       = Column(DateTime, nullable=True)
    status         = Column(String, default="active")         # "active", "paused", "completed"
    output_target  = Column(String, default="session")        # "session" (extensible later)
    session_id     = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    model          = Column(String, nullable=True)
    endpoint_url   = Column(String, nullable=True)
    endpoint_id    = Column(String, nullable=True, index=True)  # retired endpoint display compatibility
    provider_model_route_id = Column(String, nullable=True, index=True)
    workspace      = Column(String, nullable=True)
    # Opaque file-policy Workspace identity. ``workspace`` remains the raw
    # path compatibility lane for older administrator-created tasks; new
    # callers persist this ID and derive the path from current policy per run.
    workspace_id   = Column(String, nullable=True, index=True)
    # Logical Copal namespace; deliberately independent from filesystem cwd.
    copal_workspace = Column(String, nullable=False, default="default")
    allowed_tools  = Column(Text, nullable=False, default="[]")
    # Stored value is historical/API-facing. The create/update API accepts and
    # reports ``fail_on_interaction``, but agent-turn runtime always sends
    # ``pause_on_interaction`` in the turn envelope (task_scheduler.py) so a
    # task pauses and waits rather than dying on the first user interaction.
    # Do not read this column as the live runtime policy for agent turns.
    interaction_policy = Column(String, nullable=False, default="fail_on_interaction")
    max_tool_calls = Column(Integer, nullable=False, default=20)
    run_count      = Column(Integer, default=0)

    cron_expression = Column(String, nullable=True)           # cron string e.g. "*/5 * * * *"
    then_task_id   = Column(String, ForeignKey("scheduled_tasks.id", ondelete="SET NULL"), nullable=True)
    webhook_token  = Column(String, nullable=True, unique=True)
    crew_member_id = Column(String, nullable=True)     # optional link to crew_members.id
    # character_id historically referenced an agent_characters table that was
    # never actually created. Keep the column for schema compatibility but
    # drop the ForeignKey so SQLAlchemy table sort doesn't fail on flush.
    character_id   = Column(String, nullable=True)
    max_steps      = Column(Integer, nullable=True)       # max agent loop iterations (null=unlimited)
    email_results  = Column(Boolean, default=True)        # email results to character.email_to
    notifications_enabled = Column(Boolean, default=True) # per-task on/off for completion notifications

    session = relationship("Session", backref=backref("scheduled_tasks", cascade="save-update, merge"))
    then_task = relationship("ScheduledTask", remote_side=[id], foreign_keys=[then_task_id])

    __table_args__ = (
        Index('ix_scheduled_tasks_due', 'status', 'next_run'),
        Index('ix_scheduled_tasks_event', 'trigger_type', 'trigger_event', 'status'),
    )


class EditorDraft(TimestampMixin, Base):
    """Persisted in-progress gallery-editor session — layered project state
    that the user can close and reopen later. Stores the full layer payload
    as JSON (with base64-encoded PNG dataURLs per layer) plus a small
    thumbnail for the landing-screen list.
    """
    __tablename__ = "editor_drafts"

    id              = Column(String, primary_key=True, index=True)
    owner           = Column(String, nullable=True, index=True)
    name            = Column(String, nullable=False, default="Untitled")
    # If the draft was opened FROM a gallery photo, point back at it so we
    # can show "Resuming edit of <photo>" and so reopening that photo picks
    # up the same draft rather than starting fresh.
    source_image_id = Column(String, nullable=True, index=True)
    width           = Column(Integer, nullable=True)
    height          = Column(Integer, nullable=True)
    # Full draft body — layer pixels (base64 PNG dataURLs), offsets,
    # opacities, visibility, active id, next id, etc. Kept as TEXT/JSON so
    # we don't have to re-shape the model every time the editor adds a
    # new piece of state.
    payload         = Column(Text, nullable=False, default="")
    # Tiny preview (data URL, ~128px wide) for the landing list. Stored
    # inline so the list endpoint can return everything in one shot.
    thumbnail       = Column(Text, nullable=True)
    is_active       = Column(Boolean, default=True)

    __table_args__ = (
        Index('ix_editor_drafts_owner_updated', 'owner', 'is_active', 'updated_at'),
    )


class ManagedImageProject(TimestampMixin, Base):
    """Versioned managed Imps project bound to a stable Files image resource.

    This is the recovery source for editable layer/mask/text state. It is
    keyed by owner plus a stable image resource identity (provider +
    resource_id), never by a raw database row id, so a resource move keeps its
    project association. ``expected_image_revision`` records the image revision
    the editable state was built against; ``project_revision`` is the
    optimistic-concurrency counter for state commits.

    Explicit portable export is a separate, caller-chosen artifact — no
    companion sidecar file is written beside the image.
    """
    __tablename__ = "managed_image_projects"

    id                    = Column(String, primary_key=True, index=True)
    owner                 = Column(String, nullable=False, index=True)
    # Stable image resource identity — the durable key across moves/renames.
    image_provider        = Column(String, nullable=False, default="files")
    image_resource_id     = Column(String, nullable=False, index=True)
    # Stable Save-copy replay fence. Scoped by owner in repository queries.
    operation_key         = Column(String, nullable=True, index=True)
    # Image revision the current editable state was built against.
    expected_image_revision = Column(String, nullable=True)
    # Optimistic-concurrency counter for state commits.
    project_revision      = Column(Integer, nullable=False, default=1)
    name                  = Column(String, nullable=False, default="Untitled")
    width                 = Column(Integer, nullable=True)
    height                = Column(Integer, nullable=True)
    # Editable layer/mask/text state document (JSON).
    state                 = Column(Text, nullable=False, default="{}")
    is_active             = Column(Boolean, default=True)

    __table_args__ = (
        Index('ix_managed_image_projects_owner_updated', 'owner', 'is_active', 'updated_at'),
        Index('ix_managed_image_projects_image', 'image_provider', 'image_resource_id'),
        UniqueConstraint('owner', 'operation_key', name='uq_managed_image_projects_owner_operation'),
    )


class TaskRun(Base):
    """Record of a single execution of a ScheduledTask."""
    __tablename__ = "task_runs"

    id          = Column(String, primary_key=True, index=True)
    task_id     = Column(String, ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False)
    started_at  = Column(DateTime, nullable=False, default=utcnow_naive)
    finished_at = Column(DateTime, nullable=True)
    status      = Column(String, default="running")  # "running", "success", "error"
    result      = Column(Text, nullable=True)
    error       = Column(Text, nullable=True)
    tokens_used = Column(Integer, nullable=True)
    steps       = Column(Text, nullable=True)             # JSON log of agent tool calls
    model       = Column(String, nullable=True)           # model that actually ran (resolved at execution)

    task = relationship("ScheduledTask", backref=backref("runs", cascade="all, delete-orphan",
                        order_by="TaskRun.started_at.desc()"))

    __table_args__ = (
        Index('ix_task_runs_task', 'task_id', 'started_at'),
    )


class TaskWaitRequest(Base):
    """Persisted waiting question or permission for a task run (S12).

    One row per pending interaction. The run transitions
    running -> waiting -> resuming -> running/terminal. Answers are consumed
    once via CAS on ``state``. Survives host/worker restart so the wait can be
    reconstructed and answered through the original task chat.
    """
    __tablename__ = "task_wait_requests"

    id           = Column(String, primary_key=True, index=True)
    task_id      = Column(String, ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id       = Column(String, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id   = Column(String, nullable=False, index=True)
    owner        = Column(String, nullable=False, index=True)
    kind         = Column(String, nullable=False, default="question")  # "question" | "permission"
    payload      = Column(Text, nullable=False, default="{}")          # JSON: questions / permission card
    state        = Column(String, nullable=False, default="waiting")   # "waiting" | "consumed" | "cancelled"
    workspace_id = Column(String, nullable=True)
    continuation_revision = Column(Integer, nullable=False, default=0)
    actor_generation = Column(String, nullable=True)
    created_at   = Column(DateTime, nullable=False, default=utcnow_naive)
    consumed_at  = Column(DateTime, nullable=True)

    task  = relationship("ScheduledTask", backref=backref("wait_requests", cascade="all, delete-orphan"))
    run   = relationship("TaskRun", backref=backref("wait_requests", cascade="all, delete-orphan"))

    __table_args__ = (
        Index('ix_task_wait_requests_state', 'state', 'created_at'),
        Index('ix_task_wait_requests_run', 'run_id', 'state'),
    )


class Memory(Base):
    """
    SQLAlchemy model for Memory table.
    Represents persistent memory entries with metadata.
    """
    __tablename__ = "memories"
    
    # Primary key
    id = Column(String, primary_key=True, index=True)
    
    # Memory content
    text = Column(Text, nullable=False)
    
    # Categorization
    category = Column(String, default='fact')
    source = Column(String, default='user')

    # Owner (username)
    owner = Column(String, nullable=True, index=True)

    # Reference to session (nullable)
    session_id = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)

    # Timestamp as Unix timestamp
    timestamp = Column(Integer, default=lambda: int(utcnow_naive().timestamp()))

    # Relationship to Session
    session = relationship("Session", backref="memories")

    # Indexes - optimized composites
    __table_args__ = (
        Index('ix_memories_lookup', 'category', 'timestamp'),  # Composite for category-based queries
        Index('ix_memories_session', 'session_id', 'timestamp'),  # Composite for session-based queries
    )


class Note(TimestampMixin, Base):
    """A Google Keep-style note or checklist."""
    __tablename__ = "notes"

    id         = Column(String, primary_key=True, index=True)
    owner      = Column(String, nullable=True, index=True)
    title      = Column(String, default="")
    content    = Column(Text, nullable=True)
    items      = Column(Text, nullable=True)       # JSON string of [{text, done}]
    note_type  = Column(String, default="note")     # "note" or "checklist"
    color      = Column(String, nullable=True)
    label      = Column(String, nullable=True)
    pinned     = Column(Boolean, default=False)
    archived   = Column(Boolean, default=False)
    due_date   = Column(String, nullable=True)
    source     = Column(String, default="user")     # "user" or "agent"
    session_id = Column(String, nullable=True)
    sort_order = Column(Integer, default=0)
    image_url  = Column(String, nullable=True)      # uploaded image URL (relative path)
    repeat     = Column(String, default="none")     # none, daily, weekly, monthly, yearly
    # Auto-AI fields — populated by /api/notes/{id}/classify. The classification
    # JSON shape is { kind, solvable, confidence, task_prompt, tools, items?: [...] }.
    # Content hash gates re-classification (avoid LLM spend on every save).
    ai_classification = Column(Text, nullable=True)
    ai_content_hash   = Column(String, nullable=True)
    # Chat session spawned by the note's "Agent" button (solve-this-todo).
    # The note shows a clickable tag that opens this session for review.
    agent_session_id  = Column(String, nullable=True)


class CalendarCal(TimestampMixin, Base):
    """A calendar (e.g. 'Personal', 'TimeTree')."""
    __tablename__ = "calendars"

    id    = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name  = Column(String, nullable=False)
    color = Column(String, default="#5b8abf")
    source = Column(String, default="local")  # "local" or "caldav"
    # UUID of the CalDAV account in user prefs that owns this calendar.
    # NULL for local calendars and for CalDAV calendars created before
    # multi-account support was added (treated as "use any configured account").
    account_id = Column(String, nullable=True, index=True)
    caldav_base_url = Column(String, nullable=True)

    events = relationship("CalendarEvent", back_populates="calendar", cascade="all, delete-orphan")


class CalendarEvent(TimestampMixin, Base):
    """A calendar event."""
    __tablename__ = "calendar_events"

    uid         = Column(String, primary_key=True, index=True)
    calendar_id = Column(String, ForeignKey("calendars.id"), nullable=False, index=True)
    summary     = Column(String, nullable=False, default="")
    description = Column(Text, default="")
    location    = Column(String, default="")
    dtstart     = Column(DateTime, nullable=False, index=True)
    dtend       = Column(DateTime, nullable=False)
    all_day     = Column(Boolean, default=False)
    # True when dtstart/dtend are stored as UTC instants (set on import paths
    # that preserve the source TZID). False = legacy naive-local. Drives the
    # `Z`-suffix on serialization so the frontend interprets correctly.
    is_utc      = Column(Boolean, default=False, nullable=False)
    rrule       = Column(String, default="")
    recurrence_exdates = Column(Text, default="")  # JSON list of skipped occurrence starts
    color       = Column(String, nullable=True)  # per-event color override
    status      = Column(String, default="confirmed")  # confirmed, cancelled
    importance  = Column(String, default="normal")    # low | normal | high | critical
    event_type  = Column(String, nullable=True)        # work | personal | health | travel | meal | social | admin | other
    last_pinged = Column(DateTime, nullable=True)      # last time the assistant pinged about this event
    # "caldav" = pulled from a CalDAV server (so the sync may prune it when it
    # vanishes upstream). NULL/local = created locally (agent, email triage, or
    # a UI event whose write-back failed) and must NOT be pruned by the sync.
    origin      = Column(String, nullable=True, index=True)
    remote_href = Column(String, nullable=True)        # CalDAV object URL for updates/deletes
    remote_etag = Column(String, nullable=True)        # Last seen CalDAV ETag, when available
    caldav_sync_pending = Column(String, nullable=True) # create | update | delete retry marker

    calendar = relationship("CalendarCal", back_populates="events")


class CalendarDeletedEvent(TimestampMixin, Base):
    """Hidden CalDAV delete tombstone retained until remote delete succeeds."""
    __tablename__ = "caldav_deleted_events"

    uid = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    calendar_id = Column(String, nullable=True, index=True)
    remote_href = Column(String, nullable=True)
    remote_etag = Column(String, nullable=True)
    caldav_base_url = Column(String, nullable=True)
    summary = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)


class Integration(TimestampMixin, Base):
    """An external service connection (email, RSS, webhook, etc.)."""
    __tablename__ = "integrations"

    id     = Column(String, primary_key=True, index=True)
    owner  = Column(String, nullable=True, index=True)
    name   = Column(String, nullable=False)
    type   = Column(String, nullable=False)  # "email", "rss", "webhook"
    config = Column(JSON, nullable=True)     # type-specific config
    enabled = Column(Boolean, default=True)


# WARNING: Foreign-key enforcement is enabled globally for all SQLite connections.
# Any future migrations or schema changes that temporarily violate foreign-key
# constraints will fail. To perform such operations, foreign_keys must be
# temporarily disabled around the migration workflow.
CORE_CREATED_FRESH = False


def init_db():
    """Create an empty current store, or validate an existing store without upgrades."""
    from core import stats_models  # noqa: F401
    from core.provider_models import ProviderBase
    from core.operation_models import OperationBase
    from src.openclank.logging_models import LoggingBase, validate_logging_schema

    tables = [table for table in Base.metadata.sorted_tables if table.name not in _RETIRED_PROVIDER_TABLES]
    tables += list(ProviderBase.metadata.sorted_tables) + list(OperationBase.metadata.sorted_tables)
    existing = set(inspect(engine).get_table_names())
    if not existing:
        Base.metadata.create_all(bind=engine, tables=[table for table in tables if table.metadata is Base.metadata])
        ProviderBase.metadata.create_all(bind=engine)
        OperationBase.metadata.create_all(bind=engine)
        LoggingBase.metadata.create_all(bind=engine)
        _initialize_empty_transcript_search()
        global CORE_CREATED_FRESH
        CORE_CREATED_FRESH = True
    else:
        validate_logging_schema(engine)
        missing = []
        inspector = inspect(engine)
        for table in tables:
            if table.name not in existing:
                missing.append(table.name)
                continue
            columns = {column["name"] for column in inspector.get_columns(table.name)}
            missing.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in columns)
        if engine.dialect.name == "sqlite":
            if "chat_messages_fts" not in existing:
                missing.append("chat_messages_fts")
            with engine.connect() as connection:
                triggers = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'")}
            missing.extend(name for name in ("chat_messages_fts_ai", "chat_messages_fts_ad", "chat_messages_fts_au") if name not in triggers)
        retired = sorted(existing.intersection(_RETIRED_PROVIDER_TABLES))
        retired_foreign_keys = [
            f"{table}.{','.join(fk['constrained_columns'])}->{fk['referred_table']}"
            for table in ("crew_members", "scheduled_tasks") if table in existing
            for fk in inspector.get_foreign_keys(table)
            if fk["referred_table"] in _RETIRED_PROVIDER_TABLES
        ]
        if retired_foreign_keys:
            raise RuntimeError(
                "Existing database has retired provider foreign keys: "
                + ", ".join(retired_foreign_keys)
                + "; stop all writers and retain a complete backup. Restore the matching release "
                "or prepare an offline conversion before opening this store with the current release. "
                "No runtime conversion was performed."
            )
        if missing or retired:
            raise RuntimeError("Existing database does not match this release. Stop writers and retain a complete backup; restore the matching release or prepare an offline conversion. A fresh installation needs a separate empty data directory. No runtime conversion was performed. Missing schema: " + ", ".join(missing) + "; retired tables: " + ", ".join(retired))
    db_path = _sqlite_db_path(engine.url)
    if db_path is not None:
        # Fail closed-loud on the main file: this is the only access control on
        # it, so if the chmod genuinely fails (read-only FS, foreign owner) an
        # operator should hear about it. safe_chmod also returns False as a
        # Windows no-op, so guard on IS_WINDOWS to avoid a spurious warning there.
        if not safe_chmod(db_path, 0o600) and not IS_WINDOWS:
            logger.warning(
                "Could not restrict %s to 0o600; it holds secrets and may be "
                "world-readable. Check filesystem permissions and ownership.",
                db_path,
            )
        # Re-lock any sidecars present at startup. New ones inherit the main
        # file's mode (now 0o600, since we set it first), and they're usually
        # absent here, but a stale -wal/-shm/-journal left by an older 0o644
        # install could still expose secret pages. Absent sidecars are the
        # normal case, not an error — only a failed chmod warrants a warning.
        for suffix in _SQLITE_SIDECARS:
            sidecar = db_path + suffix
            if (
                os.path.exists(sidecar)
                and not safe_chmod(sidecar, 0o600)
                and not IS_WINDOWS
            ):
                logger.warning(
                    "Could not restrict %s to 0o600; it may expose DB pages.",
                    sidecar,
                )
    from src.agent_actor_accounting import recover_actor_accounting_after_restart
    recover_actor_accounting_after_restart()


def _initialize_empty_transcript_search():
    """Install transcript search for a newly created, empty SQLite store."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE VIRTUAL TABLE chat_messages_fts USING fts5(content, message_id UNINDEXED, session_id UNINDEXED, role UNINDEXED)")
        expression = "CASE WHEN instr(COALESCE(new.content, ''), ';base64,') > 0 OR instr(COALESCE(new.content, ''), 'data:image/') > 0 OR instr(COALESCE(new.content, ''), 'data:audio/') > 0 THEN '[inline media omitted from search index]' ELSE COALESCE(new.content, '') END"
        connection.exec_driver_sql(f"CREATE TRIGGER chat_messages_fts_ai AFTER INSERT ON chat_messages BEGIN INSERT INTO chat_messages_fts(content,message_id,session_id,role) VALUES ({expression},new.id,new.session_id,new.role); END")
        connection.exec_driver_sql("CREATE TRIGGER chat_messages_fts_ad AFTER DELETE ON chat_messages BEGIN DELETE FROM chat_messages_fts WHERE message_id=old.id; END")
        connection.exec_driver_sql(f"CREATE TRIGGER chat_messages_fts_au AFTER UPDATE ON chat_messages BEGIN DELETE FROM chat_messages_fts WHERE message_id=old.id; INSERT INTO chat_messages_fts(content,message_id,session_id,role) VALUES ({expression},new.id,new.session_id,new.role); END")


def get_db():
    """
    Dependency to get a database session.
    Used in FastAPI routes to inject database sessions.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from contextlib import contextmanager
from typing import Generator

@contextmanager
def get_db_session() -> Generator:
    """Context manager for database sessions"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def bulk_insert_messages(session_id: str, messages: list):
    """Efficiently insert multiple messages"""
    with get_db_session() as db:
        db.bulk_insert_mappings(
            ChatMessage,
            [
                {
                    'session_id': session_id,
                    'role': msg['role'],
                    'content': msg['content'],
                    'timestamp': utcnow_naive()
                }
                for msg in messages
            ]
        )

def cleanup_old_sessions(days: int = 30):
    """Remove sessions older than specified days"""
    from datetime import timedelta
    
    with get_db_session() as db:
        cutoff_date = utcnow_naive() - timedelta(days=days)
        
        deleted_count = db.query(Session).filter(
            Session.archived == True,
            Session.last_accessed < cutoff_date,
            Session.is_important == False
        ).delete()
        
        return deleted_count

def get_session_stats():
    """Get database statistics"""
    with get_db_session() as db:
        stats = {
            'total_sessions': db.query(Session).count(),
            'active_sessions': db.query(Session).filter(Session.archived == False).count(),
            'archived_sessions': db.query(Session).filter(Session.archived == True).count(),
            'total_messages': db.query(ChatMessage).count(),
            'total_memories': db.query(Memory).count()
        }
        return stats

def get_detailed_stats():
    """Get comprehensive database statistics including file size"""
    stats = get_session_stats()  # Use existing function
    
    # Add database file size
    db_size_mb = 0.0
    if "sqlite" in DATABASE_URL:
        db_path = DATABASE_URL.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
        
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            db_size_mb = round(db_size / (1024 * 1024), 2)
    
    stats['database_size_mb'] = db_size_mb
    return stats

def update_session_last_accessed(session_id: str):
    """Update the last_accessed timestamp for a session"""
    with get_db_session() as db:
        db_session = db.query(Session).filter(Session.id == session_id).first()
        if db_session:
            db_session.last_accessed = utcnow_naive()
            db.commit()
            return True
    return False

def get_session_mode(session_id: str):
    """Return a session's persisted `mode`, or None if unset/unknown.

    Best-effort: never raises (returns None on any DB error) so callers on hot
    request paths needn't guard it. Routed through get_db_session() so the
    connection is always returned to the pool."""
    try:
        with get_db_session() as db:
            return db.query(Session.mode).filter(Session.id == session_id).scalar()
    except Exception:
        logger.warning("Failed to read mode for session %s", session_id)
        return None

def set_session_mode(session_id: str, mode: str) -> bool:
    """Persist a session's `mode`. Best-effort: never raises, returns success.

    Routed through get_db_session() so a failure mid-write (e.g. a SQLite
    'database is locked' under concurrent streams) still returns the connection
    to the pool instead of leaking it — repeated leaks would exhaust it."""
    try:
        with get_db_session() as db:
            db.query(Session).filter(Session.id == session_id).update({"mode": mode})
        return True
    except Exception:
        logger.warning("Failed to persist mode %r for session %s", mode, session_id)
        return False

def get_session_by_id(session_id: str):
    """Get a session by ID"""
    with get_db_session() as db:
        return db.query(Session).filter(Session.id == session_id).first()

def get_upcoming_events(owner, horizon_days: int = 60, limit: int = 40):
    """Upcoming, non-cancelled events as {uid, title, start} dicts, soonest first.

    owner=None means NO owner scoping (single-user / legacy). Multi-user callers
    MUST pass the owning username — otherwise they read every tenant's events.
    The autonomous email->calendar pass relies on this to avoid disclosing (and
    acting on) other users' calendars."""
    from datetime import timedelta
    now = utcnow_naive()
    with get_db_session() as db:
        q = db.query(CalendarEvent).join(CalendarCal).filter(
            CalendarEvent.dtstart >= now,
            CalendarEvent.dtstart <= now + timedelta(days=horizon_days),
            CalendarEvent.status != "cancelled",
        )
        if owner is not None:
            q = q.filter(CalendarCal.owner == owner)
        return [
            {
                "uid": e.uid,
                "title": e.summary or "",
                "start": e.dtstart.isoformat() if e.dtstart else "",
            }
            for e in q.order_by(CalendarEvent.dtstart).limit(limit).all()
        ]

def archive_session(session_id: str):
    """Archive a session"""
    with get_db_session() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.archived = True
            db.commit()
            return True
    return False

# Initialize the database by creating all tables


init_db()
