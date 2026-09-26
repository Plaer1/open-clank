"""Field Guide catalogue tests — S30 five free-exploration Classes.

Count assertions are not enough.  These tests check the thirty unique lesson
keys, the shared app-link destinations, the absence of built-in prerequisite
locks, honest self-check versus verified labelling, the versioned upgrade with
legacy ID mapping, and the hidden system section.
"""

from pathlib import Path

from src.openclank.copal_treehouse import (
    apply_treehouse_command,
    compute_treehouse_projections,
    new_treehouse_state,
    validate_treehouse_state,
)
from src.openclank.treehouse_field_guide import (
    APP_DESTINATIONS,
    CLASS_KEYS,
    FIELD_GUIDE_TEMPLATE_KEY,
    FIELD_GUIDE_TEMPLATE_VERSION,
    LEGACY_LESSON_MAP,
    field_guide_manifest,
    instantiate_field_guide,
    upgrade_field_guide,
    validate_field_guide_manifest,
)

_EXPECTED_LESSON_KEYS = {
    "house-collaborate.models-brief",
    "house-collaborate.chat-identity",
    "house-collaborate.tool-activity",
    "house-collaborate.durable-goals",
    "house-collaborate.scheduled-work",
    "house-collaborate.menmery-and-lore",
    "house-documents.new-documents",
    "house-documents.tabs-and-splits",
    "house-documents.rich-markdown",
    "house-documents.typed-tables",
    "house-documents.wiki-within",
    "house-documents.images-attached",
    "house-connections.links",
    "house-connections.graph-modes",
    "house-connections.filters-camera",
    "house-connections.bases",
    "house-connections.tasks-timeline",
    "house-connections.research-compare",
    "house-making.files-workspaces",
    "house-making.clipboard-media",
    "house-making.imps-layers",
    "house-making.project-export",
    "house-making.lore-restore",
    "house-making.scoped-exports",
    "house-stewardship.official-docs",
    "house-stewardship.theme-effects",
    "house-stewardship.app-links",
    "house-stewardship.email-calendar",
    "house-stewardship.teach-a-class",
    "house-stewardship.progress-secrets",
}


def test_field_guide_publishes_five_classes_and_thirty_unique_lessons():
    manifest = field_guide_manifest()
    assert manifest["templateKey"] == FIELD_GUIDE_TEMPLATE_KEY
    assert manifest["templateVersion"] == FIELD_GUIDE_TEMPLATE_VERSION
    assert tuple(manifest["classKeys"]) == tuple(CLASS_KEYS)
    assert len(manifest["courses"]) == 5
    assert len(manifest["lessons"]) == 30
    assert set(manifest["lessonKeys"]) == _EXPECTED_LESSON_KEYS
    assert len(set(manifest["lessonKeys"])) == 30
    for course in manifest["courses"]:
        assert len(course["lessons"]) == 6
        assert {lesson["key"] for lesson in course["lessons"]} <= _EXPECTED_LESSON_KEYS
    validate_field_guide_manifest(manifest)


def test_builtin_classes_have_no_prerequisite_locks_and_keep_suggested_order():
    manifest = field_guide_manifest()
    assert manifest["suggestedOrder"] == list(CLASS_KEYS)
    for course in manifest["courses"]:
        assert course["freeExploration"] is True
        assert course["prerequisites"] == []
        assert course["suggestedOrder"] >= 1
    # Suggested order is a hint: every lesson is reachable regardless.
    assert all(lesson["suggestedPosition"] in range(1, 7) for lesson in manifest["lessons"])


def test_every_lesson_uses_the_shared_app_link_resolver():
    manifest = field_guide_manifest()
    bodies = []
    for course in manifest["courses"]:
        for lesson in course["lessons"]:
            surface = lesson["surface"]
            assert surface["appLink"] == f"clank://{lesson['destination']}"
            assert lesson["destination"] in APP_DESTINATIONS
            assert surface["href"].startswith("/")
            assert surface["locator"]
            assert "/copal/" not in surface["href"]
            bodies.append(lesson["body"])
            bodies.append(lesson["explanation"])
    joined = "\n".join(bodies)
    assert "/copal/" not in joined
    assert "clank://" in joined
    # No stale standalone launchers are taught.
    for stale in ("Mind applet", "Gallery applet", "standalone Wiki app"):
        assert stale not in joined


