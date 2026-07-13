# src/constants.py
"""Application-wide constants and configuration values."""
import os

from src.runtime_paths import get_app_root, get_default_data_dir

APP_VERSION = "1.0.1"

# ---------------------------------------------------------------------------
# OPENTHESIUS_DEBUG — master debug bit
# ---------------------------------------------------------------------------
# Set to "1" or "true" in .env to enable debug pipelines throughout thesius.
# Individual debug systems should gate on this flag (e.g. extra logging,
# timing instrumentation, verbose error dumps). Check via is_openthesius_debug().
def is_openthesius_debug() -> bool:
    v = os.environ.get("OPENTHESIUS_DEBUG", "")
    return v.lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# THESIUS_AGENT_HOME — per-agent process isolation (Phase 4)
# ---------------------------------------------------------------------------
# When set, derive ODYSSEUS_DATA_DIR, OPENTHESIUS_GLOBAL_CWD, MIMOCODE_HOME,
# and FM_DB_PATH so each agent process gets its own data/config under its
# agent home. Explicit overrides in the environment take precedence.
_THESIUS_AGENT_HOME = os.environ.get("THESIUS_AGENT_HOME")

# Snapshot the ORIGINAL default data dir BEFORE we possibly override
# ODYSSEUS_DATA_DIR.  This lets AUTH_FILE stay pinned to the global
# location even when data lives under the agent home.
_ORIGINAL_DEFAULT_DATA_DIR = get_default_data_dir()

if _THESIUS_AGENT_HOME:
    _h = _THESIUS_AGENT_HOME
    if "ODYSSEUS_DATA_DIR" not in os.environ:
        os.environ["ODYSSEUS_DATA_DIR"] = os.path.join(_h, "data")
    if "OPENTHESIUS_GLOBAL_CWD" not in os.environ:
        os.environ["OPENTHESIUS_GLOBAL_CWD"] = _h
    if "MIMOCODE_HOME" not in os.environ:
        os.environ["MIMOCODE_HOME"] = os.path.join(_h, ".mimocode")
    if "FM_DB_PATH" not in os.environ:
        os.environ["FM_DB_PATH"] = os.path.join(_h, "data", "frankenmemory.db")

# Base paths
BASE_DIR = os.path.join(get_app_root(), "")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.getenv("ODYSSEUS_DATA_DIR", get_default_data_dir())

# Every fm-mcp child inherits one canonical absolute database path. Resolving
# here avoids cwd-dependent split brains between Odyssius and MiMo.
FM_DB_PATH = os.path.abspath(
    os.path.expanduser(os.environ.get("FM_DB_PATH") or os.path.join(DATA_DIR, "frankenmemory.db"))
)
os.environ["FM_DB_PATH"] = FM_DB_PATH

