"""Private provenance for already-read image inputs; no content or durable store."""
from __future__ import annotations
import hashlib
import json
import re
import weakref
from .media_input_policy import REASONS

class _ReadWitness:
    __slots__ = ('__weakref__',)

class _TurnWitness:
    __slots__ = ('__weakref__',)

_reads = weakref.WeakKeyDictionary()
_turns = weakref.WeakKeyDictionary()

def _image_read(owner, upload_id, reason, emitted_bytes):
    if (type(owner) is not str or not owner or type(upload_id) is not str or not upload_id or len(upload_id)>256
            or reason not in (None,*REASONS) or (emitted_bytes is not None and type(emitted_bytes) is not bytes)):
        return None
    witness=_ReadWitness()
    _reads[witness]=(owner,upload_id,reason,hashlib.sha256(emitted_bytes).hexdigest() if emitted_bytes is not None else None)
    return witness

def _seal_turn(witnesses, *, owner, account, chat, root, current):
    if (type(witnesses) is not list or not 0<len(witnesses)<=32 or not callable(current)
            or any(type(v) is not str or not v or len(v)>256 for v in (owner,account,chat,root))):return None
    records=[];seen=set()
    for witness in witnesses:
        row=_reads.get(witness) if type(witness) is _ReadWitness else None
        if not row or row[0]!=owner or row[1] in seen:return None
        seen.add(row[1]);records.append(row)
    token=_TurnWitness();_turns[token]=(tuple(records),owner,account,chat,root,current)
    return token

def _read_turn(token, *, owner, account, chat, root):
    row=_turns.get(token) if type(token) is _TurnWitness else None
    if not row or row[1:5]!=(owner,account,chat,root):return None
    try:
        if row[5]() is not True:return None
    except Exception:return None
    return tuple({'uploadID':item[1],'sourceSHA256':item[3],
        'stage':'prepared' if item[3] is not None else 'omitted','reason':item[2]} for item in row[0])


# This existing admission seam deliberately has no positive profile yet. A
# calibrated issuer must bind explicit intent, exact route/model and current
# authority before returning an admitted decision; source bytes are otherwise
# never retained for, or passed to, the derivative path.
class _ImageViewAdmission:
    __slots__ = ()
    @property
    def state(self):
        return 'unavailable'


def _image_view_admission(*, owner, route_has_vision):
    return _ImageViewAdmission()


_read_candidates = weakref.WeakKeyDictionary()


def _eligibility_current(decision):
    # No trusted calibrated eligibility issuer exists. Its future witness must
    # recheck explicit intent, exact route/model, settings and live authority.
    return False


def _candidate_for_read(witness, original, mime, decision):
    if not _eligibility_current(decision): return
    row = _reads.get(witness) if type(witness) is _ReadWitness else None
    if not row or row[2] is not None: return
    from .media_model_view import derive_static_image
    candidate = derive_static_image(original, upload_id=row[1], media_type=mime)
    if candidate is None: return
    token = _image_view(witness, *candidate, eligibility=decision)
    if token is not None: _read_candidates[witness] = token


def _candidates_for_reads(witnesses):
    if type(witnesses) is not list: return []
    return [token for witness in witnesses if (token := _read_candidates.get(witness)) is not None]


# Request-local candidates are separate from the original read witness. They
# are not authority, are never serialized here, and disappear with the token.
class _ImageView:
    __slots__ = ('__weakref__',)

class _ImageViewTurn:
    __slots__ = ('__weakref__',)

_views = weakref.WeakKeyDictionary()
_view_turns = weakref.WeakKeyDictionary()
_VIEW_FIELDS = frozenset({'version','profile','uploadID','originalSHA256','originalMime','originalWidth','originalHeight',
    'derivedSHA256','derivedMime','derivedWidth','derivedHeight','derivedLength','transformVersion'})

def _image_view(read_witness, receipt, data, *, eligibility):
    row = _reads.get(read_witness) if type(read_witness) is _ReadWitness else None
    if (not _eligibility_current(eligibility) or not row or type(receipt) is not dict or set(receipt) != _VIEW_FIELDS or type(data) is not bytes
            or receipt.get('version') != 1 or receipt.get('profile') != 'image-downscale-v1'
            or receipt.get('transformVersion') != 'pillow-lanczos-v1' or receipt.get('uploadID') != row[1]
            or row[2] is not None or receipt.get('originalSHA256') != row[3]
            or receipt.get('derivedMime') != 'image/png' or receipt.get('derivedLength') != len(data)
            or not 0 < len(data) <= 256 * 1024 or receipt.get('derivedSHA256') != hashlib.sha256(data).hexdigest()):
        return None
    token = _ImageView(); _views[token] = (row, dict(receipt), data, eligibility); return token

def _seal_image_views(turn_witness, candidates, *, owner, account, chat, root, current):
    original = _read_turn(turn_witness, owner=owner, account=account, chat=chat, root=root)
    if (not original or not callable(current) or type(candidates) is not list or not 0 < len(candidates) <= 4):
        return None
    try:
        if current() is not True: return None
        originals = {item['uploadID']: item['sourceSHA256'] for item in original}
        selected = []; seen = set(); total = 0
        for token in candidates:
            row = _views.get(token) if type(token) is _ImageView else None
            if not row or not _eligibility_current(row[3]) or row[0][0] != owner or row[0][1] in seen or originals.get(row[0][1]) != row[0][3]: return None
            seen.add(row[0][1]); total += len(row[2]); selected.append(row)
        if total > 512 * 1024 or current() is not True or not all(_eligibility_current(row[3]) for row in selected): return None
    except Exception: return None
    result = _ImageViewTurn(); _view_turns[result] = (tuple(selected), turn_witness, owner, account, chat, root, current); return result

def _read_image_views(token, *, owner, account, chat, root):
    value = _view_turns.get(token) if type(token) is _ImageViewTurn else None
    if not value or value[2:6] != (owner, account, chat, root): return None
    try:
        if (value[6]() is not True or not all(_eligibility_current(row[3]) for row in value[0])
                or not _read_turn(value[1], owner=owner, account=account, chat=chat, root=root)): return None
        return tuple((dict(row[1]), row[2]) for row in value[0])
    except Exception: return None
