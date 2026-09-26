from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.openclank.copal_treehouse import apply_treehouse_command, new_treehouse_state
from src.openclank.copal_treehouse_repository import TreeHouseRepository, TreeHouseRepositoryError
from src.openclank.treehouse_field_guide import instantiate_field_guide


def _catalogue():
    state = new_treehouse_state("acct-owner", now=datetime(2026, 1, 1, tzinfo=UTC))
    state["profiles"]["acct-owner"] = {"id": "acct-owner", "displayName": "Owner", "roles": ["admin", "instructor", "learner"], "active": True}
    state["courses"]["course:one"] = {"id": "course:one", "title": "One", "ownerId": "acct-owner", "authorIds": ["acct-owner"], "status": "published", "moduleIds": []}
    return state


def test_grants_are_restart_safe_private_until_accept_and_revoke_cas(tmp_path):
    path = tmp_path / "treehouse.sqlite3"
    repo = TreeHouseRepository(path)
    state = _catalogue()
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    assert repo.accessible_course_refs("acct-recipient", "school") == []

    grant = repo.create_share(
        owner_account_id="acct-owner", workspace_id="school", course_id="course:one",
        recipient_account_id="acct-recipient", role="learn", access_revision=0,
        now="2026-01-01T00:00:00Z", command_id="share-1", payload={"courseId": "course:one"},
    )
    assert repo.access("acct-recipient", "school", "acct-owner", "course:one") is False
    repo.accept_share(recipient_account_id="acct-recipient", workspace_id="school", token=grant["shareToken"], now="2026-01-01T00:01:00Z")
    assert repo.access("acct-recipient", "school", "acct-owner", "course:one") is True
    assert TreeHouseRepository(path).access("acct-recipient", "school", "acct-owner", "course:one") is True

    current = repo.grant(grant["grantId"])
    assert current is not None
    repo.revoke_share(owner_account_id="acct-owner", workspace_id="school", grant_id=grant["grantId"], expected_revision=current["revision"], now="2026-01-01T00:02:00Z")
    assert repo.access("acct-recipient", "school", "acct-owner", "course:one") is False
    with pytest.raises(TreeHouseRepositoryError) as stale:
        repo.put_catalogue("acct-owner", "school", state, expected_revision=0, access_grant_id=grant["grantId"], access_revision=current["revision"])
    assert stale.value.code == "stale_access"


def test_changed_payload_replay_is_rejected_and_private_course_ids_do_not_disclose(tmp_path):
    repo = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    state = _catalogue()
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    first = repo.create_share(owner_account_id="acct-owner", workspace_id="school", course_id="course:one", recipient_account_id="acct-recipient", role="learn", access_revision=0, now="now", command_id="same", payload={"recipient": "acct-recipient"})
    with pytest.raises(TreeHouseRepositoryError) as conflict:
        repo.create_share(owner_account_id="acct-owner", workspace_id="school", course_id="course:one", recipient_account_id="acct-third", role="learn", access_revision=0, now="now", command_id="same", payload={"recipient": "acct-third"})
    assert conflict.value.code == "idempotency_conflict"
    assert repo.owner_for_course("acct-third", "school", "course.with.dots:private") is None
    assert repo.grant(first["grantId"])["recipient_account_id"] == "acct-recipient"


