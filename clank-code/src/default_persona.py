"""The default persona — one editable identity, synced across every store.

Identity metaplan rulings R10/R12/R13/R15 (2026-07-16):

- R10: the default persona is REAL — name "Open Clank" plus an editable
  system prompt; the persona modal must not pretend the default is blank.
- R12: the preset store (data/presets.json) is the canonical vehicle; the
  record lives there under "default_personas", keyed by owner.
- R13: the default persona is ONE state across the three persona stores —
  chat presets, the personal-assistant CrewMember row, and the reminder
  default voice. Editing it anywhere updates everywhere. Non-default
  personas keep their upstream independence.
- R15: background/utility LLM work speaks with the default persona's
  voice; the mechanical task framing stays separate.

The module is configured once at app init with the live PresetManager
(configure()); consumers then use the owner-scoped accessors. Chat and
reminders READ the record at use time; the assistant CrewMember row keeps
a synced COPY (the task scheduler reads personality directly), so writes
push there and assistant edits push back.
"""

import logging
import threading
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Factory seed (reset target). The prompt is the pre-fork product default —
# upstream Open Clank's legacy baseline persona — kept verbatim per Slice 03
# ("capture the existing built-in persona as a golden fixture").
FACTORY_NAME = "Open Clank"
FACTORY_PROMPT = (
    "You are a helpful, balanced assistant. Match your response style to "
    "the user's needs."
)

_STORE_KEY = "default_personas"

_preset_manager = None


def configure(preset_manager) -> None:
    """Register the live PresetManager (call once at app init)."""
    global _preset_manager
    _preset_manager = preset_manager


def _manager(preset_manager=None):
    manager = preset_manager or _preset_manager
    if manager is None:
        raise RuntimeError(
            "default_persona is not configured — call configure(preset_manager) at init"
        )
    return manager


def factory_default() -> Dict[str, str]:
    return {"name": FACTORY_NAME, "system_prompt": FACTORY_PROMPT}


_FACTORY_SEAL = object()

@dataclass(frozen=True, repr=False)
class _FactorySelection:
    owner: str
    text_sha256: str
    _seal: object = field(repr=False)

@dataclass(frozen=True, repr=False)
class _FactorySession:
    selection: _FactorySelection
    chat_id: str
    _seal: object = field(repr=False)

@dataclass(frozen=True, repr=False)
class FactoryPersonaEnvelope:
    """Private historical constructor record; never a capture grant or wire flag."""
    owner: str
    account_id: str
    chat_id: str
    root_operation_id: str
    text_sha256: str
    constructor: str = "host.factory-persona.v1"
    _seal: object = field(default=None, repr=False, compare=False)

def _persona_hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def _factory_text() -> str:
    return f"Your name is {FACTORY_NAME}. {FACTORY_PROMPT}"

def _valid_identity(value) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and len(value.encode('utf-8')) <= 256

def bind_factory_session(selection, *, owner, chat_id, system_prompt, character_name):
    if (type(selection) is not _FactorySelection or selection._seal is not _FACTORY_SEAL or selection.owner != owner
            or not _valid_identity(chat_id) or character_name != FACTORY_NAME or type(system_prompt) is not str
            or _persona_hash(system_prompt) != selection.text_sha256):
        return None
    return _FactorySession(selection, chat_id, _FACTORY_SEAL)

def bind_factory_envelope(session, *, owner, account_id, chat_id, root_operation_id, system_prompt):
    if (type(session) is not _FactorySession or session._seal is not _FACTORY_SEAL or session.chat_id != chat_id
            or session.selection.owner != owner or not all(_valid_identity(x) for x in (owner,account_id,chat_id,root_operation_id))
            or type(system_prompt) is not str or _persona_hash(system_prompt) != session.selection.text_sha256):
        return None
    return FactoryPersonaEnvelope(owner,account_id,chat_id,root_operation_id,session.selection.text_sha256,_seal=_FACTORY_SEAL)

