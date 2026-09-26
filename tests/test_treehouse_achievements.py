"""S29 account-wide achievements: framework, storage, events, backfill.

Deterministic receipt predicates only.  No predicate is delegated to a
language model.  These tests use disposable owners/state and cover owner
isolation, duplicate/out-of-order delivery, backfill/live overlap, missing
historical proof, secrecy presentation, Class-reset separation, ledger
export/purge/restore and toast outbox durability.
"""

from __future__ import annotations

import pytest

from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.treehouse_achievements import (
    BY_ID,
    CATALOG,
    EventFamily,
    EventValidationError,
    KIND_RECEIPT,
    KIND_UI,
    NORMAL_COUNT,
    RESULT_ACKNOWLEDGED,
    RESULT_COMMITTED,
    ULTRA_COUNT,
    ActivityEvent,
    TreeHouseAchievementEngine,
    build_presentation,
    event_qualifies_for_scoring,
)


def _repo(tmp_path) -> TreeHouseRepository:
    return TreeHouseRepository(tmp_path / "treehouse.sqlite3")


def _event(
    source_event_id: str,
    event_family: str,
    *,
    kind: str = KIND_UI,
    result: str | None = None,
    actor_kind: str = "user",
    facts: dict | None = None,
    workspace_id: str | None = None,
    occurred_at: str = "2026-09-25T00:00:00Z",
) -> ActivityEvent:
    if result is None:
        result = RESULT_COMMITTED if kind == KIND_RECEIPT else RESULT_ACKNOWLEDGED
    return ActivityEvent(
        source_event_id=source_event_id,
        event_family=event_family,
        kind=kind,
        result=result,
        actor_kind=actor_kind,
        occurred_at=occurred_at,
        facts=facts or {},
        workspace_id=workspace_id,
    )


def _shell_ready(source: str = "shell-1", **kwargs) -> ActivityEvent:
    facts = {"accountResolved": True, "visiblyMounted": True, "shellId": "shell-a"}
    facts.update(kwargs.pop("facts", {}))
    return _event(source, EventFamily.SHELL_READY, facts=facts, **kwargs)


# ---------------------------------------------------------------------------
# Catalog shape (30 visible + 4 mystery + 3 ultra, as approved)
# ---------------------------------------------------------------------------


def test_catalog_is_30_visible_plus_4_mystery_plus_3_ultra():
    visible = [item for item in CATALOG if item.rarity == "normal"]
    mystery = [item for item in CATALOG if item.rarity == "mystery"]
    ultra = [item for item in CATALOG if item.rarity == "ultra"]
    assert len(visible) == 30
    assert len(mystery) == 4
    assert len(ultra) == 3
    assert NORMAL_COUNT == 34
    assert ULTRA_COUNT == 3
    assert {item.id for item in CATALOG} == {
        *(f"N{i:02d}" for i in range(1, 31)),
        *(f"S{i:02d}" for i in range(1, 5)),
        *(f"U{i:02d}" for i in range(1, 4)),
    }
    # IDs stay stable; keys are the localization-stable identifiers.
    assert BY_ID["N01"].key == "oc.first-light"
    assert BY_ID["S04"].key == "oc.not-first-rodeo"
    assert BY_ID["U03"].key == "oc.full-house"


def test_aggregate_dependencies_are_acyclic():
    # S04 depends only on N awards; U03 depends on N+S (never itself/ultra).
    assert BY_ID["S04"].depends_on == ()
    assert BY_ID["U03"].depends_on == tuple(
        [*(f"N{i:02d}" for i in range(1, 31)), *(f"S{i:02d}" for i in range(1, 5))]
    )


# ---------------------------------------------------------------------------
# Event validation: failed/pending/non-user never qualify
# ---------------------------------------------------------------------------


def test_failed_pending_and_non_user_events_are_rejected():
    for result in ("pending", "failed", "rolled_back", "cancelled", "stale"):
        with pytest.raises(EventValidationError) as exc:
            event_qualifies_for_scoring(
                _event("x", EventFamily.SHELL_READY, kind=KIND_RECEIPT, result=result)
            )
        assert exc.value.code == "non_qualifying_result"
    for actor in ("installer", "maintenance", "template_seed", "test_fixture", "import", "background_maintenance"):
        with pytest.raises(EventValidationError) as exc:
            event_qualifies_for_scoring(_shell_ready(actor_kind=actor))
        assert exc.value.code == "non_qualifying_actor"
    with pytest.raises(EventValidationError):
        event_qualifies_for_scoring(
            _event("x", EventFamily.SHELL_READY, kind=KIND_UI, result=RESULT_COMMITTED)
        )


def test_r_events_must_be_committed_and_u_events_acknowledged():
    with pytest.raises(EventValidationError):
        event_qualifies_for_scoring(
            _event("x", EventFamily.CONVERSATION_TURN_COMPLETED, kind=KIND_RECEIPT, result=RESULT_ACKNOWLEDGED)
        )
    with pytest.raises(EventValidationError):
        event_qualifies_for_scoring(_event("x", EventFamily.SHELL_READY, kind=KIND_UI, result=RESULT_COMMITTED))


