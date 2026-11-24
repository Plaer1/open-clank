"""Guarded-store policy transition receipts; canonical policy remains host-owned.

This primitive is deliberately unavailable until the explicit offline capability
marker and the canonical writer hooks are installed. It does not interpret policy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from src.memery_database import GuardedStorePreparation

_MARKER = "openclank.policy_projection.v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _key(database_id: str, account: str, target: str, *, global_scope: bool = False) -> str:
    if type(global_scope) is not bool or not all(type(value) is str and value and value == value.strip() and len(value) <= 512 for value in (database_id, account, target)):
        raise ValueError("policy scope unavailable")
    encoded = json.dumps([database_id, "global"] if global_scope else [database_id, "account", account, target], ensure_ascii=False, separators=(",", ":")).encode()
    return "policy_projection.v1." + hashlib.sha256(encoded).hexdigest()


def _decode(value: str) -> dict:
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate policy projection field")
            result[key] = item
        return result
    data = json.loads(value, object_pairs_hook=unique)
    if (type(data) is not dict or set(data) != {"version", "epoch", "fingerprint", "state", "transition_id"}
            or type(data["version"]) is not int or data["version"] != 1
            or type(data["epoch"]) is not int or not 1 <= data["epoch"] < 9223372036854775807
            or not isinstance(data["fingerprint"], str) or not _HASH.fullmatch(data["fingerprint"])
            or data["state"] not in ("ready", "fenced")
            or not isinstance(data["transition_id"], str) or not _HASH.fullmatch(data["transition_id"])):
        raise ValueError("invalid policy projection")
    return data


class PolicyProjection:
    """Exact scoped CAS in the same SQLite writer domain as native publication."""

    def __init__(self, preparation: GuardedStorePreparation):
        if type(preparation) is not GuardedStorePreparation:
            raise ValueError("guarded preparation required")
        preparation.assert_valid()
        self._preparation = preparation

    def read_scope(self, account: str, target: str, *, global_scope: bool = False) -> dict | None:
        """Read an exact scope, preserving fenced and absent as distinct states."""
        prepared = self._preparation
        prepared.assert_valid()
        key = _key(prepared.database_id, account, target, global_scope=global_scope)
        with closing(sqlite3.connect(Path(prepared.database_path).as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("BEGIN")
            if (db.execute("SELECT value FROM fm_meta WHERE key=?", (_MARKER,)).fetchone() != ("1",)
                    or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone() != (prepared.database_id,)
                    or db.execute("PRAGMA user_version").fetchone() != (prepared.schema_version,)):
                raise ValueError("policy projection unavailable")
            row = db.execute("SELECT value FROM fm_meta WHERE key=?", (key,)).fetchone()
            result = None if row is None else _decode(row[0])
            prepared.assert_valid()
            db.rollback()
        return result

    def ready_refs(self, account: str, target: str) -> dict:
        """Read both publication references from one snapshot; never repair."""
        prepared = self._preparation
        prepared.assert_valid()
        keys = (_key(prepared.database_id, account, target),
                _key(prepared.database_id, account, target, global_scope=True))
        with closing(sqlite3.connect(Path(prepared.database_path).as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("BEGIN")
            if (db.execute("SELECT value FROM fm_meta WHERE key=?", (_MARKER,)).fetchone() != ("1",)
                    or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone() != (prepared.database_id,)
                    or db.execute("PRAGMA user_version").fetchone() != (prepared.schema_version,)):
                raise ValueError("policy projection unavailable")
            rows = [db.execute("SELECT value FROM fm_meta WHERE key=?", (key,)).fetchone() for key in keys]
            if any(row is None for row in rows):
                raise ValueError("policy projection unavailable")
            values = [_decode(row[0]) for row in rows]
            if any(value['state'] != 'ready' for value in values):
                raise ValueError("policy projection fenced")
            prepared.assert_valid()
            db.rollback()
        return {name: {key: value[key] for key in ('epoch', 'fingerprint', 'transition_id')}
                for name, value in zip(('account', 'global'), values)}

    def ready_refs_bounded(self, account: str, target: str) -> dict:
        """Read existing policy facts for a send boundary; never evaluate or grant policy."""
        import os
        prepared = self._preparation
        st = os.stat(prepared.database_path, follow_symlinks=False)
        if (st.st_dev, st.st_ino) != (prepared.device, prepared.inode) or st.st_nlink != 1:
            raise ValueError("policy projection unavailable")
        keys = (_key(prepared.database_id, account, target),
                _key(prepared.database_id, account, target, global_scope=True))
        with closing(sqlite3.connect(Path(prepared.database_path).as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
            budget = [0]
            def progress():
                budget[0] += 1
                return 1 if budget[0] > 100 else 0
            db.set_progress_handler(progress, 1000)
            db.execute("BEGIN")
            if (db.execute("PRAGMA schema_version").fetchone() != (prepared.schema_cookie,)
                    or db.execute("SELECT value FROM fm_meta WHERE key=?", (_MARKER,)).fetchone() != ("1",)
                    or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone() != (prepared.database_id,)):
                raise ValueError("policy projection unavailable")
            rows = [db.execute("SELECT value FROM fm_meta WHERE key=? AND length(CAST(value AS BLOB))<=4096", (key,)).fetchone() for key in keys]
            if any(row is None for row in rows): raise ValueError("policy projection unavailable")
            values = [_decode(row[0]) for row in rows]
            if any(value['state'] != 'ready' for value in values): raise ValueError("policy projection fenced")
            db.rollback()
        after = os.stat(prepared.database_path, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino): raise ValueError("policy projection changed")
        return {name: {key: value[key] for key in ('epoch', 'fingerprint', 'transition_id')}
                for name, value in zip(('account', 'global'), values)}

    def provision_fenced(self, account: str, target: str, fingerprint: str, *, global_scope: bool = False) -> dict:
        """Create a scope in the unavailable state; ready requires reconciliation."""
        if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
            raise ValueError("policy fingerprint unavailable")
        prepared = self._preparation
        prepared.assert_valid()
        key = _key(prepared.database_id, account, target, global_scope=global_scope)
        value = {"version": 1, "epoch": 1, "fingerprint": fingerprint,
                 "state": "fenced", "transition_id": secrets.token_hex(32)}
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with closing(sqlite3.connect(Path(prepared.database_path).as_uri() + "?mode=rw", uri=True, timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            if (db.execute("SELECT value FROM fm_meta WHERE key=?", (_MARKER,)).fetchone() != ("1",)
                    or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone() != (prepared.database_id,)
                    or db.execute("PRAGMA user_version").fetchone() != (prepared.schema_version,)):
                raise ValueError("policy projection unavailable")
            db.execute("INSERT INTO fm_meta(key,value) VALUES(?,?)", (key, encoded))
            prepared.assert_valid()
            db.commit()
        return value

    def _transition(self, account: str, target: str, expected: dict, replacement: dict, *, global_scope: bool = False) -> None:
        prepared = self._preparation
        prepared.assert_valid()
        key = _key(prepared.database_id, account, target, global_scope=global_scope)
        old = json.dumps(_decode(json.dumps(expected)), sort_keys=True, separators=(",", ":"))
        new = json.dumps(_decode(json.dumps(replacement)), sort_keys=True, separators=(",", ":"))
        with closing(sqlite3.connect(Path(prepared.database_path).as_uri() + "?mode=rw", uri=True, timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            marker = db.execute("SELECT value FROM fm_meta WHERE key=?", (_MARKER,)).fetchone()
            identity = db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
            version = db.execute("PRAGMA user_version").fetchone()
            row = db.execute("SELECT value FROM fm_meta WHERE key=?", (key,)).fetchone()
            if (marker != ("1",) or identity != (prepared.database_id,) or version != (prepared.schema_version,)
                    or row is None or json.dumps(_decode(row[0]), sort_keys=True, separators=(",", ":")) != old):
                raise ValueError("policy projection unavailable or changed")
            if db.execute("UPDATE fm_meta SET value=? WHERE key=? AND value=?", (new, key, row[0])).rowcount != 1:
                raise ValueError("policy projection changed")
            prepared.assert_valid()
            db.commit()

    def _transition_pair(self, account: str, target: str, global_before: dict, account_before: dict,
                         global_after: dict, account_after: dict) -> None:
        """CAS both existing scopes together in the existing SQLite authority."""
        prepared = self._preparation
        prepared.assert_valid()
        pairs = [
            (_key(prepared.database_id, 'global', 'global', global_scope=True), global_before, global_after),
            (_key(prepared.database_id, account, target), account_before, account_after),
        ]
        with closing(sqlite3.connect(Path(prepared.database_path).as_uri() + "?mode=rw", uri=True, timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            if (db.execute("SELECT value FROM fm_meta WHERE key=?", (_MARKER,)).fetchone() != ("1",)
                    or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone() != (prepared.database_id,)
                    or db.execute("PRAGMA user_version").fetchone() != (prepared.schema_version,)):
                raise ValueError("policy projection unavailable")
            for key, before, after in pairs:
                old = json.dumps(_decode(json.dumps(before)), sort_keys=True, separators=(",", ":"))
                new = json.dumps(_decode(json.dumps(after)), sort_keys=True, separators=(",", ":"))
                row = db.execute("SELECT value FROM fm_meta WHERE key=?", (key,)).fetchone()
                if row is None or json.dumps(_decode(row[0]), sort_keys=True, separators=(",", ":")) != old:
                    raise ValueError("policy projection unavailable or changed")
                if db.execute("UPDATE fm_meta SET value=? WHERE key=? AND value=?", (new, key, row[0])).rowcount != 1:
                    raise ValueError("policy projection changed")
            prepared.assert_valid()
            db.commit()

    def fence_pair(self, account: str, target: str, global_before: dict, account_before: dict) -> tuple[dict, dict]:
        global_before, account_before = _decode(json.dumps(global_before)), _decode(json.dumps(account_before))
        if global_before['state'] != 'ready' or account_before['state'] != 'ready':
            raise ValueError('policy already fenced')
        def fenced(row):
            return {**row, 'epoch':row['epoch']+1, 'state':'fenced', 'transition_id':secrets.token_hex(32)}
        global_after, account_after = fenced(global_before), fenced(account_before)
        self._transition_pair(account, target, global_before, account_before, global_after, account_after)
        return global_after, account_after

    def ready_pair(self, account: str, target: str, global_fenced: dict, account_fenced: dict,
                   global_fingerprint: str, account_fingerprint: str) -> None:
        global_fenced, account_fenced = _decode(json.dumps(global_fenced)), _decode(json.dumps(account_fenced))
        if (global_fenced['state'] != 'fenced' or account_fenced['state'] != 'fenced'
                or not isinstance(global_fingerprint,str) or not _HASH.fullmatch(global_fingerprint)
                or not isinstance(account_fingerprint,str) or not _HASH.fullmatch(account_fingerprint)):
            raise ValueError('policy reconciliation unavailable')
        self._transition_pair(account,target,global_fenced,account_fenced,
            {**global_fenced,'state':'ready','fingerprint':global_fingerprint},
            {**account_fenced,'state':'ready','fingerprint':account_fingerprint})

    def fence(self, account: str, target: str, expected: dict, *, global_scope: bool = False) -> dict:
        current = _decode(json.dumps(expected))
        if current["state"] != "ready":
            raise ValueError("policy already fenced")
        fenced = {**current, "epoch": current["epoch"] + 1, "state": "fenced", "transition_id": secrets.token_hex(32)}
        self._transition(account, target, current, fenced, global_scope=global_scope)
        return fenced

    def ready(self, account: str, target: str, fenced: dict, fingerprint: str, *, global_scope: bool = False) -> dict:
        current = _decode(json.dumps(fenced))
        if current["state"] != "fenced" or not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
            raise ValueError("policy reconciliation unavailable")
        ready = {**current, "state": "ready", "fingerprint": fingerprint}
        self._transition(account, target, current, ready, global_scope=global_scope)
        return ready


_PREFERENCE_KEYS = ('memory_policy', 'memory_mode', 'auto_memory', 'memory_enabled')
_coordinator = None
_registration_lock = threading.Lock()
_reset_handoff = None


def preference_inputs(prefs: dict) -> dict:
    if type(prefs) is not dict:
        raise ValueError('preferences unavailable')
    result = {key: [key in prefs, prefs.get(key)] for key in _PREFERENCE_KEYS}
    # Absence means the historical default On and keeps existing projection
    # fingerprints valid; an explicit choice becomes a fenced policy input.
    if 'memory_question_seeds' in prefs:
        result['memory_question_seeds'] = [True, prefs['memory_question_seeds']]
    if 'memory_assistant_interpretation' in prefs:
        result['memory_assistant_interpretation'] = [True, prefs['memory_assistant_interpretation']]
    for key in ('memory_policy_importance_enabled', 'memory_policy_connections_enabled', 'memory_policy_consolidation_enabled'):
        if key in prefs: result[key] = [True, prefs[key]]
    if prefs.get('memory_policy_consolidation_interval_days') is not None:
        result['memory_policy_consolidation_interval_days'] = [True, prefs['memory_policy_consolidation_interval_days']]
    agent_settings = prefs.get('agent_settings')
    if (type(prefs.get('_skill_draft_policy_version')) is int and prefs['_skill_draft_policy_version']==1
            and type(agent_settings) is dict and 'distill' in agent_settings):
        result['agent_settings_distill_version'] = [True, 1]
        result['agent_settings_distill'] = [True, agent_settings['distill']]
    if type(prefs.get('_document_vector_profile_version')) is int and prefs['_document_vector_profile_version']==1:
        result['document_vector_profile_version'] = [True, 1]
        result['document_vector_profile'] = ['document_vector_profile' in prefs, prefs.get('document_vector_profile')]
    if 'memory_policy_connections_predicate' in prefs: result['memory_policy_connections_predicate'] = [True, prefs['memory_policy_connections_predicate']]
    return result


def exact_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def global_settings_fingerprint(settings: dict) -> str:
    if type(settings) is not dict:
        raise ValueError('global policy unavailable')
    value = ["memory_graph" in settings, settings.get("memory_graph")]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class PolicyTransitionUnavailable(ValueError):
    """A policy transition cannot safely start with current authority."""


_RANK_CALL_PROOF_SEAL = object()


class _RankPreparationProof:
    """One-call evidence, never an independent authority or reusable cache."""
    def __init__(self, seal, coordinator, user, account, target, provider, profile, prepared):
        if seal is not _RANK_CALL_PROOF_SEAL:
            raise ValueError('rank preparation unavailable')
        self._seal=seal;self.coordinator=coordinator;self.user=user;self.account=account;self.target=target
        self.provider=provider;self.profile=profile;self.prepared=prepared;self.active=True
        self.transport=provider._owned_transport;self.generation=provider._owned_generation
        self.process=self.transport.process;self.task=provider._owner_task
        self.cookie=prepared.schema_cookie;self.policy_facts=None

    def check(self, coordinator, user, account, target, provider):
        import os
        p=self.prepared
        if (self._seal is not _RANK_CALL_PROOF_SEAL or not self.active or self.coordinator is not coordinator
                or (self.user,self.account,self.target)!=(user,account,target) or self.provider is not provider
                or policy_coordinator() is not coordinator or coordinator.projection._preparation is not p
                or provider._guarded_preparation is not p or self.profile.provider is not provider
                or provider._owned_transport is not self.transport or provider._owned_generation is not self.generation
                or self.transport.process is not self.process or provider._owner_task is not self.task
                or not provider._host_bounded_write_available() or coordinator.resolve(user)!=(account,target)
                or p.schema_cookie!=self.cookie):
            raise ValueError('rank preparation changed')
        self.profile.assert_current()
        before=os.stat(p.database_path,follow_symlinks=False)
        if os.path.realpath(p.database_path)!=p.database_path or (before.st_dev,before.st_ino)!=(p.device,p.inode) or before.st_nlink!=1:
            raise ValueError('rank database changed')
        expected={'openclank.assistant_interpretation.v1':('1',) if p.capabilities.get('assistant_interpretation') is True else None,
            'openclank.s05.account_chat.v1':('1',) if p.capabilities.get('account_chat') is True else None,
            'openclank.document_fts.v1':('1',) if p.capabilities.get('document_fts') is True else None,
            'openclank.s05.skill_draft.v1':p._skill_draft_marker,'openclank.s05.skill_purge.v1':p._skill_purge_marker,
            'openclank.vector_profiles.v1':p._vector_profiles_marker,'openclank.document_vector_build_intent.v1':p._document_vector_intent_marker,
            'openclank.policy_projection.v1':('1',)}
        with closing(sqlite3.connect(Path(p.database_path).as_uri()+'?mode=ro',uri=True,timeout=0.05)) as db:
            db.execute('BEGIN')
            if (db.execute('PRAGMA user_version').fetchone()!=(p.schema_version,) or db.execute('PRAGMA schema_version').fetchone()!=(self.cookie,)
                    or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()!=(p.database_id,)):
                raise ValueError('rank database changed')
            if any(db.execute('SELECT value FROM fm_meta WHERE key=?',(key,)).fetchone()!=value for key,value in expected.items()):
                raise ValueError('rank profile changed')
            db.rollback()
        after=os.stat(p.database_path,follow_symlinks=False)
        if os.path.realpath(p.database_path)!=p.database_path or (after.st_dev,after.st_ino)!=(before.st_dev,before.st_ino) or after.st_nlink!=1:
            raise ValueError('rank database changed')
        from routes.prefs_routes import PREFERENCE_LOCK,_load_for_user
        from src.settings import SETTINGS_LOCK,_read_persisted_settings
        with PREFERENCE_LOCK:
            prefs=_load_for_user(user);account_hash=coordinator.fingerprint(user,prefs);inputs=exact_json(preference_inputs(prefs))
        with SETTINGS_LOCK:global_hash=global_settings_fingerprint(_read_persisted_settings())
        refs=coordinator.projection.ready_refs_bounded(account,target)
        if refs['account']['fingerprint']!=account_hash or refs['global']['fingerprint']!=global_hash:
            raise ValueError('rank policy changed')
        facts=(refs,inputs,account_hash,global_hash)
        if self.policy_facts is None:self.policy_facts=facts
        elif self.policy_facts!=facts:raise ValueError('rank policy changed')
        if coordinator.resolve(user)!=(account,target):raise ValueError('rank account changed')
        return True

    def close(self):
        self.active=False


class PolicyTransitionCoordinator:
    """Host-owned preference transition; SQLite fence precedes preference lock."""

    def __init__(self, projection: PolicyProjection, *, resolve, fingerprint, assert_current):
        if type(projection) is not PolicyProjection or not all(callable(x) for x in (resolve, fingerprint, assert_current)):
            raise ValueError('policy transition authority unavailable')
        self.projection, self.resolve, self.fingerprint, self.assert_current = projection, resolve, fingerprint, assert_current
        self._lock = threading.Lock()
        self._local = threading.local()
        # Only fences created by this coordinator may be resumed after a failed ready CAS.
        self._initializing = {}

    def qualify_initial(self, account, target, read_snapshot, get_fingerprint, *, global_scope=False, check_target=None):
        """Complete only this process's exact original initialization fence."""
        if not self._lock.acquire(blocking=False):
            raise PolicyTransitionUnavailable('policy transition busy')
        try:
            self._qualify_initial_locked(account, target, read_snapshot, get_fingerprint, global_scope, check_target)
        finally:
            self._lock.release()

    def _qualify_initial_locked(self, account, target, read_snapshot, get_fingerprint, global_scope, check_target):
        self.assert_current()
        if check_target is not None and check_target() is not True:
            raise ValueError('policy target changed')
        before = read_snapshot()
        snapshot = hashlib.sha256(exact_json(before).encode('utf-8')).hexdigest()
        fingerprint = get_fingerprint(before)
        scope = (account, target, global_scope)
        row = self.projection.read_scope(account, target, global_scope=global_scope)
        pending = self._initializing.get(scope)
        if row is None:
            if pending is not None:
                raise ValueError('policy initialization changed')
            row = self.projection.provision_fenced(account, target, fingerprint, global_scope=global_scope)
            pending = (dict(row), snapshot, fingerprint)
            self._initializing[scope] = pending
        if row['state'] == 'ready':
            if pending is not None or row['fingerprint'] != fingerprint:
                if pending is None:
                    self.projection.fence(account, target, row, global_scope=global_scope)
                raise ValueError('policy reconciliation unavailable')
            return row
        if (pending is None or row != pending[0] or snapshot != pending[1]
                or fingerprint != pending[2]):
            raise ValueError('policy reconciliation unavailable')
        after = read_snapshot()
        if (hashlib.sha256(exact_json(after).encode('utf-8')).hexdigest() != snapshot or get_fingerprint(after) != fingerprint
                or (check_target is not None and check_target() is not True)):
            raise ValueError('policy changed during initialization')
        self.assert_current()
        ready = self.projection.ready(account, target, row, fingerprint, global_scope=global_scope)
        del self._initializing[scope]
        return ready

    def _account_row_for_transition(self, user, account, target):
        row = self.projection.read_scope(account, target)
        if row is not None:
            return row
        # Account creation wakes the registry asynchronously. An authorized
        # first write may precede that wake. Qualify the canonical snapshot
        # under the same lock and target checks as the background initializer.
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user

        def read_preferences():
            with PREFERENCE_LOCK:
                return copy.deepcopy(_load_for_user(user))

        return self._qualify_initial_locked(
            account, target, read_preferences, lambda prefs: self.fingerprint(user, prefs),
            False, lambda: self.resolve(user) == (account, target))

    def prepare_explicit_account(self, user, account, target):
        """Allow an authenticated first submission to initialize an absent scope."""
        self.assert_current()
        if self.resolve(user) != (account, target):
            raise ValueError('policy target changed')
        if self.projection.read_scope(account, target) is not None:
            return
        if not self._lock.acquire(blocking=False):
            raise PolicyTransitionUnavailable('policy transition busy')
        try:
            self.assert_current()
            if self.resolve(user) != (account, target):
                raise ValueError('policy target changed')
            self._account_row_for_transition(user, account, target)
        finally:
            self._lock.release()

    def begin(self, user, before: dict):
        if not self._lock.acquire(blocking=False):
            raise PolicyTransitionUnavailable('policy transition busy')
        try:
            self.assert_current()
            account, target = self.resolve(user)
            expected = self._account_row_for_transition(user, account, target)
            fingerprint = self.fingerprint(user, before)
            if expected is None or expected['state'] != 'ready' or expected['fingerprint'] != fingerprint:
                raise PolicyTransitionUnavailable('policy transition unavailable')
            fenced = self.projection.fence(account, target, expected)
            token = object()
            self._local.active = (token, user, exact_json(preference_inputs(before)), account, target, fenced)
            return token
        except sqlite3.OperationalError as exc:
            self._lock.release()
            if getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise PolicyTransitionUnavailable("policy transition temporarily unavailable") from exc
            raise
        except BaseException:
            self._lock.release()
            raise

    def begin_combined(self, user, before_preferences: dict, before_settings: dict):
        """Take the one owner lock and atomically fence both existing scopes."""
        if not self._lock.acquire(blocking=False):
            raise PolicyTransitionUnavailable('policy transition busy')
        try:
            self.assert_current()
            account, target = self.resolve(user)
            account_row = self._account_row_for_transition(user, account, target)
            global_row = self.projection.read_scope('global','global',global_scope=True)
            account_fp = self.fingerprint(user,before_preferences)
            global_fp = global_settings_fingerprint(before_settings)
            if (account_row is None or global_row is None or account_row['state'] != 'ready'
                    or global_row['state'] != 'ready' or account_row['fingerprint'] != account_fp
                    or global_row['fingerprint'] != global_fp or self.resolve(user) != (account,target)):
                raise PolicyTransitionUnavailable('combined policy transition unavailable')
            global_fence, account_fence = self.projection.fence_pair(account,target,global_row,account_row)
            token = object()
            self._local.active = (token,user,exact_json(preference_inputs(before_preferences)),account,target,account_fence)
            self._local.global_active = (token,None,global_fp,'global','global',global_fence)
            return token
        except sqlite3.OperationalError as exc:
            self._lock.release()
            if getattr(exc, 'sqlite_errorcode', 0) & 255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                raise PolicyTransitionUnavailable('policy transition temporarily unavailable') from exc
            raise
        except BaseException:
            self._lock.release()
            raise

    def active_combined_account_token(self, user, before: dict):
        active = getattr(self._local,'active',None)
        other = getattr(self._local,'global_active',None)
        if active is None or other is None or active[0] is not other[0]: return None
        self.validate(active[0],user,before)
        return active[0]

    def finish_combined(self, token, user, after_preferences: dict, after_settings: dict):
        active = getattr(self._local,'active',None)
        other = getattr(self._local,'global_active',None)
        if active is None or other is None or active[0] is not token or other[0] is not token or active[1] != user:
            raise ValueError('combined policy transition token unavailable')
        self.assert_current()
        if self.resolve(user) != (active[3],active[4]): raise ValueError('policy target changed')
        self.projection.ready_pair(active[3],active[4],other[5],active[5],
            global_settings_fingerprint(after_settings), self.fingerprint(user,after_preferences))

    def checked_refs(self, user, account, target):
        """Bind ready references to fresh canonical inputs and live target."""
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.settings import SETTINGS_LOCK, _read_persisted_settings
        self.assert_current()
        if self.resolve(user) != (account, target):
            raise ValueError('policy target changed')
        with PREFERENCE_LOCK:
            prefs = _load_for_user(user)
            account_hash = self.fingerprint(user, prefs)
        with SETTINGS_LOCK:
            global_hash = global_settings_fingerprint(_read_persisted_settings())
        refs = self.projection.ready_refs(account, target)
        if (refs['account']['fingerprint'] != account_hash or refs['global']['fingerprint'] != global_hash
                or self.resolve(user) != (account, target)):
            raise ValueError('policy projection changed')
        self.assert_current()
        return refs

    def _rank_cold_preparation(self, user, account, target, provider):
        from src.openclank.memory_job_admission import SingleHostProfile
        profile=getattr(self.assert_current,'__self__',None);prepared=self.projection._preparation
        if (type(profile) is not SingleHostProfile or getattr(self.assert_current,'__func__',None) is not SingleHostProfile.assert_current
                or profile.provider is not provider or provider._guarded_preparation is not prepared
                or policy_coordinator() is not self or self.resolve(user)!=(account,target)):
            raise ValueError('rank preparation unavailable')
        self.assert_current();cookie=prepared.schema_cookie;prepared.assert_valid()
        if prepared.schema_cookie!=cookie:raise ValueError('rank preparation changed')
        proof=_RankPreparationProof(_RANK_CALL_PROOF_SEAL,self,user,account,target,provider,profile,prepared)
        proof.check(self,user,account,target,provider)
        return proof

    def _rank_read_context(self, user, account, target, provider, *, _proof=None):
        """One-call rank admission; the returned closures expire on close."""
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.settings import SETTINGS_LOCK, _read_persisted_settings
        from src.openclank.memory_job_admission import SingleHostProfile
        from src.openclank.memory_policy import KEY, policy_view
        from src.memery_provider import NATIVE_RANK_LEARNING_OPERATION
        profile=getattr(self.assert_current,'__self__',None)
        prepared=self.projection._preparation
        if (type(profile) is not SingleHostProfile or getattr(self.assert_current,'__func__',None) is not SingleHostProfile.assert_current
                or profile.provider is not provider or provider._guarded_preparation is not prepared
                or policy_coordinator() is not self or not provider._native_memory_ops_ready()
                or NATIVE_RANK_LEARNING_OPERATION not in provider._native_memory_ops_support()):
            raise ValueError('rank admission unavailable')
        if _proof is None:
            self.assert_current();prepared.assert_valid()  # warm call: one complete qualification
        else:
            if type(_proof) is not _RankPreparationProof:raise ValueError('rank preparation unavailable')
            _proof.check(self,user,account,target,provider)
        transport=provider._owned_transport;generation=provider._owned_generation
        process=transport.process;task=provider._owner_task;witness=provider._native_memory_ops_witness
        active=[True]
        def facts():
            if _proof is not None:_proof.check(self,user,account,target,provider)
            if (not active[0] or policy_coordinator() is not self or profile.provider is not provider
                    or provider._guarded_preparation is not prepared or self.projection._preparation is not prepared
                    or provider._owned_transport is not transport or provider._owned_generation is not generation
                    or transport.process is not process or provider._owner_task is not task
                    or provider._native_memory_ops_witness is not witness or not provider._native_memory_ops_ready()
                    or NATIVE_RANK_LEARNING_OPERATION not in provider._native_memory_ops_support()
                    or self.resolve(user)!=(account,target)):
                raise ValueError('rank admission changed')
            self.assert_current()
            with PREFERENCE_LOCK:
                prefs=_load_for_user(user);account_hash=self.fingerprint(user,prefs)
                inputs=preference_inputs(prefs);accepted=policy_view(prefs.get(KEY))['accepted']
            with SETTINGS_LOCK:global_hash=global_settings_fingerprint(_read_persisted_settings())
            refs=self.projection.ready_refs_bounded(account,target)
            if refs['account']['fingerprint']!=account_hash or refs['global']['fingerprint']!=global_hash:
                raise ValueError('rank policy changed')
            if self.resolve(user)!=(account,target) or not provider._native_memory_ops_ready():
                raise ValueError('rank admission changed')
            return refs,inputs,accepted,account_hash
        refs,inputs,accepted,account_hash=facts()
        if accepted is None:return None
        rank=accepted['policy'].get('rank_learning',{})
        if rank.get('enabled') is not True or not any(rank.get(k) is True for k in ('salience','age_decay','retrieval_boost')):
            return None
        variants=[exact_json({'preferences':inputs,'can_manage_memory':value}) for value in (False,True)]
        matches=[raw for raw in variants if hashlib.sha256(raw.encode('utf-8')).hexdigest()==account_hash]
        if len(matches)!=1:raise ValueError('rank policy unavailable')
        def recheck():
            current,current_inputs,_,current_hash=facts()
            return current==refs and current_inputs==inputs and current_hash==account_hash
        def close():active[0]=False
        return refs,matches[0],recheck,close

    def validate(self, token, user, current: dict):
        active = getattr(self._local, 'active', None)
        if active is None or active[0] is not token or active[1] != user or active[2] != exact_json(preference_inputs(current)):
            raise ValueError('policy transition token unavailable')
        self.assert_current()
        if self.resolve(user) != (active[3], active[4]):
            raise ValueError('policy target changed')

    def validate_authority(self, user):
        active = getattr(self._local, 'active', None)
        if active is None or active[1] != user:
            raise ValueError('policy transition token unavailable')
        self.assert_current()
        if self.resolve(user) != (active[3], active[4]):
            raise ValueError('policy target changed')

    def finish(self, token, user, after: dict):
        active = getattr(self._local, 'active', None)
        if active is None or active[0] is not token or active[1] != user:
            raise ValueError('policy transition token unavailable')
        self.assert_current()
        if self.resolve(user) != (active[3], active[4]):
            raise ValueError('policy target changed')
        self.projection.ready(active[3], active[4], active[5], self.fingerprint(user, after))

    def close(self, token):
        active = getattr(self._local, 'active', None)
        if active is None or active[0] is not token:
            raise ValueError('policy transition token unavailable')
        del self._local.active
        other = getattr(self._local, 'global_active', None)
        if other is not None:
            if other[0] is not token: raise ValueError('policy transition token unavailable')
            del self._local.global_active
        self._lock.release()

    def begin_global(self, before: dict):
        if not self._lock.acquire(blocking=False):
            raise PolicyTransitionUnavailable('policy transition busy')
        try:
            self.assert_current()
            expected = self.projection.read_scope('global', 'global', global_scope=True)
            if expected is None or expected['state'] != 'ready' or expected['fingerprint'] != global_settings_fingerprint(before):
                raise PolicyTransitionUnavailable('global policy transition unavailable')
            fenced = self.projection.fence('global', 'global', expected, global_scope=True)
            token = object()
            self._local.active = (token, None, global_settings_fingerprint(before), 'global', 'global', fenced)
            return token
        except sqlite3.OperationalError as exc:
            self._lock.release()
            if getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise PolicyTransitionUnavailable("policy transition temporarily unavailable") from exc
            raise
        except BaseException:
            self._lock.release()
            raise

    def validate_global(self, token, before: dict):
        active = getattr(self._local, 'global_active', None) or getattr(self._local, 'active', None)
        if active is None or active[0] is not token or active[1] is not None or active[2] != global_settings_fingerprint(before):
            raise ValueError('global policy transition token unavailable')
        self.assert_current()

    def active_global_token(self, before: dict):
        active = getattr(self._local, 'global_active', None) or getattr(self._local, 'active', None)
        if active is None or active[1] is not None:
            return None
        self.validate_global(active[0], before)
        return active[0]

    def finish_global(self, token, after: dict):
        active = getattr(self._local, 'active', None)
        if active is None or active[0] is not token or active[1] is not None:
            raise ValueError('global policy transition token unavailable')
        self.assert_current()
        self.projection.ready('global', 'global', active[5], global_settings_fingerprint(after), global_scope=True)