@dataclass(frozen=True, repr=False)
class _FactoryGeneralEnvelope:
    """Exact process-local assembly evidence; never a policy or Files grant."""
    factory: FactoryPersonaEnvelope
    snapshot_json: str
    defaults_sha256: str
    text_sha256: str
    _seal: object = field(default=None, repr=False, compare=False)


def _factory_general_values(factory, snapshot_json, *, owner, account_id, chat_id, root_operation_id):
    from src.general_hex_composition import defaults_instruction
    try:
        if type(snapshot_json) is not str or len(snapshot_json.encode('utf-8')) > 262144: return None
        snapshot=json.loads(snapshot_json)
        if (type(snapshot) is not dict or snapshot.get('owner_id') != owner or snapshot.get('boundary_id') != root_operation_id
                or type(snapshot.get('snapshot_hash')) is not str): return None
        body={k:v for k,v in snapshot.items() if k not in ('snapshot_hash','boundary_id')}
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
        if digest != snapshot['snapshot_hash']: return None
        base=_factory_text()
        if not verify_factory_envelope(factory,owner=owner,account_id=account_id,chat_id=chat_id,root_operation_id=root_operation_id,system_prompt=base): return None
        defaults=defaults_instruction(snapshot)
        if type(defaults) is not str or not defaults: return None
        composed=base.rstrip()+'\n\n'+defaults
        return {'version':2,'constructor':'host.factory-persona-general.v1',
                'textSHA256':_persona_hash(composed),'factorySHA256':factory.text_sha256,
                'defaultsSHA256':_persona_hash(defaults),'snapshotSHA256':digest}
    except (ValueError,TypeError,KeyError,UnicodeError,OverflowError,RecursionError):
        return None


def bind_factory_general_envelope(factory, snapshot, defaults, system_prompt, *, owner, account_id, chat_id, root_operation_id):
    """Seal the actual formatter output and original factory at this boundary."""
    from src.general_hex_composition import defaults_instruction
    try:
        encoded=json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        values=_factory_general_values(factory,encoded,owner=owner,account_id=account_id,chat_id=chat_id,root_operation_id=root_operation_id)
        if (values is None or type(defaults) is not str or defaults_instruction(snapshot) != defaults
                or _persona_hash(defaults) != values['defaultsSHA256'] or type(system_prompt) is not str
                or _persona_hash(system_prompt) != values['textSHA256']): return None
        return _FactoryGeneralEnvelope(factory,encoded,values['defaultsSHA256'],values['textSHA256'],_FACTORY_SEAL)
    except (ValueError,TypeError,KeyError,UnicodeError,OverflowError,RecursionError):
        return None


@dataclass(frozen=True, repr=False)
class _StyleTurnSnapshot:
    owner: str
    account_id: str
    chat_id: str
    root_operation_id: str
    mode: str
    revision: int
    _seal: object = field(default=None, repr=False, compare=False)


def capture_style_turn(*, owner, account_id, chat_id, root_operation_id, mode, revision):
    from src.openclank.response_style import style_snapshot
    if not all(_valid_identity(x) for x in (owner, account_id, chat_id, root_operation_id)):
        raise ValueError('style identity unavailable')
    value = style_snapshot(mode, revision)
    return _StyleTurnSnapshot(owner, account_id, chat_id, root_operation_id, value['mode'], value['revision'], _FACTORY_SEAL)


def read_style_turn(snapshot, *, owner, account_id, chat_id, root_operation_id):
    from src.openclank.response_style import style_snapshot
    if (type(snapshot) is not _StyleTurnSnapshot or snapshot._seal is not _FACTORY_SEAL
            or (snapshot.owner, snapshot.account_id, snapshot.chat_id, snapshot.root_operation_id)
                != (owner, account_id, chat_id, root_operation_id)):
        return None
    return style_snapshot(snapshot.mode, snapshot.revision)