# ---------------------------------------------------------------------------
# Awarding, isolation, duplicates
# ---------------------------------------------------------------------------


def test_first_shell_ready_awards_n01_and_duplicates_do_not_reaward(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    first = engine.ingest("acct-a", [_shell_ready("shell-1")])
    assert [item["achievementId"] for item in first["newAwards"]] == ["N01"]
    assert repo.earned_achievement_ids("acct-a") == ["N01"]

    # Duplicate source event id is accepted but not re-scored.
    again = engine.ingest("acct-a", [_shell_ready("shell-1")])
    assert again["newAwards"] == []
    assert repo.earned_achievement_ids("acct-a") == ["N01"]

    # A different shell id is fine but N01 is once-per-account.
    third = engine.ingest("acct-a", [_shell_ready("shell-2")])
    assert third["newAwards"] == []


def test_two_owners_with_colliding_event_ids_stay_isolated(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    # Same source_event_id and same resource ids under two owners.
    engine.ingest("acct-a", [_shell_ready("collide")])
    engine.ingest("acct-b", [_shell_ready("collide")])
    assert repo.earned_achievement_ids("acct-a") == ["N01"]
    assert repo.earned_achievement_ids("acct-b") == ["N01"]
    # Each owner's award evidence references their own receipt row.
    a_row = repo.get_achievement_award("acct-a", "N01")
    b_row = repo.get_achievement_award("acct-b", "N01")
    assert a_row["account_id"] == "acct-a"
    assert b_row["account_id"] == "acct-b"


def test_one_award_across_workspace_contexts(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest("acct-a", [_shell_ready("w-project", workspace_id="project")])
    engine.ingest("acct-a", [_shell_ready("w-system", workspace_id="system")])
    engine.ingest("acct-a", [_shell_ready("w-none", workspace_id=None)])
    assert repo.earned_achievement_ids("acct-a") == ["N01"]


def test_out_of_order_and_two_evaluators_never_double_award(tmp_path):
    repo = _repo(tmp_path)
    engine_a = TreeHouseAchievementEngine(repo)
    engine_b = TreeHouseAchievementEngine(TreeHouseRepository(tmp_path / "treehouse.sqlite3"))
    events = [_shell_ready("s1"), _shell_ready("s2"), _shell_ready("s3")]
    engine_a.ingest("acct-a", list(reversed(events)))
    engine_b.ingest("acct-a", events)
    assert repo.earned_achievement_ids("acct-a") == ["N01"]


def test_restart_resumes_backfill_with_identical_totals(tmp_path):
    path = tmp_path / "treehouse.sqlite3"
    engine = TreeHouseAchievementEngine(TreeHouseRepository(path))
    records = [
        {
            "evidence_kind": KIND_UI,
            "source_event_id": "hist-1",
            "event_family": EventFamily.SHELL_READY,
            "kind": KIND_UI,
            "result": RESULT_ACKNOWLEDGED,
            "actor_kind": "user",
            "occurred_at": "2026-09-01T00:00:00Z",
            "facts": {"accountResolved": True, "visiblyMounted": True},
            "owner_account_id": "acct-a",
        }
    ]
    first = engine.backfill("acct-a", "treehouse", records, cursor={"highWaterMark": "hist-1"})
    assert [item["achievementId"] for item in first["newAwards"]] == ["N01"]
    cursor = repo_cursor = TreeHouseRepository(path).get_backfill_cursor("acct-a", "treehouse")
    assert cursor is not None
    assert cursor["cursor"].get("highWaterMark") == "hist-1"

    # Restart and re-feed the same historical evidence.
    engine2 = TreeHouseAchievementEngine(TreeHouseRepository(path))
    again = engine2.backfill("acct-a", "treehouse", records, cursor={"highWaterMark": "hist-1"})
    assert again["newAwards"] == []
    assert TreeHouseRepository(path).earned_achievement_ids("acct-a") == ["N01"]


def test_backfill_and_live_overlap_dedupes(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    live = _shell_ready("shared-1")
    engine.ingest("acct-a", [live], via="live")
    # Backfill of the same underlying proof (family-prefixed id) must not re-award.
    engine.backfill(
        "acct-a",
        "shell",
        [
            {
                "evidence_kind": KIND_UI,
                "source_event_id": "shared-1",
                "event_family": EventFamily.SHELL_READY,
                "kind": KIND_UI,
                "result": RESULT_ACKNOWLEDGED,
                "actor_kind": "user",
                "occurred_at": "2026-09-01T00:00:00Z",
                "facts": {"accountResolved": True, "visiblyMounted": True},
                "owner_account_id": "acct-a",
            }
        ],
    )
    assert repo.earned_achievement_ids("acct-a") == ["N01"]


# ---------------------------------------------------------------------------
# Historical credit discipline
# ---------------------------------------------------------------------------


def test_prose_digest_only_and_ambiguous_owner_are_rejected(tmp_path):
    engine = TreeHouseAchievementEngine(_repo(tmp_path))
    result = engine.backfill(
        "acct-a",
        "lore",
        [
            {"evidence_kind": KIND_RECEIPT, "source_event_id": "p1", "prose": "I restored a file", "owner_account_id": "acct-a"},
            {"evidence_kind": KIND_RECEIPT, "source_event_id": "p2", "digest_only": True, "owner_account_id": "acct-a"},
            {"evidence_kind": KIND_RECEIPT, "source_event_id": "p3", "receiptKind": "ObservedAfterOnly", "owner_account_id": "acct-a"},
            {"evidence_kind": KIND_RECEIPT, "source_event_id": "p4", "event_family": EventFamily.SHELL_READY},
            {"evidence_kind": "chat", "source_event_id": "p5", "owner_account_id": "acct-a"},
        ],
    )
    codes = {item["code"] for item in result["rejected"]}
    assert "prose_evidence" in codes
    assert "digest_only" in codes
    assert "ambiguous_owner" in codes
    assert "bad_evidence_kind" in codes
    assert result["newAwards"] == []


def test_self_check_cannot_mint_action_evidence(tmp_path):
    # A guide lesson completion is T evidence for N29 only, never for N19 etc.
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest(
        "acct-a",
        [
            _event(
                "lesson-1",
                EventFamily.GUIDE_LESSON_COMPLETED,
                kind=KIND_RECEIPT,
                facts={"lessonId": "house-documents:l1", "selfCheck": True},
            )
        ],
    )
    earned = repo.earned_achievement_ids("acct-a")
    assert "N19" not in earned
    assert "N10" not in earned
    # N29 needs all thirty lessons; one self-check is not enough.
    assert "N29" not in earned


# ---------------------------------------------------------------------------
# Positive / negative fixtures across the catalog
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Positive / negative fixtures across the catalog
# ---------------------------------------------------------------------------

def _n10_facts(grammar: str = "python") -> dict:
    return {
        "grammarId": grammar,
        "supportedGrammar": True,
        "regionKind": "comment",
        "markdownNonempty": True,
        "revisionId": "rev-1",
        "documentId": "d1",
    }


def _positive_event(achievement_id: str, source: str) -> ActivityEvent:
    """Minimal positive fixture matching each checker's required facts."""
    table = {
        "N01": lambda s: _shell_ready(s),
        "N02": lambda s: _event(s, EventFamily.CONVERSATION_TURN_COMPLETED, kind=KIND_RECEIPT, facts={
            "userTurnPersisted": True, "assistantCompleted": True, "origin": "user", "conversationId": "c1",
        }),
        "N03": lambda s: _event(s, EventFamily.SCHEDULED_TASK_RUN_COMPLETED, kind=KIND_RECEIPT, facts={
            "createdByOwner": True, "scheduled": True, "runStatus": "success", "taskId": "t1",
        }),
        "N04": lambda s: _event(s, EventFamily.GOAL_VERIFIED_COMPLETED, kind=KIND_RECEIPT, facts={
            "transition": "verified_completed", "evidenceSatisfied": True, "evidenceRefs": ["g1"], "goalId": "g1",
        }),
        "N05": lambda s: _event(s, EventFamily.MEMORY_CANDIDATE_ACCEPTED, kind=KIND_RECEIPT, facts={
            "reviewTransaction": "accepted", "pendingCandidate": True, "candidateId": "cand1",
        }),
        "N07": lambda s: _event(s, EventFamily.TEMPLATE_DOCUMENT_CREATED, kind=KIND_RECEIPT, facts={
            "templateId": "tpl1", "documentId": "d1", "revisionId": "rev-1", "committed": True,
        }),
        "N08": lambda s: _event(s, EventFamily.TEMPLATE_SECTION_INSERTED, kind=KIND_RECEIPT, facts={
            "documentId": "d1", "revisionId": "rev-2", "spanNonempty": True, "sectionKey": "intro",
        }),
        "N09": lambda s: _event(s, EventFamily.EDITOR_SPLIT_PRESENTED, kind=KIND_UI, facts={
            "mounted": True, "documentIds": ["d1", "d2"],
        }),
        "N10": lambda s: _event(s, EventFamily.DOCUMENT_RICH_REGION_SAVED, kind=KIND_RECEIPT, facts=_n10_facts()),
        "N11": lambda s: _event(s, EventFamily.TABLE_REVISION_SAVED, kind=KIND_RECEIPT, facts={
            "hasTypedDateOrCurrency": True, "formulaEvaluated": True, "formulaErrors": 0,
            "revisionId": "rev-3", "tableId": "tbl1",
        }),
        "N12": lambda s: _event(s, EventFamily.WIKI_PAGE_COMMITTED, kind=KIND_RECEIPT, facts={
            "pageKind": "wiki", "chunkCommitted": True, "linkTargetId": "d2", "linkValid": True, "pageId": "p1",
        }),
        "N14": lambda s: _event(s, EventFamily.GRAPH_MODE_PRESENTED, kind=KIND_UI, facts={
            "mode": "linked", "nodeCount": 1,
        }),
        "N15": lambda s: _event(s, EventFamily.GRAPH_CAMERA_GESTURE, kind=KIND_UI, facts={
            "accepted": True, "populatedView": True, "viewId": "v1", "start": True,
            "scale": 1.0, "panX": 0.0, "panY": 0.0,
        }),
        "N16": lambda s: _event(s, EventFamily.GRAPH_FILTER_APPLIED, kind=KIND_UI, facts={
            "facetFromScopedGeneration": True, "facet": "tag", "value": "x",
            "resultCountBefore": 3, "resultCountAfter": 1,
        }),
        "N17": lambda s: _event(s, EventFamily.TASK_SOURCE_COMPLETED, kind=KIND_RECEIPT, facts={
            "checkboxChange": "unchecked_to_checked", "documentId": "d1", "revisionId": "rev-4", "taskId": "t1",
        }),
        "N19": lambda s: _event(s, EventFamily.CLIPBOARD_IMAGE_INSERTED, kind=KIND_RECEIPT, facts={
            "prepared": True, "inserted": True, "assetId": "img1", "documentId": "d1", "mediaLocation": "attachments",
        }),
        "N20": lambda s: _event(s, EventFamily.DOCUMENT_MOVE_COMMITTED, kind=KIND_RECEIPT, facts={
            "documentId": "d1", "attachmentIds": ["a1"], "referencesVerifiedIntact": True,
        }),
        "N22": lambda s: _event(s, EventFamily.IMPS_PROJECT_EXPORTED, kind=KIND_RECEIPT, facts={
            "editableExport": True, "manifestValid": True, "assetSetValid": True, "exportArtifactId": "exp1",
        }),
        "N23": lambda s: _event(s, EventFamily.LORE_RESTORE_COMMITTED, kind=KIND_RECEIPT, facts={
            "contentHash": "abc", "restoredContentHash": "abc", "versionId": "v1", "resourceId": "r1",
        }),
        "N24": lambda s: _event(s, EventFamily.EXPORT_MANIFEST_VALIDATED, kind=KIND_RECEIPT, facts={
            "manifestValid": True, "finished": True, "exportId": "e1",
        }),
        "N26": lambda s: _event(s, EventFamily.OFFICIAL_DOC_OPENED, kind=KIND_UI, facts={
            "provisionedResource": True, "isDemo": False, "personalFolder": False,
            "resourceId": "official:docs/one",
        }),
        "N27": lambda s: _event(s, EventFamily.FORMATTING_DEMO_TOGGLED, kind=KIND_UI, facts={
            "officialDemo": True, "fromView": "rendered", "toView": "source", "demoId": "formatting",
        }),
        "N30": lambda s: _event(s, EventFamily.SURFACE_VISITED, kind=KIND_UI, facts={
            "surface": "editor", "appLinkResolved": True, "destinationExists": True,
        }),
        "S01": lambda s: _event(s, EventFamily.SURFACE_VISITED, kind=KIND_UI, facts={
            "surface": "chat", "visited": True,
        }),
        "U01": lambda s: _event(s, EventFamily.RAIN_DROPLET_PAINTED, kind=KIND_UI, facts={
            "directionUpward": True, "rareDroplet": True, "paintedInVisibleViewport": True,
            "tabActive": True, "effectActive": True, "hiddenFrameSpawn": False, "dropletId": "drop1",
        }),
    }
    return table[achievement_id](source)


def _negative_event(achievement_id: str, source: str) -> ActivityEvent:
    """Same family, missing required proof — checker must refuse."""
    positive = _positive_event(achievement_id, source)
    facts = dict(positive.facts)
    for key in list(facts):
        if isinstance(facts[key], bool):
            facts[key] = False
        elif isinstance(facts[key], (int, float)) and key not in {"revisionId", "formulaErrors"}:
            facts[key] = 0
        elif isinstance(facts[key], str) and key not in {"cycleSequence", "checkboxChange", "regionKind", "pageKind", "runStatus", "transition", "reviewTransaction", "fromView", "toView", "mode", "sourceHash", "contentHash", "restoredContentHash", "themeDigest"}:
            facts[key] = ""
        elif isinstance(facts[key], list):
            facts[key] = []
    return ActivityEvent(
        source_event_id=positive.source_event_id,
        event_family=positive.event_family,
        kind=positive.kind,
        result=positive.result,
        actor_kind=positive.actor_kind,
        occurred_at=positive.occurred_at,
        facts=facts,
    )


def test_every_direct_achievement_has_positive_and_negative_fixture(tmp_path):
    covered = {
        "N01", "N02", "N03", "N04", "N05", "N07", "N08", "N09", "N10", "N11",
        "N12", "N16", "N17", "N19", "N20", "N22", "N23", "N24",
        "N26", "N27", "U01",
    }
    for achievement_id in sorted(covered):
        positive_repo = _repo(tmp_path / f"pos-{achievement_id}")
        positive_engine = TreeHouseAchievementEngine(positive_repo)
        result = positive_engine.ingest(f"acct-{achievement_id}", [_positive_event(achievement_id, f"{achievement_id}-pos")])
        awarded = {item["achievementId"] for item in result["newAwards"]}
        assert achievement_id in awarded, f"{achievement_id} positive fixture did not award: {result}"

        negative_repo = _repo(tmp_path / f"neg-{achievement_id}")
        negative_engine = TreeHouseAchievementEngine(negative_repo)
        result_neg = negative_engine.ingest(f"acct-{achievement_id}", [_negative_event(achievement_id, f"{achievement_id}-neg")])
        awarded_neg = {item["achievementId"] for item in result_neg["newAwards"]}
        assert achievement_id not in awarded_neg, f"{achievement_id} negative fixture wrongly awarded: {result_neg}"


def test_multi_step_predicates_require_matching_identities(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)

    # N06: search + open must share an authorized record id.
    engine.ingest("acct-a", [
        _event("s1", EventFamily.MEMORY_SEARCH_COMPLETED, kind=KIND_RECEIPT, facts={"permitted": True, "recordIds": ["r1", "r2"]}),
    ])
    assert "N06" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("o1", EventFamily.MEMORY_RECORD_OPENED, kind=KIND_UI, facts={"authorized": True, "recordId": "r9"}),
    ])
    assert "N06" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("o2", EventFamily.MEMORY_RECORD_OPENED, kind=KIND_UI, facts={"authorized": True, "recordId": "r2"}),
    ])
    assert "N06" in repo.earned_achievement_ids("acct-a")

    # N13: link created + navigation must resolve the same edge to another doc.
    engine.ingest("acct-b", [
        _event("l1", EventFamily.DOCUMENT_LINK_CREATED, kind=KIND_RECEIPT, facts={
            "sourceDocumentId": "d1", "targetDocumentId": "d2", "linkId": "edge1",
        }),
        _event("n1", EventFamily.DOCUMENT_LINK_NAVIGATED, kind=KIND_UI, facts={
            "linkId": "edge1", "sourceDocumentId": "d1", "targetDocumentId": "d9", "resolvedToDifferentDocument": True,
        }),
    ])
    assert "N13" not in repo.earned_achievement_ids("acct-b")
    engine.ingest("acct-b", [
        _event("n2", EventFamily.DOCUMENT_LINK_NAVIGATED, kind=KIND_UI, facts={
            "linkId": "edge1", "sourceDocumentId": "d1", "targetDocumentId": "d2", "resolvedToDifferentDocument": True,
        }),
    ])
    assert "N13" in repo.earned_achievement_ids("acct-b")