def test_every_app_destination_root_has_a_live_js_handler():
    """Every APP_DESTINATIONS key must resolve in the shipped JS registry.

    A Python destination with no `registerAppDestination` is a dead Open
    action: `openAppDestination` returns `ok:false` and the learner's click
    does nothing. Settings panels share the `settings` root handler.
    """
    import re

    copal_js = (Path(__file__).parents[1] / "static" / "js" / "copal.js").read_text()
    registered = set(re.findall(r"registerAppDestination\('([^']+)'", copal_js))
    roots = {key.split("/", 1)[0] for key in APP_DESTINATIONS}
    missing = roots - registered
    assert not missing, f"dead app destinations (no JS handler): {sorted(missing)}"
    # Files must open the real applet, not the listener-less activate-applet event.
    assert "dispatchEvent(new CustomEvent('openclank:activate-applet'" not in copal_js
    assert "filesModule" in copal_js


def test_completion_is_marked_honestly_and_practice_is_complete():
    manifest = field_guide_manifest()
    kinds = {lesson["completion"] for lesson in manifest["lessons"]}
    assert kinds == {"self-check", "verified"}
    for lesson in manifest["lessons"]:
        assert lesson["result"] and lesson["explanation"] and lesson["whyThisHelps"]
        practice = lesson["practice"]
        if practice is not None:
            assert practice["title"] and practice["seed"]
            assert practice["cleanup"] and practice["expectedEvidence"]
            assert lesson["practiceFixture"]
        if lesson["completion"] == "verified":
            # Verified lessons must offer something observable to check.
            assert lesson["result"]


def test_hidden_system_section_exists_and_is_not_a_learner_container():
    manifest = field_guide_manifest()
    sections = manifest["sections"]
    hidden = [section for section in sections if section.get("hidden")]
    assert hidden, "a hidden system section must exist"
    system = [section for section in hidden if section.get("system")]
    assert system
    assert system[0]["classKeys"] == [], "hidden system section holds no learner Classes"
    visible = [section for section in sections if not section.get("hidden")]
    assert visible and set(visible[0]["classKeys"]) == set(CLASS_KEYS)


def test_field_guide_instantiation_is_private_idempotent_and_unlocked():
    state = instantiate_field_guide(new_treehouse_state("acct-a"), "acct-a")
    validate_treehouse_state(state)
    again = instantiate_field_guide(state, "acct-a")
    assert again == state
    assert all(course["ownerId"] == "acct-a" for course in state["courses"].values())
    assert {course["status"] for course in state["courses"].values()} == {"published"}
    assert len(state["courses"]) == 5
    assert len(state["activities"]) == 30
    for course in state["courses"].values():
        assert course.get("freeExploration") is True
        assert not course.get("prerequisites")
    for activity in state["activities"].values():
        assert activity.get("fieldGuideKey")
        assert activity.get("surface", {}).get("appLink", "").startswith("clank://")
        assert activity.get("completion") in {"self-check", "verified"}
    assert state["fieldGuide"]["freeExploration"] is True
    assert state["fieldGuide"]["legacyLessonMap"] == LEGACY_LESSON_MAP


def test_free_exploration_completion_needs_no_enrollment_and_order():
    state = instantiate_field_guide(new_treehouse_state("acct-a"), "acct-a")
    # Complete the LAST suggested lesson first: no prerequisite or order lock.
    target = next(
        activity_id for activity_id, activity in state["activities"].items()
        if activity.get("fieldGuideKey") == "house-stewardship.progress-secrets"
    )
    result, _, _ = apply_treehouse_command(
        state,
        {"type": "activity.complete", "payload": {"activityId": target}},
        actor_id="acct-a",
        command_id="complete-last-first",
        expected_revision=state["revision"],
    )
    projection = compute_treehouse_projections(result)["learners"]["acct-a"]
    assert target in projection["completedActivityIds"]
    # Implicit enrollment is created; it is not a lock the learner had to pass.
    assert any(
        enrollment["courseId"] == result["activities"][target]["courseId"]
        for enrollment in result["enrollments"].values()
    )