def register_policy_coordinator(coordinator: PolicyTransitionCoordinator):
    global _coordinator
    with _registration_lock:
        if _reset_handoff is not None or type(coordinator) is not PolicyTransitionCoordinator or _coordinator is not None:
            raise ValueError('policy coordinator unavailable')
        coordinator.assert_current()
        _coordinator = coordinator


def policy_coordinator():
    return _coordinator


def unregister_policy_coordinator(coordinator: PolicyTransitionCoordinator):
    global _coordinator
    with _registration_lock:
        if _reset_handoff is not None or _coordinator is not coordinator or type(coordinator) is not PolicyTransitionCoordinator:
            raise ValueError('policy coordinator changed')
        if not coordinator._lock.acquire(blocking=False):
            raise ValueError('policy transition still active')
        try:
            _coordinator = None
        finally:
            coordinator._lock.release()


class _ResetPreparationWitness:
    __slots__ = ('coordinator', 'projection', 'old', 'identity', 'capabilities', 'new', 'released')

    def __init__(self, coordinator, projection, old, identity, capabilities):
        self.coordinator, self.projection, self.old = coordinator, projection, old
        self.identity, self.capabilities = identity, capabilities
        self.new, self.released = None, False


def begin_reset_preparation_handoff(old_preparation):
    """Fence registration and transitions before an original reset is issued."""
    from src.openclank.fm_reset_receipt import _identity
    global _reset_handoff
    if not _registration_lock.acquire(blocking=False):
        raise ValueError('policy handoff busy')
    coordinator = None
    locked = False
    try:
        if _reset_handoff is not None:
            raise ValueError('policy handoff busy')
        old_preparation.assert_valid()
        identity, capabilities = _identity(old_preparation), dict(old_preparation.capabilities)
        coordinator = _coordinator
        projection = old = None
        if coordinator is not None:
            if type(coordinator) is not PolicyTransitionCoordinator or not coordinator._lock.acquire(blocking=False):
                raise PolicyTransitionUnavailable('policy transition busy')
            locked = True
            projection = coordinator.projection
            if type(projection) is not PolicyProjection:
                raise ValueError('policy projection changed')
            old = projection._preparation
            old.assert_valid()
            if (_identity(old) != identity or old.schema_cookie != old_preparation.schema_cookie
                    or dict(old.capabilities) != capabilities):
                raise ValueError('policy preparation changed')
        _reset_handoff = _ResetPreparationWitness(coordinator, projection, old, identity, capabilities)
        return _reset_handoff
    except BaseException:
        if locked:
            coordinator._lock.release()
        raise
    finally:
        _registration_lock.release()