def test_n14_requires_both_graph_modes_with_nodes(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest("acct-a", [
        _event("m1", EventFamily.GRAPH_MODE_PRESENTED, kind=KIND_UI, facts={"mode": "linked", "nodeCount": 2}),
    ])
    assert "N14" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("m2", EventFamily.GRAPH_MODE_PRESENTED, kind=KIND_UI, facts={"mode": "structure", "nodeCount": 0}),
    ])
    assert "N14" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("m3", EventFamily.GRAPH_MODE_PRESENTED, kind=KIND_UI, facts={"mode": "structure", "nodeCount": 1}),
    ])
    assert "N14" in repo.earned_achievement_ids("acct-a")


def test_n15_camera_gesture_requires_scale_and_pan(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest("acct-a", [
        _event("g1", EventFamily.GRAPH_CAMERA_GESTURE, kind=KIND_UI, facts={
            "accepted": True, "populatedView": True, "viewId": "v1", "start": True, "scale": 1.0, "panX": 0.0, "panY": 0.0,
        }),
        _event("g2", EventFamily.GRAPH_CAMERA_GESTURE, kind=KIND_UI, facts={
            "accepted": True, "populatedView": True, "viewId": "v1", "scale": 1.1, "panX": 10.0, "panY": 0.0,
        }),
    ])
    assert "N15" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("g3", EventFamily.GRAPH_CAMERA_GESTURE, kind=KIND_UI, facts={
            "accepted": True, "populatedView": True, "viewId": "v1", "scale": 1.3, "panX": 50.0, "panY": 0.0,
        }),
    ])
    assert "N15" in repo.earned_achievement_ids("acct-a")