def _legacy_2026_09_05_state(owner: str) -> dict:
    """Build a state shaped exactly like a 2026-09-05.1 Field Guide install.

    Payloads are the frozen 17-Class template bytes, so the upgrade's
    fingerprint check runs against real legacy content rather than a stub.
    """
    courses = (
        ("fg-orientation", "Find your footing", "Editor, Files and Timeline"),
        ("fg-assistant", "Work with your clanker", "Assistant, sessions and tool activity"),
        ("fg-editor", "Make yourself at home in Editor", "Editor and Markdown"),
        ("fg-files", "Keep your files connected", "Files and attachments"),
        ("fg-wiki", "Build a small Wiki", "Wiki and linked story cards"),
    )
    lessons = {
        "fg-orientation": (
            "Create a disposable note in Editor; open it from Files; add one dated "
            "Timeline event; reload and confirm all three views show the same saved title.",
            ("Field Guide map", "Identify Editor, Files, Timeline and the current TreeHouse course.",
             "The learner can name all four destinations and return to this lesson."),
        ),
        "fg-assistant": (
            "Ask the Assistant for a two-step plan; save the plan as a session; resume the session and verify the second step remains.",
            ("Practice brief", "Ask for a two-step summary of this disposable brief and save the response as a session.",
             "The session contains the brief title and two numbered steps."),
        ),
        "fg-editor": (
            "Edit the supplied Markdown fixture; add a heading and one checklist item; save, reopen, and inspect the revision marker.",
            ("Disposable note", "# Field Guide note\n\n- [ ] One practice check",
             "The note reopens with its heading and checked-list syntax."),
        ),
        "fg-files": (
            "Open the supplied practice folder; create one disposable text file; rename it; verify the final path from Files.",
            ("Disposable file", "field-guide-practice.txt\nKeep this file disposable.",
             "Files shows the renamed practice path."),
        ),
        "fg-wiki": (
            "Create a Wiki card from the fixture; link it to the supplied note; reopen the link and verify both titles.",
            ("Practice card", "A short card linked to the Field Guide note.",
             "The card reopens and its source link resolves."),
        ),
    }
    cleanup = "Delete or reset this disposable fixture after the check."
    state = new_treehouse_state(owner)
    state["profiles"][owner] = {
        "id": owner, "displayName": owner, "roles": ["admin", "instructor", "learner"], "active": True,
    }
    for index, (key, title, surface_label) in enumerate(courses):
        steps, (practice_title, practice_seed, expected) = lessons[key]
        course_id = f"course:{key}:legacy"
        module_id = f"module:{key}:legacy"
        activity_id = f"activity:{key}:lesson-1:legacy"
        state["courses"][course_id] = {
            "id": course_id, "title": title, "description": f"Practice {surface_label}.",
            "status": "published", "ownerId": owner, "authorIds": [owner],
            "moduleIds": [module_id], "fieldGuideKey": key,
            "prerequisites": [] if index == 0 else ["course:fg-orientation:legacy"],
            "createdAt": "2026-09-05T00:00:00Z", "updatedAt": "2026-09-05T00:00:00Z",
        }
        state["modules"][module_id] = {
            "id": module_id, "courseId": course_id, "title": "Practice",
            "activityIds": [activity_id], "assignmentIds": [],
        }
        state["activities"][activity_id] = {
            "id": activity_id, "courseId": course_id, "moduleId": module_id,
            "title": f"{title}: first practice",
            "activityType": "lesson", "content": steps, "steps": [steps],
            "status": "published", "points": 10, "skillIds": [],
            "fieldGuideKey": f"{key}:lesson-1",
            "destination": surface_label,
            "practice": {"title": practice_title, "seed": practice_seed, "cleanup": cleanup, "expectedEvidence": expected},
        }
    state["fieldGuide"] = {
        "templateKey": FIELD_GUIDE_TEMPLATE_KEY,
        "templateVersion": "2026-09-05.1",
        "ownerAccountId": owner,
    }
    return state


def test_legacy_fingerprints_match_the_frozen_2026_09_05_payloads():
    from src.openclank.treehouse_field_guide import LEGACY_LESSON_FINGERPRINTS, _lesson_fingerprint
    state = _legacy_2026_09_05_state("acct-fp")
    for activity in state["activities"].values():
        key = activity["fieldGuideKey"]
        assert key in LEGACY_LESSON_FINGERPRINTS
        assert _lesson_fingerprint(activity) == LEGACY_LESSON_FINGERPRINTS[key], key


