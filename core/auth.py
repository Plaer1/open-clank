"""
Authentication module — multi-user password hashing, session tokens, config persistence.
Config stored in data/auth.json. Uses bcrypt directly.
"""

import enum
import copy
import importlib
import json
import math
import os
import secrets
import threading
import time
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import bcrypt
import pyotp

logger = logging.getLogger(__name__)


from core.atomic_io import atomic_write_json as _atomic_write_json  # noqa: E402
from core.middleware import INTERNAL_TOOL_USER  # noqa: E402

_LEGACY_DEFAULT_PRIVILEGES = {
    "can_use_agent": True,
    "can_use_browser": True,
    "can_use_bash": False,
    "can_use_documents": True,
    "can_use_research": True,
    "can_generate_images": True,
    "can_manage_memory": True,
    "max_messages_per_day": 0,
    "allowed_models": [],
    "allowed_models_restricted": False,
    # Explicit "block every model" sentinel. An empty `allowed_models` list is
    # ambiguous — it's also what gets sent when the admin clicks "[All]" — so
    # we need a dedicated flag to express "this user may use no models at all"
    # distinctly from "this user has no restriction".
    "block_all_models": False,
}

_OPTIONAL_FEATURES = tuple(key for key in _LEGACY_DEFAULT_PRIVILEGES if key.startswith("can_"))
DEFAULT_PRIVILEGES = {
    **_LEGACY_DEFAULT_PRIVILEGES,
    **dict.fromkeys(_OPTIONAL_FEATURES, False),
    "block_all_models": True,
}
ADMIN_PRIVILEGES = {
    **DEFAULT_PRIVILEGES,
    **dict.fromkeys(_OPTIONAL_FEATURES, True),
    "block_all_models": False,
}


def _saved_privileges(user: dict) -> dict:
    stored = user.get("privileges")
    if user.get("privilege_policy") != 1:
        if user.get("is_admin") is True and stored == ADMIN_PRIVILEGES:
            stored = user.get("privileges_before_admin")
        if stored == _LEGACY_DEFAULT_PRIVILEGES:
            return {}
    if not isinstance(stored, dict):
        return {}
    return {key: value for key, value in stored.items() if key in DEFAULT_PRIVILEGES}


def _effective_privileges(user: dict, owner_id: str | None) -> dict:
    if owner_id and user.get("account_id") == owner_id and user.get("full_access") is True:
        return copy.deepcopy(ADMIN_PRIVILEGES)
    saved = _saved_privileges(user)
    effective = {**DEFAULT_PRIVILEGES, **saved}
    if (user.get("privilege_policy") != 1
            and user.get("is_admin") is True
            and user.get("privileges") == ADMIN_PRIVILEGES
            and user.get("privileges_before_admin") is None):
        # Pre-policy admins already had chat and model access. A bootstrap
        # owner ID was not recorded, so dropping both would strand the account.
        # The compatibility grant is effective only while this legacy record
        # qualifies. It does not become a saved grant on role changes.
        effective.update(can_use_agent=True, block_all_models=False)
    if "block_all_models" not in saved and (
        saved.get("allowed_models") or saved.get("allowed_models_restricted") is True
    ):
        effective["block_all_models"] = False
    for key in _OPTIONAL_FEATURES:
        effective[key] = effective[key] is True
    return copy.deepcopy(effective)


from src.constants import AUTH_FILE, PASSWORD_MIN_LENGTH
from src.owner_identity import (
    FORBIDDEN_STORED_IDENTITIES,
    FORBIDDEN_STORED_PREFIXES,
    is_reserved_stored_identity,
)
DEFAULT_AUTH_PATH = AUTH_FILE
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 days (sliding window, renewed by activity)
# Sliding expiration: a validated request extends the session, but the
# extension write is throttled so a busy poller doesn't rewrite the sessions
# file on every call, and an absolute cap from creation keeps a stolen or
# never-closed token from living forever.
SESSION_TOUCH_INTERVAL = 60 * 5  # seconds between persisted extensions
SESSION_ABSOLUTE_TTL = 60 * 60 * 24 * 30  # 30 days from creation, no renewal past this

# Usernames the auth + middleware layer reserve as internal "synthetic owner"
# sentinels; they must never belong to a real account. The most dangerous is
# "internal-tool": `core.middleware.require_admin` treats any request whose
# `current_user == "internal-tool"` as the in-process tool loopback and grants
# admin, and because the cookie auth path sets `current_user` to the raw
# username, an account literally named "internal-tool" would be silently
# treated as an admin by every `require_admin`-gated route. "api" collides with
# the bearer-token owner-attribution sentinel. "demo"/"system" round out the
# synthetic-owner set the rest of the codebase already special-cases (see
# `_SYNTHETIC_OWNERS` in routes/assistant_routes.py and the matching guards in
# src/task_scheduler.py / routes/research_routes.py) — a real account with one
# of those names would be denied an assistant and inconsistently owner-scoped.
# Refuse to create or rename into any of them so the sentinels can't be
# impersonated. (Keep this in sync with that synthetic-owner set.)
RESERVED_USERNAMES = frozenset({INTERNAL_TOOL_USER, "api", "demo", "system"})
COPAL_RESERVED_USERNAMES = frozenset(
    {
        "shared",
        "local",
        "__copal_unclaimed_owner__",
        "__copal_unclaimed_workspace__",
    }
)
RESERVED_USERNAME_PREFIXES = ("user:", "deleted:")


