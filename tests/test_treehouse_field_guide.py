from pathlib import Path

from src.openclank.copal_treehouse import new_treehouse_state, validate_treehouse_state
from src.openclank.treehouse_field_guide import field_guide_manifest, instantiate_field_guide, validate_field_guide_manifest


def test_field_guide_manifest_covers_courses_badges_achievements_and_surfaces():
    manifest = field_guide_manifest()
    assert manifest["templateKey"] == "open-clank-field-guide"
    assert len(manifest["courses"]) == 17
    assert len(manifest["badges"]) == 17
    assert len(manifest["achievements"]) == 6
    assert manifest["coverageContract"]["kind"] == "disposable-mounted"
    assert manifest["coverageContract"]["courseCount"] == 17
    assert Path(__file__).with_name("treehouse_field_guide_browser_acceptance.mjs").exists()
    assert {"fg-models", "fg-automation", "fg-research-media", "fg-communications", "fg-operations"} <= set(manifest["coverage"])
    assert {lesson["surface"]["key"] for course in manifest["courses"] for lesson in course["lessons"]} == {
        "assistant", "automation", "bases", "communications", "continuity", "editor", "files", "graph", "models",
        "operations", "research", "settings", "tasks", "teaching", "timeline", "treehouse", "wiki",
    }
    validate_field_guide_manifest(manifest)
    assert all(
        lesson["practiceFixture"] and lesson["verifier"]
        and lesson["surface"]["href"] and lesson["surface"]["locator"]
        and lesson["practice"]["seed"] and lesson["practice"]["cleanup"]
        and lesson["verifierSpec"]["evidence"]
        and lesson["verifierSpec"]["command"] == "node tests/treehouse_field_guide_browser_acceptance.mjs"
        and len(lesson["verifierSpec"]["assertions"]) >= 3
        for course in manifest["courses"] for lesson in course["lessons"]
    )
    shipped_markup = (Path(__file__).parents[1] / "static" / "index.html").read_text()
    def locator_is_shipped(locator):
        if locator.startswith("#"):
            return f'id="{locator[1:]}"' in shipped_markup
        if locator == "[data-files-launcher]":
            return "data-files-launcher" in shipped_markup
        if locator.startswith("[data-copal-view="):
            view = locator.split("=", 1)[1].rstrip("]")
            return f'data-copal-view="{view}"' in shipped_markup
        return False

    assert all(
        locator_is_shipped(lesson["surface"]["locator"])
        for course in manifest["courses"] for lesson in course["lessons"]
    )


def test_field_guide_instantiation_is_private_and_idempotent():
    state = instantiate_field_guide(new_treehouse_state("acct-a"), "acct-a")
    validate_treehouse_state(state)
    again = instantiate_field_guide(state, "acct-a")
    assert again == state
    assert all(course["ownerId"] == "acct-a" for course in state["courses"].values())
    assert {course["status"] for course in state["courses"].values()} == {"published"}
    assert all(activity.get("surface", {}).get("href") and activity.get("practice", {}).get("seed") for activity in state["activities"].values())
