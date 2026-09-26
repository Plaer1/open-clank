"""Account-wide built-in achievements with deterministic receipt predicates.

This module is the S29 achievement framework.  Awards are once-per-account
receipt predicates over structured evidence.  No predicate is delegated to a
language model: every qualification path is a pure function of committed
operation receipts (``R``), authenticated narrow UI acknowledgments (``U``) or
recomputation over existing TreeHouse awards/events (``T``).

Workspace, project and applet are event *context* only.  The partition is the
stable account identity.  Installer, maintenance, template-seeding and test
fixture actors never qualify as real-user activity.

Catalog: 30 visible normal + 4 mystery (``???``) + 3 ultra rares, as approved in
the TreeHouse final audit.  IDs stay stable across wording/localization updates.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

CATALOG_REVISION = "2026-09-25.1"
PREDICATE_VERSION = "1"
EVENT_SCHEMA_VERSION = 1

NORMAL_COUNT = 34  # 30 visible N + 4 mystery S
ULTRA_COUNT = 3

# ---------------------------------------------------------------------------
# Event kinds, results and actor policy
# ---------------------------------------------------------------------------

KIND_RECEIPT = "R"
KIND_UI = "U"
KIND_AGGREGATE = "T"

RESULT_COMMITTED = "committed"
RESULT_ACKNOWLEDGED = "acknowledged"
NON_QUALIFYING_RESULTS = frozenset({"pending", "failed", "rolled_back", "cancelled", "stale"})

# Actors that may produce qualifying user activity.
QUALIFYING_ACTOR_KINDS = frozenset({"user", "agent_on_user_request"})
# Explicitly never real-user activity.
NON_QUALIFYING_ACTOR_KINDS = frozenset(
    {"installer", "maintenance", "template_seed", "test_fixture", "import", "background_maintenance"}
)

# ---------------------------------------------------------------------------
# Event families (proposed producer contracts)
# ---------------------------------------------------------------------------

class EventFamily:
    SHELL_READY = "shell.ready"
    CONVERSATION_TURN_COMPLETED = "conversation.turn.completed"
    SCHEDULED_TASK_RUN_COMPLETED = "scheduled.task.run.completed"
    GOAL_VERIFIED_COMPLETED = "goal.verified.completed"
    MEMORY_CANDIDATE_ACCEPTED = "memory.candidate.accepted"
    MEMORY_SEARCH_COMPLETED = "memory.search.completed"
    MEMORY_RECORD_OPENED = "memory.record.opened"
    TEMPLATE_DOCUMENT_CREATED = "template.document.created"
    TEMPLATE_SECTION_INSERTED = "template.section.inserted"
    EDITOR_SPLIT_PRESENTED = "editor.split.presented"
    DOCUMENT_RICH_REGION_SAVED = "document.rich-region.saved"
    TABLE_REVISION_SAVED = "table.revision.saved"
    WIKI_PAGE_COMMITTED = "wiki.page.committed"
    DOCUMENT_LINK_CREATED = "document.link.created"
    DOCUMENT_LINK_NAVIGATED = "document.link.navigated"
    GRAPH_MODE_PRESENTED = "graph.mode.presented"
    GRAPH_CAMERA_GESTURE = "graph.camera.gesture"
    GRAPH_FILTER_APPLIED = "graph.filter.applied"
    TASK_SOURCE_COMPLETED = "task.source.completed"
    TIMELINE_EVENT_COMMITTED = "timeline.event.committed"
    TIMELINE_EVENT_MOVED = "timeline.event.moved"
    CLIPBOARD_IMAGE_INSERTED = "clipboard.image.inserted"
    DOCUMENT_MOVE_COMMITTED = "document.move.committed"
    IMPS_PROJECT_SAVED = "imps.project.saved"
    IMPS_PROJECT_REOPENED = "imps.project.reopened"
    IMPS_PROJECT_EXPORTED = "imps.project.exported"
    LORE_RESTORE_COMMITTED = "lore.restore.committed"
    EXPORT_MANIFEST_VALIDATED = "export.manifest.validated"
    THEME_PREFERENCE_SAVED = "theme.preference.saved"
    THEME_SETTINGS_LOADED = "theme.settings.loaded"
    OFFICIAL_DOC_OPENED = "official.doc.opened"
    FORMATTING_DEMO_TOGGLED = "formatting.demo.toggled"
    CLASS_PREVIEWED = "class.previewed"
    CLASS_PUBLISHED = "class.published"
    SURFACE_VISITED = "surface.visited"
    CHAT_WORKSPACE_REBOUND = "chat.workspace.rebound"
    CHAT_ACTION_COMPLETED = "chat.action.completed"
    SOURCE_VIEW_CYCLE = "source.view.cycle"
    RAIN_DROPLET_PAINTED = "rain.droplet.painted"
    GUIDE_LESSON_COMPLETED = "guide.lesson.completed"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

Rarities = ("normal", "mystery", "ultra")


@dataclass(frozen=True)
class AchievementDefinition:
    id: str
    key: str
    title: str
    rarity: str
    evidence_kind: str
    event_families: tuple[str, ...]
    summary: str
    # Aggregate achievements depend on other earned achievement ids.
    depends_on: tuple[str, ...] = ()
    # Extra secrecy notes for presentation.
    hidden_until_earned: bool = False


def _n(num: int, key: str, title: str, kind: str, families: Sequence[str], summary: str) -> AchievementDefinition:
    return AchievementDefinition(
        id=f"N{num:02d}",
        key=key,
        title=title,
        rarity="normal",
        evidence_kind=kind,
        event_families=tuple(families),
        summary=summary,
    )


def _s(num: int, key: str, title: str, kind: str, families: Sequence[str], summary: str) -> AchievementDefinition:
    return AchievementDefinition(
        id=f"S{num:02d}",
        key=key,
        title=title,
        rarity="mystery",
        evidence_kind=kind,
        event_families=tuple(families),
        summary=summary,
        hidden_until_earned=False,  # slot visible as ??? from the beginning
    )


def _u(num: int, key: str, title: str, kind: str, families: Sequence[str], summary: str, depends_on: Sequence[str] = ()) -> AchievementDefinition:
    return AchievementDefinition(
        id=f"U{num:02d}",
        key=key,
        title=title,
        rarity="ultra",
        evidence_kind=kind,
        event_families=tuple(families),
        summary=summary,
        depends_on=tuple(depends_on),
        hidden_until_earned=True,  # unearned ultra entries are absent
    )


CATALOG: tuple[AchievementDefinition, ...] = (
    _n(1, "oc.first-light", "First Light", KIND_UI, (EventFamily.SHELL_READY,),
       "Mount the shell for this account."),
    _n(2, "oc.in-good-company", "In Good Company", KIND_RECEIPT, (EventFamily.CONVERSATION_TURN_COMPLETED,),
       "Finish an owner-initiated chat turn."),
    _n(3, "oc.on-the-clock", "On the Clock", KIND_RECEIPT, (EventFamily.SCHEDULED_TASK_RUN_COMPLETED,),
       "Run a scheduled task you created to success."),
    _n(4, "oc.proof-of-work", "Proof of Work", KIND_RECEIPT, (EventFamily.GOAL_VERIFIED_COMPLETED,),
       "Verify a durable goal complete with its evidence."),
    _n(5, "oc.selective-menmery", "Selective Menmery", KIND_RECEIPT, (EventFamily.MEMORY_CANDIDATE_ACCEPTED,),
       "Accept a pending memory candidate."),
    _n(6, "oc.total-recall", "Total Recall", "mixed", (EventFamily.MEMORY_SEARCH_COMPLETED, EventFamily.MEMORY_RECORD_OPENED),
       "Find a memory by scoped search and open that record."),
    _n(7, "oc.fresh-mould", "Fresh Mould", KIND_RECEIPT, (EventFamily.TEMPLATE_DOCUMENT_CREATED,),
       "Create a document from a template."),
    _n(8, "oc.spliced-in", "Spliced In", KIND_RECEIPT, (EventFamily.TEMPLATE_SECTION_INSERTED,),
       "Insert a template section into a document."),
    _n(9, "oc.two-windows", "Two Ways to See It", KIND_UI, (EventFamily.EDITOR_SPLIT_PRESENTED,),
       "Present two distinct document panes at once."),
    _n(10, "oc.comments-alive", "The Comments Are Alive", KIND_RECEIPT, (EventFamily.DOCUMENT_RICH_REGION_SAVED,),
       "Save a rich comment/docstring region."),
    _n(11, "oc.numbers-with-manners", "Numbers With Manners", KIND_RECEIPT, (EventFamily.TABLE_REVISION_SAVED,),
       "Save a typed table with a working formula."),
    _n(12, "oc.wiki-within", "A Wiki Within", KIND_RECEIPT, (EventFamily.WIKI_PAGE_COMMITTED,),
       "Commit a Wiki page with a real link."),
    _n(13, "oc.thread-puller", "Thread Puller", "mixed", (EventFamily.DOCUMENT_LINK_CREATED, EventFamily.DOCUMENT_LINK_NAVIGATED),
       "Create a document link and follow it."),
    _n(14, "oc.two-maps", "Two Maps, One World", KIND_UI, (EventFamily.GRAPH_MODE_PRESENTED,),
       "See both Graph modes with nodes."),
    _n(15, "oc.room-to-roam", "Room to Roam", KIND_UI, (EventFamily.GRAPH_CAMERA_GESTURE,),
       "Zoom and pan a populated Graph view."),
    _n(16, "oc.choose-your-threads", "Choose Your Threads", KIND_UI, (EventFamily.GRAPH_FILTER_APPLIED,),
       "Apply a scoped Graph filter that changes results."),
    _n(17, "oc.checked-at-source", "Checked at the Source", KIND_RECEIPT, (EventFamily.TASK_SOURCE_COMPLETED,),
       "Check a source-backed task box."),
    _n(18, "oc.time-shaper", "Time Shaper", KIND_RECEIPT, (EventFamily.TIMELINE_EVENT_COMMITTED, EventFamily.TIMELINE_EVENT_MOVED),
       "Move a Timeline event to a new date."),
    _n(19, "oc.picture-this", "Picture This", KIND_RECEIPT, (EventFamily.CLIPBOARD_IMAGE_INSERTED,),
       "Insert a clipboard image into a document."),
    _n(20, "oc.baggage-included", "Baggage Included", KIND_RECEIPT, (EventFamily.DOCUMENT_MOVE_COMMITTED,),
       "Move a document with its attachments intact."),
    _n(21, "oc.impish", "Impish", KIND_RECEIPT, (EventFamily.IMPS_PROJECT_SAVED, EventFamily.IMPS_PROJECT_REOPENED),
       "Save and reopen a layered Imps project."),
    _n(22, "oc.take-the-tools", "Take the Tools With You", KIND_RECEIPT, (EventFamily.IMPS_PROJECT_EXPORTED,),
       "Export an editable Imps project."),
    _n(23, "oc.return-of-the-byte", "Return of the Byte", KIND_RECEIPT, (EventFamily.LORE_RESTORE_COMMITTED,),
       "Restore a Lore version and match its hash."),
    _n(24, "oc.packed-and-checked", "Packed and Checked", KIND_RECEIPT, (EventFamily.EXPORT_MANIFEST_VALIDATED,),
       "Finish a scoped export with a valid manifest."),
    _n(25, "oc.house-colours", "House Colours", "mixed", (EventFamily.THEME_PREFERENCE_SAVED, EventFamily.THEME_SETTINGS_LOADED),
       "Save a theme and see it rehydrate."),
    _n(26, "oc.read-the-house", "Read the House", KIND_UI, (EventFamily.OFFICIAL_DOC_OPENED,),
       "Open an official documentation page."),
    _n(27, "oc.under-the-markdown", "Under the Markdown", KIND_UI, (EventFamily.FORMATTING_DEMO_TOGGLED,),
       "Toggle the official formatting demo views."),
    _n(28, "oc.trail-maker", "Trail Maker", KIND_RECEIPT, (EventFamily.CLASS_PREVIEWED, EventFamily.CLASS_PUBLISHED),
       "Preview and publish your own Class."),
    _n(29, "oc.field-guide-finished", "The House Tour, Your Way", KIND_AGGREGATE, (EventFamily.GUIDE_LESSON_COMPLETED,),
       "Complete all thirty guide lessons."),
    _n(30, "oc.shortcuts-grow", "Shortcuts Grow Here", KIND_UI, (EventFamily.SURFACE_VISITED,),
       "Open shared app links from Editor and TreeHouse."),
    _s(1, "oc.five-branches", "Five Branches", KIND_UI, (EventFamily.SURFACE_VISITED,),
       "Visit five core surfaces."),
    _s(2, "oc.same-clank-new-digs", "Same Clank, New Digs", "mixed", (EventFamily.CHAT_WORKSPACE_REBOUND, EventFamily.CHAT_ACTION_COMPLETED),
       "Rebind a chat to a new workspace and keep using it."),
    _s(3, "oc.nothing-up-sleeve", "Nothing Up My Sleeve", KIND_UI, (EventFamily.SOURCE_VIEW_CYCLE,),
       "Cycle rich/source/rich without changing the source."),
    _s(4, "oc.not-first-rodeo", "Not My First Rodeo", KIND_AGGREGATE, (),
       "Earn ten distinct normal achievements."),
    _u(1, "oc.up-the-downpour", "Up the Downpour", KIND_UI, (EventFamily.RAIN_DROPLET_PAINTED,),
       "Witness a rare upward rain droplet in view."),
    _u(2, "oc.many-tongues", "Many Tongues, One Notebook", KIND_RECEIPT, (EventFamily.DOCUMENT_RICH_REGION_SAVED,),
       "Save rich comment regions in five languages."),
    _u(3, "oc.full-house", "Full House", KIND_AGGREGATE, (),
       "Earn every normal achievement in this catalog.",
       depends_on=tuple(f"N{i:02d}" for i in range(1, 31)) + tuple(f"S{i:02d}" for i in range(1, 5))),
)

BY_ID: dict[str, AchievementDefinition] = {item.id: item for item in CATALOG}
BY_KEY: dict[str, AchievementDefinition] = {item.key: item for item in CATALOG}
NORMAL_IDS: tuple[str, ...] = tuple(item.id for item in CATALOG if item.rarity != "ultra")
ULTRA_IDS: tuple[str, ...] = tuple(item.id for item in CATALOG if item.rarity == "ultra")
VISIBLE_NORMAL_IDS: tuple[str, ...] = tuple(item.id for item in CATALOG if item.rarity == "normal")
MYSTERY_IDS: tuple[str, ...] = tuple(item.id for item in CATALOG if item.rarity == "mystery")

# Families that participate in N30 (shared app links from Editor and TreeHouse).
N30_EDITOR_SURFACES = frozenset({"editor", "documents"})
N30_TREEHOUSE_SURFACES = frozenset({"treehouse"})
S01_REQUIRED_SURFACES = frozenset({"chat", "editor", "files", "graph", "treehouse"})

# Grammar IDs that count for U02 comment/docstring regions.  Aliases dedupe;
# Markdown / Plain text / strict JSON are explicitly not comment grammars.
U02_COMMENT_GRAMMARS = frozenset({
    "python", "javascript", "typescript", "java", "kotlin", "swift",
    "go", "rust", "c", "cpp", "csharp", "ruby", "php", "scala",
    "shell", "bash", "sql", "r", "matlab", "perl", "lua", "haskell",
    "elixir", "erlang", "clojure", "objectivec", "dart", "groovy",
})
U02_EXCLUDED_GRAMMARS = frozenset({"markdown", "md", "plaintext", "plain-text", "text", "json", "json-strict"})
U02_REQUIRED_GRAMMARS = 5

U03_REQUIRES_NORMAL = frozenset(NORMAL_IDS)
S04_REQUIRED_N_AWARDS = 10
N29_REQUIRED_LESSONS = 30


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActivityEvent:
    """One structured evidence record.

    ``source_event_id`` is the stable producer identity used for dedupe.
    ``facts`` carries only the minimal predicate facts, never full document or
    chat bodies.
    """

    source_event_id: str
    event_family: str
    kind: str
    result: str
    actor_kind: str
    occurred_at: str
    facts: Mapping[str, Any]
    workspace_id: str | None = None
    schema_version: int = EVENT_SCHEMA_VERSION
    source_hash: str | None = None

    def normalized(self) -> dict[str, Any]:
        return {
            "source_event_id": self.source_event_id,
            "event_family": self.event_family,
            "kind": self.kind,
            "result": self.result,
            "actor_kind": self.actor_kind,
            "occurred_at": self.occurred_at,
            "workspace_id": self.workspace_id,
            "schema_version": int(self.schema_version),
            "facts": dict(self.facts),
            "source_hash": self.source_hash,
        }


class EventValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise EventValidationError(code, message)


def validate_event(event: ActivityEvent) -> None:
    _require(bool(event.source_event_id and str(event.source_event_id).strip()), "missing_source_event_id", "source_event_id is required")
    _require(bool(event.event_family and str(event.event_family).strip()), "missing_event_family", "event_family is required")
    _require(event.kind in {KIND_RECEIPT, KIND_UI}, "bad_kind", "kind must be R or U")
    _require(bool(event.actor_kind), "missing_actor_kind", "actor_kind is required")
    _require(event.actor_kind not in NON_QUALIFYING_ACTOR_KINDS, "non_qualifying_actor", "installer/maintenance/seed/test actors cannot award")
    _require(event.actor_kind in QUALIFYING_ACTOR_KINDS, "non_qualifying_actor", "actor_kind cannot qualify as user activity")
    _require(bool(event.occurred_at), "missing_occurred_at", "occurred_at is required")
    if event.kind == KIND_RECEIPT:
        _require(event.result == RESULT_COMMITTED, "non_qualifying_result", "R events must be committed")
    else:
        _require(event.result == RESULT_ACKNOWLEDGED, "non_qualifying_result", "U events must be acknowledged")
    _require(not isinstance(event.facts, (str, bytes)), "bad_facts", "facts must be a mapping")
    if int(event.schema_version) > EVENT_SCHEMA_VERSION:
        raise EventValidationError("schema_too_new", "event schema version is newer than this evaluator")


def event_qualifies_for_scoring(event: ActivityEvent) -> bool:
    """True when an event may contribute to an award.

    Failed/pending/rolled-back operations never qualify.  Non-user actors never
    qualify.  Malformed events raise rather than silently scoring.
    """
    validate_event(event)
    return True


# ---------------------------------------------------------------------------
# Incremental predicate state
# ---------------------------------------------------------------------------

@dataclass
class PredicateState:
    """Small counters/sets updated per event.  Never a full vault rescan."""

    counters: dict[str, int] = field(default_factory=dict)
    sets: dict[str, set[str]] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    last_events: dict[str, dict[str, Any]] = field(default_factory=dict)

    def bump(self, key: str, amount: int = 1) -> int:
        value = int(self.counters.get(key, 0)) + int(amount)
        self.counters[key] = value
        return value

    def add(self, key: str, member: str) -> set[str]:
        bucket = self.sets.setdefault(key, set())
        bucket.add(member)
        return bucket

    def members(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def set_flag(self, key: str, value: bool) -> None:
        self.flags[key] = bool(value)

    def flag(self, key: str) -> bool:
        return bool(self.flags.get(key, False))

    def remember(self, key: str, payload: Mapping[str, Any]) -> None:
        self.last_events[key] = dict(payload)

    def recall(self, key: str) -> dict[str, Any]:
        return dict(self.last_events.get(key, {}))

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "sets": {key: sorted(value) for key, value in self.sets.items()},
            "flags": dict(self.flags),
            "last_events": {key: dict(value) for key, value in self.last_events.items()},
        }

    def restore(self, payload: Mapping[str, Any]) -> None:
        self.counters = {str(key): int(value) for key, value in dict(payload.get("counters") or {}).items()}
        self.sets = {str(key): set(value or []) for key, value in dict(payload.get("sets") or {}).items()}
        self.flags = {str(key): bool(value) for key, value in dict(payload.get("flags") or {}).items()}
        self.last_events = {str(key): dict(value) for key, value in dict(payload.get("last_events") or {}).items()}


@dataclass(frozen=True)
class Qualification:
    achievement_id: str
    evidence_refs: tuple[str, ...]
    facts: Mapping[str, Any]


def _f(event: ActivityEvent) -> Mapping[str, Any]:
    return event.facts or {}


def _truth(facts: Mapping[str, Any], name: str) -> bool:
    return bool(facts.get(name))


def _ident(facts: Mapping[str, Any], name: str) -> str:
    value = facts.get(name)
    return "" if value is None else str(value)


# --- individual deterministic checkers ------------------------------------

def _check_n01(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    # First shell.ready U after the shell resolves this account and visibly mounts.
    if event.event_family != EventFamily.SHELL_READY:
        return None
    facts = _f(event)
    if not (_truth(facts, "accountResolved") and _truth(facts, "visiblyMounted")):
        return None
    if state.flag("N01"):
        return None
    state.set_flag("N01", True)
    return Qualification("N01", (event.source_event_id,), {"shellId": _ident(facts, "shellId")})


def _check_n02(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.CONVERSATION_TURN_COMPLETED:
        return None
    facts = _f(event)
    if not (_truth(facts, "userTurnPersisted") and _truth(facts, "assistantCompleted")):
        return None
    if _ident(facts, "origin") in {"seed", "import", "background_maintenance", "maintenance"}:
        return None
    if _ident(facts, "origin") and _ident(facts, "origin") not in {"user", "user_initiated", "agent_on_user_request"}:
        return None
    if not (_ident(facts, "origin") or _truth(facts, "ownerInitiated")):
        return None
    if state.flag("N02"):
        return None
    state.set_flag("N02", True)
    return Qualification("N02", (event.source_event_id,), {
        "conversationId": _ident(facts, "conversationId"),
        "sessionId": _ident(facts, "sessionId"),
        "taskId": _ident(facts, "taskId"),
    })


def _check_n03(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.SCHEDULED_TASK_RUN_COMPLETED:
        return None
    facts = _f(event)
    if not (_truth(facts, "createdByOwner") and _truth(facts, "scheduled")):
        return None
    if _ident(facts, "runStatus") != "success":
        return None
    if state.flag("N03"):
        return None
    state.set_flag("N03", True)
    return Qualification("N03", (event.source_event_id,), {
        "taskId": _ident(facts, "taskId"),
        "sessionId": _ident(facts, "sessionId"),
    })


def _check_n04(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.GOAL_VERIFIED_COMPLETED:
        return None
    facts = _f(event)
    if _ident(facts, "transition") != "verified_completed":
        return None
    if not _truth(facts, "evidenceSatisfied"):
        return None
    if not list(facts.get("evidenceRefs") or []):
        return None
    if _truth(facts, "modelClaimOnly"):
        return None
    if state.flag("N04"):
        return None
    state.set_flag("N04", True)
    return Qualification("N04", (event.source_event_id,), {"goalId": _ident(facts, "goalId")})


def _check_n05(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.MEMORY_CANDIDATE_ACCEPTED:
        return None
    facts = _f(event)
    if _ident(facts, "reviewTransaction") != "accepted":
        return None
    if not _truth(facts, "pendingCandidate"):
        return None
    if state.flag("N05"):
        return None
    state.set_flag("N05", True)
    return Qualification("N05", (event.source_event_id,), {"candidateId": _ident(facts, "candidateId")})


def _check_n06(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    # search-result R + record-open U share an authorized record ID.
    if event.event_family == EventFamily.MEMORY_SEARCH_COMPLETED:
        facts = _f(event)
        if not _truth(facts, "permitted"):
            return None
        for record_id in facts.get("recordIds") or []:
            state.add("N06:searched", str(record_id))
        return None
    if event.event_family == EventFamily.MEMORY_RECORD_OPENED:
        facts = _f(event)
        if not _truth(facts, "authorized"):
            return None
        record_id = _ident(facts, "recordId")
        if not record_id or record_id not in state.members("N06:searched"):
            return None
        if state.flag("N06"):
            return None
        state.set_flag("N06", True)
        return Qualification("N06", (event.source_event_id,), {"recordId": record_id})
    return None


def _check_n07(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.TEMPLATE_DOCUMENT_CREATED:
        return None
    facts = _f(event)
    if not (_ident(facts, "templateId") and _ident(facts, "documentId") and _ident(facts, "revisionId")):
        return None
    if not _truth(facts, "committed"):
        return None
    if state.flag("N07"):
        return None
    state.set_flag("N07", True)
    return Qualification("N07", (event.source_event_id,), {"templateId": _ident(facts, "templateId"), "documentId": _ident(facts, "documentId")})


def _check_n08(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.TEMPLATE_SECTION_INSERTED:
        return None
    facts = _f(event)
    if not (_ident(facts, "documentId") and _ident(facts, "revisionId")):
        return None
    if not _truth(facts, "spanNonempty"):
        return None
    if state.flag("N08"):
        return None
    state.set_flag("N08", True)
    return Qualification("N08", (event.source_event_id,), {"documentId": _ident(facts, "documentId"), "sectionKey": _ident(facts, "sectionKey")})


def _check_n09(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.EDITOR_SPLIT_PRESENTED:
        return None
    facts = _f(event)
    if not _truth(facts, "mounted"):
        return None
    panes = [str(item) for item in (facts.get("documentIds") or []) if item]
    if len(panes) < 2 or len(set(panes)) < 2:
        return None
    if _truth(facts, "buttonUnavailableClick"):
        return None
    if state.flag("N09"):
        return None
    state.set_flag("N09", True)
    return Qualification("N09", (event.source_event_id,), {"documentIds": sorted(set(panes))})


def _check_n10(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.DOCUMENT_RICH_REGION_SAVED:
        return None
    facts = _f(event)
    if not _rich_region_qualifies(facts):
        return None
    if state.flag("N10"):
        return None
    state.set_flag("N10", True)
    return Qualification("N10", (event.source_event_id,), {"documentId": _ident(facts, "documentId"), "grammarId": _ident(facts, "grammarId")})


def _rich_region_qualifies(facts: Mapping[str, Any]) -> bool:
    grammar = _ident(facts, "grammarId").lower()
    if not grammar or grammar in U02_EXCLUDED_GRAMMARS:
        return False
    if _ident(facts, "regionKind") not in {"comment", "docstring"}:
        return False
    if not _truth(facts, "markdownNonempty"):
        return False
    if not _ident(facts, "revisionId"):
        return False
    if not _truth(facts, "supportedGrammar"):
        # Unknown grammars never silently qualify.
        return False
    return True


def _check_n11(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.TABLE_REVISION_SAVED:
        return None
    facts = _f(event)
    if not _truth(facts, "hasTypedDateOrCurrency"):
        return None
    if not _truth(facts, "formulaEvaluated"):
        return None
    if int(facts.get("formulaErrors") or 0) > 0:
        return None
    if not _ident(facts, "revisionId"):
        return None
    if state.flag("N11"):
        return None
    state.set_flag("N11", True)
    return Qualification("N11", (event.source_event_id,), {"tableId": _ident(facts, "tableId")})


def _check_n12(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.WIKI_PAGE_COMMITTED:
        return None
    facts = _f(event)
    if _ident(facts, "pageKind") not in {"wiki", "Wiki", "wiki-page", "wiki_page"}:
        return None
    if not _truth(facts, "chunkCommitted"):
        return None
    if not (_ident(facts, "linkTargetId") and _truth(facts, "linkValid")):
        return None
    if state.flag("N12"):
        return None
    state.set_flag("N12", True)
    return Qualification("N12", (event.source_event_id,), {"pageId": _ident(facts, "pageId"), "linkTargetId": _ident(facts, "linkTargetId")})


def _check_n13(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family == EventFamily.DOCUMENT_LINK_CREATED:
        facts = _f(event)
        link_id = _ident(facts, "linkId")
        source = _ident(facts, "sourceDocumentId")
        target = _ident(facts, "targetDocumentId")
        if not (link_id and source and target) or source == target:
            return None
        state.remember(f"N13:link:{link_id}", {"source": source, "target": target})
        state.add("N13:links", link_id)
        return None
    if event.event_family == EventFamily.DOCUMENT_LINK_NAVIGATED:
        facts = _f(event)
        link_id = _ident(facts, "linkId")
        stored = state.recall(f"N13:link:{link_id}")
        if not stored:
            return None
        if _ident(facts, "sourceDocumentId") != stored.get("source"):
            return None
        if _ident(facts, "targetDocumentId") != stored.get("target"):
            return None
        if not _truth(facts, "resolvedToDifferentDocument"):
            return None
        if state.flag("N13"):
            return None
        state.set_flag("N13", True)
        return Qualification("N13", (event.source_event_id,), {"linkId": link_id})
    return None


def _check_n14(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.GRAPH_MODE_PRESENTED:
        return None
    facts = _f(event)
    mode = _ident(facts, "mode")
    if mode not in {"linked", "structure", "linked-document", "linked_document"}:
        return None
    normalized = "linked" if mode.startswith("linked") else "structure"
    if int(facts.get("nodeCount") or 0) < 1:
        return None
    state.add("N14:modes", normalized)
    if state.members("N14:modes") >= {"linked", "structure"}:
        if state.flag("N14"):
            return None
        state.set_flag("N14", True)
        return Qualification("N14", (event.source_event_id,), {"modes": sorted(state.members("N14:modes"))})
    return None


def _check_n15(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.GRAPH_CAMERA_GESTURE:
        return None
    facts = _f(event)
    if not (_truth(facts, "accepted") and _truth(facts, "populatedView")):
        return None
    view_id = _ident(facts, "viewId") or "default"
    key = f"N15:{view_id}"
    if _truth(facts, "start"):
        state.remember(key, {"scale": float(facts.get("scale") or 1.0), "x": float(facts.get("panX") or 0.0), "y": float(facts.get("panY") or 0.0)})
        return None
    start = state.recall(key)
    if not start:
        return None
    scale = float(facts.get("scale") or 1.0)
    pan_x = float(facts.get("panX") or 0.0)
    pan_y = float(facts.get("panY") or 0.0)
    base_scale = float(start.get("scale") or 1.0) or 1.0
    scale_ratio = scale / base_scale if base_scale else 1.0
    scale_change = max(scale_ratio, 1.0 / scale_ratio if scale_ratio else 1.0)
    pan_distance = ((pan_x - float(start.get("x") or 0.0)) ** 2 + (pan_y - float(start.get("y") or 0.0)) ** 2) ** 0.5
    if scale_change >= 1.20 and pan_distance >= 48.0:
        if state.flag("N15"):
            return None
        state.set_flag("N15", True)
        return Qualification("N15", (event.source_event_id,), {"scaleChange": scale_change, "panDistance": pan_distance})
    return None


def _check_n16(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.GRAPH_FILTER_APPLIED:
        return None
    facts = _f(event)
    if not _truth(facts, "facetFromScopedGeneration"):
        return None
    facet = _ident(facts, "facet")
    value = _ident(facts, "value")
    if not (facet and value):
        return None
    before = facts.get("resultCountBefore")
    after = facts.get("resultCountAfter")
    if before is None or after is None or int(before) == int(after):
        return None
    if state.flag("N16"):
        return None
    state.set_flag("N16", True)
    return Qualification("N16", (event.source_event_id,), {"facet": facet, "value": value})


def _check_n17(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.TASK_SOURCE_COMPLETED:
        return None
    facts = _f(event)
    if _ident(facts, "checkboxChange") != "unchecked_to_checked":
        return None
    if not (_ident(facts, "documentId") and _ident(facts, "revisionId") and _ident(facts, "taskId")):
        return None
    if state.flag("N17"):
        return None
    state.set_flag("N17", True)
    return Qualification("N17", (event.source_event_id,), {
        "taskId": _ident(facts, "taskId"),
        "sessionId": _ident(facts, "sessionId"),
    })


def _check_n18(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family == EventFamily.TIMELINE_EVENT_COMMITTED:
        facts = _f(event)
        event_id = _ident(facts, "timelineEventId")
        if not event_id:
            return None
        state.remember(f"N18:created:{event_id}", {"date": _ident(facts, "date"), "track": _ident(facts, "track"), "owner": _ident(facts, "ownerAccountId")})
        state.add("N18:events", event_id)
        return None
    if event.event_family == EventFamily.TIMELINE_EVENT_MOVED:
        facts = _f(event)
        event_id = _ident(facts, "timelineEventId")
        created = state.recall(f"N18:created:{event_id}")
        if not created:
            return None
        if _ident(facts, "ownerAccountId") and created.get("owner") and _ident(facts, "ownerAccountId") != created.get("owner"):
            return None
        new_date = _ident(facts, "date")
        new_track = _ident(facts, "track")
        if not new_date and not new_track:
            return None
        if new_date and created.get("date") and new_date == created.get("date") and new_track == created.get("track"):
            return None
        if not _truth(facts, "committed"):
            return None
        if state.flag("N18"):
            return None
        state.set_flag("N18", True)
        return Qualification("N18", (event.source_event_id,), {"timelineEventId": event_id})
    return None


def _check_n19(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.CLIPBOARD_IMAGE_INSERTED:
        return None
    facts = _f(event)
    if not (_truth(facts, "prepared") and _truth(facts, "inserted")):
        return None
    if not (_ident(facts, "assetId") and _ident(facts, "documentId") and _ident(facts, "mediaLocation")):
        return None
    if state.flag("N19"):
        return None
    state.set_flag("N19", True)
    return Qualification("N19", (event.source_event_id,), {"assetId": _ident(facts, "assetId")})


def _check_n20(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.DOCUMENT_MOVE_COMMITTED:
        return None
    facts = _f(event)
    attachments = [str(item) for item in (facts.get("attachmentIds") or []) if item]
    if not attachments:
        return None
    if not _truth(facts, "referencesVerifiedIntact"):
        return None
    if state.flag("N20"):
        return None
    state.set_flag("N20", True)
    return Qualification("N20", (event.source_event_id,), {"documentId": _ident(facts, "documentId"), "attachmentCount": len(attachments)})


def _check_n21(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family == EventFamily.IMPS_PROJECT_SAVED:
        facts = _f(event)
        layers = [str(item) for item in (facts.get("layerIds") or []) if item]
        if len(layers) < 2:
            return None
        if int(facts.get("editableLayerCount") or 0) < 2:
            return None
        project_id = _ident(facts, "projectId")
        revision = _ident(facts, "revisionId")
        if not (project_id and revision):
            return None
        state.remember(f"N21:saved:{project_id}", {"revisionId": revision, "layerIds": sorted(layers)})
        return None
    if event.event_family == EventFamily.IMPS_PROJECT_REOPENED:
        facts = _f(event)
        project_id = _ident(facts, "projectId")
        saved = state.recall(f"N21:saved:{project_id}")
        if not saved:
            return None
        if _ident(facts, "revisionId") != saved.get("revisionId"):
            return None
        reopened_layers = sorted(str(item) for item in (facts.get("layerIds") or []) if item)
        if reopened_layers != list(saved.get("layerIds") or []):
            return None
        if state.flag("N21"):
            return None
        state.set_flag("N21", True)
        return Qualification("N21", (event.source_event_id,), {"projectId": project_id})
    return None


def _check_n22(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.IMPS_PROJECT_EXPORTED:
        return None
    facts = _f(event)
    if not (_truth(facts, "editableExport") and _truth(facts, "manifestValid") and _truth(facts, "assetSetValid")):
        return None
    if not _ident(facts, "exportArtifactId"):
        return None
    if state.flag("N22"):
        return None
    state.set_flag("N22", True)
    return Qualification("N22", (event.source_event_id,), {"exportArtifactId": _ident(facts, "exportArtifactId")})


def _check_n23(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.LORE_RESTORE_COMMITTED:
        return None
    facts = _f(event)
    expected = _ident(facts, "contentHash")
    actual = _ident(facts, "restoredContentHash")
    if not expected or not actual or expected != actual:
        return None
    if not _ident(facts, "versionId"):
        return None
    if _truth(facts, "digestOnly") or _ident(facts, "receiptKind") == "ObservedAfterOnly":
        return None
    if state.flag("N23"):
        return None
    state.set_flag("N23", True)
    return Qualification("N23", (event.source_event_id,), {"resourceId": _ident(facts, "resourceId"), "versionId": _ident(facts, "versionId")})


def _check_n24(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.EXPORT_MANIFEST_VALIDATED:
        return None
    facts = _f(event)
    if not _truth(facts, "manifestValid"):
        return None
    if not _truth(facts, "finished"):
        return None
    if _truth(facts, "filenameOnly"):
        return None
    if state.flag("N24"):
        return None
    state.set_flag("N24", True)
    return Qualification("N24", (event.source_event_id,), {"exportId": _ident(facts, "exportId")})


def _check_n25(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family == EventFamily.THEME_PREFERENCE_SAVED:
        facts = _f(event)
        digest = _ident(facts, "themeDigest")
        if not digest or not _truth(facts, "confirmed"):
            return None
        state.remember("N25:saved", {"themeDigest": digest})
        return None
    if event.event_family == EventFamily.THEME_SETTINGS_LOADED:
        facts = _f(event)
        saved = state.recall("N25:saved")
        if not saved:
            return None
        if _ident(facts, "themeDigest") != saved.get("themeDigest"):
            return None
        if not _truth(facts, "rehydrated"):
            return None
        if state.flag("N25"):
            return None
        state.set_flag("N25", True)
        return Qualification("N25", (event.source_event_id,), {"themeDigest": _ident(facts, "themeDigest")})
    return None


def _check_n26(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.OFFICIAL_DOC_OPENED:
        return None
    facts = _f(event)
    if not _truth(facts, "provisionedResource"):
        return None
    if _truth(facts, "isDemo") or _truth(facts, "personalFolder"):
        return None
    if not _ident(facts, "resourceId"):
        return None
    if state.flag("N26"):
        return None
    state.set_flag("N26", True)
    return Qualification("N26", (event.source_event_id,), {"resourceId": _ident(facts, "resourceId")})


def _check_n27(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.FORMATTING_DEMO_TOGGLED:
        return None
    facts = _f(event)
    if not _truth(facts, "officialDemo"):
        return None
    views = {_ident(facts, "fromView"), _ident(facts, "toView")}
    if views != {"rendered", "source"}:
        return None
    if state.flag("N27"):
        return None
    state.set_flag("N27", True)
    return Qualification("N27", (event.source_event_id,), {"demoId": _ident(facts, "demoId")})


def _check_n28(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family == EventFamily.CLASS_PREVIEWED:
        facts = _f(event)
        class_id = _ident(facts, "classId")
        if not class_id or not _truth(facts, "previewedAsLearner"):
            return None
        if _truth(facts, "seededOfficial"):
            return None
        state.add("N28:previewed", class_id)
        return None
    if event.event_family == EventFamily.CLASS_PUBLISHED:
        facts = _f(event)
        class_id = _ident(facts, "classId")
        if class_id not in state.members("N28:previewed"):
            return None
        if not _truth(facts, "authoredByOwner"):
            return None
        if _truth(facts, "seededOfficial"):
            return None
        if int(facts.get("lessonCount") or 0) < 1:
            return None
        if state.flag("N28"):
            return None
        state.set_flag("N28", True)
        return Qualification("N28", (event.source_event_id,), {"classId": class_id})
    return None


def _check_n29(state: PredicateState, event: ActivityEvent, earned: Mapping[str, Any]) -> Qualification | None:
    # Recomputation over TreeHouse guide lesson completion records.
    if event.event_family == EventFamily.GUIDE_LESSON_COMPLETED:
        facts = _f(event)
        lesson_key = _ident(facts, "lessonKey")
        if not lesson_key:
            return None
        if not _truth(facts, "committed"):
            return None
        state.add("N29:lessons", lesson_key)
    if state.flag("N29"):
        return None
    if len(state.members("N29:lessons")) >= N29_REQUIRED_LESSONS:
        state.set_flag("N29", True)
        return Qualification("N29", tuple(sorted(state.members("N29:lessons"))), {"lessonCount": N29_REQUIRED_LESSONS})
    return None


def _check_n30(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.SURFACE_VISITED:
        return None
    facts = _f(event)
    if not _truth(facts, "appLinkResolved"):
        return None
    if not _truth(facts, "destinationExists"):
        return None
    surface = _ident(facts, "surface")
    source_kind = _ident(facts, "sourceKind")
    if source_kind == "editor" or surface in N30_EDITOR_SURFACES:
        state.set_flag("N30:editor", True)
    if source_kind == "treehouse" or surface in N30_TREEHOUSE_SURFACES:
        state.set_flag("N30:treehouse", True)
    if state.flag("N30:editor") and state.flag("N30:treehouse"):
        if state.flag("N30"):
            return None
        state.set_flag("N30", True)
        return Qualification("N30", (event.source_event_id,), {"surfaces": ["editor", "treehouse"]})
    return None


def _check_s01(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.SURFACE_VISITED:
        return None
    facts = _f(event)
    surface = _ident(facts, "surface")
    if surface not in S01_REQUIRED_SURFACES:
        return None
    if not _truth(facts, "visited"):
        return None
    if _truth(facts, "storedUrlOnly"):
        return None
    state.add("S01:surfaces", surface)
    if state.members("S01:surfaces") >= S01_REQUIRED_SURFACES:
        if state.flag("S01"):
            return None
        state.set_flag("S01", True)
        return Qualification("S01", (event.source_event_id,), {"surfaces": sorted(state.members("S01:surfaces"))})
    return None


def _check_s02(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family == EventFamily.CHAT_WORKSPACE_REBOUND:
        facts = _f(event)
        session_id = _ident(facts, "sessionId")
        new_workspace = _ident(facts, "newWorkspaceId")
        if not session_id or not new_workspace:
            return None
        if not _truth(facts, "sessionIdPreserved"):
            return None
        state.remember(f"S02:rebind:{session_id}", {"workspace": new_workspace})
        state.add("S02:sessions", session_id)
        return None
    if event.event_family == EventFamily.CHAT_ACTION_COMPLETED:
        facts = _f(event)
        session_id = _ident(facts, "sessionId")
        rebind = state.recall(f"S02:rebind:{session_id}")
        if not rebind:
            return None
        if _ident(facts, "workspaceId") != rebind.get("workspace"):
            return None
        if not _truth(facts, "authorized"):
            return None
        if state.flag("S02"):
            return None
        state.set_flag("S02", True)
        return Qualification("S02", (event.source_event_id,), {"sessionId": session_id})
    return None


def _check_s03(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.SOURCE_VIEW_CYCLE:
        return None
    facts = _f(event)
    if not _truth(facts, "supportedProgrammingDocument"):
        return None
    sequence = _ident(facts, "cycleSequence")
    if sequence not in {"rich", "source"}:
        return None
    document_id = _ident(facts, "documentId")
    revision = _ident(facts, "sourceRevisionId")
    source_hash = _ident(facts, "sourceHash")
    if not (document_id and revision and source_hash):
        return None
    key = f"S03:{document_id}"
    if sequence == "rich":
        # Returning to rich while a source phase is open completes the cycle
        # (handled by the S03-close checker).  Do not clobber that phase here.
        current = state.recall(key)
        if current.get("phase") == "source" and current.get("revision") == revision and current.get("hash") == source_hash:
            return None
        state.remember(key, {"phase": "rich-open", "revision": revision, "hash": source_hash})
        return None
    opened = state.recall(key)
    if not opened or opened.get("phase") != "rich-open":
        return None
    if opened.get("revision") != revision or opened.get("hash") != source_hash:
        return None
    state.remember(key, {"phase": "source", "revision": revision, "hash": source_hash})
    return None


def _check_s03_close(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    """Third leg: returning to rich with unchanged source completes S03."""
    if event.event_family != EventFamily.SOURCE_VIEW_CYCLE:
        return None
    facts = _f(event)
    if _ident(facts, "cycleSequence") != "rich":
        return None
    if not _truth(facts, "supportedProgrammingDocument"):
        return None
    document_id = _ident(facts, "documentId")
    revision = _ident(facts, "sourceRevisionId")
    source_hash = _ident(facts, "sourceHash")
    key = f"S03:{document_id}"
    opened = state.recall(key)
    if not opened or opened.get("phase") != "source":
        return None
    if opened.get("revision") != revision or opened.get("hash") != source_hash:
        return None
    if state.flag("S03"):
        return None
    state.set_flag("S03", True)
    return Qualification("S03", (event.source_event_id,), {"documentId": document_id})


def _check_u01(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.RAIN_DROPLET_PAINTED:
        return None
    facts = _f(event)
    if not _truth(facts, "directionUpward"):
        return None
    if not _truth(facts, "rareDroplet"):
        return None
    if not (_truth(facts, "paintedInVisibleViewport") and _truth(facts, "tabActive") and _truth(facts, "effectActive")):
        return None
    if _truth(facts, "hiddenFrameSpawn"):
        return None
    if state.flag("U01"):
        return None
    state.set_flag("U01", True)
    return Qualification("U01", (event.source_event_id,), {"dropletId": _ident(facts, "dropletId")})


def _check_u02(state: PredicateState, event: ActivityEvent) -> Qualification | None:
    if event.event_family != EventFamily.DOCUMENT_RICH_REGION_SAVED:
        return None
    facts = _f(event)
    if not _rich_region_qualifies(facts):
        return None
    grammar = _canonical_grammar(_ident(facts, "grammarId"))
    if not grammar:
        return None
    state.add("U02:grammars", grammar)
    if len(state.members("U02:grammars")) >= U02_REQUIRED_GRAMMARS:
        if state.flag("U02"):
            return None
        state.set_flag("U02", True)
        return Qualification("U02", (event.source_event_id,), {"grammars": sorted(state.members("U02:grammars"))})
    return None


def _canonical_grammar(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value or value in U02_EXCLUDED_GRAMMARS:
        return ""
    aliases = {
        "js": "javascript", "ts": "typescript", "py": "python",
        "golang": "go", "rs": "rust", "c++": "cpp", "c#": "csharp",
        "objc": "objectivec", "sh": "shell", "zsh": "shell",
    }
    value = aliases.get(value, value)
    return value if value in U02_COMMENT_GRAMMARS else ""


# Aggregate checkers -------------------------------------------------------

def _check_s04(earned_ids: set[str]) -> Qualification | None:
    if "S04" in earned_ids:
        return None
    # S04 depends only on N awards; it must not depend on itself or other aggregates.
    n_earned = {item for item in earned_ids if item.startswith("N") and item in BY_ID}
    if len(n_earned) >= S04_REQUIRED_N_AWARDS:
        return Qualification("S04", tuple(sorted(n_earned)), {"earnedNormal": len(n_earned)})
    return None


def _check_u03(earned_ids: set[str]) -> Qualification | None:
    if "U03" in earned_ids:
        return None
    if U03_REQUIRES_NORMAL <= earned_ids:
        return Qualification("U03", tuple(sorted(U03_REQUIRES_NORMAL)), {"catalogRevision": CATALOG_REVISION})
    return None


_SINGLE_CHECKERS: dict[str, Callable[[PredicateState, ActivityEvent], Qualification | None]] = {
    "N01": _check_n01,
    "N02": _check_n02,
    "N03": _check_n03,
    "N04": _check_n04,
    "N05": _check_n05,
    "N06": _check_n06,
    "N07": _check_n07,
    "N08": _check_n08,
    "N09": _check_n09,
    "N10": _check_n10,
    "N11": _check_n11,
    "N12": _check_n12,
    "N13": _check_n13,
    "N14": _check_n14,
    "N15": _check_n15,
    "N16": _check_n16,
    "N17": _check_n17,
    "N18": _check_n18,
    "N19": _check_n19,
    "N20": _check_n20,
    "N21": _check_n21,
    "N22": _check_n22,
    "N23": _check_n23,
    "N24": _check_n24,
    "N25": _check_n25,
    "N26": _check_n26,
    "N27": _check_n27,
    "N28": _check_n28,
    "N30": _check_n30,
    "S01": _check_s01,
    "S02": _check_s02,
    "S03": _check_s03,
    "U01": _check_u01,
    "U02": _check_u02,
}

# Index definitions by event family so ingestion only touches affected checks.
_FAMILY_INDEX: dict[str, tuple[str, ...]] = {}
for _aid, _checker in _SINGLE_CHECKERS.items():
    for _family in BY_ID[_aid].event_families:
        _FAMILY_INDEX[_family] = _FAMILY_INDEX.get(_family, ()) + (_aid,)
# S03 has a second leg (return to rich) handled alongside the family.
_FAMILY_INDEX[EventFamily.SOURCE_VIEW_CYCLE] = _FAMILY_INDEX.get(EventFamily.SOURCE_VIEW_CYCLE, ()) + ("S03-close",)
_FAMILY_INDEX[EventFamily.GUIDE_LESSON_COMPLETED] = _FAMILY_INDEX.get(EventFamily.GUIDE_LESSON_COMPLETED, ()) + ("N29",)

_FAMILY_INDEX_FROZEN: dict[str, tuple[str, ...]] = dict(_FAMILY_INDEX)


def families_index() -> Mapping[str, tuple[str, ...]]:
    return dict(_FAMILY_INDEX_FROZEN)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AwardRecord:
    account_id: str
    achievement_id: str
    earned_at: str
    evidence_refs: tuple[str, ...]
    awarded_via: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    predicate_version: str = PREDICATE_VERSION
    catalog_revision: str = CATALOG_REVISION

    def as_dict(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "achievementId": self.achievement_id,
            "achievementKey": BY_ID[self.achievement_id].key,
            "earnedAt": self.earned_at,
            "evidenceRefs": list(self.evidence_refs),
            "awardedVia": self.awarded_via,
            "predicateVersion": self.predicate_version,
            "catalogRevision": self.catalog_revision,
        }


class TreeHouseAchievementEngine:
    """Deterministic account-wide award engine.

    Ingestion is idempotent on ``(account_id, source_event_id)``.  Awards are
    unique on ``(account_id, achievement_id)``.  Duplicate, out-of-order and
    backfill/live overlapping deliveries never double-award.
    """

    def __init__(self, repository: Any):
        self.repository = repository

    # -- live / backfill ingestion -----------------------------------------

    def ingest(
        self,
        account_id: str,
        events: Sequence[ActivityEvent | Mapping[str, Any]],
        *,
        via: str = "live",
    ) -> dict[str, Any]:
        account = str(account_id)
        accepted: list[str] = []
        rejected: list[dict[str, str]] = []
        new_awards: list[AwardRecord] = []
        for raw in events:
            event = self._coerce(raw)
            try:
                event_qualifies_for_scoring(event)
            except EventValidationError as exc:
                rejected.append({"sourceEventId": getattr(event, "source_event_id", ""), "code": exc.code, "message": exc.message})
                continue
            stored = self.repository.put_activity_receipt(account, event, via=via)
            if not stored.get("inserted"):
                # Duplicate delivery: keep existing receipt, do not re-score.
                accepted.append(event.source_event_id)
                continue
            accepted.append(event.source_event_id)
            awards = self._score_event(account, event, via=via)
            new_awards.extend(awards)
        aggregate_awards = self.evaluate_aggregates(account, via=via)
        new_awards.extend(aggregate_awards)
        for award in new_awards:
            self.repository.enqueue_award_notification(account, award.achievement_id, batch_id=via if via == "backfill" else None)
        return {
            "accepted": accepted,
            "rejected": rejected,
            "newAwards": [item.as_dict() for item in new_awards],
        }

    def _coerce(self, raw: ActivityEvent | Mapping[str, Any]) -> ActivityEvent:
        if isinstance(raw, ActivityEvent):
            return raw
        payload = dict(raw)
        return ActivityEvent(
            source_event_id=str(payload.get("source_event_id") or payload.get("sourceEventId") or ""),
            event_family=str(payload.get("event_family") or payload.get("eventFamily") or ""),
            kind=str(payload.get("kind") or KIND_RECEIPT),
            result=str(payload.get("result") or ""),
            actor_kind=str(payload.get("actor_kind") or payload.get("actorKind") or ""),
            occurred_at=str(payload.get("occurred_at") or payload.get("occurredAt") or ""),
            facts=dict(payload.get("facts") or {}),
            workspace_id=payload.get("workspace_id") or payload.get("workspaceId"),
            schema_version=int(payload.get("schema_version") or payload.get("schemaVersion") or EVENT_SCHEMA_VERSION),
            source_hash=payload.get("source_hash") or payload.get("sourceHash"),
        )

    def _score_event(self, account_id: str, event: ActivityEvent, *, via: str) -> list[AwardRecord]:
        state = self._load_state(account_id)
        earned = set(self.repository.earned_achievement_ids(account_id))
        produced: list[AwardRecord] = []
        checkers = _FAMILY_INDEX_FROZEN.get(event.event_family, ())
        for name in checkers:
            qualification: Qualification | None = None
            if name == "S03-close":
                qualification = _check_s03_close(state, event)
            elif name == "N29":
                qualification = _check_n29(state, event, earned)
            else:
                checker = _SINGLE_CHECKERS.get(name)
                if checker is not None:
                    qualification = checker(state, event)
            if qualification is None:
                continue
            if qualification.achievement_id in earned:
                continue
            record = self._commit_award(account_id, qualification, via=via, occurred_at=event.occurred_at)
            if record is not None:
                produced.append(record)
                earned.add(record.achievement_id)
        self._save_state(account_id, state)
        return produced

    def evaluate_aggregates(self, account_id: str, *, via: str = "live") -> list[AwardRecord]:
        """Evaluate S04/U03/N29-class aggregates to a fixed point.

        The dependency graph is acyclic: S04 depends only on N awards and U03
        depends on N+S awards (never on itself or other ultra rares).
        """
        produced: list[AwardRecord] = []
        earned = set(self.repository.earned_achievement_ids(account_id))
        state = self._load_state(account_id)
        # N29 is a recomputation over guide lessons already folded into state.
        if "N29" not in earned:
            qualification = _check_n29(state, ActivityEvent(
                source_event_id=f"aggregate:N29:{account_id}",
                event_family=EventFamily.GUIDE_LESSON_COMPLETED,
                kind=KIND_AGGREGATE,
                result=RESULT_COMMITTED,
                actor_kind="user",
                occurred_at=_now_iso(),
                facts={},
            ), earned)
            if qualification is not None:
                record = self._commit_award(account_id, qualification, via=via, occurred_at=_now_iso())
                if record is not None:
                    produced.append(record)
                    earned.add(record.achievement_id)
        if "S04" not in earned:
            qualification = _check_s04(earned)
            if qualification is not None:
                record = self._commit_award(account_id, qualification, via=via, occurred_at=_now_iso())
                if record is not None:
                    produced.append(record)
                    earned.add(record.achievement_id)
        if "U03" not in earned:
            qualification = _check_u03(earned)
            if qualification is not None:
                record = self._commit_award(account_id, qualification, via=via, occurred_at=_now_iso())
                if record is not None:
                    produced.append(record)
                    earned.add(record.achievement_id)
        self._save_state(account_id, state)
        return produced

    def _commit_award(self, account_id: str, qualification: Qualification, *, via: str, occurred_at: str) -> AwardRecord | None:
        record = AwardRecord(
            account_id=account_id,
            achievement_id=qualification.achievement_id,
            earned_at=occurred_at or _now_iso(),
            evidence_refs=tuple(qualification.evidence_refs),
            awarded_via=via,
            facts=dict(qualification.facts),
        )
        stored = self.repository.put_achievement_award(record)
        if not stored.get("inserted"):
            return None
        return record

    def _load_state(self, account_id: str) -> PredicateState:
        state = PredicateState()
        payload = self.repository.get_predicate_state(account_id)
        if payload:
            state.restore(payload)
        return state

    def _save_state(self, account_id: str, state: PredicateState) -> None:
        self.repository.put_predicate_state(account_id, state.snapshot())

    # -- presentation ------------------------------------------------------

    def presentation(self, account_id: str, *, admin: bool = False) -> dict[str, Any]:
        return build_presentation(self.repository.earned_achievement_ids(account_id), admin=admin)

    # -- backfill ----------------------------------------------------------

    def backfill(
        self,
        account_id: str,
        source_family: str,
        records: Sequence[Mapping[str, Any]],
        *,
        cursor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ingest historical structured evidence with resumable cursors.

        Only compatible structured proof is accepted.  Ambiguous owner/history,
        digest-only restore claims and prose descriptions are rejected.  No LLM
        reads Lore/chat to decide achievements.
        """
        events: list[ActivityEvent] = []
        rejected: list[dict[str, str]] = []
        for record in records:
            try:
                events.append(self._backfill_event(source_family, record))
            except EventValidationError as exc:
                rejected.append({"sourceEventId": str(record.get("source_event_id") or record.get("sourceEventId") or ""), "code": exc.code, "message": exc.message})
        result = self.ingest(account_id, events, via="backfill")
        result["rejected"] = rejected + result.get("rejected", [])
        self.repository.put_backfill_cursor(account_id, source_family, {
            "cursor": dict(cursor or {}),
            "predicateVersion": PREDICATE_VERSION,
            "catalogRevision": CATALOG_REVISION,
            "processed": len(events),
        })
        return result

    def _backfill_event(self, source_family: str, record: Mapping[str, Any]) -> ActivityEvent:
        evidence_kind = str(record.get("evidence_kind") or record.get("evidenceKind") or "")
        if evidence_kind not in {KIND_RECEIPT, KIND_UI}:
            raise EventValidationError("bad_evidence_kind", "backfill evidence must be structured R or U proof")
        if record.get("prose") or record.get("description_only"):
            raise EventValidationError("prose_evidence", "prose descriptions cannot mint action evidence")
        if record.get("digest_only") or record.get("receiptKind") == "ObservedAfterOnly":
            raise EventValidationError("digest_only", "digest-only restore claims are insufficient")
        if not record.get("owner_account_id") and not record.get("ownerAccountId") and not record.get("account_id"):
            raise EventValidationError("ambiguous_owner", "historical evidence must carry an explicit owner")
        payload = dict(record)
        payload.setdefault("kind", evidence_kind)
        payload.setdefault("result", RESULT_COMMITTED if evidence_kind == KIND_RECEIPT else RESULT_ACKNOWLEDGED)
        payload.setdefault("actor_kind", record.get("actor_kind") or record.get("actorKind") or "user")
        payload.setdefault("actorKind", payload["actor_kind"])
        event = self._coerce(payload)
        if event.source_hash is None and record.get("source_hash"):
            event = ActivityEvent(**{**event.normalized(), "source_hash": record.get("source_hash")})
        # Bind the source family into the stable identity so backfill records
        # keep a distinct receipt identity from live delivery.  Overlap between
        # live and backfill is deduped at the award layer (once per account per
        # achievement), not the receipt layer.
        if source_family and not event.source_event_id.startswith(f"{source_family}:"):
            event = ActivityEvent(
                source_event_id=f"{source_family}:{event.source_event_id}",
                event_family=event.event_family,
                kind=event.kind,
                result=event.result,
                actor_kind=event.actor_kind,
                occurred_at=event.occurred_at,
                facts=event.facts,
                workspace_id=event.workspace_id,
                schema_version=event.schema_version,
                source_hash=event.source_hash,
            )
        return event


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Presentation rules
# ---------------------------------------------------------------------------