def _check_reset_witness(witness, *, rebound):
    if (type(witness) is not _ResetPreparationWitness or witness is not _reset_handoff
            or witness.released or (witness.new is not None) != rebound
            or _coordinator is not witness.coordinator
            or (witness.coordinator is not None and
                (not witness.coordinator._lock.locked() or witness.coordinator.projection is not witness.projection
                 or witness.projection._preparation is not (witness.new if rebound else witness.old)))):
        raise ValueError('policy handoff unavailable')


def rebind_reset_preparation_handoff(witness, new_preparation):
    from src.openclank.fm_reset_receipt import _identity
    # Validation may read SQLite; do not hold the registration lock during it.
    new_preparation.assert_valid()
    identity, capabilities = _identity(new_preparation), dict(new_preparation.capabilities)
    if not _registration_lock.acquire(blocking=False):
        raise ValueError('policy handoff busy')
    try:
        _check_reset_witness(witness, rebound=False)
        if identity != witness.identity or capabilities != witness.capabilities:
            raise ValueError('new policy preparation changed')
        if witness.projection is not None:
            witness.projection._preparation = new_preparation
        witness.new = new_preparation
        return witness
    finally:
        _registration_lock.release()


def finish_reset_preparation_handoff(witness):
    global _reset_handoff
    if not _registration_lock.acquire(blocking=False):
        raise ValueError('policy handoff busy')
    try:
        _check_reset_witness(witness, rebound=True)
        witness.released = True
        _reset_handoff = None
        if witness.coordinator is not None:
            witness.coordinator._lock.release()
        return True
    finally:
        _registration_lock.release()


def abort_reset_preparation_handoff(witness):
    """Release an unchanged witness only when the reset was positively unsent."""
    global _reset_handoff
    if not _registration_lock.acquire(blocking=False):
        raise ValueError('policy handoff busy')
    try:
        _check_reset_witness(witness, rebound=False)
        witness.released = True
        _reset_handoff = None
        if witness.coordinator is not None:
            witness.coordinator._lock.release()
        return True
    finally:
        _registration_lock.release()