def _styled_factory_values(factory, style, snapshot_json, *, owner, account_id, chat_id, root_operation_id):
    from src.openclank.response_style import style_instruction
    try:
        captured = read_style_turn(style, owner=owner, account_id=account_id, chat_id=chat_id, root_operation_id=root_operation_id)
        if not captured or captured['mode'] == 'normal': return None
        base = _factory_text()
        if not verify_factory_envelope(factory, owner=owner, account_id=account_id, chat_id=chat_id, root_operation_id=root_operation_id, system_prompt=base): return None
        general = None
        if snapshot_json is not None:
            values = _factory_general_values(factory, snapshot_json, owner=owner, account_id=account_id, chat_id=chat_id, root_operation_id=root_operation_id)
            if values is None: return None
            from src.general_hex_composition import defaults_instruction
            defaults = defaults_instruction(json.loads(snapshot_json))
            base = base.rstrip() + '\n\n' + defaults
            general = {'defaultsSHA256': values['defaultsSHA256'], 'snapshotSHA256': values['snapshotSHA256']}
        directive = style_instruction(captured['mode'])
        if type(directive) is not str or not directive: return None
        composed = base.rstrip() + '\n\n' + directive if base else directive
        commitment = {'version':1, 'accountID':account_id, 'chatID':chat_id, 'rootOperationID':root_operation_id,
                      'mode':captured['mode'], 'revision':captured['revision']}
        snapshot_hash = _persona_hash(json.dumps(commitment, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))
        return {'version':3, 'constructor':'host.factory-persona-style.v1', 'textSHA256':_persona_hash(composed),
                'factorySHA256':factory.text_sha256, 'styleSHA256':_persona_hash(directive),
                'styleMode':captured['mode'], 'styleRevision':captured['revision'], 'styleSnapshotSHA256':snapshot_hash, 'general':general}
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        return None


@dataclass(frozen=True, repr=False)
class _FactoryStyleEnvelope:
    factory: FactoryPersonaEnvelope
    style: _StyleTurnSnapshot
    snapshot_json: Optional[str]
    values_json: str
    _seal: object = field(default=None, repr=False, compare=False)


def bind_factory_style_envelope(factory, style, snapshot, system_prompt, *, owner, account_id, chat_id, root_operation_id):
    try:
        encoded = None if snapshot is None else json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        values = _styled_factory_values(factory, style, encoded, owner=owner, account_id=account_id, chat_id=chat_id, root_operation_id=root_operation_id)
        if values is None or type(system_prompt) is not str or _persona_hash(system_prompt) != values['textSHA256']: return None
        return _FactoryStyleEnvelope(factory, style, encoded, json.dumps(values, sort_keys=True, separators=(',', ':')), _FACTORY_SEAL)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return None


def read_factory_envelope(proof, *, owner, account_id, chat_id, root_operation_id):
    """Read only an original sealed factory record; this is not a grant."""
    if type(proof) is _FactoryStyleEnvelope:
        if proof._seal is not _FACTORY_SEAL: return None
        values = _styled_factory_values(proof.factory, proof.style, proof.snapshot_json, owner=owner, account_id=account_id, chat_id=chat_id, root_operation_id=root_operation_id)
        if values is None or json.dumps(values, sort_keys=True, separators=(',', ':')) != proof.values_json: return None
        return values
    if type(proof) is _FactoryGeneralEnvelope:
        if proof._seal is not _FACTORY_SEAL: return None
        values=_factory_general_values(proof.factory,proof.snapshot_json,owner=owner,account_id=account_id,chat_id=chat_id,root_operation_id=root_operation_id)
        if values is None or values['defaultsSHA256'] != proof.defaults_sha256 or values['textSHA256'] != proof.text_sha256: return None
        return values
    if (type(proof) is not FactoryPersonaEnvelope or proof._seal is not _FACTORY_SEAL or
            (proof.owner,proof.account_id,proof.chat_id,proof.root_operation_id)!=(owner,account_id,chat_id,root_operation_id) or
            proof.constructor != "host.factory-persona.v1" or type(proof.text_sha256) is not str or
            len(proof.text_sha256)!=64 or any(ch not in '0123456789abcdef' for ch in proof.text_sha256)):
        return None
    return {'version':1,'constructor':proof.constructor,'textSHA256':proof.text_sha256}