def build_presentation(earned_ids: Iterable[str], *, admin: bool = False) -> dict[str, Any]:
    """Normal display is ``A/34``, then ``A/34 + U`` once any ultra rare is earned.

    Locked S entries show ``???``.  Unearned U entries and their pool size are
    absent.  Admin/edit mode shows spoilers in alpha order.
    """
    earned = {str(item) for item in earned_ids if str(item) in BY_ID}
    earned_ultra = sorted(item for item in earned if BY_ID[item].rarity == "ultra")
    earned_normal = sorted(item for item in earned if BY_ID[item].rarity != "ultra")
    # Plan: normal ``A/34``, earned-ultra suffix `` + U``; omit at zero.
    suffix = " + U" if earned_ultra else ""
    counter = f"{len(earned_normal)}/{NORMAL_COUNT}{suffix}"

    entries: list[dict[str, Any]] = []
    for definition in sorted(CATALOG, key=lambda item: item.id):
        is_earned = definition.id in earned
        if definition.rarity == "ultra" and not is_earned and not admin:
            continue  # unearned ultra entries and their pool size remain absent
        if definition.rarity == "ultra" and not is_earned and admin:
            entries.append(_entry(definition, earned=False, locked=True, admin=True))
            continue
        if definition.rarity == "mystery" and not is_earned and not admin:
            entries.append({
                "id": definition.id,
                "key": definition.key,
                "title": "???",
                "rarity": "mystery",
                "earned": False,
                "locked": True,
                "evidenceKind": definition.evidence_kind,
                "summary": "",
            })
            continue
        entries.append(_entry(definition, earned=is_earned, locked=not is_earned, admin=admin))

    if admin:
        entries = sorted(entries, key=lambda item: (str(item.get("title") or ""), item["id"]))

    return {
        "catalogRevision": CATALOG_REVISION,
        "predicateVersion": PREDICATE_VERSION,
        "counter": counter,
        "normalTotal": NORMAL_COUNT,
        "normalEarned": len(earned_normal),
        "ultraEarned": len(earned_ultra),
        # Unwon ultra entries and their pool size remain absent; only earned
        # ultras are advertised (earning U01 reveals U01 only).
        "ultraTotalAdvertised": len(earned_ultra),
        "entries": entries,
        "earnedIds": sorted(earned),
    }


def _entry(definition: AchievementDefinition, *, earned: bool, locked: bool, admin: bool) -> dict[str, Any]:
    title = definition.title if (earned or admin or definition.rarity != "mystery") else "???"
    return {
        "id": definition.id,
        "key": definition.key,
        "title": title,
        "rarity": definition.rarity,
        "earned": earned,
        "locked": locked,
        "evidenceKind": definition.evidence_kind,
        "summary": definition.summary if (earned or admin or definition.rarity == "normal") else "",
    }


def payload_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