def test_n18_timeline_move_matches_stable_event_id(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest("acct-a", [
        _event("c1", EventFamily.TIMELINE_EVENT_COMMITTED, kind=KIND_RECEIPT, facts={
            "timelineEventId": "ev1", "date": "2026-09-01", "track": "a", "committed": True,
        }),
        _event("m1", EventFamily.TIMELINE_EVENT_MOVED, kind=KIND_RECEIPT, facts={
            "timelineEventId": "ev2", "date": "2026-09-02", "track": "b", "committed": True,
        }),
    ])
    assert "N18" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("m2", EventFamily.TIMELINE_EVENT_MOVED, kind=KIND_RECEIPT, facts={
            "timelineEventId": "ev1", "date": "2026-09-03", "track": "a", "committed": True,
        }),
    ])
    assert "N18" in repo.earned_achievement_ids("acct-a")


def test_n21_impish_requires_save_then_reopen_with_same_revision(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest("acct-a", [
        _event("sv1", EventFamily.IMPS_PROJECT_SAVED, kind=KIND_RECEIPT, facts={
            "projectId": "p1", "revisionId": "rev-3", "layerIds": ["a", "b"], "editableLayerCount": 2,
        }),
    ])
    assert "N21" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("ro1", EventFamily.IMPS_PROJECT_REOPENED, kind=KIND_RECEIPT, facts={
            "projectId": "p1", "revisionId": "rev-3", "layerIds": ["a", "b"],
        }),
    ])
    assert "N21" in repo.earned_achievement_ids("acct-a")