def verify_factory_envelope(proof, *, owner, account_id, chat_id, root_operation_id, system_prompt):
    return (type(proof) is FactoryPersonaEnvelope and proof._seal is _FACTORY_SEAL and
        (proof.owner,proof.account_id,proof.chat_id,proof.root_operation_id)==(owner,account_id,chat_id,root_operation_id) and
        proof.constructor == "host.factory-persona.v1" and type(system_prompt) is str and _persona_hash(system_prompt)==proof.text_sha256)

def _resolve_default_persona(owner: str = "", preset_manager=None, *, _with_origin=False):
    manager = _manager(preset_manager)
    store_present = _STORE_KEY in manager.presets
    stored = manager.presets.get(_STORE_KEY)
    records = stored or {}
    record = records.get(owner or "") if isinstance(records, dict) else None
    if not isinstance(record, dict):
        persona = {**factory_default(), "is_factory": True}
        # Malformed containers or an explicitly malformed owner record still
        # preserve the ordinary fallback, but cannot prove factory origin.
        clean_absence = not store_present or (isinstance(stored, dict) and (owner or "") not in stored)
        selection = _FactorySelection(owner or "", _persona_hash(_factory_text()), _FACTORY_SEAL) if _with_origin and clean_absence and _valid_identity(owner) else None
        return persona, selection
    name = str(record.get("name") or "").strip() or FACTORY_NAME
    prompt = str(record.get("system_prompt") or "").strip() or FACTORY_PROMPT
    return {"name": name, "system_prompt": prompt, "is_factory": False}, None

def get_default_persona(owner: str = "", preset_manager=None) -> Dict[str, Any]:
    """Return the owner's default persona; the factory seed if never edited."""
    return _resolve_default_persona(owner,preset_manager)[0]

def _get_default_persona_with_origin(owner: str = "", preset_manager=None):
    return _resolve_default_persona(owner,preset_manager,_with_origin=True)


_persona_write_lock = threading.RLock()


class PersonaConflict(ValueError):
    pass