def is_reserved_username(username: str | None) -> bool:
    key = str(username or "").strip().lower()
    return (
        key in RESERVED_USERNAMES
        or key in COPAL_RESERVED_USERNAMES
        or key.startswith(RESERVED_USERNAME_PREFIXES)
        # Owner-identity mapping names (Default/Local/__odysseus_local__ and the
        # local-installation partition key) must never become a stored human
        # account either. This is the seam every create/rename path already
        # calls, so wiring the predicate here makes the refusal real.
        or is_reserved_stored_identity(username)
    )


def normalize_known_username(users: Dict[str, Any], username: str | None) -> Optional[str]:
    """Return a normalized username only when it exists in the auth user map."""
    key = str(username or "").strip().lower()
    if not key or key not in users or is_reserved_username(key):
        return None
    return key


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


class SetAdminResult(enum.Enum):
    """Outcome of AuthManager.set_admin, so callers can map each case to a
    precise response instead of guessing from a bare bool."""
    OK = "ok"
    USER_NOT_FOUND = "user_not_found"
    NOT_AUTHORIZED = "not_authorized"   # requester is not an admin
    LAST_ADMIN = "last_admin"           # would remove the last remaining admin


class AuthManager:
    """Manages multi-user password + session-token auth system."""

    def __init__(self, auth_path: str = DEFAULT_AUTH_PATH):
        self.auth_path = auth_path
        self._sessions_path = os.path.join(os.path.dirname(auth_path), "sessions.json")
        self._config: Dict[str, Any] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}  # token -> {username, expiry}
        # Guards mutations of self._sessions and the on-disk sessions.json.
        # Validate/create/revoke run concurrently from the FastAPI threadpool.
        self._sessions_lock = threading.RLock()
        # Guards all mutations of self._config and the on-disk auth.json so
        # concurrent create/delete/rename/privilege operations don't interleave
        # and corrupt the user database.
        self._config_lock = threading.Lock()
        # Guards the first-run setup check-and-write so concurrent requests
        # cannot both observe is_configured==False and both create admin accounts.
        self._setup_lock = threading.Lock()
        self._account_lifecycle_fence = None
        self._load()
        self._validate_current_auth()
        self._load_sessions()

    def _validate_current_auth(self) -> None:
        if not self._config:
            return
        users = self._config.get("users")
        if not isinstance(users, dict):
            raise RuntimeError("Legacy auth requires .clanker/tools/migrations/python/secondary.py auth")
        seen = set()
        for username, user in users.items():
            if is_reserved_username(username):
                continue
            identity = str(user.get("account_id") or "") if isinstance(user, dict) else ""
            if (not identity.startswith("account-") or identity in seen
                    or "is_admin" not in user or username != username.strip().lower()):
                raise RuntimeError("Legacy auth requires .clanker/tools/migrations/python/secondary.py auth")
            seen.add(identity)

    def configure_account_lifecycle_fence(self, checker) -> None:
        """Deny authenticated writes while an immutable account is converging."""
        self._account_lifecycle_fence = checker if callable(checker) else None

    def _is_account_lifecycle_fenced(self, username: str) -> bool:
        checker = self._account_lifecycle_fence
        if not callable(checker):
            return False
        try:
            return bool(checker(username))
        except Exception:
            # An unreadable lifecycle ledger cannot safely authorize writes.
            return True

    def is_account_lifecycle_fenced(self, username: str) -> bool:
        """Public fail-closed fence shared by cookie and bearer auth."""
        return self._is_account_lifecycle_fenced(username)

    def _load(self):
        try:
            if os.path.exists(self.auth_path):
                with open(self.auth_path, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
                logger.info("Auth config loaded")
            else:
                self._config = {}
                logger.info("No auth config found — first-run setup required")
        except Exception as e:
            logger.error("Existing auth config could not be loaded; authentication is unavailable")
            raise RuntimeError("Existing auth config is unreadable or malformed; restore or explicitly repair it before startup") from e

    def _load_sessions(self):
        """Load persisted session tokens from disk, pruning expired ones."""
        try:
            if os.path.exists(self._sessions_path):
                with open(self._sessions_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                loaded = {}
                changed = False
                for token, raw in (data.items() if isinstance(data, dict) else ()):
                    if not isinstance(raw, dict):
                        changed = True
                        continue
                    try:
                        expiry = float(raw.get("expiry", 0))
                    except (TypeError, ValueError):
                        changed = True
                        continue
                    created = raw.get("created")
                    if not isinstance(created, (int, float)):
                        raise RuntimeError("Legacy auth sessions require .clanker/tools/migrations/python/secondary.py auth-sessions")
                    absolute_expiry = float(created) + SESSION_ABSOLUTE_TTL
                    if expiry <= now or now >= absolute_expiry:
                        changed = True
                        continue
                    if expiry > absolute_expiry:
                        raw["expiry"] = absolute_expiry
                        changed = True
                    loaded[str(token)] = raw
                self._sessions = loaded
                if changed:
                    self._save_sessions()
                logger.info(f"Loaded {len(self._sessions)} session(s) from disk")
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"Failed to load sessions: {e}")
            self._sessions = {}

    def _save_sessions(self):
        """Persist session tokens to disk (atomic, lock-guarded)."""
        try:
            with self._sessions_lock:
                snapshot = dict(self._sessions)
            _atomic_write_json(self._sessions_path, snapshot)
        except Exception as e:
            logger.error(f"Failed to save sessions: {e}")





    def _save(self, config=None):
        from src.openclank.memory_policy_projection import policy_coordinator
        config = self._config if config is None else config
        coordinator = policy_coordinator()
        if coordinator is not None:
            try:
                try:
                    with open(self.auth_path, 'r', encoding='utf-8') as stream:
                        previous = json.load(stream)
                except FileNotFoundError:
                    # First save on a fresh data dir: no previous users to compare.
                    previous = {'users': {}}
                old_users, new_users = previous['users'], config['users']
                if type(old_users) is not dict or type(new_users) is not dict:
                    raise ValueError('authentication policy unavailable')
                for name in old_users.keys() & new_users.keys():
                    old, new = old_users[name], new_users[name]
                    if type(old) is not dict or type(new) is not dict:
                        raise ValueError('authentication policy unavailable')
                    old_allowed = _effective_privileges(old, previous.get("first_run_owner"))["can_manage_memory"]
                    new_allowed = _effective_privileges(new, config.get("first_run_owner"))["can_manage_memory"]
                    if old_allowed != new_allowed:
                        coordinator.validate_authority(name)
            except (OSError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError('authentication policy unavailable') from exc
        _atomic_write_json(self.auth_path, config, indent=2)

    def _commit_config(self, candidate: dict) -> None:
        try:
            self._save(candidate)
        except BaseException:
            # A late Lore publication failure can follow the atomic live write.
            # Keep in-memory reads aligned with a positively identified commit.
            try:
                with open(self.auth_path, encoding="utf-8") as saved:
                    if json.load(saved) == candidate:
                        self._config = candidate
            except (OSError, ValueError):
                pass
            raise
        self._config = candidate

    @property
    def users(self) -> Dict[str, Any]:
        return self._config.get("users", {})

    @property
    def signup_enabled(self) -> bool:
        return self._config.get("signup_enabled", False)

    @signup_enabled.setter
    def signup_enabled(self, value: bool):
        with self._config_lock:
            self._config["signup_enabled"] = value
            self._save()

    @property
    def is_configured(self) -> bool:
        return len(self.users) > 0

    def policy(self) -> dict:
        """Return public auth policy constants for the frontend."""
        return {
            "password_min_length": PASSWORD_MIN_LENGTH,
            "reserved_usernames": sorted(
                RESERVED_USERNAMES | COPAL_RESERVED_USERNAMES | FORBIDDEN_STORED_IDENTITIES
            ),
            "reserved_username_prefixes": sorted(
                set(RESERVED_USERNAME_PREFIXES) | set(FORBIDDEN_STORED_PREFIXES)
            ),
            "signup_enabled": self.signup_enabled,
            "session_days": TOKEN_TTL // 86400,
        }

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def setup(self, username: str, password: str) -> bool:
        """First-run admin setup. Only works if no users exist."""
        with self._setup_lock:
            if self.is_configured:
                return False
            return self._create_user(username, password, is_admin=True, bootstrap=True)

    def create_user(self, username: str, password: str, is_admin: bool = False) -> bool:
        return self._create_user(username, password, is_admin=is_admin, bootstrap=False)

    def _create_user(self, username: str, password: str, *, is_admin: bool, bootstrap: bool) -> bool:
        username = username.strip().lower()
        if not username:
            return False
        if is_reserved_username(username):
            logger.warning("Refused to create reserved username '%s'", username)
            return False
        with self._config_lock:
            if username in self.users or (bootstrap and self.users):
                return False
            candidate = copy.deepcopy(self._config)
            account_id = f"account-{uuid.uuid4().hex}"
            candidate.setdefault("users", {})[username] = {
                "account_id": account_id,
                "password_hash": _hash_password(password),
                "created": time.time(),
                "is_admin": is_admin,
                "privilege_policy": 1,
                "privileges": {},
            }
            if bootstrap:
                candidate["first_run_owner"] = account_id
                candidate["users"][username]["full_access"] = False
            self._commit_config(candidate)
        logger.info(f"Created user '{username}' (admin={is_admin})")
        return True

    def delete_user(self, username: str, requesting_user: str) -> bool:
        """Delete a user. Only admins can delete, and can't delete themselves.

        SECURITY: also revoke every active session token belonging to this
        user so any open browser tab they have gets kicked back to /login
        on the next request. Without this the user kept full access until
        their cookie expired naturally (default ~30 days).
        """
        username = username.strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            if username == requesting_user:
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            # Purge every provider/model identity owned by this account before
            # removing the auth row. Otherwise deleting and recreating the same
            # username silently resurrects its old endpoints and credentials.
            # Keep this in one DB transaction with API-token revocation.
            try:
                database = importlib.import_module("core.database")
                purge_specs = (
                    ("ApiToken", "owner"),
                    ("ModelEndpoint", "owner"),
                    ("ProviderAuthSession", "owner"),
                    ("MimoAuthStore", "owner"),
                    ("MimoProjection", "owner"),
                    ("MimoProjectionState", "owner_id"),
                    ("ModelShare", "owner"),
                    ("ModelShareSubscription", "subscriber"),
                )
                removed = {}
                with database.get_db_session() as db:
                    model_share = getattr(database, "ModelShare", None)
                    share_subscription = getattr(
                        database,
                        "ModelShareSubscription",
                        None,
                    )
                    if model_share is not None and share_subscription is not None:
                        share_ids = [
                            row.id
                            for row in db.query(model_share).filter(
                                model_share.owner == username
                            ).all()
                        ]
                        share_grants = db.query(share_subscription).filter(
                            (share_subscription.subscriber == username)
                            | (
                                share_subscription.share_id.in_(share_ids)
                                if share_ids
                                else False
                            )
                        ).delete(synchronize_session=False)
                        removed["ModelShareSubscription"] = share_grants
                    for model_name, owner_column in purge_specs:
                        if model_name == "ModelShareSubscription":
                            continue
                        model = getattr(database, model_name, None)
                        if model is None:
                            continue
                        column = getattr(model, owner_column)
                        removed[model_name] = db.query(model).filter(
                            column == username
                        ).delete(synchronize_session=False)
                if any(removed.values()):
                    logger.info(
                        "Purged deleted user '%s' provider state: %s",
                        username,
                        ", ".join(
                            f"{name}={count}" for name, count in removed.items() if count
                        ),
                    )
            except Exception:
                logger.warning(
                    "Failed to purge provider state for deleted user '%s'", username
                )
                return False
            del self._config["users"][username]
            self._save()
        # Purge all sessions belonging to this user. validate_token doesn't
        # cross-check `self.users`, so without this step a deleted user's
        # cookie keeps authenticating.
        revoked = 0
        with self._sessions_lock:
            to_drop = [tok for tok, sess in self._sessions.items()
                       if (sess or {}).get("username") == username]
            for tok in to_drop:
                self._sessions.pop(tok, None)
                revoked += 1
        if revoked:
            self._save_sessions()
        logger.info(f"Deleted user '{username}' (by {requesting_user}); revoked {revoked} active session(s)")
        return True

    def delete_user_auth_barrier(self, username: str, requesting_user: str) -> bool:
        """Remove only auth identity/session state for a lifecycle saga.

        The account lifecycle coordinator stages and purges every data domain
        around this barrier. Keeping those effects out of this method makes a
        crash after auth deletion distinguishable and forward-resumable.
        """
        username = str(username or "").strip().lower()
        requesting_user = str(requesting_user or "").strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            if username == requesting_user:
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            del self._config["users"][username]
            self._save()
        revoked = 0
        with self._sessions_lock:
            to_drop = [
                token
                for token, session in self._sessions.items()
                if (session or {}).get("username") == username
            ]
            for token in to_drop:
                self._sessions.pop(token, None)
                revoked += 1
        if revoked:
            self._save_sessions()
        logger.info(
            "Deleted auth identity '%s' (by %s); revoked %d active session(s)",
            username,
            requesting_user,
            revoked,
        )
        return True

    def rename_user(self, old_username: str, new_username: str, requesting_user: str) -> bool:
        """Rename a user in auth config and active sessions. Admin only."""
        old_username = old_username.strip().lower()
        new_username = new_username.strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        if not old_username or not new_username:
            return False
        if is_reserved_username(new_username):
            logger.warning("Refused to rename '%s' into reserved username '%s'", old_username, new_username)
            return False
        with self._config_lock:
            if old_username not in self.users:
                return False
            if new_username in self.users:
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            self._config.setdefault("users", {})[new_username] = self._config["users"].pop(old_username)
            self._save()

        renamed_sessions = 0
        with self._sessions_lock:
            for sess in self._sessions.values():
                sess_user = str((sess or {}).get("username") or "").strip().lower()
                if sess_user == old_username:
                    sess["username"] = new_username
                    renamed_sessions += 1
        if renamed_sessions:
            self._save_sessions()
        logger.info(
            "Renamed user '%s' -> '%s' (by %s); updated %d active session(s)",
            old_username, new_username, requesting_user, renamed_sessions,
        )
        return True

    def is_admin(self, username: str) -> bool:
        key = normalize_known_username(self.users, username)
        return bool(key and self.users[key].get("is_admin") is True)

    def account_id(self, username: str) -> Optional[str]:
        """Return the immutable identity for an existing account."""
        key = normalize_known_username(self.users, username)
        user = self.users.get(key) if key else None
        if not isinstance(user, dict):
            return None
        value = str(user.get("account_id") or "").strip()
        return value or None

    def username_for_account_id(self, account_id: str) -> Optional[str]:
        """Resolve an immutable account identity to its current display key."""
        expected = str(account_id or "").strip()
        if not expected:
            return None
        for username, user in self.users.items():
            if str((user or {}).get("account_id") or "").strip() == expected:
                return username
        return None

    def list_users(self) -> List[Dict[str, Any]]:
        return [
            {
                "username": u,
                "account_id": d.get("account_id"),
                "is_admin": d.get("is_admin", False),
                "privileges": self.get_privileges(u),
            }
            for u, d in self.users.items()
        ]

    def get_privileges(self, username: str) -> Dict[str, Any]:
        config = self._config
        users = config.get("users", {})
        key = normalize_known_username(users, username)
        if key is None:
            return {}
        return _effective_privileges(users[key], config.get("first_run_owner"))

    def full_access_status(self, username: str) -> dict:
        config = self._config
        users = config.get("users", {})
        key = normalize_known_username(users, username)
        owner_id = config.get("first_run_owner")
        user = users.get(key, {})
        eligible = bool(owner_id and user.get("account_id") == owner_id)
        return {
            "eligible": eligible,
            "enabled": eligible and user.get("full_access") is True,
        }

    def filesystem_access_eligible(self, username: str, account_id: str) -> bool:
        config = self._config
        users = config.get("users", {})
        key = normalize_known_username(users, username)
        user = users.get(key, {})
        return bool(account_id and user.get("account_id") == account_id and
                    (user.get("is_admin") is True or config.get("first_run_owner") == account_id))

    def set_filesystem_access(self, username: str, account_id: str, scope: str, enabled: bool, *, repository):
        with self._config_lock:
            if self._is_account_lifecycle_fenced(username) or not self.filesystem_access_eligible(username, account_id):
                return None
            return repository.set_full_filesystem_access(account_id, scope, enabled)

    def set_owner_full_access(self, username: str, account_id: str, enabled: bool) -> bool:
        from src.openclank.memory_policy_projection import policy_coordinator
        username = username.strip().lower()
        if type(enabled) is not bool:
            raise ValueError("Full access requires a boolean")

        def owner_snapshot():
            if self._is_account_lifecycle_fenced(username):
                return None
            user = self.users.get(username)
            if (not user or not account_id or self._config.get("first_run_owner") != account_id
                    or user.get("account_id") != account_id):
                return None
            return user

        def commit():
            candidate = copy.deepcopy(self._config)
            candidate["users"][username]["full_access"] = enabled
            self._commit_config(candidate)
            return True

        with self._config_lock:
            user = owner_snapshot()
            if user is None:
                return False
            if (user.get("full_access") is True) == enabled:
                return True
            coordinator = policy_coordinator()
            if coordinator is None or _effective_privileges(user, None)["can_manage_memory"] is True:
                return commit()
            expected = copy.deepcopy(user)

        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        with PREFERENCE_LOCK:
            before = copy.deepcopy(_load_for_user(username))
        token = coordinator.begin(username, before)
        try:
            with self._config_lock:
                if owner_snapshot() != expected:
                    raise ValueError("Authentication changed during access transition")
                commit()
            with PREFERENCE_LOCK:
                after = copy.deepcopy(_load_for_user(username))
            coordinator.finish(token, username, after)
            return True
        finally:
            coordinator.close(token)

    def set_privileges(self, username: str, privileges: Dict[str, Any]) -> bool:
        from src.openclank.memory_policy_projection import policy_coordinator
        coordinator = policy_coordinator()
        name = username.strip().lower()
        if coordinator is None:
            return self._set_privileges_inner(name, privileges)
        with self._config_lock:
            current = copy.deepcopy(self.users.get(name))
            if not current or current.get('is_admin') or 'can_manage_memory' not in privileges:
                return self._set_privileges_inner_unlocked(name, privileges)
            old = _effective_privileges(current, self._config.get("first_run_owner"))["can_manage_memory"]
            if old == (privileges["can_manage_memory"] is True):
                return self._set_privileges_inner_unlocked(name, privileges)
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        with PREFERENCE_LOCK:
            before = copy.deepcopy(_load_for_user(name))
        token = coordinator.begin(name, before)
        try:
            result = self._set_privileges_inner(name, privileges, expected=current)
            with PREFERENCE_LOCK:
                after = copy.deepcopy(_load_for_user(name))
            coordinator.finish(token, name, after)
            return result
        finally:
            coordinator.close(token)

    def _set_privileges_inner(self, username: str, privileges: Dict[str, Any], *, expected=None) -> bool:
        """Update privileges for a user. Can't modify admin privileges."""
        username = username.strip().lower()
        with self._config_lock:
            if expected is not None and json.dumps(self.users.get(username), sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
                raise ValueError('authentication changed during policy transition')
            return self._set_privileges_inner_unlocked(username, privileges)

    def _set_privileges_inner_unlocked(self, username: str, privileges: Dict[str, Any]) -> bool:
        if username not in self.users:
            return False
        if self.users[username].get("is_admin") or self.full_access_status(username)["eligible"]:
            return False
        current = _saved_privileges(self.users[username])
        updated = {**current, **{key: value for key, value in privileges.items() if key in DEFAULT_PRIVILEGES}}
        if json.dumps(current, sort_keys=True, allow_nan=False) == json.dumps(updated, sort_keys=True, allow_nan=False):
            return True
        candidate = copy.deepcopy(self._config)
        user = candidate["users"][username]
        user["privileges"] = updated
        user["privilege_policy"] = 1
        self._commit_config(candidate)
        logger.info("Updated privileges for %r", username)
        return True

    def set_admin(self, username: str, is_admin: bool, requesting_user: str) -> SetAdminResult:
        """Change the durable role without changing optional self capabilities."""
        username = (username or "").strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        is_admin = bool(is_admin)
        with self._config_lock:
            target = self.users.get(username)
            if target is None:
                return SetAdminResult.USER_NOT_FOUND
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return SetAdminResult.NOT_AUTHORIZED
            if bool(target.get("is_admin")) == is_admin:
                return SetAdminResult.OK
            if not is_admin and sum(bool(user.get("is_admin")) for user in self.users.values()) <= 1:
                return SetAdminResult.LAST_ADMIN
            candidate = copy.deepcopy(self._config)
            updated = candidate["users"][username]
            updated["privileges"] = _saved_privileges(target)
            updated["privilege_policy"] = 1
            updated["is_admin"] = is_admin
            account_id = str(target.get("account_id") or "")
            retire_files = bool(not is_admin and account_id and account_id != self._config.get("first_run_owner"))
            if retire_files:
                from src.openclank.file_policy import FilePolicyRepository

                FilePolicyRepository().retire_full_filesystem_access(account_id)
            self._commit_config(candidate)
        if retire_files:
            from src.openclank.files_service_client import close_clients_for_owner

            close_clients_for_owner(username)
        logger.info("Set is_admin=%s for %r (by %r)", is_admin, username, requesting_user)
        return SetAdminResult.OK

    def change_password(self, username: str, current_password: str, new_password: str) -> bool:
        username = username.strip().lower()
        # A short non-empty password is never valid.  The only exception is the
        # exact empty string, whose administrator check is made atomically with
        # the write below so a concurrent demotion cannot leave a regular user
        # with an empty credential.
        if new_password != "" and len(new_password) < PASSWORD_MIN_LENGTH:
            return False
        with self._config_lock:
            target = self._config.get("users", {}).get(username)
            if not target:
                return False
            if new_password == "" and not target.get("is_admin"):
                return False
            if not _verify_password(current_password, target["password_hash"]):
                return False
            target["password_hash"] = _hash_password(new_password)
            self._save()
        return True

    # ------------------------------------------------------------------
    # TOTP two-factor authentication
    # ------------------------------------------------------------------

    def totp_enabled(self, username: str) -> bool:
        """Check if 2FA is enabled for a user."""
        user = self.users.get(username.strip().lower(), {})
        return bool(user.get("totp_enabled"))

    def totp_generate_secret(self, username: str) -> Optional[str]:
        """Generate a new TOTP secret for a user. Returns the secret (not yet enabled)."""
        username = username.strip().lower()
        if username not in self.users:
            return None
        secret = pyotp.random_base32()
        with self._config_lock:
            self._config["users"][username]["totp_secret_pending"] = secret
            self._save()
        return secret

    def totp_get_provisioning_uri(self, username: str, secret: str) -> str:
        """Get the otpauth:// URI for QR code generation."""
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=username, issuer_name="Open Clank")

    def totp_confirm_enable(self, username: str, code: str) -> bool:
        """Verify a TOTP code against the pending secret, then enable 2FA."""
        username = username.strip().lower()
        user = self.users.get(username, {})
        secret = user.get("totp_secret_pending")
        if not secret:
            return False
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            return False
        # Enable 2FA
        with self._config_lock:
            self._config["users"][username]["totp_secret"] = secret
            self._config["users"][username]["totp_enabled"] = True
            self._config["users"][username].pop("totp_secret_pending", None)
            # Generate backup codes
            backup = [secrets.token_hex(4) for _ in range(8)]
            self._config["users"][username]["totp_backup_codes"] = backup
            self._save()
        logger.info(f"2FA enabled for '{username}'")
        return True

    def totp_verify(self, username: str, code: str) -> bool:
        """Verify a TOTP code for login."""
        username = username.strip().lower()
        user = self.users.get(username, {})
        if not user.get("totp_enabled"):
            return True  # 2FA not enabled, always pass
        secret = user.get("totp_secret")
        if not secret:
            # 2FA is enabled but no secret is stored (corrupt/partially-written
            # auth.json). Fail closed — returning True here bypassed the second
            # factor entirely.
            return False
        # Check backup codes first
        backup = user.get("totp_backup_codes", [])
        if code in backup:
            with self._config_lock:
                backup.remove(code)
                self._config["users"][username]["totp_backup_codes"] = backup
                self._save()
            logger.info(f"Backup code used for '{username}' ({len(backup)} remaining)")
            return True
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)

    def totp_disable(self, username: str, password: str) -> bool:
        """Disable 2FA for a user. Requires password confirmation."""
        username = username.strip().lower()
        if not self.verify_password(username, password):
            return False
        with self._config_lock:
            self._config["users"][username].pop("totp_secret", None)
            self._config["users"][username].pop("totp_secret_pending", None)
            self._config["users"][username].pop("totp_backup_codes", None)
            self._config["users"][username]["totp_enabled"] = False
            self._save()
        logger.info(f"2FA disabled for '{username}'")
        return True

    # ------------------------------------------------------------------
    # Login / logout / session tokens
    # ------------------------------------------------------------------

    def verify_password(self, username: str, password: str) -> bool:
        username = username.strip().lower()
        if normalize_known_username(self.users, username) is None:
            return False
        return _verify_password(password, self.users[username]["password_hash"])

    def create_session(self, username: str, password: str) -> Optional[str]:
        """Verify credentials and return a session token, or None."""
        username = username.strip().lower()
        if not self.verify_password(username, password):
            return None
        return self.create_session_trusted(username)

    def create_session_trusted(self, username: str, remember: bool = False) -> Optional[str]:
        """Issue a session token for an already-verified user.
        Call only after verify_password (and TOTP if enabled) have passed.
        ``remember`` records whether the login asked for a persistent cookie so
        the middleware knows it may refresh the cookie's max_age as the
        session slides."""
        username = username.strip().lower()
        token = secrets.token_hex(32)
        now = time.time()
        with self._config_lock:
            if normalize_known_username(self.users, username) is None:
                logger.warning("Refused to issue session for unavailable user '%s'", username)
                return None
            with self._sessions_lock:
                self._sessions[token] = {
                    "username": username,
                    "expiry": now + TOKEN_TTL,
                    "created": now,
                    "touched": now,
                    # Cookie Max-Age is derived from this same issuance tick,
                    # never from a later time.time() call in the route.
                    "cookie_issued_at": now,
                    "remember": bool(remember),
                }
        self._save_sessions()
        return token

    def _slide_session_expiry(self, session: Dict[str, Any], now: float) -> bool:
        """Extend a valid session's expiry on activity (sliding expiration).

        Returns True when the session record changed and should be persisted.
        Sessions persisted before sliding expiration have no ``created`` stamp;
        derive it from the fixed window they were issued under. Past the
        absolute cap the session is left to expire at its current ``expiry``.
        """
        created = session.get("created")
        changed = False
        if not isinstance(created, (int, float)):
            created = session["expiry"] - TOKEN_TTL
            session["created"] = created
            changed = True
        absolute_expiry = float(created) + SESSION_ABSOLUTE_TTL
        if session.get("expiry", 0) > absolute_expiry:
            session["expiry"] = absolute_expiry
            changed = True
        if now >= absolute_expiry:
            return changed
        if now - session.get("touched", 0) < SESSION_TOUCH_INTERVAL:
            return changed
        session["touched"] = now
        session["expiry"] = min(now + TOKEN_TTL, absolute_expiry)
        session["cookie_issued_at"] = now
        return True

    @staticmethod
    def _session_absolute_expiry(session: Dict[str, Any]) -> Optional[float]:
        """Return the hard expiry, repairing a legacy missing creation stamp."""
        try:
            created = session.get("created")
            if not isinstance(created, (int, float)):
                return None
            return float(created) + SESSION_ABSOLUTE_TTL
        except (TypeError, ValueError):
            return None

    def validate_session(self, token: Optional[str]) -> Tuple[bool, bool]:
        """Validate a session token, applying sliding expiration.

        Returns ``(valid, refresh_cookie)``. ``refresh_cookie`` is True when
        this call extended the session and the session was created from a
        "remember me" login, meaning the client cookie's max_age should be
        renewed too so an active browser doesn't outlive its cookie.
        """
        if not token:
            return False, False
        expired = False
        deleted_user = False
        slid = False
        remember = False
        now = time.time()
        with self._sessions_lock:
            session = self._sessions.get(token)
            if session is None:
                return False, False
            absolute_expiry = self._session_absolute_expiry(session)
            if (
                absolute_expiry is None
                or now >= absolute_expiry
                or now >= float(session.get("expiry", 0))
            ):
                self._sessions.pop(token, None)
                expired = True
            else:
                # SECURITY: if the user record has since been removed (admin
                # deleted them while their cookie was still valid), drop the
                # session so the next request kicks them out instead of
                # silently authenticating against a non-existent account.
                if is_reserved_username(session.get("username")):
                    return False, False
                if session.get("username") not in self.users:
                    self._sessions.pop(token, None)
                    deleted_user = True
                elif self._is_account_lifecycle_fenced(session.get("username")):
                    return False, False
                else:
                    slid = self._slide_session_expiry(session, now)
                    remember = bool(session.get("remember"))
        if expired or deleted_user or slid:
            self._save_sessions()
        if expired or deleted_user:
            return False, False
        return True, slid and remember

    def validate_token(self, token: Optional[str]) -> bool:
        valid, _refresh_cookie = self.validate_session(token)
        return valid

    def get_username_for_token(self, token: Optional[str]) -> Optional[str]:
        """Return the username associated with a valid token."""
        if not token:
            return None
        expired = False
        deleted_user = False
        with self._sessions_lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            now = time.time()
            absolute_expiry = self._session_absolute_expiry(session)
            if (
                absolute_expiry is None
                or now >= absolute_expiry
                or now >= float(session.get("expiry", 0))
            ):
                self._sessions.pop(token, None)
                expired = True
            else:
                _u = session["username"]
                if is_reserved_username(_u):
                    return None
                # SECURITY: orphan check — same rationale as validate_token.
                if _u not in self.users:
                    self._sessions.pop(token, None)
                    deleted_user = True
                elif self._is_account_lifecycle_fenced(_u):
                    return None
                else:
                    return _u
        if expired or deleted_user:
            self._save_sessions()
        return None

    def session_cookie_lifetime(self, token: Optional[str]) -> Dict[str, Any]:
        """Return one cookie lifetime anchored to the token's issuance tick.

        HTTP ``Max-Age`` has whole-second precision while our server expiry is
        fractional. Round the shared absolute boundary upward so a browser
        never discards a still-valid server token early; ``expires`` carries
        the exact server boundary and the server remains authoritative at that
        boundary. Most importantly, route latency cannot turn a seven-day
        token into a 604799-second cookie.
        """
        if not token:
            return {"max_age": 0, "expires": datetime.fromtimestamp(0, timezone.utc)}
        with self._sessions_lock:
            session = self._sessions.get(token)
            if not session:
                return {"max_age": 0, "expires": datetime.fromtimestamp(0, timezone.utc)}
            absolute_expiry = self._session_absolute_expiry(session)
            if absolute_expiry is None:
                return {"max_age": 0, "expires": datetime.fromtimestamp(0, timezone.utc)}
            try:
                expires = min(float(session.get("expiry", 0)), absolute_expiry)
                issued_at = float(
                    session.get(
                        "cookie_issued_at",
                        session.get("touched", session.get("created", expires)),
                    )
                )
                remaining = expires - issued_at
            except (TypeError, ValueError):
                return {"max_age": 0, "expires": datetime.fromtimestamp(0, timezone.utc)}
            return {
                "max_age": max(0, int(math.ceil(remaining))),
                "expires": datetime.fromtimestamp(expires, timezone.utc),
            }

    def session_remaining_ttl(self, token: Optional[str]) -> int:
        """Backward-compatible cookie Max-Age accessor."""
        return int(self.session_cookie_lifetime(token)["max_age"])

    def revoke_token(self, token: str):
        with self._sessions_lock:
            self._sessions.pop(token, None)
        self._save_sessions()

    def revoke_user_sessions(self, username: str, except_token: Optional[str] = None) -> int:
        """Revoke active browser sessions for a user, optionally preserving one."""
        username = username.strip().lower()
        revoked = 0
        with self._sessions_lock:
            to_drop = [
                token for token, session in self._sessions.items()
                if token != except_token and (session or {}).get("username") == username
            ]
            for token in to_drop:
                self._sessions.pop(token, None)
                revoked += 1
            if revoked:
                self._save_sessions()
        return revoked

    def status(self, token: Optional[str]) -> Dict[str, Any]:
        username = self.get_username_for_token(token)
        authenticated = username is not None
        result = {
            "configured": self.is_configured,
            "authenticated": authenticated,
            "username": username,
            "is_admin": self.is_admin(username) if username else False,
        }
        if authenticated:
            result["privileges"] = self.get_privileges(username)
            result["full_access"] = self.full_access_status(username)
        return result