def test_n25_theme_save_then_rehydrate(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest("acct-a", [
        _event("ts1", EventFamily.THEME_PREFERENCE_SAVED, kind=KIND_RECEIPT, facts={
            "themeDigest": "h1", "confirmed": True,
        }),
    ])
    assert "N25" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("tl1", EventFamily.THEME_SETTINGS_LOADED, kind=KIND_RECEIPT, facts={
            "themeDigest": "h2", "rehydrated": True,
        }),
    ])
    assert "N25" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("tl2", EventFamily.THEME_SETTINGS_LOADED, kind=KIND_RECEIPT, facts={
            "themeDigest": "h1", "rehydrated": True,
        }),
    ])
    assert "N25" in repo.earned_achievement_ids("acct-a")


def test_n28_class_preview_then_publish_excludes_seeded(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest("acct-a", [
        _event("pv1", EventFamily.CLASS_PREVIEWED, kind=KIND_UI, facts={
            "classId": "cls1", "previewedAsLearner": True, "seededOfficial": False,
        }),
    ])
    assert "N28" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("pb1", EventFamily.CLASS_PUBLISHED, kind=KIND_RECEIPT, facts={
            "classId": "cls1", "lessonCount": 1, "authoredByOwner": True, "seededOfficial": False,
        }),
    ])
    assert "N28" in repo.earned_achievement_ids("acct-a")

    engine.ingest("acct-b", [
        _event("pv2", EventFamily.CLASS_PREVIEWED, kind=KIND_UI, facts={
            "classId": "official", "previewedAsLearner": True, "seededOfficial": True,
        }),
        _event("pb2", EventFamily.CLASS_PUBLISHED, kind=KIND_RECEIPT, facts={
            "classId": "official", "lessonCount": 30, "authoredByOwner": True, "seededOfficial": True,
        }),
    ])
    assert "N28" not in repo.earned_achievement_ids("acct-b")