def set_default_persona(
    owner: str,
    *,
    name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    preset_manager=None,
    sync_assistant: bool = True,
    expected: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Write the owner's default persona and sync the other stores (R13).

    sync_assistant=False is for the assistant-settings write-back path,
    where the CrewMember row is the SOURCE of the edit and pushing to it
    again would be redundant.
    """
    with _persona_write_lock:
        manager = _manager(preset_manager)
        current = get_default_persona(owner, preset_manager=manager)
        if expected is not None and any(current.get(k) != v for k, v in expected.items()):
            raise PersonaConflict("Persona changed")
        record = {
            "name": (name if name is not None else current["name"]).strip() or FACTORY_NAME,
            "system_prompt": (
                system_prompt if system_prompt is not None else current["system_prompt"]
            ).strip()
            or FACTORY_PROMPT,
        }
        presets = dict(manager.presets)
        store = presets.get(_STORE_KEY)
        if not isinstance(store, dict):
            store = {}
        presets[_STORE_KEY] = {**store, owner or "": record}
        if manager.save(presets) is False:
            raise OSError("Could not save the default persona")

        if record["name"] != current["name"]:
            # The principal bootstrap memo keys on the resolved label; drop the
            # owner's entry so the next read re-syncs the assistant entity.
            try:
                from services.memory.principal_context import invalidate_principal_cache

                invalidate_principal_cache(owner)
            except Exception:
                logger.debug("principal cache invalidation unavailable", exc_info=True)

        if sync_assistant:
            _push_to_assistant(owner, record)
        return {**record, "is_factory": False}


def reset_default_persona(owner: str, *, preset_manager=None) -> Dict[str, Any]:
    """Restore the factory seed (and sync it out like any edit)."""
    return set_default_persona(
        owner,
        name=FACTORY_NAME,
        system_prompt=FACTORY_PROMPT,
        preset_manager=preset_manager,
    )


def rename_default_persona_owner(
    old_owner: str,
    new_owner: str,
    *,
    preset_manager=None,
) -> bool:
    """Move one persisted persona record without merging owner identities."""

    old_owner = str(old_owner or "").strip().lower()
    new_owner = str(new_owner or "").strip().lower()
    if not old_owner or not new_owner:
        raise ValueError("old and new persona owners are required")
    if old_owner == new_owner:
        return False
    manager = _manager(preset_manager)
    records = manager.presets.get(_STORE_KEY)
    if not isinstance(records, dict):
        return False
    source_keys = [
        key for key in records if str(key or "").strip().lower() == old_owner
    ]
    destination_keys = [
        key for key in records if str(key or "").strip().lower() == new_owner
    ]
    if destination_keys:
        raise ValueError("target persona owner already has durable state")
    if not source_keys:
        return False
    if len(source_keys) != 1:
        raise ValueError("source persona owner has ambiguous durable state")

    updated_records = dict(records)
    record = updated_records.pop(source_keys[0])
    updated_records[new_owner] = record
    updated_presets = dict(manager.presets)
    updated_presets[_STORE_KEY] = updated_records
    if manager.save(updated_presets) is not True:
        raise RuntimeError("default persona owner rename could not be persisted")
    try:
        from services.memory.principal_context import invalidate_principal_cache

        invalidate_principal_cache(old_owner)
        invalidate_principal_cache(new_owner)
    except Exception:
        logger.debug("principal cache invalidation unavailable", exc_info=True)
    return True


def purge_default_persona_owner(
    owner: str,
    *,
    preset_manager=None,
) -> bool:
    """Delete exactly one account identity's persisted persona record."""

    owner = str(owner or "").strip().lower()
    if not owner:
        raise ValueError("persona owner is required")
    manager = _manager(preset_manager)
    records = manager.presets.get(_STORE_KEY)
    if not isinstance(records, dict):
        return False
    source_keys = [
        key for key in records if str(key or "").strip().lower() == owner
    ]
    if not source_keys:
        return False
    updated_records = dict(records)
    for key in source_keys:
        updated_records.pop(key, None)
    updated_presets = dict(manager.presets)
    updated_presets[_STORE_KEY] = updated_records
    if manager.save(updated_presets) is not True:
        raise RuntimeError("default persona owner purge could not be persisted")
    try:
        from services.memory.principal_context import invalidate_principal_cache

        invalidate_principal_cache(owner)
    except Exception:
        logger.debug("principal cache invalidation unavailable", exc_info=True)
    return True


def sync_from_assistant(
    owner: str,
    *,
    name: Optional[str] = None,
    personality: Optional[str] = None,
    preset_manager=None,
) -> None:
    """Assistant-settings edit → default persona record (reverse direction).

    Called by the assistant PATCH route after it updates the CrewMember row.
    """
    if name is None and personality is None:
        return
    try:
        set_default_persona(
            owner,
            name=name,
            system_prompt=personality,
            preset_manager=preset_manager,
            sync_assistant=False,
        )
    except RuntimeError:
        # Not configured (e.g. unit tests exercising the route in isolation).
        logger.debug("default_persona not configured; assistant sync skipped")


def _push_to_assistant(owner: str, record: Dict[str, str]) -> None:
    """Default persona edit → personal-assistant CrewMember row (R13)."""
    if not owner:
        return
    try:
        from core.database import CrewMember, SessionLocal

        db = SessionLocal()
        try:
            crew = (
                db.query(CrewMember)
                .filter(
                    CrewMember.owner == owner,
                    CrewMember.is_default_assistant == True,  # noqa: E712
                )
                .first()
            )
            if crew is None:
                # Nothing seeded yet — the seed path reads the record itself.
                return
            crew.name = record["name"]
            crew.personality = record["system_prompt"]
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("default persona → assistant sync failed for %s: %s", owner, exc)