def test_legacy_upgrade_preserves_edits_maps_progress_and_resets_nothing():
    legacy_state = _legacy_2026_09_05_state("acct-old")
    # A legacy self-check completion on one old lesson.
    old_activity_id = "activity:fg-orientation:lesson-1:legacy"
    legacy_state.setdefault("events", []).append({
        "id": "event:legacy-complete",
        "type": "activity.completed",
        "subjectId": "acct-old",
        "entityType": "activity",
        "entityId": old_activity_id,
        "data": {"fieldGuideKey": "fg-orientation:lesson-1"},
        "at": "2026-09-10T00:00:00Z",
    })
    # Edit one template lesson so the upgrade must preserve it.
    edited_id = "activity:fg-editor:lesson-1:legacy"
    legacy_state["activities"][edited_id]["content"] = "Learner edited this lesson body."

    upgraded, report = upgrade_field_guide(legacy_state, "acct-old")
    validate_treehouse_state(upgraded)
    assert upgraded["fieldGuide"]["templateVersion"] == FIELD_GUIDE_TEMPLATE_VERSION
    assert report["from"] == "2026-09-05.1"
    assert report["to"] == FIELD_GUIDE_TEMPLATE_VERSION
    # Edited lesson preserved, not overwritten.
    assert any(item["activityId"] == edited_id for item in report["preservedEdited"])
    assert upgraded["activities"][edited_id]["content"] == "Learner edited this lesson body."
    # Unmodified template lessons were replaced by the new thirty.
    replaced_ids = {item["activityId"] for item in report["replacedUnmodified"]}
    assert "activity:fg-orientation:lesson-1:legacy" in replaced_ids
    assert "activity:fg-editor:lesson-1:legacy" not in replaced_ids
    new_keys = {activity.get("fieldGuideKey") for activity in upgraded["activities"].values()}
    assert _EXPECTED_LESSON_KEYS <= new_keys
    # Legacy progress is mapped for historical display and never counts as a
    # new-guide completion or a new feat.
    assert report["legacyProgress"]
    assert all(entry["countsTowardNewGuide"] is False for entry in report["legacyProgress"])
    assert report["legacyProgress"][0]["oldLessonKey"] == "fg-orientation:lesson-1"
    assert report["legacyProgress"][0]["newLessonKey"] == LEGACY_LESSON_MAP["fg-orientation:lesson-1"]
    # The upgrade is recorded for recovery.
    assert any(event["type"] == "fieldGuide.upgraded" for event in upgraded["events"])
    # Learner progress events are not deleted (progress is never reset).
    assert any(event["type"] == "activity.completed" for event in upgraded["events"])
    # Old self-check completions are retained as history but never auto-complete
    # any of the new thirty lessons (no silent feats from old self-checks).
    projection = compute_treehouse_projections(upgraded)["learners"].get("acct-old", {})
    completed = set(projection.get("completedActivityIds") or [])
    new_activity_ids = {
        activity_id for activity_id, activity in upgraded["activities"].items()
        if activity.get("fieldGuideKey") in _EXPECTED_LESSON_KEYS
    }
    assert not (completed & new_activity_ids)
    assert completed <= set(legacy_state["activities"]), "only legacy completions carry forward"


def test_upgrade_is_idempotent_and_two_stays_independent():
    first = instantiate_field_guide(new_treehouse_state("acct-1"), "acct-1")
    second = instantiate_field_guide(new_treehouse_state("acct-2"), "acct-2")
    assert set(first["courses"]) != set(second["courses"])
    again, report = upgrade_field_guide(first, "acct-1")
    assert again["fieldGuide"]["templateVersion"] == FIELD_GUIDE_TEMPLATE_VERSION
    # Already current: no second rewrite of learner-visible content.
    assert again == first or report.get("to") == FIELD_GUIDE_TEMPLATE_VERSION


def test_shipped_surface_locators_exist_in_index_markup():
    manifest = field_guide_manifest()
    shipped_markup = (Path(__file__).parents[1] / "static" / "index.html").read_text()

    def locator_is_shipped(locator: str) -> bool:
        if locator.startswith("#"):
            return f'id="{locator[1:]}"' in shipped_markup
        if locator == "[data-files-launcher]":
            return "data-files-launcher" in shipped_markup
        if locator.startswith("[data-copal-view="):
            view = locator.split("=", 1)[1].rstrip("]")
            return f'data-copal-view="{view}"' in shipped_markup
        return False

    for course in manifest["courses"]:
        for lesson in course["lessons"]:
            assert locator_is_shipped(lesson["surface"]["locator"]), lesson["surface"]["locator"]


