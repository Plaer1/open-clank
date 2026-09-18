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


def get_default_persona(owner: str = "", preset_manager=None) -> Dict[str, Any]:
    """Return the owner's default persona; the factory seed if never edited."""
    manager = _manager(preset_manager)
    records = manager.presets.get(_STORE_KEY) or {}
    record = records.get(owner or "") if isinstance(records, dict) else None
    if not isinstance(record, dict):
        return {**factory_default(), "is_factory": True}
    name = str(record.get("name") or "").strip() or FACTORY_NAME
    prompt = str(record.get("system_prompt") or "").strip() or FACTORY_PROMPT
    return {"name": name, "system_prompt": prompt, "is_factory": False}


def set_default_persona(
    owner: str,
    *,
    name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    preset_manager=None,
    sync_assistant: bool = True,
) -> Dict[str, Any]:
    """Write the owner's default persona and sync the other stores (R13).

    sync_assistant=False is for the assistant-settings write-back path,
    where the CrewMember row is the SOURCE of the edit and pushing to it
    again would be redundant.
    """
    manager = _manager(preset_manager)
    current = get_default_persona(owner, preset_manager=manager)
    record = {
        "name": (name if name is not None else current["name"]).strip() or FACTORY_NAME,
        "system_prompt": (
            system_prompt if system_prompt is not None else current["system_prompt"]
        ).strip()
        or FACTORY_PROMPT,
    }
    presets = manager.presets
    store = presets.get(_STORE_KEY)
    if not isinstance(store, dict):
        store = {}
    store[owner or ""] = record
    presets[_STORE_KEY] = store
    manager.save(presets)

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