def test_progress_storage_revision_and_grant_epoch_are_guarded_atomically(tmp_path):
    repo = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    state = _catalogue()
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    share = repo.create_share(
        owner_account_id="acct-owner", workspace_id="school", course_id="course:one",
        recipient_account_id="acct-recipient", role="learn", access_revision=0,
        now="2026-01-01T00:00:00Z", command_id="share-race", payload={"courseId": "course:one"},
    )
    repo.accept_share(recipient_account_id="acct-recipient", workspace_id="school", token=share["shareToken"], now="2026-01-01T00:01:00Z")
    grant = repo.grant(share["grantId"])
    assert grant is not None
    guard = {"grant_id": share["grantId"], "access_revision": grant["revision"], "expected_reset_epoch": 0, "reset_epoch": 0}

    def write(marker):
        try:
            return repo.put_progress(
                "acct-recipient", "acct-owner", "school", "course:one",
                {"marker": marker}, expected_revision=0, **guard,
            )
        except TreeHouseRepositoryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, ("first", "second")))
    assert sorted(outcomes, key=str) == [1, "stale"]
    progress, revision = repo.get_progress("acct-recipient", "acct-owner", "school", "course:one")
    assert revision == 1 and progress["marker"] in {"first", "second"}

    current = repo.grant(share["grantId"])
    repo.revoke_share(owner_account_id="acct-owner", workspace_id="school", grant_id=share["grantId"], expected_revision=current["revision"], now="2026-01-01T00:02:00Z")
    with pytest.raises(TreeHouseRepositoryError) as revoked:
        repo.put_progress(
            "acct-recipient", "acct-owner", "school", "course:one", {"marker": "late"},
            expected_revision=1, grant_id=share["grantId"], access_revision=grant["revision"],
            expected_reset_epoch=0, reset_epoch=0,
        )
    assert revoked.value.code == "stale_access"

    with pytest.raises(TreeHouseRepositoryError) as reset:
        repo.put_progress(
            "acct-recipient", "acct-owner", "school", "course:one", {"marker": "old"},
            expected_revision=1, expected_reset_epoch=1, reset_epoch=1,
        )
    assert reset.value.code == "stale_attempt"


def test_first_catalogue_initialization_returns_one_committed_winner(tmp_path):
    repo = TreeHouseRepository(tmp_path / "treehouse.sqlite3")

    def initialize(owner):
        state = new_treehouse_state(owner)
        return repo.create_catalogue_if_absent("acct-owner", "school", state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(initialize, ("first", "second")))
    assert sorted(bool(created) for _state, _revision, created in outcomes) == [False, True]
    winner, revision = repo.get_catalogue("acct-owner", "school")
    assert winner is not None and winner["profiles"]["owner"]["displayName"] in {"first", "second"}
    assert revision == int(winner["revision"]) == 0


def test_learner_reset_scrubs_revoked_rows_across_restart_and_fences_reshare(tmp_path):
    path = tmp_path / "treehouse.sqlite3"
    repo = TreeHouseRepository(path)
    state = _catalogue()
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    first = repo.create_share(owner_account_id="acct-owner", workspace_id="school", course_id="course:one", recipient_account_id="acct-learner", role="learn", access_revision=0, now="now", command_id="share-one", payload={"courseId": "course:one"})
    repo.accept_share(recipient_account_id="acct-learner", workspace_id="school", token=first["shareToken"], now="now")
    accepted = repo.grant(first["grantId"])
    assert accepted is not None
    repo.put_progress("acct-learner", "acct-owner", "school", "course:one", {"profiles": {"acct-learner": {"id": "acct-learner"}}, "submissions": {"old": {"profileId": "acct-learner"}}}, expected_revision=0, grant_id=first["grantId"], access_revision=accepted["revision"], expected_reset_epoch=0, reset_epoch=0)
    repo.revoke_share(owner_account_id="acct-owner", workspace_id="school", grant_id=first["grantId"], expected_revision=accepted["revision"], now="now")

    reset = repo.reset_progress_all("acct-learner", "school", [], command_id="reset-all", payload={"type": "progress.reset", "payload": {}})
    assert reset["generation"] == 1
    cleared, revision = TreeHouseRepository(path).get_progress("acct-learner", "acct-owner", "school", "course:one")
    assert revision == 2 and cleared["submissions"] == {} and cleared["events"] == []

    reshared = repo.create_share(owner_account_id="acct-owner", workspace_id="school", course_id="course:one", recipient_account_id="acct-learner", role="learn", access_revision=0, now="now", command_id="share-two", payload={"courseId": "course:one"})
    repo.accept_share(recipient_account_id="acct-learner", workspace_id="school", token=reshared["shareToken"], now="now")
    current = repo.grant(reshared["grantId"])
    assert current is not None
    with pytest.raises(TreeHouseRepositoryError, match="reset") as stale:
        repo.put_progress("acct-learner", "acct-owner", "school", "course:one", {"marker": "old-attempt"}, expected_revision=2, grant_id=reshared["grantId"], access_revision=current["revision"], expected_reset_epoch=0, reset_epoch=0)
    assert stale.value.code == "stale_attempt"