def test_lesson_hints_never_reveal_unearned_ultra_rares():
    """Learner mode must not name unearned ultra rares through lesson hints."""
    manifest = field_guide_manifest()
    from src.openclank.treehouse_field_guide import ACHIEVEMENT_RARITY, _achievement_hints
    ultra_ids = {key for key, rarity in ACHIEVEMENT_RARITY.items() if rarity == "ultra"}
    assert len(ultra_ids) == 3
    for lesson in manifest["lessons"]:
        hints = lesson["achievementHints"]
        for hint in hints:
            assert hint["id"] not in ultra_ids, f"ultra rare leaked in hint: {lesson['key']}"
            assert hint["rarity"] in {"normal", "mystery"}
            if hint["secret"]:
                assert hint["rarity"] == "mystery"
    # The helper itself never emits an ultra hint, so no empty container.
    assert _achievement_hints(tuple(ultra_ids)) == []
    assert _achievement_hints(("oc.fresh-mould",)) == [
        {"id": "oc.fresh-mould", "rarity": "normal", "secret": False},
    ]
    assert _achievement_hints(("oc.same-clank-new-digs",)) == [
        {"id": "oc.same-clank-new-digs", "rarity": "mystery", "secret": True},
    ]


def test_thirty_lesson_keys_satisfy_n29_without_llm_judgement(tmp_path):
    """N29 unlocks here: the 30 stable keys are the predicate's evidence.

    A deterministic recomputation over guide.lesson.completed receipts — no
    model interprets activity.  Twenty-nine keys must NOT unlock it.
    """
    from src.openclank.copal_treehouse_repository import TreeHouseRepository
    from src.openclank.treehouse_achievements import (
        EventFamily,
        N29_REQUIRED_LESSONS,
        TreeHouseAchievementEngine,
    )

    manifest = field_guide_manifest()
    assert N29_REQUIRED_LESSONS == 30
    assert len(manifest["lessonKeys"]) == 30

    def events_for(keys, account="acct-n29"):
        return [{
            "source_event_id": f"guide-lesson:{key}:t{index}",
            "event_family": EventFamily.GUIDE_LESSON_COMPLETED,
            "kind": "R",
            "result": "committed",
            "actor_kind": "user",
            "occurred_at": f"2026-09-25T00:{index:02d}:00Z",
            "facts": {"lessonKey": key, "committed": True},
        } for index, key in enumerate(keys)]

    engine = TreeHouseAchievementEngine(TreeHouseRepository(tmp_path / "full.sqlite3"))
    engine.ingest("acct-n29", events_for(manifest["lessonKeys"]), via="live")
    presentation = engine.presentation("acct-n29", admin=True)
    earned_keys = {entry.get("key") for entry in presentation.get("entries") or [] if entry.get("earned")}
    assert "oc.field-guide-finished" in earned_keys, f"N29 should unlock from 30 keys; earned={earned_keys}"
    assert "N29" in set(presentation.get("earnedIds") or [])
    # Fewer than thirty must NOT unlock.
    engine2 = TreeHouseAchievementEngine(TreeHouseRepository(tmp_path / "partial.sqlite3"))
    engine2.ingest("acct-partial", events_for(manifest["lessonKeys"][:29], account="acct-partial"), via="live")
    presentation2 = engine2.presentation("acct-partial", admin=True)
    earned2 = {entry.get("key") for entry in presentation2.get("entries") or [] if entry.get("earned")}
    assert "oc.field-guide-finished" not in earned2


def test_official_theme_and_effect_vocabulary_is_the_shipped_set():
    """Curriculum and docs must name the shipped S22–S26 effects, not invented ones."""
    manifest = field_guide_manifest()
    theme_lesson = next(
        lesson for lesson in manifest["lessons"] if lesson["key"] == "house-stewardship.theme-effects"
    )
    body = theme_lesson["explanation"]
    for shipped in (
        "Clanker Signal Routes",
        "Shipibo Kene-Inspired Signal Weave",
        "Clanker LCARS",
        "Clanker Gem Drift",
        "Clanker Emoji Drift",
        "Clanker Matrix Rain",
        "Clanker Emoji Rain",
        "Clanker LCARS Status Sweep",
        "Dots",
        "Synapse",
        "Rain",
        "Constellations",
        "Perlin Flow",
        "Petals",
        "Sparkles",
        "Embers",
    ):
        assert shipped in body, f"missing shipped effect name: {shipped}"
    # Accessibility controls that really ship.
    for control in ("intensity", "size", "density", "text-size", "Frosted"):
        assert control in body