# Data file paths
# Single source of truth: every persisted file/dir lives under DATA_DIR, which
# is the ONLY place ODYSSEUS_DATA_DIR is read. Import these constants instead of
# re-deriving paths from __file__ or a relative "data" literal.
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
PERSONAL_DIR = os.path.join(DATA_DIR, "personal_docs")
RUNBOOK_DIR = os.path.join(PERSONAL_DIR, "runbook")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
FEATURES_FILE = os.path.join(DATA_DIR, "features.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
# Auth stays global even when THESIUS_AGENT_HOME is set — one user, one auth.
AUTH_FILE = os.path.join(
    _ORIGINAL_DEFAULT_DATA_DIR if _THESIUS_AGENT_HOME else DATA_DIR,
    "auth.json",
)
USER_PREFS_FILE = os.path.join(DATA_DIR, "user_prefs.json")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
INTEGRATIONS_FILE = os.path.join(DATA_DIR, "integrations.json")
CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
APP_KEY_FILE = os.path.join(DATA_DIR, ".app_key")
EMBEDDING_ENDPOINT_FILE = os.path.join(DATA_DIR, "embedding_endpoint.json")
COOKBOOK_STATE_FILE = os.path.join(DATA_DIR, "cookbook_state.json")
BG_JOBS_FILE = os.path.join(DATA_DIR, "bg_jobs.json")
VAULT_FILE = os.path.join(DATA_DIR, "vault.json")
TIDY_CALENDAR_STATE_FILE = os.path.join(DATA_DIR, "tidy_calendar_state.json")
SKILLS_FILE = os.path.join(DATA_DIR, "skills.json")
APP_DB = os.path.join(DATA_DIR, "app.db")
SCHEDULED_EMAILS_DB = os.path.join(DATA_DIR, "scheduled_emails.db")
EMAIL_CACHE_DB = os.path.join(DATA_DIR, "email_cache.db")

# Data subdirectories
PERSONAL_UPLOADS_DIR = os.path.join(DATA_DIR, "personal_uploads")
EMOJI_CACHE_DIR = os.path.join(DATA_DIR, "emoji_cache")
RAG_DIR = os.path.join(DATA_DIR, "rag")
CHROMA_DIR = os.path.join(DATA_DIR, "chroma")
BG_JOBS_DIR = os.path.join(DATA_DIR, "bg_jobs")
DEEP_RESEARCH_DIR = os.path.join(DATA_DIR, "deep_research")
MCP_OAUTH_DIR = os.path.join(DATA_DIR, "mcp_oauth")
GENERATED_IMAGES_DIR = os.path.join(DATA_DIR, "generated_images")
TTS_CACHE_DIR = os.path.join(DATA_DIR, "tts_cache")
EMAIL_URGENCY_CACHE_DIR = os.path.join(DATA_DIR, "email_urgency_cache")
SKILLS_DIR = os.path.join(DATA_DIR, "skills")
GALLERY_DIR = os.path.join(DATA_DIR, "gallery")
GALLERY_UPLOADS_DIR = os.path.join(DATA_DIR, "gallery_uploads")
MEMORY_VECTORS_DIR = os.path.join(DATA_DIR, "memory_vectors")

# Paths with an intentional dedicated env override, defaulting under DATA_DIR.
MAIL_ATTACHMENTS_DIR = os.getenv("ODYSSEUS_MAIL_ATTACHMENTS_DIR", os.path.join(DATA_DIR, "mail-attachments"))
# `or` (not os.getenv's default arg) so a PRESENT-but-EMPTY value falls back to
# the default. docker-compose.yml injects `FASTEMBED_CACHE_PATH=${FASTEMBED_CACHE_PATH:-}`,
# which sets the var to "" when the host hasn't defined it. os.getenv(name, default)
# only returns the default when the var is ABSENT, so the empty string would win →
# os.makedirs("") raises [Errno 2] No such file or directory: '' → FastEmbed fails to
# init and all vector features (RAG, semantic memory, tool index) silently degrade.
FASTEMBED_CACHE_DIR = os.getenv("FASTEMBED_CACHE_PATH") or os.path.join(DATA_DIR, "fastembed_cache")

# Agent tool output limits (single source of truth — imported by tool_execution.py,
# tool_implementations.py, agent_tools.py, and any other module that needs them)
MAX_OUTPUT_CHARS = 10_000       # cap for bash/python/web_search/web_fetch output
MAX_READ_CHARS = 20_000         # cap for read_file / document preview
MAX_DIFF_LINES = 400            # cap for edit_file unified-diff display

# web_fetch response-size policy (#3812). MAX_OUTPUT_CHARS above only trims
# what the agent SEES; these caps bound what the server downloads, parses,
# and writes to the content cache. The soft cap is the default download
# budget; the agent can raise it per call (full/max_bytes) but never past
# the hard cap, so a model can't decide to pull a multi-GB file.
WEB_FETCH_SOFT_MAX_BYTES = 2_000_000    # default download budget (2 MB)
WEB_FETCH_HARD_MAX_BYTES = 20_000_000   # absolute ceiling, even with override (20 MB)

# API Configuration
MAX_CONTEXT_MESSAGES = 90
REQUEST_TIMEOUT = 20
OPENAI_COMPAT_PATH = "/v1/chat/completions"

# Outbound UA for web_fetch / web_search scraping; common desktop UA so pages serve normal HTML.
WEB_FETCH_USER_AGENT = os.environ.get(
    "WEB_FETCH_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
)

# Environment variables with defaults
DEFAULT_HOST = os.getenv("LLM_HOST", "localhost")
LLM_HOSTS = [h.strip() for h in os.getenv("LLM_HOSTS", "").split(",") if h.strip()]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SEARXNG_INSTANCE = os.getenv("SEARXNG_INSTANCE", "http://localhost:8080")


# Cleanup configuration
CLEANUP_ENABLED = os.getenv("CLEANUP_ENABLED", "True").lower() == "true"
CLEANUP_INTERVAL_HOURS = int(os.getenv("CLEANUP_INTERVAL_HOURS", "24"))

# Auth policy
PASSWORD_MIN_LENGTH = 8

# Default parameters
DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_TOKENS = 0


def internal_api_base() -> str:
    """Base URL for in-process loopback calls to Odysseus's own API.

    Agent tools and background jobs reach admin-gated routes by calling the
    running server over HTTP. Resolution order:
      1. ODYSSEUS_INTERNAL_BASE  - explicit override (e.g. behind a TLS proxy).
      2. APP_PORT                - http://127.0.0.1:$APP_PORT (docker-compose).
      3. Fallback http://127.0.0.1:7000 - legacy default.

    127.0.0.1 (not "localhost") avoids IPv6/DNS ambiguity for a strictly-local
    call. Without this, loopback tools fail with "All connection attempts
    failed" whenever the server is not on port 7000.
    """
    override = os.environ.get("ODYSSEUS_INTERNAL_BASE")
    if override:
        return override.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('APP_PORT', '7000')}"