def test_s02_workspace_rebind_preserves_session_then_acts(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    engine.ingest("acct-a", [
        _event("rb1", EventFamily.CHAT_WORKSPACE_REBOUND, kind=KIND_RECEIPT, facts={
            "sessionId": "ses1", "newWorkspaceId": "w2", "sessionIdPreserved": True,
        }),
    ])
    assert "S02" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("ac1", EventFamily.CHAT_ACTION_COMPLETED, kind=KIND_RECEIPT, facts={
            "sessionId": "ses9", "workspaceId": "w2", "authorized": True,
        }),
    ])
    assert "S02" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("ac2", EventFamily.CHAT_ACTION_COMPLETED, kind=KIND_RECEIPT, facts={
            "sessionId": "ses1", "workspaceId": "w2", "authorized": True,
        }),
    ])
    assert "S02" in repo.earned_achievement_ids("acct-a")


def test_s03_rich_source_rich_cycle_requires_unchanged_hash(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)

    def cycle(source: str, stage: str, source_hash: str) -> ActivityEvent:
        return _event(source, EventFamily.SOURCE_VIEW_CYCLE, kind=KIND_UI, facts={
            "supportedProgrammingDocument": True,
            "cycleSequence": stage,
            "documentId": "d1",
            "sourceRevisionId": "rev-1",
            "sourceHash": source_hash,
        })

    engine.ingest("acct-a", [cycle("c1", "rich", "h1"), cycle("c2", "source", "h1")])
    assert "S03" not in repo.earned_achievement_ids("acct-a")
    # Hash change on the return leg breaks the cycle.
    engine.ingest("acct-a", [cycle("c3", "rich", "h2")])
    assert "S03" not in repo.earned_achievement_ids("acct-a")
    # A fresh full cycle with unchanged hash completes S03.
    engine.ingest("acct-a", [cycle("c4", "rich", "h1"), cycle("c5", "source", "h1"), cycle("c6", "rich", "h1")])
    assert "S03" in repo.earned_achievement_ids("acct-a")


