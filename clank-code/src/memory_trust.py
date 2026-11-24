"""Trust-by-provenance classifier (memory-trust metaplan T1/T3/T7).

One function decides, for both hosts, whether a memory carries force at
injection or stays behind the untrusted firewall:

- hand-authored (source_type == "human"): ALWAYS trusted — no toggle.
- explicitly pinned: ALWAYS trusted — a pin is a user endorsement (T3).
- anything else (ai / procedural / auto_extracted): trusted only when
  the MASTER toggle is on AND the record's kind switch is on (T7).
- kind "raw" (and anything unrecognized) is never auto-trusted.

The classifier keys on the record's own fields, never on digest-array
membership: fm's digest "pinned" array auto-includes every persona-kind
record, so being listed there is NOT an endorsement (audit finding F7).
"""

from typing import Any, Dict, Mapping, Tuple

BEHAVIOR_KINDS = ("instruction", "persona")
KNOWLEDGE_KINDS = ("fact", "episodic", "fabric", "wiki")
TRUSTABLE_KINDS = BEHAVIOR_KINDS + KNOWLEDGE_KINDS

# e's T7 defaults: flipping the master trusts auto-captured knowledge;
# auto-captured behavior needs a second, deliberate flip.
DEFAULT_KIND_TRUST: Dict[str, bool] = {
    "instruction": False,
    "persona": False,
    "fact": True,
    "episodic": True,
    "fabric": True,
    "wiki": True,
}

MASTER_PREF = "memory_trust_auto"
KINDS_PREF = "memory_trust_auto_kinds"


def trust_prefs(prefs: Any) -> Tuple[bool, Dict[str, bool]]:
    """Sanitize raw per-user prefs into (master, kind_switches).

    Unknown kind keys are dropped, non-boolean values are rejected by
    treating the affected switch as disabled, and missing kinds take the T7
    defaults.  In particular, the string ``"false"`` is not truthy.
    """
    if not isinstance(prefs, Mapping):
        return False, dict(DEFAULT_KIND_TRUST)
    master = prefs.get(MASTER_PREF, False)
    if not isinstance(master, bool):
        master = False
    kinds = dict(DEFAULT_KIND_TRUST)
    raw_kinds = prefs.get(KINDS_PREF)
    if isinstance(raw_kinds, Mapping):
        for kind, value in raw_kinds.items():
            if kind in kinds:
                if isinstance(value, bool):
                    kinds[kind] = value
                else:
                    # Fail closed for malformed persisted preference values;
                    # do not let Python's bool("false") become True.
                    kinds[kind] = False
    return master, kinds


def _field(entry: Any, name: str, default: Any = None) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def trusted(entry: Any, prefs: Any) -> bool:
    """True when this memory carries force at injection.

    `entry` is anything record-shaped: a MemoryRecord, an enriched digest
    pinned entry, or a plain dict off the fm wire. Requires explicit
    source_type/pinned fields — absent fields read as untrusted, so a
    degraded payload fails CLOSED.
    """
    if str(_field(entry, "source_type") or "") == "human":
        return True
    if _field(entry, "pinned", False) is True:
        return True
    kind = str(_field(entry, "kind") or "")
    if kind not in TRUSTABLE_KINDS:
        return False
    master, kinds = trust_prefs(prefs)
    return master and kinds.get(kind, False)
