"""Bounded, raw UTF-8 review groups; no Git or authorization effects."""
from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass

MAX_SIDE_BYTES = 256 * 1024
MAX_LINES = 1000
MAX_GROUPS = 128
MAX_DISPLAY_BYTES = 64 * 1024


class HunkUnavailable(ValueError):
    """The requested exact text review or selection is unavailable."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lines(data: bytes) -> list[bytes]:
    if type(data) is not bytes or len(data) > MAX_SIDE_BYTES or b'\0' in data:
        raise HunkUnavailable('unsupported bytes')
    try:
        data.decode('utf-8', 'strict')
    except UnicodeDecodeError as exc:
        raise HunkUnavailable('unsupported text') from exc
    # Split only on LF: splitlines() also splits Unicode/control separators and
    # would change the byte-level meaning of a line.
    pieces = data.split(b'\n')
    return [piece + b'\n' for piece in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])


@dataclass(frozen=True)
class _Group:
    group_id: str
    start: int
    end: int
    target_start: int
    target_end: int
    replacement: tuple[bytes, ...]
    before: str
    after: str


@dataclass(frozen=True)
class HunkProjection:
    before_sha256: str
    after_sha256: str
    _before: tuple[bytes, ...]
    _groups: tuple[_Group, ...]

    def review(self) -> dict:
        return {'version': 1, 'beforeSHA256': self.before_sha256, 'afterSHA256': self.after_sha256,
                'groups': [{'id': group.group_id, 'beforeRange': [group.start, group.end],
                            'afterRange': [group.target_start, group.target_end],
                            'before': group.before, 'after': group.after}
                           for group in self._groups]}

    def apply(self, ids: list[str]) -> bytes:
        if type(ids) is not list or not ids or len(ids) > MAX_GROUPS or any(type(value) is not str for value in ids):
            raise HunkUnavailable('invalid selection')
        selected = set(ids)
        if len(selected) != len(ids) or not selected.issubset({group.group_id for group in self._groups}):
            raise HunkUnavailable('stale or duplicate selection')
        result: list[bytes] = []
        offset = 0
        for group in self._groups:
            if group.group_id not in selected:
                continue
            result.extend(self._before[offset:group.start])
            result.extend(group.replacement)
            offset = group.end
        result.extend(self._before[offset:])
        if sum(map(len, result)) > MAX_SIDE_BYTES:
            raise HunkUnavailable('reconstructed output budget exceeded')
        return b''.join(result)


def project(before: bytes, after: bytes, *, before_mode: int, after_mode: int,
            before_exists: bool = True, after_exists: bool = True) -> HunkProjection:
    """Project exact index→target changes; caller re-derives before confirmation."""
    if before_exists is not True or after_exists is not True or type(before_mode) is not int or type(after_mode) is not int or before_mode not in (0o100644, 0o100755) or before_mode != after_mode:
        raise HunkUnavailable('unsupported entry or mode')
    left, right = _lines(before), _lines(after)
    if len(left) + len(right) > MAX_LINES:
        raise HunkUnavailable('line budget exceeded')
    before_hash, after_hash = _digest(before), _digest(after)
    groups = []
    display_bytes = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        original, replacement = b''.join(left[i1:i2]), b''.join(right[j1:j2])
        display_bytes += len(original) + len(replacement)
        if len(groups) >= MAX_GROUPS or display_bytes > MAX_DISPLAY_BYTES:
            raise HunkUnavailable('review budget exceeded')
        material = ['openclank.git-hunk.v1', before_hash, after_hash, tag, i1, i2, j1, j2,
                    _digest(original), _digest(replacement)]
        group_id = _digest(json.dumps(material, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        groups.append(_Group(group_id, i1, i2, j1, j2, tuple(right[j1:j2]), original.decode('utf-8'), replacement.decode('utf-8')))
    return HunkProjection(before_hash, after_hash, tuple(left), tuple(groups))
