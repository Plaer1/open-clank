"""Owner-scoped local emoji consent; no network or scheduler side effects.

The immutable packaged artwork remains owned by theme_emoji_assets. This
state records intent only; future acquisition must check its exact revision.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core.atomic_io import AtomicWriteConflict, atomic_write_bytes, fingerprint_bytes, _path_locks
from src.constants import DATA_DIR

_PATTERNS = frozenset({'clanker-emoji-drift', 'clanker-emoji-rain', 'clanker-matrix-rain'})
_STATES = frozenset({'unknown', 'accepted', 'declined', 'revoked'})
_KEYS = frozenset({'version', 'installation_id', 'consent', 'consent_revision', 'generation', 'selection_id', 'requested_pattern'})

class ThemeEmojiStateError(ValueError):
    pass

class ThemeEmojiLocalState:
    def __init__(self, root: str | Path | None = None, *, artwork_probe: Callable[[], bool] | None = None):
        self.root = Path(root) if root is not None else Path(DATA_DIR) / 'assets' / 'theme-emoji'
        self.artwork_probe = artwork_probe or (lambda: False)

    def _owner_path(self, owner: str) -> Path:
        if not isinstance(owner, str) or not owner or len(owner.encode('utf-8')) > 256:
            raise ThemeEmojiStateError('owner is unavailable')
        return self.root / 'owners' / (hashlib.sha256(owner.encode('utf-8')).hexdigest() + '.json')

    @staticmethod
    def _read(path: Path) -> tuple[dict[str, Any] | None, str | None]:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None, None
        if len(raw) > 16 * 1024:
            raise ThemeEmojiStateError('local theme state is unavailable')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ThemeEmojiStateError('local theme state is unavailable') from exc
        if not isinstance(value, dict):
            raise ThemeEmojiStateError('local theme state is unavailable')
        return value, fingerprint_bytes(raw)

    def _installation(self) -> str:
        path = self.root / 'installation.json'
        for _ in range(3):
            saved, _fingerprint = self._read(path)
            if saved is not None:
                if set(saved) != {'version', 'installation_id'} or saved.get('version') != 1 or not self._valid_id(saved.get('installation_id')):
                    raise ThemeEmojiStateError('installation state is unavailable')
                return saved['installation_id']
            identity = uuid.uuid4().hex
            try:
                atomic_write_bytes(str(path), json.dumps({'version':1, 'installation_id':identity}, separators=(',', ':')).encode(), require_missing=True, mode=0o600)
                return identity
            except AtomicWriteConflict:
                continue
        raise ThemeEmojiStateError('installation state changed')

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) and len(value) == 32 and all(c in '0123456789abcdef' for c in value)

    def _state(self, owner: str) -> tuple[dict[str, Any], str | None]:
        value, fingerprint = self._read(self._owner_path(owner))
        if value is None:
            return {'version':1, 'installation_id':None, 'consent':'unknown', 'consent_revision':0,
                    'generation':0, 'selection_id':None, 'requested_pattern':None}, None
        if (set(value) != _KEYS or value.get('version') != 1 or not self._valid_id(value.get('installation_id'))
                or value.get('consent') not in _STATES or type(value.get('consent_revision')) is not int or value['consent_revision'] < 0
                or type(value.get('generation')) is not int or value['generation'] < 0
                or (value.get('selection_id') is not None and not self._valid_id(value['selection_id']))
                or (value.get('requested_pattern') is not None and value['requested_pattern'] not in _PATTERNS)):
            raise ThemeEmojiStateError('local theme state is unavailable')
        installation, _ = self._read(self.root / 'installation.json')
        if (not isinstance(installation, dict) or set(installation) != {'version', 'installation_id'}
                or installation.get('version') != 1 or installation.get('installation_id') != value['installation_id']):
            raise ThemeEmojiStateError('installation state is unavailable')
        return value, fingerprint

    def _change(self, owner: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        # All consent transitions and physical overlay deletion serialize here.
        with _path_locks([str(self.root / 'consent-domain')]):
            path = self._owner_path(owner)
            for _ in range(3):
                state, previous = self._state(owner)
                state['installation_id'] = state['installation_id'] or self._installation()
                change(state)
                state['generation'] += 1
                raw = json.dumps(state, separators=(',', ':'), sort_keys=True).encode()
                try:
                    atomic_write_bytes(str(path), raw, expected_fingerprint=previous, require_missing=previous is None, mode=0o600)
                    return state
                except AtomicWriteConflict:
                    continue
            raise ThemeEmojiStateError('local theme state changed')

    def delete_overlay(self, owner: str) -> dict[str, str]:
        """Remove shared local overlay only when no owner still consents.

        Caller must own the nonblocking installation acquisition lease and the
        app account barrier. This never touches the immutable packaged pack.
        """
        from src.openclank.theme_emoji_inventory import EmojiInventory
        with _path_locks([str(self.root / 'consent-domain')]):
            own, _ = self._state(owner)
            if own['consent'] not in {'revoked', 'declined'}:
                raise ThemeEmojiStateError('Disable consent before deleting artwork')
            directory = self.root / 'owners'
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                raise ThemeEmojiStateError('Consent inventory is unavailable') from exc
            for path in entries:
                if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name) or path.is_symlink() or not path.is_file():
                    raise ThemeEmojiStateError('Consent inventory requires review')
                value, _ = self._read(path)
                if (not isinstance(value, dict) or set(value) != _KEYS or value.get('version') != 1
                        or value.get('installation_id') != own['installation_id'] or value.get('consent') not in _STATES
                        or type(value.get('consent_revision')) is not int or value['consent_revision'] < 0
                        or type(value.get('generation')) is not int or value['generation'] < 0
                        or (value.get('selection_id') is not None and not self._valid_id(value['selection_id']))
                        or (value.get('requested_pattern') is not None and value['requested_pattern'] not in _PATTERNS)):
                    raise ThemeEmojiStateError('Consent inventory requires review')
                if value['consent'] == 'accepted':
                    raise ThemeEmojiStateError('Another owner still uses this artwork')
            path = self.root / 'inventory.sqlite3'
            for suffix in ('-wal', '-shm', '-journal'):
                if os.path.lexists(str(path) + suffix):
                    raise ThemeEmojiStateError('Overlay cleanup requires review')
            try:
                st = path.lstat()
            except FileNotFoundError:
                return {'outcome':'absent', 'diskReclamation':'unconfirmed'}
            if not stat.S_ISREG(st.st_mode):
                raise ThemeEmojiStateError('Overlay cleanup requires review')
            try:
                EmojiInventory(path).verify_deletable()
                os.unlink(path)
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise ThemeEmojiStateError('Overlay deletion is pending or unavailable') from exc
            return {'outcome':'removed', 'diskReclamation':'unconfirmed'}

    def obligation(self, owner: str) -> dict[str, Any]:
        """Read the exact consent fence; this does not create installation state."""
        state, _ = self._state(owner)
        return {key: state[key] for key in ('installation_id', 'consent', 'consent_revision')}

    def status(self, owner: str) -> dict[str, Any]:
        state, _ = self._state(owner)
        ready = False
        if state['consent'] == 'accepted':
            try: ready = self.artwork_probe() is True
            except (OSError, ValueError, RuntimeError): pass
        return {'consent':state['consent'], 'consent_revision':state['consent_revision'], 'generation':state['generation'],
                'requested_pattern':state['requested_pattern'], 'ready':ready, 'acquisition_available':False}

    def begin_selection(self, owner: str, pattern: str) -> dict[str, Any]:
        if pattern not in _PATTERNS:
            raise ThemeEmojiStateError('theme pattern is unsupported')
        def change(state: dict[str, Any]) -> None:
            state['selection_id'] = uuid.uuid4().hex
            state['requested_pattern'] = pattern
        state = self._change(owner, change)
        return {**self.status(owner), 'selection_id':state['selection_id'], 'prompt_required':state['consent'] != 'accepted'}

    def decide(self, owner: str, selection_id: str, expected_revision: int, accepted: bool) -> dict[str, Any]:
        if not self._valid_id(selection_id) or type(expected_revision) is not int or type(accepted) is not bool:
            raise ThemeEmojiStateError('theme consent request is invalid')
        def change(state: dict[str, Any]) -> None:
            if state['selection_id'] != selection_id or state['consent_revision'] != expected_revision:
                raise ThemeEmojiStateError('theme consent request changed')
            state['consent'] = 'accepted' if accepted else 'declined'
            state['consent_revision'] += 1
            state['selection_id'] = None
        self._change(owner, change)
        return self.status(owner)

    def revoke(self, owner: str, expected_revision: int) -> dict[str, Any]:
        if type(expected_revision) is not int:
            raise ThemeEmojiStateError('theme consent request is invalid')
        def change(state: dict[str, Any]) -> None:
            if state['consent_revision'] != expected_revision:
                raise ThemeEmojiStateError('theme consent request changed')
            state['consent'] = 'revoked'; state['consent_revision'] += 1; state['selection_id'] = None
        self._change(owner, change)
        return self.status(owner)
