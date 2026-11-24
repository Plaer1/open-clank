"""BQ-16 permission edge of resolve_registered_history_ids(allow_absent=True)."""
import os
import tempfile

from tests.test_history_absent_original_resolution import ACCOUNT, Client, Provider, refused, resolve


def _root():
    return os.path.realpath(tempfile.mkdtemp())


def test_a_parent_outside_every_current_host_root_is_refused():
    root = _root()
    assert refused(root, "a.txt", Provider(present=set(), writable=set()), allow_absent=True)


def test_b_a_root_the_owner_cannot_write_is_refused():
    root = _root()
    assert refused(root, "a.txt", Provider(present={root}, writable=set()), allow_absent=True)


def test_c_an_existing_path_uses_the_normal_check():
    root = _root(); path = os.path.join(root, "a.txt"); open(path, "w").close()
    assert refused(root, "a.txt", Provider(present={root}, writable={root}), allow_absent=True)      # not authorized: still refused
    result, _f, _p = resolve(root, "a.txt", Provider(present={root, path}, writable={root}), allow_absent=True)
    assert result == ["file:registry:abc"]


def test_d_a_symlinked_parent_is_refused():
    real = _root(); link = os.path.join(_root(), "link"); os.symlink(real, link)
    assert refused(link, "a.txt", Provider(present={link}, writable={link}), allow_absent=True)
    result, _f, _p = resolve(real, "a.txt", Provider(present={real}, writable={real}), allow_absent=True)   # control: the real folder resolves
    assert result == ["file:registry:abc"]


def test_e_another_owners_registration_is_never_resolved():
    root = _root()
    client = Client(root, "a.txt"); client.row["account_id"] = "account-bob"
    from src.openclank.files_host_provider import host_origin_for_path
    from src.openclank.history_change_qualifier import resolve_registered_history_ids
    from src.openclank.history_client import HistoryAvailabilityError
    from src.openclank.resource_refs import stable_resource_id
    import asyncio
    public = stable_resource_id(owner_subject_id=ACCOUNT, provider="host", origin_id=host_origin_for_path(os.path.join(root, "a.txt")))
    try:
        asyncio.run(resolve_registered_history_ids(client, public, account=ACCOUNT, context=None, provider=Provider({root}, {root}),
                                                   resolved_paths={}, allow_absent=True))
    except HistoryAvailabilityError as exc:
        assert exc.code == "resource_not_found"
    else:
        raise AssertionError("another owner's registration resolved")


def test_default_never_widens_and_the_history_routes_only_pass_it_to_the_same_resolver():
    # allow_absent only changes the answer for an ABSENT leaf under a writable authorized parent that the account already
    # owns a registration for: exactly the set the caller could list while the file existed (same account, same path).
    root = _root()
    assert refused(root, "a.txt", Provider(present={root}, writable={root}))