def test_accepted_share_reset_order_is_fenced_after_restart(tmp_path):
    repo = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    state = _catalogue()
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    share = repo.create_share(owner_account_id="acct-owner", workspace_id="school", course_id="course:one", recipient_account_id="acct-learner", role="learn", access_revision=0, now="now", command_id="share", payload={"courseId": "course:one"})
    repo.accept_share(recipient_account_id="acct-learner", workspace_id="school", token=share["shareToken"], now="now")
    grant = repo.grant(share["grantId"])
    assert grant is not None
    repo.reset_progress_all("acct-learner", "school", [], command_id="reset", payload={"type": "progress.reset", "payload": {}})
    restarted = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    with pytest.raises(TreeHouseRepositoryError) as stale:
        restarted.put_progress("acct-learner", "acct-owner", "school", "course:one", {"marker": "captured-before-reset"}, expected_revision=0, grant_id=share["grantId"], access_revision=grant["revision"], expected_reset_epoch=0, reset_epoch=0)
    assert stale.value.code == "stale_attempt"


def test_non_first_field_guide_share_grants_prerequisite_closure_and_revoke_closes_it(tmp_path):
    repo = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    state = instantiate_field_guide(new_treehouse_state("acct-owner"), "acct-owner")
    # Built-in Field Guide Classes are free exploration: no prerequisite locks,
    # so a share grants exactly the shared Class (no closure to walk).
    assert all(not course.get("prerequisites") for course in state["courses"].values() if course.get("fieldGuideKey"))
    first = next(course_id for course_id, course in state["courses"].items() if course.get("fieldGuideKey") == "house-collaborate")
    second = next(course_id for course_id, course in state["courses"].items() if course.get("fieldGuideKey") == "house-documents")
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    share = repo.create_share(owner_account_id="acct-owner", workspace_id="school", course_id=second, recipient_account_id="acct-learner", role="learn", access_revision=state["revision"], now="now", command_id="share-non-first", payload={"courseId": second})
    repo.accept_share(recipient_account_id="acct-learner", workspace_id="school", token=share["shareToken"], now="now")
    assert {ref["courseId"] for ref in repo.accessible_course_refs("acct-learner", "school")} == {second}
    grant = repo.grant(share["grantId"])
    assert grant is not None
    repo.revoke_share(owner_account_id="acct-owner", workspace_id="school", grant_id=share["grantId"], expected_revision=grant["revision"], now="now")
    assert repo.accessible_course_refs("acct-learner", "school") == []
    # User-authored Classes keep prerequisite support; sharing one still grants
    # its prerequisite closure and revoke closes the whole closure.
    state, _, _ = apply_treehouse_command(state, {"type": "course.create", "payload": {"id": "course:authored-base", "title": "Authored base"}}, actor_id="acct-owner", command_id="ac-1", expected_revision=state["revision"])
    state, _, _ = apply_treehouse_command(state, {"type": "course.create", "payload": {"id": "course:authored-next", "title": "Authored next"}}, actor_id="acct-owner", command_id="ac-2", expected_revision=state["revision"])
    state["courses"]["course:authored-next"]["prerequisites"] = ["course:authored-base"]
    state["courses"]["course:authored-base"]["status"] = "published"
    state["courses"]["course:authored-next"]["status"] = "published"
    repo.put_catalogue("acct-owner", "school", state, expected_revision=None)
    share = repo.create_share(owner_account_id="acct-owner", workspace_id="school", course_id="course:authored-next", recipient_account_id="acct-learner", role="learn", access_revision=state["revision"], now="now", command_id="share-authored", payload={"courseId": "course:authored-next"})
    repo.accept_share(recipient_account_id="acct-learner", workspace_id="school", token=share["shareToken"], now="now")
    assert {ref["courseId"] for ref in repo.accessible_course_refs("acct-learner", "school")} == {"course:authored-base", "course:authored-next"}
    grant = repo.grant(share["grantId"])
    repo.revoke_share(owner_account_id="acct-owner", workspace_id="school", grant_id=share["grantId"], expected_revision=grant["revision"], now="now")
    assert repo.accessible_course_refs("acct-learner", "school") == []