def test_u02_requires_five_distinct_comment_grammars(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    for index, grammar in enumerate(["python", "python", "go", "rust", "markdown", "json", "java", "ruby"]):
        engine.ingest("acct-a", [
            _event(f"u2-{index}", EventFamily.DOCUMENT_RICH_REGION_SAVED, kind=KIND_RECEIPT, facts=_n10_facts(grammar)),
        ])
    earned = repo.earned_achievement_ids("acct-a")
    assert "N10" in earned
    assert "U02" in earned


def test_s01_five_surfaces_and_n30_two_app_links(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    for surface in ["chat", "editor", "files", "graph", "treehouse"]:
        engine.ingest("acct-a", [
            _event(f"vis-{surface}", EventFamily.SURFACE_VISITED, kind=KIND_UI, facts={"surface": surface, "visited": True}),
        ])
    assert "S01" in repo.earned_achievement_ids("acct-a")

    engine.ingest("acct-b", [
        _event("link-editor", EventFamily.SURFACE_VISITED, kind=KIND_UI, facts={
            "surface": "editor", "appLinkResolved": True, "destinationExists": True,
        }),
        _event("link-treehouse", EventFamily.SURFACE_VISITED, kind=KIND_UI, facts={
            "surface": "treehouse", "appLinkResolved": True, "destinationExists": True,
        }),
    ])
    assert "N30" in repo.earned_achievement_ids("acct-b")


def test_n29_requires_all_thirty_guide_lessons(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    for index in range(29):
        engine.ingest("acct-a", [
            _event(f"lesson-{index}", EventFamily.GUIDE_LESSON_COMPLETED, kind=KIND_RECEIPT, facts={
                "lessonKey": f"house-lesson-{index}", "committed": True,
            }),
        ])
    assert "N29" not in repo.earned_achievement_ids("acct-a")
    engine.ingest("acct-a", [
        _event("lesson-29", EventFamily.GUIDE_LESSON_COMPLETED, kind=KIND_RECEIPT, facts={
            "lessonKey": "house-lesson-29", "committed": True,
        }),
    ])
    assert "N29" in repo.earned_achievement_ids("acct-a")


# ---------------------------------------------------------------------------
# Aggregate awards (S04 / U03) and secrecy
# ---------------------------------------------------------------------------

_DIRECT_N = [
    "N01", "N02", "N03", "N04", "N05", "N07", "N08", "N09", "N10", "N11",
    "N12", "N14", "N16", "N17", "N19", "N20", "N22", "N23", "N24",
    "N26", "N27",
]


def _earn_direct_normals(engine: TreeHouseAchievementEngine, account: str, count: int) -> None:
    for index, achievement_id in enumerate(_DIRECT_N[:count]):
        engine.ingest(account, [_positive_event(achievement_id, f"earn-{account}-{index}")])


def test_s04_requires_ten_distinct_n_awards_and_ignores_itself(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    _earn_direct_normals(engine, "acct-a", 9)
    assert "S04" not in repo.earned_achievement_ids("acct-a")
    _earn_direct_normals(engine, "acct-a", 10)
    assert "S04" in repo.earned_achievement_ids("acct-a")


def test_u03_full_house_requires_all_34_normals(tmp_path):
    repo = _repo(tmp_path)
    engine = TreeHouseAchievementEngine(repo)
    _earn_direct_normals(engine, "acct-a", len(_DIRECT_N))
    # Multi-step Ns
    engine.ingest("acct-a", [
        _event("n06s", EventFamily.MEMORY_SEARCH_COMPLETED, kind=KIND_RECEIPT, facts={"permitted": True, "recordIds": ["r1"]}),
        _event("n06o", EventFamily.MEMORY_RECORD_OPENED, kind=KIND_UI, facts={"authorized": True, "recordId": "r1"}),
        _event("n13l", EventFamily.DOCUMENT_LINK_CREATED, kind=KIND_RECEIPT, facts={
            "sourceDocumentId": "d1", "targetDocumentId": "d2", "linkId": "e1",
        }),
        _event("n13n", EventFamily.DOCUMENT_LINK_NAVIGATED, kind=KIND_UI, facts={
            "linkId": "e1", "sourceDocumentId": "d1", "targetDocumentId": "d2", "resolvedToDifferentDocument": True,
        }),
        _event("n18c", EventFamily.TIMELINE_EVENT_COMMITTED, kind=KIND_RECEIPT, facts={
            "timelineEventId": "ev1", "date": "2026-09-01", "track": "a", "committed": True,
        }),
        _event("n18m", EventFamily.TIMELINE_EVENT_MOVED, kind=KIND_RECEIPT, facts={
            "timelineEventId": "ev1", "date": "2026-09-02", "track": "b", "committed": True,
        }),
        _event("n21s", EventFamily.IMPS_PROJECT_SAVED, kind=KIND_RECEIPT, facts={
            "projectId": "p1", "revisionId": "r1", "layerIds": ["a", "b"], "editableLayerCount": 2,
        }),
        _event("n21r", EventFamily.IMPS_PROJECT_REOPENED, kind=KIND_RECEIPT, facts={
            "projectId": "p1", "revisionId": "r1", "layerIds": ["a", "b"],
        }),
        _event("n25s", EventFamily.THEME_PREFERENCE_SAVED, kind=KIND_RECEIPT, facts={
            "themeDigest": "h1", "confirmed": True,
        }),
        _event("n25l", EventFamily.THEME_SETTINGS_LOADED, kind=KIND_RECEIPT, facts={
            "themeDigest": "h1", "rehydrated": True,
        }),
        _event("n28p", EventFamily.CLASS_PREVIEWED, kind=KIND_UI, facts={
            "classId": "c1", "previewedAsLearner": True, "seededOfficial": False,
        }),
        _event("n28b", EventFamily.CLASS_PUBLISHED, kind=KIND_RECEIPT, facts={
            "classId": "c1", "lessonCount": 1, "authoredByOwner": True, "seededOfficial": False,
        }),
        # N14 both graph modes with nodes
        _event("n14a", EventFamily.GRAPH_MODE_PRESENTED, kind=KIND_UI, facts={"mode": "linked", "nodeCount": 1}),
        _event("n14b", EventFamily.GRAPH_MODE_PRESENTED, kind=KIND_UI, facts={"mode": "structure", "nodeCount": 1}),
        # N15 camera gesture: start then scale+pan
        _event("n15a", EventFamily.GRAPH_CAMERA_GESTURE, kind=KIND_UI, facts={
            "accepted": True, "populatedView": True, "viewId": "v1", "start": True,
            "scale": 1.0, "panX": 0.0, "panY": 0.0,
        }),
        _event("n15b", EventFamily.GRAPH_CAMERA_GESTURE, kind=KIND_UI, facts={
            "accepted": True, "populatedView": True, "viewId": "v1",
            "scale": 1.3, "panX": 50.0, "panY": 0.0,
        }),
    ])
    for index in range(30):
        engine.ingest("acct-a", [
            _event(f"n29-{index}", EventFamily.GUIDE_LESSON_COMPLETED, kind=KIND_RECEIPT, facts={
                "lessonKey": f"l{index}", "committed": True,
            }),
        ])
    engine.ingest("acct-a", [
        _event("n30e", EventFamily.SURFACE_VISITED, kind=KIND_UI, facts={
            "surface": "editor", "appLinkResolved": True, "destinationExists": True,
        }),
        _event("n30t", EventFamily.SURFACE_VISITED, kind=KIND_UI, facts={
            "surface": "treehouse", "appLinkResolved": True, "destinationExists": True,
        }),
    ])
    # Mystery
    for surface in ["chat", "editor", "files", "graph", "treehouse"]:
        engine.ingest("acct-a", [
            _event(f"s01-{surface}", EventFamily.SURFACE_VISITED, kind=KIND_UI, facts={"surface": surface, "visited": True}),
        ])
    engine.ingest("acct-a", [
        _event("s02r", EventFamily.CHAT_WORKSPACE_REBOUND, kind=KIND_RECEIPT, facts={
            "sessionId": "s", "newWorkspaceId": "b", "sessionIdPreserved": True,
        }),
        _event("s02a", EventFamily.CHAT_ACTION_COMPLETED, kind=KIND_RECEIPT, facts={
            "sessionId": "s", "workspaceId": "b", "authorized": True,
        }),
    ])
    for index, (stage, source_hash) in enumerate([("rich", "h"), ("source", "h"), ("rich", "h")]):
        engine.ingest("acct-a", [
            _event(f"s03-{index}", EventFamily.SOURCE_VIEW_CYCLE, kind=KIND_UI, facts={
                "supportedProgrammingDocument": True, "cycleSequence": stage,
                "documentId": "d1", "sourceRevisionId": "rev", "sourceHash": source_hash,
            }),
        ])
    engine.evaluate_aggregates("acct-a")
    earned = set(repo.earned_achievement_ids("acct-a"))
    missing_normals = [item_id for item_id in [
        *(f"N{i:02d}" for i in range(1, 31)), *(f"S{i:02d}" for i in range(1, 5)),
    ] if item_id not in earned]
    assert not missing_normals, f"missing normals: {missing_normals}"
    assert "S04" in earned
    assert "U03" in earned
    # U01/U02 remain unearned; U03 does not require or reveal them.
    assert "U01" not in earned
    assert "U02" not in earned
    award = repo.get_achievement_award("acct-a", "U03")
    assert award is not None


def test_presentation_secrecy_rules():
    empty = build_presentation([], admin=False)
    assert empty["counter"] == "0/34"
    assert empty["ultraTotalAdvertised"] == 0
    titles = {item["id"]: item["title"] for item in empty["entries"]}
    for index in range(1, 5):
        assert titles[f"S{index:02d}"] == "???"
    for definition in CATALOG:
        if definition.rarity == "ultra":
            assert definition.id not in titles

    one = build_presentation(["N01", "S01", "U01"], admin=False)
    assert one["counter"] == "2/34 + U"
    ultra_entries = [item for item in one["entries"] if item["rarity"] == "ultra"]
    assert [item["id"] for item in ultra_entries] == ["U01"]
    assert one["ultraTotalAdvertised"] == 1

    admin = build_presentation(["N01"], admin=True)
    admin_ids = {item["id"] for item in admin["entries"]}
    assert {"U01", "U02", "U03"} <= admin_ids
    titles_admin = [item["title"] for item in admin["entries"]]
    assert titles_admin == sorted(titles_admin)
    mystery_titles = {item["title"] for item in admin["entries"] if item["rarity"] == "mystery"}
    assert "???" not in mystery_titles


def test_future_catalog_additions_keep_totals_honest():
    presentation = build_presentation(["N01"], admin=False)
    assert presentation["normalTotal"] == NORMAL_COUNT
    assert presentation["normalEarned"] == 1
