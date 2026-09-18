from __future__ import annotations

import json
from dataclasses import replace

import pytest

import src.secret_storage as secret_storage
from src.openclank.files_facade import (
    FilesFacade,
    FilesFacadeError,
    ProviderContext,
    ProviderContent,
    ProviderPage,
    ProviderResource,
)
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.resource_refs import issue_resource_ref, resolve_resource_ref
from src.openclank.files_host_provider import HostFilesProvider, _origin
from src.openclank.files_service_client import FilesServiceError


@pytest.fixture(autouse=True)
def isolated_app_key(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    yield
    secret_storage._fernet = None


class FakeProvider:
    name = "gallery"

    def __init__(self):
        self.calls = []
        self.stat_calls = []

    async def roots(self, context):
        return [ProviderResource("photos", "Photos", "provider_root", ("children", "stat"))]

    async def children(self, context, *, parent_origin_id, cursor, snapshot, limit, sort, query):
        self.calls.append((context, parent_origin_id, cursor, snapshot, limit, sort, query))
        if snapshot not in {None, "gallery-1"}:
            raise RuntimeError("stale snapshot")
        offset = int(cursor or 0)
        rows = tuple(
            ProviderResource(
                f"image-{index}",
                f"Photo {index}",
                "image",
                ("stat", "open", "preview", "download", "favorite"),
                parent_origin_id="photos",
                mime_type="image/png",
                size=100 + index,
                preview_kind="image",
            )
            for index in range(offset, min(offset + limit, 3))
        )
        next_cursor = str(offset + len(rows)) if offset + len(rows) < 3 else None
        return ProviderPage(rows, next_cursor=next_cursor, total=3, snapshot="gallery-1")

    async def stat(self, context, *, origin_id):
        self.stat_calls.append((context, origin_id))
        if origin_id == "photos":
            return ProviderResource("photos", "Photos", "provider_root", ("children", "stat"))
        return ProviderResource(
            origin_id,
            "Photo",
            "image",
            ("stat", "open", "preview", "favorite"),
            parent_origin_id="photos",
            provenance={"provider_id": "must-not-leak"},
            open_target={"app": "gallery", "legacy_url": "/gallery/must-not-leak"},
            thumbnail_url="/legacy/thumbnail/must-not-leak",
            preview_kind="image",
        )

    async def search(self, context, *, query, limit, sort):
        self.calls.append((context, "search", query, limit, sort))
        return ProviderPage(tuple(
            ProviderResource(
                f"search-{index}",
                f"{query} {index}",
                "image",
                ("stat", "open", "preview", "download"),
                mime_type="image/png",
                preview_kind="image",
            )
            for index in range(min(limit, 3))
        ), total=3, snapshot="search-1")

    async def content(self, context, *, origin_id):
        return ProviderContent(
            origin_id=origin_id,
            filename="photo.png",
            media_type="image/png",
            data=b"png",
            size=3,
        )

    async def action(self, context, *, origin_id, action, args):
        assert action == "favorite.set"
        assert set(args) == {"value"}
        return ProviderResource(
            origin_id,
            "Photo",
            "image",
            ("stat", "open", "preview", "favorite"),
            provenance={"favorite": bool(args["value"]), "private_id": "must-not-leak"},
            open_target={"app": "gallery"},
        )


def _context(**values):
    defaults = {
        "owner_subject_id": "account-alice",
        "owner_username": "alice",
        "policy_generation": 4,
    }
    defaults.update(values)
    return ProviderContext(**defaults)


@pytest.mark.asyncio
async def test_facade_pages_provider_without_exposing_origin_ids():
    provider = FakeProvider()
    facade = FilesFacade([provider])
    roots = await facade.roots(_context())
    root = roots["entries"][0]
    assert root["name"] == "Photos"
    assert "photos" not in root["ref"]

    first = await facade.children(
        _context(),
        parent_ref=root["ref"],
        limit=2,
        sort={"key": "name", "direction": "asc", "directories_first": True},
    )
    assert [entry["name"] for entry in first["entries"]] == ["Photo 0", "Photo 1"]
    assert all("origin_id" not in entry for entry in first["entries"])
    assert first["next_cursor"].startswith("fc1.")

    second = await facade.children(
        _context(),
        parent_ref=root["ref"],
        cursor=first["next_cursor"],
        limit=2,
        sort={"key": "name", "direction": "asc", "directories_first": True},
    )
    assert [entry["name"] for entry in second["entries"]] == ["Photo 2"]
    assert provider.calls[-1][2:4] == ("2", "gallery-1")


@pytest.mark.asyncio
async def test_refs_and_cursors_cannot_cross_owner_policy_or_query():
    facade = FilesFacade([FakeProvider()])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    page = await facade.children(context, parent_ref=root["ref"], limit=1, query="cat")

    with pytest.raises(FilesFacadeError) as wrong_owner:
        await facade.children(_context(owner_subject_id="account-bob"), parent_ref=root["ref"])
    assert wrong_owner.value.code == "resource_unavailable"

    with pytest.raises(FilesFacadeError) as stale_ref:
        await facade.children(replace(context, policy_generation=5), parent_ref=root["ref"])
    assert stale_ref.value.code == "resource_ref_stale"

    with pytest.raises(FilesFacadeError) as wrong_query:
        await facade.children(
            context,
            parent_ref=root["ref"],
            cursor=page["next_cursor"],
            limit=1,
            query="dog",
        )
    assert wrong_query.value.code == "stale_cursor"


@pytest.mark.asyncio
async def test_folder_sort_capabilities_are_public_enforced_and_cursor_bound():
    class NegotiatedProvider(FakeProvider):
        sort_keys = ("name", "modified")

        def supported_sort_keys(self, *, parent_origin_id):
            assert parent_origin_id == "photos"
            return self.sort_keys

        async def roots(self, context):
            return [ProviderResource(
                "photos",
                "Photos",
                "provider_root",
                ("children", "stat"),
                child_sort_keys=("name", "modified"),
            )]

    provider = NegotiatedProvider()
    facade = FilesFacade([provider])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    assert root["sort_keys"] == ["name", "modified"]

    for key in root["sort_keys"]:
        for direction in ("asc", "desc"):
            spec = {"key": key, "direction": direction, "directories_first": False}
            first = await facade.children(context, parent_ref=root["ref"], limit=1, sort=spec)
            assert first["sort"] == spec
            assert first["sort_keys"] == ["name", "modified"]
            assert first["next_cursor"]
            second = await facade.children(
                context,
                parent_ref=root["ref"],
                cursor=first["next_cursor"],
                limit=1,
                sort=spec,
            )
            assert len(second["entries"]) == 1
            with pytest.raises(FilesFacadeError) as stale:
                await facade.children(
                    context,
                    parent_ref=root["ref"],
                    cursor=first["next_cursor"],
                    limit=1,
                    sort={**spec, "direction": "desc" if direction == "asc" else "asc"},
                )
            assert stale.value.code == "stale_cursor"

    contract_page = await facade.children(
        context, parent_ref=root["ref"], limit=1, sort={"key": "name"},
    )
    provider.sort_keys = ("name", "size")
    with pytest.raises(FilesFacadeError) as changed_contract:
        await facade.children(
            context,
            parent_ref=root["ref"],
            cursor=contract_page["next_cursor"],
            limit=1,
            sort={"key": "name"},
        )
    assert changed_contract.value.code == "stale_cursor"
    provider.sort_keys = ("name", "modified")

    with pytest.raises(FilesFacadeError) as unsupported:
        await facade.children(context, parent_ref=root["ref"], sort={"key": "size"})
    assert unsupported.value.code == "unsupported_sort"
    assert not any(call[-2]["key"] == "size" for call in provider.calls)


@pytest.mark.asyncio
async def test_provider_wide_search_seals_results_and_isolates_failed_provider():
    class FailedLibraryProvider:
        name = "library"

        async def roots(self, context):
            return []

        async def search(self, context, *, query, limit, sort):
            raise RuntimeError("private backend detail")

    provider = FakeProvider()
    facade = FilesFacade([provider, FailedLibraryProvider()])
    result = await facade.search(
        _context(),
        query="needle",
        limit=2,
        sort={"key": "name", "direction": "asc", "directories_first": True},
    )
    assert [row["name"] for row in result["entries"]] == ["needle 0", "needle 1"]
    assert all(row["ref"].startswith("rr1.") for row in result["entries"])
    assert "search-" not in json.dumps(result["entries"])
    assert result["providers"] == {
        "gallery": {"available": True},
        "library": {"available": False, "code": "provider_unavailable"},
    }
    assert "private backend detail" not in json.dumps(result)


@pytest.mark.asyncio
async def test_provider_wide_search_honors_every_advertised_sort_mode():
    class SearchProvider(FakeProvider):
        def __init__(self, name, rows):
            super().__init__()
            self.name = name
            self.rows = tuple(rows)

        async def search(self, context, *, query, limit, sort):
            return ProviderPage(self.rows[:limit], total=len(self.rows), snapshot=f"{self.name}-search")

    facade = FilesFacade([
        SearchProvider("gallery", [
            ProviderResource("g-z", "Zulu", "image", ("stat",), sort_kind="image", size=30, modified_unix_ms=10),
            ProviderResource("g-a", "Alpha", "file", ("stat",), sort_kind="audio", size=10, modified_unix_ms=30),
        ]),
        SearchProvider("library", [
            ProviderResource("l-m", "Middle", "document", ("stat",), sort_kind="markdown", size=20, modified_unix_ms=20),
        ]),
    ])
    field = {
        "name": lambda row: row["name"].casefold(),
        "kind": lambda row: row["sort_kind"].casefold(),
        "size": lambda row: row["size"],
        "modified": lambda row: row["modified_unix_ms"],
    }
    for key in ("name", "kind", "modified", "size"):
        for direction in ("asc", "desc"):
            result = await facade.search(
                _context(), query="needle", limit=10,
                sort={"key": key, "direction": direction, "directories_first": False},
            )
            assert result["sort_keys"] == ["name", "kind", "modified", "size"]
            assert result["sort"] == {"key": key, "direction": direction, "directories_first": False}
            values = [field[key](row) for row in result["entries"]]
            assert values == sorted(values, reverse=direction == "desc")


@pytest.mark.asyncio
async def test_one_failed_provider_does_not_erase_or_count_peers():
    class Broken(FakeProvider):
        name = "copal"

        async def roots(self, context):
            raise RuntimeError("private record count 123")

    facade = FilesFacade([FakeProvider(), Broken()])
    result = await facade.roots(_context())
    assert [row["name"] for row in result["entries"]] == ["Photos"]
    assert result["providers"]["gallery"] == {"available": True}
    assert result["providers"]["copal"] == {
        "available": False,
        "code": "provider_unavailable",
    }


@pytest.mark.asyncio
async def test_stat_reissues_current_generation_ref_and_checks_capability():
    facade = FilesFacade([FakeProvider()])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    page = await facade.children(context, parent_ref=root["ref"], limit=1)
    photo = page["entries"][0]
    stat = await facade.stat(context, resource_ref=photo["ref"])
    assert stat["id"] == photo["id"]
    assert stat["policy_generation"] == context.policy_generation

    open_only = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-open-only",
        kind="image",
        capabilities=("open",),
        policy_generation=context.policy_generation,
    )
    with pytest.raises(FilesFacadeError) as denied:
        await facade.stat(context, resource_ref=open_only.token)
    assert denied.value.code == "resource_unavailable"


@pytest.mark.asyncio
async def test_reveal_restats_parent_and_returns_only_fresh_refs():
    provider = FakeProvider()
    facade = FilesFacade([provider])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    photo = (await facade.children(context, parent_ref=root["ref"], limit=1))["entries"][0]
    result = await facade.reveal(context, resource_ref=photo["ref"])
    assert result["provider"] == "gallery"
    assert [entry["name"] for entry in result["ancestors"]] == ["Photos"]
    assert result["parent"]["name"] == "Photos"
    assert result["parent"]["capabilities"] == ["children", "stat"]
    assert result["resource"]["name"] == "Photo"
    assert result["resource"]["id"] != result["parent"]["id"]
    serialized = json.dumps(result)
    assert "image-0" not in serialized
    assert "photos" not in result["parent"]["ref"]
    assert [call[1] for call in provider.stat_calls[-2:]] == ["image-0", "photos"]


@pytest.mark.asyncio
async def test_reveal_returns_root_to_parent_chain_with_opaque_parent_links():
    class Nested(FakeProvider):
        async def stat(self, context, *, origin_id):
            self.stat_calls.append((context, origin_id))
            resources = {
                "image-0": ProviderResource(
                    "image-0", "Photo", "image", ("stat", "open", "preview"),
                    parent_origin_id="album-1", preview_kind="image", open_target={"app": "gallery"},
                ),
                "album-1": ProviderResource(
                    "album-1", "Trip", "album", ("children", "stat", "open"),
                    parent_origin_id="albums", open_target={"app": "gallery"},
                ),
                "albums": ProviderResource(
                    "albums", "Albums", "virtual_folder", ("children", "stat"),
                    parent_origin_id="root",
                ),
                "root": ProviderResource("root", "Gallery", "provider_root", ("children", "stat")),
            }
            return resources[origin_id]

    context = _context()
    expired_locator = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-0",
        kind="image",
        capabilities=("stat", "open", "preview"),
        policy_generation=context.policy_generation,
    )
    facade = FilesFacade([Nested()])
    result = await facade.reveal(context, resource_ref=expired_locator.token)
    assert [entry["name"] for entry in result["ancestors"]] == ["Gallery", "Albums", "Trip"]
    assert result["parent"]["id"] == result["ancestors"][-1]["id"]
    parent_ids = [None, result["ancestors"][0]["id"], result["ancestors"][1]["id"]]
    for public, expected_parent in zip(result["ancestors"], parent_ids):
        decoded = resolve_resource_ref(
            public["ref"],
            expected_owner_subject_id=context.owner_subject_id,
            current_policy_generation=context.policy_generation,
        )
        assert decoded.parent_stable_id == expected_parent
    target = resolve_resource_ref(
        result["resource"]["ref"],
        expected_owner_subject_id=context.owner_subject_id,
        current_policy_generation=context.policy_generation,
    )
    assert target.parent_stable_id == result["parent"]["id"]
    assert "image-0" not in json.dumps(result)


@pytest.mark.asyncio
async def test_exact_reissue_accepts_expired_managed_identity_but_reauthorizes_current_record():
    context = _context()
    expired = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-0",
        kind="image",
        capabilities=("stat", "open", "preview"),
        policy_generation=context.policy_generation - 1,
        ttl_seconds=1,
        now_unix_ms=1,
    )
    facade = FilesFacade([FakeProvider()])
    renewed = await facade.reissue_exact(context, resource_ref=expired.token)
    assert renewed["resource"]["id"] == expired.stable_id
    assert renewed["resource"]["policy_generation"] == context.policy_generation
    assert renewed["resource"]["ref"] != expired.token
    assert "image-0" not in json.dumps(renewed)

    with pytest.raises(FilesFacadeError) as wrong_owner:
        await facade.reissue_exact(
            _context(owner_subject_id="account-bob"),
            resource_ref=expired.token,
        )
    assert wrong_owner.value.code == "resource_unavailable"

    host = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="host",
        origin_id="host:v1:sealed",
        kind="file",
        capabilities=("stat", "open"),
        policy_generation=context.policy_generation,
    )
    with pytest.raises(FilesFacadeError) as host_denied:
        await facade.reissue_exact(context, resource_ref=host.token)
    assert host_denied.value.code == "resource_unavailable"

    open_only = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-0",
        kind="image",
        capabilities=("open",),
        policy_generation=context.policy_generation,
    )
    with pytest.raises(FilesFacadeError) as capability_denied:
        await facade.reissue_exact(context, resource_ref=open_only.token)
    assert capability_denied.value.code == "resource_unavailable"


@pytest.mark.asyncio
async def test_watch_requires_current_owner_bound_folder_capability_and_returns_no_origin():
    class WatchProvider(FakeProvider):
        async def roots(self, context):
            return [ProviderResource("watched-private-origin", "Watched", "folder", ("children", "stat", "watch"))]

        async def stat(self, context, *, origin_id):
            assert origin_id == "watched-private-origin"
            return ProviderResource(origin_id, "Watched", "folder", ("children", "stat", "watch"))

        async def watch(self, context, *, origin_id):
            assert origin_id == "watched-private-origin"
            yield {"sequence": 0, "kind": "modified", "rescan_required": False, "observed_unix_ms": 1}

    facade = FilesFacade([WatchProvider()])
    context = _context()
    resource = (await facade.roots(context))["entries"][0]
    events = await facade.watch(context, resource_ref=resource["ref"])
    event = await anext(events)
    assert event == {"sequence": 0, "kind": "modified", "rescan_required": False, "observed_unix_ms": 1}
    assert "origin" not in json.dumps(event)

    with pytest.raises(FilesFacadeError) as cross_owner:
        await facade.watch(_context(owner_subject_id="account-bob"), resource_ref=resource["ref"])
    assert cross_owner.value.code == "resource_unavailable"

    stale = replace(context, policy_generation=context.policy_generation + 1)
    with pytest.raises(FilesFacadeError) as stale_ref:
        await facade.watch(stale, resource_ref=resource["ref"])
    assert stale_ref.value.code == "resource_ref_stale"


@pytest.mark.asyncio
async def test_content_requires_download_and_rejects_provider_identity_mismatch():
    facade = FilesFacade([FakeProvider()])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    photo = (await facade.children(context, parent_ref=root["ref"], limit=1))["entries"][0]
    content = await facade.content(context, resource_ref=photo["ref"])
    assert content.data == b"png"
    assert content.filename == "photo.png"

    preview_only = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-preview-only",
        kind="image",
        capabilities=("preview",),
        policy_generation=context.policy_generation,
    )
    with pytest.raises(FilesFacadeError) as denied:
        await facade.content(context, resource_ref=preview_only.token)
    assert denied.value.code == "resource_unavailable"
    preview = await facade.content(context, resource_ref=preview_only.token, capability="preview")
    assert preview.data == b"png"

    class Mismatch(FakeProvider):
        async def content(self, context, *, origin_id):
            return ProviderContent("other-image", "photo.png", "image/png", data=b"png")

    mismatched = FilesFacade([Mismatch()])
    mismatch_root = (await mismatched.roots(context))["entries"][0]
    mismatch_photo = (await mismatched.children(context, parent_ref=mismatch_root["ref"], limit=1))["entries"][0]
    with pytest.raises(FilesFacadeError) as invalid:
        await mismatched.content(context, resource_ref=mismatch_photo["ref"])
    assert invalid.value.code == "provider_unavailable"


@pytest.mark.asyncio
async def test_open_rechecks_provider_and_returns_only_trusted_app_with_refreshed_ref():
    provider = FakeProvider()
    facade = FilesFacade([provider])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    photo = (await facade.children(context, parent_ref=root["ref"], limit=1))["entries"][0]

    result = await facade.open(context, resource_ref=photo["ref"])

    assert provider.stat_calls[-1][1] == "image-0"
    assert result["action"] == "open"
    assert result["target"] == {"app": "gallery"}
    assert result["resource"]["id"] == photo["id"]
    assert result["resource"]["ref"] != photo["ref"]
    assert result["resource"]["policy_generation"] == context.policy_generation
    serialized = repr(result)
    assert "must-not-leak" not in serialized
    assert "origin_id" not in serialized
    assert "legacy_url" not in serialized


@pytest.mark.asyncio
async def test_idempotent_action_rechecks_capability_and_returns_only_refreshed_resource():
    provider = FakeProvider()
    facade = FilesFacade([provider])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    photo = (await facade.children(context, parent_ref=root["ref"], limit=1))["entries"][0]

    result = await facade.action(
        context,
        resource_ref=photo["ref"],
        action="favorite.set",
        args={"value": True},
    )

    assert provider.stat_calls[-1][1] == "image-0"
    assert result["action"] == "favorite.set"
    assert result["state"] == {"favorite": True}
    assert result["resource"]["ref"] != photo["ref"]
    assert result["resource"]["provenance"]["favorite"] is True
    assert "must-not-leak" not in json.dumps(result)

    with pytest.raises(FilesFacadeError) as invalid_args:
        await facade.action(
            context,
            resource_ref=photo["ref"],
            action="favorite.set",
            args={"value": "true"},
        )
    assert invalid_args.value.code == "invalid_resource_request"

    without_favorite = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-0",
        kind="image",
        capabilities=("stat",),
        policy_generation=context.policy_generation,
    )
    with pytest.raises(FilesFacadeError) as denied:
        await facade.action(
            context,
            resource_ref=without_favorite.token,
            action="favorite.set",
            args={"value": True},
        )
    assert denied.value.code == "resource_unavailable"


@pytest.mark.asyncio
async def test_places_persist_opaque_origin_without_creating_authority(tmp_path):
    repository = FilePolicyRepository(tmp_path / "places.db")
    provider = FakeProvider()
    facade = FilesFacade([provider], place_repository=repository)
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    photo = (await facade.children(context, parent_ref=root["ref"], limit=1))["entries"][0]
    generation = repository.generation()

    saved = await facade.save_place(context, resource_ref=photo["ref"])
    saved_again = await facade.save_place(context, resource_ref=saved["resource"]["ref"])
    assert saved_again["resource"]["place_id"] == saved["resource"]["place_id"]
    assert repository.generation() == generation
    listed = await facade.places(context)
    assert len(listed["entries"]) == 1
    assert listed["entries"][0]["id"] == photo["id"]
    assert listed["entries"][0]["ref"].startswith("rr1.")
    assert "image-0" not in json.dumps(listed)

    assert (await facade.places(_context(
        owner_subject_id="account-bob", owner_username="bob",
    )))["entries"] == []
    with pytest.raises(FilesFacadeError) as denied:
        await facade.remove_place(
            _context(owner_subject_id="account-bob", owner_username="bob"),
            place_id=saved["resource"]["place_id"],
        )
    assert denied.value.code == "resource_unavailable"
    removed = await facade.remove_place(context, place_id=saved["resource"]["place_id"])
    assert removed == {"version": 1, "removed": True}
    assert (await facade.places(context))["entries"] == []


@pytest.mark.asyncio
async def test_recents_touch_only_after_open_or_content_and_restat_before_listing(tmp_path):
    class RevalidatingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.revoked = set()

        async def stat(self, context, *, origin_id):
            if origin_id in self.revoked:
                self.stat_calls.append((context, origin_id))
                raise RuntimeError("private revocation detail")
            return await super().stat(context, origin_id=origin_id)

    repository = FilePolicyRepository(tmp_path / "recents.db")
    provider = RevalidatingProvider()
    facade = FilesFacade([provider], place_repository=repository)
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    photos = (await facade.children(context, parent_ref=root["ref"], limit=2))["entries"]

    # Merely browsing does not record authority-bearing or speculative history.
    assert (await facade.recents(context))["entries"] == []
    await facade.open(context, resource_ref=photos[0]["ref"])
    await facade.content(context, resource_ref=photos[1]["ref"])

    listed = await facade.recents(context)
    assert {entry["id"] for entry in listed["entries"]} == {
        photos[0]["id"],
        photos[1]["id"],
    }
    assert all(entry["ref"].startswith("rr1.") for entry in listed["entries"])
    assert all(entry["recent_id"].startswith("recent-") for entry in listed["entries"])
    serialized = json.dumps(listed)
    assert "image-0" not in serialized
    assert "image-1" not in serialized
    assert "origin_id" not in serialized

    # Recents are owner metadata and never cross an account boundary.
    bob = _context(owner_subject_id="account-bob", owner_username="bob")
    assert (await facade.recents(bob))["entries"] == []
    assert await facade.clear_recents(bob) == {"version": 1, "removed": 0}
    assert len((await facade.recents(context))["entries"]) == 2

    # Listing re-stats through the current provider; a revoked/missing row is
    # omitted without exposing whether its private origin still exists.
    provider.stat_calls.clear()
    provider.revoked.add("image-0")
    after_revocation = await facade.recents(context)
    assert [entry["id"] for entry in after_revocation["entries"]] == [photos[1]["id"]]
    assert {origin for _call_context, origin in provider.stat_calls} == {"image-0", "image-1"}
    assert "private revocation detail" not in json.dumps(after_revocation)

    assert await facade.clear_recents(context) == {"version": 1, "removed": 2}
    assert (await facade.recents(context))["entries"] == []


@pytest.mark.asyncio
async def test_saved_search_facade_persists_only_owner_scoped_query_recipes(tmp_path):
    repository = FilePolicyRepository(tmp_path / "searches.db")
    facade = FilesFacade([FakeProvider()], place_repository=repository)
    alice = _context()
    bob = _context(owner_subject_id="account-bob", owner_username="bob")

    saved = await facade.save_search(
        alice,
        name="  Latest photos  ",
        provider="gallery",
        query="  sunset  ",
        sort={"key": "modified", "direction": "desc", "directories_first": False},
    )
    search_id = saved["search"]["id"]
    assert search_id.startswith("search-")
    assert saved["search"]["name"] == "Latest photos"
    assert saved["search"]["query"] == "sunset"
    assert set(saved["search"]) == {
        "id", "name", "provider", "query", "sort", "updated_unix_ms",
    }
    assert not {"ref", "resource", "results", "capabilities"} & set(saved["search"])
    assert (await facade.saved_searches(bob))["entries"] == []

    with pytest.raises(FilesFacadeError) as cross_owner:
        await facade.remove_saved_search(bob, search_id=search_id)
    assert cross_owner.value.code == "resource_unavailable"
    assert [row["id"] for row in (await facade.saved_searches(alice))["entries"]] == [search_id]

    with pytest.raises(FilesFacadeError) as unavailable_provider:
        await facade.save_search(
            alice,
            name="Raw",
            provider="raw-filesystem",
            query="secret",
            sort={},
        )
    assert unavailable_provider.value.code == "invalid_resource_request"
    with pytest.raises(FilesFacadeError) as empty_query:
        await facade.save_search(
            alice,
            name="Empty",
            provider="all",
            query="   ",
            sort={},
        )
    assert empty_query.value.code == "invalid_saved_search"

    assert await facade.remove_saved_search(alice, search_id=search_id) == {
        "version": 1,
        "removed": True,
    }
    assert (await facade.saved_searches(alice))["entries"] == []


@pytest.mark.asyncio
async def test_open_checks_sealed_capability_before_provider_stat_and_rejects_provider_drift():
    provider = FakeProvider()
    facade = FilesFacade([provider])
    context = _context()
    open_ref = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-authorized",
        kind="image",
        capabilities=("open",),
        policy_generation=context.policy_generation,
    )
    with pytest.raises(FilesFacadeError) as wrong_owner:
        await facade.open(
            replace(context, owner_subject_id="account-bob"),
            resource_ref=open_ref.token,
        )
    assert wrong_owner.value.code == "resource_unavailable"
    with pytest.raises(FilesFacadeError) as stale_generation:
        await facade.open(
            replace(context, policy_generation=context.policy_generation + 1),
            resource_ref=open_ref.token,
        )
    assert stale_generation.value.code == "resource_ref_stale"
    assert provider.stat_calls == []

    without_open = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-no-open",
        kind="image",
        capabilities=("stat",),
        policy_generation=context.policy_generation,
    )
    with pytest.raises(FilesFacadeError) as denied:
        await facade.open(context, resource_ref=without_open.token)
    assert denied.value.code == "resource_unavailable"
    assert provider.stat_calls == []

    class Revoked(FakeProvider):
        async def stat(self, context, *, origin_id):
            self.stat_calls.append((context, origin_id))
            return ProviderResource(
                origin_id,
                "Photo",
                "image",
                ("stat",),
                open_target={"app": "gallery"},
            )

    revoked = Revoked()
    with pytest.raises(FilesFacadeError) as changed:
        await FilesFacade([revoked]).open(context, resource_ref=issue_resource_ref(
            owner_subject_id=context.owner_subject_id,
            provider="gallery",
            origin_id="image-revoked",
            kind="image",
            capabilities=("open",),
            policy_generation=context.policy_generation,
        ).token)
    assert changed.value.code == "resource_unavailable"
    assert revoked.stat_calls[-1][1] == "image-revoked"


@pytest.mark.asyncio
async def test_open_rejects_untrusted_provider_target_and_identity_mismatch():
    context = _context()

    class Untrusted(FakeProvider):
        async def stat(self, context, *, origin_id):
            return ProviderResource(
                origin_id,
                "Photo",
                "image",
                ("open",),
                open_target={"app": "https://evil.invalid", "path": "/private"},
            )

    token = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-untrusted",
        kind="image",
        capabilities=("open",),
        policy_generation=context.policy_generation,
    ).token
    with pytest.raises(FilesFacadeError) as untrusted:
        await FilesFacade([Untrusted()]).open(context, resource_ref=token)
    assert untrusted.value.code == "provider_unavailable"

    class Mismatch(FakeProvider):
        async def stat(self, context, *, origin_id):
            return ProviderResource(
                "other-image",
                "Photo",
                "image",
                ("open",),
                open_target={"app": "gallery"},
            )

    with pytest.raises(FilesFacadeError) as mismatch:
        await FilesFacade([Mismatch()]).open(context, resource_ref=token)
    assert mismatch.value.code == "provider_unavailable"


@pytest.mark.asyncio
async def test_list_and_stat_never_serialize_provider_navigation_urls():
    context = _context()
    provider = FakeProvider()
    facade = FilesFacade([provider])
    roots = await facade.roots(context)
    root = roots["entries"][0]
    page = await facade.children(context, parent_ref=root["ref"])
    listed = page["entries"][0]
    assert "open_target" not in listed
    assert "thumbnail_url" not in listed

    stat_ref = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="gallery",
        origin_id="image-stat",
        kind="image",
        capabilities=("stat",),
        policy_generation=context.policy_generation,
    )
    public = await facade.stat(context, resource_ref=stat_ref.token)
    assert "open_target" not in public
    assert "thumbnail_url" not in public
    assert "must-not-leak" not in json.dumps(public)


@pytest.mark.asyncio
async def test_copal_ref_workspace_must_match_sealed_origin():
    class Copal(FakeProvider):
        name = "copal"

    context = _context()
    mismatched = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="copal",
        origin_id="document:other:note-id",
        kind="document",
        capabilities=("download",),
        policy_generation=context.policy_generation,
        workspace_id="course",
    )
    with pytest.raises(FilesFacadeError) as invalid:
        await FilesFacade([Copal()]).content(context, resource_ref=mismatched.token)
    assert invalid.value.code == "provider_unavailable"


@pytest.mark.asyncio
async def test_transfer_reauthorizes_both_ends_and_replays_lost_response():
    class TransferProvider(FakeProvider):
        name = "host"

        async def roots(self, context):
            return [ProviderResource("root", "Root", "folder", ("children", "stat", "write"))]

        async def children(self, context, *, parent_origin_id, cursor, snapshot, limit, sort, query):
            return ProviderPage((
                ProviderResource("source", "a.md", "file", ("stat", "download", "move"), parent_origin_id="root"),
                ProviderResource("dest", "Dest", "folder", ("children", "stat", "write"), parent_origin_id="root"),
            ), total=2)

        async def stat(self, context, *, origin_id):
            if origin_id == "root": return ProviderResource(origin_id, "Root", "folder", ("children", "stat", "write"))
            if origin_id == "dest": return ProviderResource(origin_id, "Dest", "folder", ("children", "stat", "write"), parent_origin_id="root")
            return ProviderResource(origin_id, "a.md", "file", ("stat", "download", "move"), parent_origin_id="root", revision={"kind": "hostFingerprint", "value": "v1"})

        async def transfer(self, context, **kwargs):
            self.calls.append(("transfer", kwargs))
            return await self.stat(context, origin_id="source")

    provider = TransferProvider()
    facade = FilesFacade([provider])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    rows = (await facade.children(context, parent_ref=root["ref"]))["entries"]
    source, destination = rows
    request = {
        "operation_id": "transfer-1",
        "generation": context.policy_generation,
        "kind": "move",
        "sources": [{"item_id": "item-1", "resource_ref": source["ref"], "expected_revision": {"kind": "hostFingerprint", "value": "v1"}}],
        "destination_ref": destination["ref"],
    }
    result = await facade.transfer_resources(context, **request)
    replay = await facade.transfer_resources(context, **request)
    assert result == replay
    assert result["state"] == "complete"
    assert result["items"][0]["outcome"] == "committed"
    assert len([call for call in provider.calls if call[0] == "transfer"]) == 1
    assert '"resource_ref": "source"' not in json.dumps(result)

    with pytest.raises(FilesFacadeError) as changed:
        await facade.transfer_resources(_context(policy_generation=5), **request)
    assert changed.value.code == "resource_ref_stale"


@pytest.mark.asyncio
async def test_transfer_rejects_self_descendant_and_unsupported_provider_kind():
    class Nested(FakeProvider):
        name = "host"

        async def roots(self, context):
            return [ProviderResource("source-dir", "Source", "folder", ("children", "stat", "write"))]

        async def stat(self, context, *, origin_id):
            if origin_id == "source-dir": return ProviderResource(origin_id, "Source", "folder", ("children", "stat", "write"))
            if origin_id == "child-dir": return ProviderResource(origin_id, "Child", "folder", ("children", "stat", "write"), parent_origin_id="source-dir")
            raise FilesFacadeError("missing", code="resource_unavailable")

        async def transfer(self, context, **kwargs):
            raise AssertionError("invalid destination must be rejected before provider mutation")

    facade = FilesFacade([Nested()])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    child = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="child-dir", kind="folder", capabilities=("children", "stat", "write"), policy_generation=context.policy_generation, parent_stable_id=root["id"])
    denied = await facade.transfer_resources(context, operation_id="nested-1", generation=4, kind="move", sources=[{"item_id": "i", "resource_ref": root["ref"]}], destination_ref=child.token)
    assert denied["state"] == "partial"
    assert denied["items"][0]["outcome"] == "denied"
    assert denied["items"][0]["code"] == "invalid_destination"


@pytest.mark.asyncio
async def test_resolve_resource_key_and_base_query_stay_provider_bound():
    class QueryProvider(FakeProvider):
        name = "copal"

        async def resolve_resource_key(self, context, *, resource_key):
            assert resource_key["provider"] == "copal"
            return ProviderResource("base", "Base", "document", ("stat", "read"), parent_origin_id="corpus")

        async def stat(self, context, *, origin_id):
            if origin_id == "base": return ProviderResource(origin_id, "Base", "document", ("stat", "read"), revision={"kind": "copalHead", "value": "h1"})
            if origin_id == "corpus": return ProviderResource(origin_id, "Corpus", "folder", ("children", "stat"))
            if origin_id == "context": return ProviderResource(origin_id, "Context", "document", ("stat", "read"))
            raise FilesFacadeError("missing", code="resource_unavailable")

        async def query_base(self, context, **kwargs):
            assert kwargs["base_origin_id"] == "base"
            assert kwargs["corpus_origin_id"] == "corpus"
            assert kwargs["context_origin_id"] == "context"
            return {"snapshot_id": "snap-1", "complete": True, "rows": [{"resource_key": "row-1"}]}

    provider = QueryProvider()
    facade = FilesFacade([provider])
    context = _context()
    base = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="copal", origin_id="base", kind="document", capabilities=("stat", "read"), policy_generation=context.policy_generation)
    corpus = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="copal", origin_id="corpus", kind="folder", capabilities=("children", "stat"), policy_generation=context.policy_generation)
    resolved = await facade.resolve_resource(context, resource_key={"provider": "copal", "resource_id": "base"})
    assert resolved["provider"] == "copal"
    context_ref = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="copal", origin_id="context", kind="document", capabilities=("stat", "read"), policy_generation=context.policy_generation)
    result = await facade.query_base_resource(context, base_ref=base.token, expected_revision={"kind": "copalHead", "value": "h1"}, corpus_ref=corpus.token, generation=4, page_size=10, context_ref=context_ref.token)
    assert result["snapshot_id"] == "snap-1"
    assert result["corpus"]["resource_key"]


@pytest.mark.asyncio
async def test_operation_receipts_survive_facade_reinstantiation_and_redact_after_revoke(tmp_path):
    class DurableProvider:
        name = "host"

        async def roots(self, context): return [ProviderResource("root", "Root", "folder", ("children", "stat", "write"))]
        async def children(self, context, **kwargs): return ProviderPage((ProviderResource("source", "a", "file", ("stat", "download", "move"), parent_origin_id="root"), ProviderResource("dest", "Dest", "folder", ("children", "stat", "write"), parent_origin_id="root")), total=2)
        async def stat(self, context, *, origin_id):
            if origin_id == "root": return ProviderResource(origin_id, "Root", "folder", ("children", "stat", "write"))
            if origin_id == "dest": return ProviderResource(origin_id, "Dest", "folder", ("children", "stat", "write"), parent_origin_id="root")
            return ProviderResource(origin_id, "a", "file", ("stat", "download", "move"), parent_origin_id="root")
        async def transfer(self, context, **kwargs): return await self.stat(context, origin_id="source")

    policy = FilePolicyRepository(tmp_path / "receipts.db")
    context = _context()
    first = FilesFacade([DurableProvider()], operation_store=policy)
    root = (await first.roots(context))["entries"][0]
    entries = (await first.children(context, parent_ref=root["ref"]))["entries"]
    request = {"operation_id": "durable-op", "generation": 4, "kind": "move", "sources": [{"item_id": "i", "resource_ref": entries[0]["ref"]}], "destination_ref": entries[1]["ref"]}
    original = await first.transfer_resources(context, **request)
    restarted = FilesFacade([DurableProvider()], operation_store=FilePolicyRepository(tmp_path / "receipts.db"))
    assert await restarted.operation_receipt(context, operation_id="durable-op") == original
    redacted = await restarted.operation_receipt(_context(policy_generation=5), operation_id="durable-op")
    assert redacted["items"][0]["outcome"] == "committed"
    assert "resource_ref" not in redacted["items"][0]


@pytest.mark.asyncio
async def test_host_base_snapshot_is_authorized_metadata_only_and_bounded():
    class Registry:
        def app_scope(self, username, *, is_admin=False): return {"host": True, "generation": 4}
        def visibility_for_subject(self, username): return []

    class Client:
        def __init__(self): self.operations = []
        async def request(self, operation, path, payload=None):
            self.operations.append(operation)
            if operation == "stat":
                folder = path == "/authorized"
                return {"data": {"path": path, "kind": "Directory" if folder else "File", "size": 4, "modified_unix_ms": 2}}
            if operation == "list_directory":
                return {"data": {"path": path, "entries": [{"name": "a.md", "kind": "File", "size": 4, "modified_unix_ms": 2}], "next_cursor": None, "generation": 4}}
            raise AssertionError(operation)

    client = Client()
    provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: client)
    result = await provider.query_base(_context(), base_origin_id=_origin("/authorized/a.md"), corpus_origin_id=_origin("/authorized"), page=0, page_size=100)
    assert result["complete"] is True
    assert result["rows"][0]["origin_id"]
    assert "read_lines" not in client.operations


@pytest.mark.asyncio
async def test_import_legacy_provider_fails_closed_before_dispatch(monkeypatch):
    import src.openclank.files_facade as facade_module

    class ImportProvider(FakeProvider):
        name = "host"
        def __init__(self):
            super().__init__()
            self.import_calls = 0
        async def stat(self, context, *, origin_id):
            return ProviderResource(origin_id, "Dest", "folder", ("children", "stat", "write"))
        async def import_file(self, context, **kwargs):
            self.import_calls += 1

    class Upload:
        filename = "oversized.bin"
        async def read(self, size=-1):
            return b"12345" if size else b""

    monkeypatch.setattr(facade_module, "FILES_IMPORT_MAX_BYTES", 4)
    context = _context()
    destination = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="dest", kind="folder", capabilities=("children", "stat", "write"), policy_generation=context.policy_generation)
    provider = ImportProvider()
    with pytest.raises(FilesFacadeError) as oversized:
            await FilesFacade([provider]).import_file(context, upload=Upload(), metadata={"operation_id": "import-limit", "item_id": "item", "generation": context.policy_generation, "destination_ref": destination.token, "name": "x.bin", "collision": "fail"})
    assert oversized.value.code == "unsupported_provider_kind"
    assert provider.import_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("generation", "expected_code"),
    [
        ("bad", "invalid_resource_request"),
        (True, "invalid_resource_request"),
        ("true", "invalid_resource_request"),
        (-1, "invalid_resource_request"),
        (2**63, "resource_ref_stale"),
        (None, "resource_ref_stale"),
    ],
)
async def test_import_metadata_generation_is_typed_before_provider_dispatch(generation, expected_code):
    provider = FakeProvider()
    facade = FilesFacade([provider])
    metadata = {
        "operation_id": "typed-import",
        "item_id": "item",
        "destination_ref": "opaque-destination-ref",
        "name": "upload.txt",
        "collision": "fail",
    }
    if generation is not None:
        metadata["generation"] = generation

    with pytest.raises(FilesFacadeError) as error:
        await facade.import_file(_context(), upload=object(), metadata=metadata)

    assert error.value.code == expected_code
    assert provider.stat_calls == []


@pytest.mark.asyncio
async def test_operation_reservation_allows_one_concurrent_provider_dispatch():
    import asyncio

    class OnceProvider:
        name = "host"

        def __init__(self):
            self.dispatches = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def roots(self, context):
            return [ProviderResource("root", "Root", "folder", ("children", "stat", "write"))]

        async def children(self, context, **kwargs):
            return ProviderPage((
                ProviderResource("source", "source", "file", ("stat", "download", "move"), parent_origin_id="root"),
                ProviderResource("dest", "dest", "folder", ("children", "stat", "write"), parent_origin_id="root"),
            ), total=2)

        async def stat(self, context, *, origin_id):
            if origin_id == "dest":
                return ProviderResource(origin_id, "dest", "folder", ("children", "stat", "write"), parent_origin_id="root")
            return ProviderResource(origin_id, "source", "file", ("stat", "download", "move"), parent_origin_id="root")

        async def transfer(self, context, **kwargs):
            self.dispatches += 1
            self.started.set()
            await self.release.wait()
            return await self.stat(context, origin_id="source")

    provider = OnceProvider()
    shared_store = {}
    facade = FilesFacade([provider], operation_store=shared_store)
    second_facade = FilesFacade([provider], operation_store=shared_store)
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    source, destination = (await facade.children(context, parent_ref=root["ref"]))["entries"]
    request = {"operation_id": "once-op", "generation": 4, "kind": "move", "sources": [{"item_id": "one", "resource_ref": source["ref"]}], "destination_ref": destination["ref"]}
    first = asyncio.create_task(facade.transfer_resources(context, **request))
    await provider.started.wait()
    concurrent = await second_facade.transfer_resources(context, **request)
    assert concurrent["state"] == "pending"
    provider.release.set()
    assert (await first)["state"] == "complete"
    assert provider.dispatches == 1


@pytest.mark.asyncio
async def test_import_reservation_binds_content_digest_and_shared_store_is_owner_scoped():
    import hashlib

    class Upload:
        def __init__(self, data): self.data = data
        async def read(self, size=-1):
            if not self.data: return b""
            data, self.data = self.data, b""
            return data

    class StagedProvider:
        name = "host"
        def __init__(self): self.dispatches = 0; self.stages = {}; self.next_id = 0
        async def stat(self, context, *, origin_id):
            return ProviderResource(origin_id, "Dest", "folder", ("children", "stat", "write"))
        async def stage_import(self, context, **kwargs):
            self.next_id += 1; stage_id = str(self.next_id); data = b""
            while chunk := await kwargs["upload"].read(512 * 1024): data += chunk
            self.stages[stage_id] = data
            return {"stage_id": stage_id, "length": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "max_chunk_bytes": 512 * 1024, "max_total_bytes": 64 * 1024 * 1024}
        async def abort_import(self, context, *, stage): self.stages.pop(stage["stage_id"], None)
        async def finish_import(self, context, **kwargs):
            self.dispatches += 1; self.stages.pop(kwargs["stage"]["stage_id"])
            return ProviderResource("created", "x", "file", ("stat", "download"))

    context = _context()
    destination = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="dest", kind="folder", capabilities=("children", "stat", "write"), policy_generation=4).token
    provider = StagedProvider(); store = {}
    facade = FilesFacade([provider], operation_store=store)
    metadata = {"operation_id": "same", "item_id": "item", "generation": 4, "destination_ref": destination, "name": "x.bin", "collision": "fail"}
    await facade.import_file(context, upload=Upload(b"one"), metadata=metadata)
    with pytest.raises(FilesFacadeError) as conflict:
        await facade.import_file(context, upload=Upload(b"two"), metadata=metadata)
    assert conflict.value.code == "idempotency_conflict"
    assert provider.dispatches == 1
    other = _context(owner_subject_id="account-bob", owner_username="bob")
    other_destination = issue_resource_ref(owner_subject_id=other.owner_subject_id, provider="host", origin_id="dest", kind="folder", capabilities=("children", "stat", "write"), policy_generation=4).token
    other_meta = {**metadata, "destination_ref": other_destination}
    await FilesFacade([provider], operation_store=store).import_file(other, upload=Upload(b"one"), metadata=other_meta)
    assert provider.dispatches == 2


@pytest.mark.asyncio
async def test_nested_import_walks_authorized_tree_with_deterministic_directory_operations():
    class Upload:
        content_type = "text/plain"

        def __init__(self, data): self.data = data

        async def read(self, size=-1):
            data, self.data = self.data, b""
            return data

    class NestedProvider:
        name = "host"

        def __init__(self):
            self.directories = {"root": None}
            self.files = {}
            self.directory_operations = []
            self.staged_destination = None

        def supported_sort_keys(self, *, parent_origin_id): return ("name", "kind")

        async def roots(self, context):
            return [ProviderResource("root", "Drop", "folder", ("children", "stat", "write"))]

        async def children(self, context, *, parent_origin_id, cursor, snapshot, limit, sort, query):
            rows = []
            for origin, parent in self.directories.items():
                if parent == parent_origin_id:
                    rows.append(ProviderResource(origin, origin.split(":")[-1], "folder", ("children", "stat", "write"), parent_origin_id=parent))
            for origin, (parent, name) in self.files.items():
                if parent == parent_origin_id:
                    rows.append(ProviderResource(origin, name, "file", ("stat", "download"), parent_origin_id=parent))
            return ProviderPage(tuple(rows), total=len(rows))

        async def stat(self, context, *, origin_id):
            if origin_id == "root":
                return ProviderResource("root", "Drop", "folder", ("children", "stat", "write"))
            if origin_id in self.directories:
                return ProviderResource(origin_id, origin_id.split(":")[-1], "folder", ("children", "stat", "write"), parent_origin_id=self.directories[origin_id])
            parent, name = self.files[origin_id]
            return ProviderResource(origin_id, name, "file", ("stat", "download"), parent_origin_id=parent)

        async def create_directory(self, context, *, parent_origin_id, name, operation_id=None):
            origin = f"dir:{parent_origin_id}:{name}"
            self.directory_operations.append(operation_id)
            self.directories[origin] = parent_origin_id
            return await self.stat(context, origin_id=origin)

        async def stage_import(self, context, **kwargs):
            self.staged_destination = kwargs["destination_origin_id"]
            data = b""
            while chunk := await kwargs["upload"].read(512 * 1024): data += chunk
            import hashlib
            return {"stage_id": "stage", "length": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}

        async def finish_import(self, context, **kwargs):
            origin = f"file:{kwargs['destination_origin_id']}:{kwargs['name']}"
            self.files[origin] = (kwargs["destination_origin_id"], kwargs["name"])
            return await self.stat(context, origin_id=origin)

    context = _context()
    provider = NestedProvider()
    facade = FilesFacade([provider], operation_store={})
    destination = issue_resource_ref(
        owner_subject_id=context.owner_subject_id, provider="host", origin_id="root", kind="folder",
        capabilities=("children", "stat", "write"), policy_generation=context.policy_generation,
    ).token
    receipt = await facade.import_file(
        context,
        upload=Upload(b"nested"),
        metadata={
            "operation_id": "nested-import", "item_id": "item", "generation": 4,
            "destination_ref": destination, "name": "note.txt", "relative_path": "one/two/note.txt", "collision": "fail",
        },
    )
    assert receipt["state"] == "complete"
    assert provider.staged_destination == "dir:dir:root:one:two"
    assert len(provider.directory_operations) == 2
    assert len(set(provider.directory_operations)) == 2
    assert all(operation.startswith("nested-import-dir-") for operation in provider.directory_operations)
    assert provider.files

    # A different file operation reuses the authorized nested tree instead of
    # attempting a second mkdir for either segment.
    await facade.import_file(
        context,
        upload=Upload(b"second"),
        metadata={
            "operation_id": "nested-import-2", "item_id": "item-2", "generation": 4,
            "destination_ref": destination, "name": "other.txt", "relative_path": "one/two/other.txt", "collision": "fail",
        },
    )
    assert len(provider.directory_operations) == 2


@pytest.mark.asyncio
async def test_nested_import_rejects_traversal_before_directory_or_stage_side_effects():
    class Provider:
        name = "host"
        async def stat(self, context, *, origin_id):
            return ProviderResource(origin_id, "Drop", "folder", ("children", "stat", "write"))
        async def children(self, *args, **kwargs): raise AssertionError("children must not run")
        async def stage_import(self, *args, **kwargs): raise AssertionError("stage must not run")

    class Upload:
        async def read(self, size=-1): raise AssertionError("upload must not be consumed")

    context = _context()
    destination = issue_resource_ref(
        owner_subject_id=context.owner_subject_id, provider="host", origin_id="root", kind="folder",
        capabilities=("children", "stat", "write"), policy_generation=context.policy_generation,
    ).token
    with pytest.raises(FilesFacadeError) as error:
        await FilesFacade([Provider()]).import_file(
            context, upload=Upload(), metadata={
                "operation_id": "unsafe-import", "item_id": "item", "generation": 4,
                "destination_ref": destination, "name": "note.txt", "relative_path": "safe/../note.txt", "collision": "fail",
            },
        )
    assert error.value.code == "invalid_resource_request"

    with pytest.raises(FilesFacadeError) as typed:
        await FilesFacade([Provider()]).import_file(
            context, upload=Upload(), metadata={
                "operation_id": "typed-import", "item_id": "item", "generation": 4,
                "destination_ref": destination, "name": "note.txt", "relative_path": ["safe", "note.txt"], "collision": "fail",
            },
        )
    assert typed.value.code == "invalid_resource_request"


@pytest.mark.asyncio
async def test_import_provider_sees_durable_binding_before_stage_side_effect():
    import hashlib

    class Upload:
        content_type = "application/octet-stream"
        async def read(self, size=-1):
            if getattr(self, "done", False):
                return b""
            self.done = True
            return b"payload"

    class HostStore(dict):
        def get_operation(self, *, owner_subject_id, operation_id):
            return self.get((owner_subject_id, operation_id))
        def record_operation(self, **kwargs):
            self[(kwargs["owner_subject_id"], kwargs["operation_id"])] = {
                "digest": kwargs["request_digest"], "generation": kwargs["generation"], "receipt": dict(kwargs["receipt"]),
            }

    store = HostStore()

    class Provider:
        name = "host"
        async def stat(self, context, *, origin_id):
            return ProviderResource(origin_id, "Dest", "folder", ("children", "stat", "write"))
        async def stage_import(self, context, **kwargs):
            reserved = store[(context.owner_subject_id, "reserved")]
            binding = reserved["receipt"]["_import_binding"]
            assert reserved["receipt"]["state"] == "pending"
            assert binding["account_id"] == context.owner_subject_id
            assert binding["workspace_id"] == context.workspace_id
            assert binding["upload_size"] == 7
            assert binding["upload_digest"].startswith("sha256:")
            assert binding["declared_type"] == "application/octet-stream"
            return {"stage_id": "stage", "length": 7, "digest": "sha256:" + hashlib.sha256(b"payload").hexdigest()}
        async def finish_import(self, context, **kwargs):
            return ProviderResource("created", "payload.bin", "file", ("stat", "download"))

    context = _context(workspace_id="copal-a")
    destination = issue_resource_ref(
        owner_subject_id=context.owner_subject_id, provider="host", origin_id="dest", kind="folder",
        capabilities=("children", "stat", "write"), policy_generation=context.policy_generation,
    ).token
    facade = FilesFacade([Provider()], operation_store=store)
    await facade.import_file(context, upload=Upload(), metadata={
        "operation_id": "reserved", "item_id": "item", "generation": 4,
        "destination_ref": destination, "name": "payload.bin", "collision": "fail",
    })


@pytest.mark.asyncio
async def test_transfer_rejects_duplicate_item_and_resource_before_reservation():
    provider = FakeProvider()
    facade = FilesFacade([provider])
    context = _context()
    root = (await facade.roots(context))["entries"][0]
    source = (await facade.children(context, parent_ref=root["ref"]))["entries"][0]
    destination = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="gallery", origin_id="dest", kind="folder", capabilities=("children", "stat", "write"), policy_generation=4).token
    with pytest.raises(FilesFacadeError) as duplicate_item:
        await facade.transfer_resources(context, operation_id="dup-item", generation=4, kind="copy", sources=[{"item_id": "x", "resource_ref": source["ref"]}, {"item_id": "x", "resource_ref": source["ref"]}], destination_ref=destination)
    assert duplicate_item.value.code == "invalid_resource_request"


@pytest.mark.asyncio
async def test_host_stage_import_aborts_malformed_advertisement_and_oversized_reader():
    class Registry:
        def app_scope(self, username, *, is_admin=False): return {"host": True, "generation": 4}
        def visibility_for_subject(self, username): return []

    class Client:
        def __init__(self, malformed=False): self.malformed = malformed; self.aborts = []
        async def stage_begin(self, path):
            data = {"stage_id": "opaque"}
            if not self.malformed: data.update({"max_chunk_bytes": 2, "max_total_bytes": 4})
            return {"data": data}
        async def stage_abort(self, stage_id): self.aborts.append(stage_id)
        async def stage_chunk(self, stage_id, chunk, *, offset): raise AssertionError("chunk should be rejected")

    class Upload:
        content_type = "text/plain"
        async def read(self, size=-1): return b"123"

    destination = _origin("/tmp")
    malformed_client = Client(malformed=True)
    provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: malformed_client)
    with pytest.raises(FilesFacadeError):
        await provider.stage_import(_context(), destination_origin_id=destination, name="x.txt", upload=Upload(), operation_id="x", item_id="i")
    assert malformed_client.aborts == ["opaque"]
    bounded_client = Client()
    provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: bounded_client)
    with pytest.raises(FilesFacadeError) as oversized:
        await provider.stage_import(_context(), destination_origin_id=destination, name="x.txt", upload=Upload(), operation_id="x", item_id="i")
    assert oversized.value.code == "upload_too_large"


@pytest.mark.asyncio
async def test_host_collision_does_not_adopt_preexisting_matching_file_and_lost_finish_recovers_owned_target(tmp_path):
    class Registry:
        def app_scope(self, username, *, is_admin=False): return {"host": True, "generation": 4}
        def visibility_for_subject(self, username): return []

    context = _context()
    digest = "sha256:" + __import__("hashlib").sha256(b"payload").hexdigest()
    destination = _origin(str(tmp_path))

    class ConflictClient:
        async def stage_finish(self, *args, **kwargs):
            raise FilesServiceError("exists", code="conflict")
        async def request(self, operation, path, payload=None):
            if operation == "stat":
                return {"data": {"path": path, "kind": "File", "size": 7, "fingerprint": {"kind": "hostFingerprint", "value": digest.removeprefix("sha256:")}}}
            raise AssertionError(operation)

    class HostStore(dict):
        def get_operation(self, *, owner_subject_id, operation_id):
            return self.get((owner_subject_id, operation_id))
        def record_operation(self, **kwargs):
            self[(kwargs["owner_subject_id"], kwargs["operation_id"])] = {
                "digest": kwargs["request_digest"], "generation": kwargs["generation"], "receipt": dict(kwargs["receipt"]),
            }

    store = HostStore()
    provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: ConflictClient(), operation_store=store)
    with pytest.raises(FilesFacadeError) as conflict:
        await provider.finish_import(
            context, destination_origin_id=destination, name="same.bin", collision="fail",
            stage={"stage_id": "stage", "length": 7, "digest": digest, "_client": ConflictClient()},
            operation_id="collision", item_id="item",
        )
    assert conflict.value.code == "resource_changed"
    assert store[(context.owner_subject_id, "__host_import__collision")]["receipt"]["phase"] == "failed"
    assert await provider.operation_status(context, operation_id="collision") is None

    class DifferentClient(ConflictClient):
        async def request(self, operation, path, payload=None):
            if operation == "stat":
                return {"data": {"path": path, "kind": "File", "size": 7, "fingerprint": {"kind": "hostFingerprint", "value": "different-bytes"}}}
            raise AssertionError(operation)

    different_provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: DifferentClient(), operation_store=HostStore())
    with pytest.raises(FilesFacadeError) as different:
        await different_provider.finish_import(
            context, destination_origin_id=destination, name="different.bin", collision="fail",
            stage={"stage_id": "stage", "length": 7, "digest": digest, "_client": DifferentClient()},
            operation_id="different", item_id="item",
        )
    assert different.value.code == "resource_changed"

    class LostClient:
        def __init__(self): self.stat_calls = 0
        async def stage_finish(self, stage_id, target, *, length, digest):
            return {"data": {"path": target}}
        async def request(self, operation, path, payload=None):
            if operation != "stat": raise AssertionError(operation)
            self.stat_calls += 1
            if self.stat_calls == 1:
                raise FilesServiceError("missing", code="invalid_path")
            return {"data": {"path": path, "kind": "File", "size": 7, "fingerprint": {"kind": "hostFingerprint", "value": digest.removeprefix("sha256:")}}}

    class LostStore(dict):
        def get_operation(self, *, owner_subject_id, operation_id):
            return self.get((owner_subject_id, operation_id))
        def record_operation(self, **kwargs):
            if kwargs["receipt"].get("phase") == "complete":
                raise OSError("completion marker response lost")
            self[(kwargs["owner_subject_id"], kwargs["operation_id"])] = {"owner": kwargs["owner_subject_id"], "digest": kwargs["request_digest"], "generation": kwargs["generation"], "receipt": dict(kwargs["receipt"])}

    lost_client = LostClient()
    lost_store = LostStore()
    lost_provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: lost_client, operation_store=lost_store)
    with pytest.raises(OSError):
        await lost_provider.finish_import(
            context, destination_origin_id=destination, name="created.bin", collision="rename",
            stage={"stage_id": "stage", "length": 7, "digest": digest, "_client": lost_client},
            operation_id="lost", item_id="item",
        )
    # A fresh provider instance models process restart; only the durable
    # marker and the provider's exact target fingerprint are used for replay.
    recovered_provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: lost_client, operation_store=lost_store)
    recovered = await recovered_provider.operation_status(context, operation_id="lost")
    assert recovered is not None and recovered["state"] == "complete"
    assert recovered["items"][0]["outcome"] == "committed"


@pytest.mark.asyncio
async def test_pending_receipt_reconciles_from_provider_status_after_persistence_failure():
    class Store(dict):
        def __init__(self):
            super().__init__()
            self.fail_update = True
        def __setitem__(self, key, value):
            if self.fail_update and self:
                raise OSError("receipt persistence interrupted")
            return super().__setitem__(key, value)

    class Upload:
        async def read(self, size=-1):
            if getattr(self, "done", False): return b""
            self.done = True
            return b"bytes"

    class Provider:
        name = "host"
        async def stat(self, context, *, origin_id):
            return ProviderResource(origin_id, "Dest", "folder" if origin_id == "dest" else "x", ("children", "stat", "write") if origin_id == "dest" else ("stat", "download"))
        async def stage_import(self, context, **kwargs):
            data = b""
            while chunk := await kwargs["upload"].read(512 * 1024): data += chunk
            import hashlib
            return {"stage_id": "s", "length": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
        async def finish_import(self, context, **kwargs):
            return ProviderResource("created", "x", "file", ("stat", "download"))
        async def abort_import(self, context, *, stage): pass
        async def operation_status(self, context, *, operation_id):
            return {"state": "complete", "items": [{"item_id": "item", "outcome": "committed", "resource_ref": "sealed-ref", "receipt_id": operation_id}]}

    context = _context()
    destination = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="dest", kind="folder", capabilities=("children", "stat", "write"), policy_generation=4).token
    store = Store()
    provider = Provider()
    facade = FilesFacade([provider], operation_store=store)
    metadata = {"operation_id": "lost", "item_id": "item", "generation": 4, "destination_ref": destination, "name": "x.bin", "collision": "fail"}
    with pytest.raises(OSError):
        await facade.import_file(context, upload=Upload(), metadata=metadata)
    store.fail_update = False
    reconciled = await FilesFacade([provider], operation_store=store).operation_receipt(context, operation_id="lost")
    assert reconciled["state"] == "complete"
    assert reconciled["items"][0]["receipt_id"] == "lost"


@pytest.mark.asyncio
async def test_import_replays_provider_commit_after_facade_receipt_write_loss_without_finishing_twice():
    import hashlib

    class Upload:
        def __init__(self, data): self.data = data
        async def read(self, size=-1):
            data, self.data = self.data, b""
            return data

    class Store(dict):
        def __setitem__(self, key, value):
            if value.get("receipt", {}).get("state") == "complete" and not getattr(self, "allow_complete", False):
                raise OSError("facade receipt write interrupted")
            return super().__setitem__(key, value)

    class Provider:
        name = "host"
        def __init__(self): self.stages = {}; self.finish_calls = 0; self.committed = None; self.request_digest = None
        async def stat(self, context, *, origin_id):
            if origin_id == "dest":
                return ProviderResource(origin_id, "Dest", "folder", ("children", "stat", "write"))
            return ProviderResource(origin_id, "created.txt", "file", ("stat", "download"))
        async def stage_import(self, context, **kwargs):
            data = b""
            while chunk := await kwargs["upload"].read(512 * 1024): data += chunk
            stage_id = str(len(self.stages) + 1)
            self.stages[stage_id] = data
            return {"stage_id": stage_id, "length": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
        async def abort_import(self, context, *, stage): self.stages.pop(stage["stage_id"], None)
        async def operation_status(self, context, *, operation_id, **kwargs):
            return ({**self.committed, "_request_digest": self.request_digest} if self.committed else None)
        async def finish_import(self, context, **kwargs):
            self.finish_calls += 1
            self.stages.pop(kwargs["stage"]["stage_id"])
            self.request_digest = kwargs["request_digest"]
            self.committed = {"state": "complete", "items": [{"item_id": "item", "outcome": "committed", "resource_ref": "sealed-ref", "resource_key": "created-key", "receipt_id": "lost"}]}
            return self.committed

    context = _context(workspace_id="workspace-a")
    destination = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="dest", kind="folder", capabilities=("children", "stat", "write"), policy_generation=4).token
    metadata = {"operation_id": "lost", "item_id": "item", "generation": 4, "destination_ref": destination, "name": "created.txt", "collision": "fail"}
    provider = Provider(); store = Store(); facade = FilesFacade([provider], operation_store=store)
    with pytest.raises(OSError):
        await facade.import_file(context, upload=Upload(b"bytes"), metadata=metadata)
    assert provider.finish_calls == 1
    store.allow_complete = True
    replay = await FilesFacade([provider], operation_store=store).import_file(context, upload=Upload(b"bytes"), metadata=metadata)
    assert replay["state"] == "complete"
    assert replay["items"] == provider.committed["items"]
    assert provider.finish_calls == 1
    assert not provider.stages


@pytest.mark.asyncio
async def test_attachment_requires_typed_preparation_descriptor_and_projects_receipt():
    class Provider:
        name = "host"
        async def stat(self, context, *, origin_id):
            if origin_id == "source": return ProviderResource(origin_id, "Source", "file", ("stat", "download"), revision={"kind": "hostFingerprint", "value": "s1"})
            return ProviderResource(origin_id, "Doc", "file", ("stat", "write"), revision={"kind": "copalHead", "value": "t1"})
        async def prepare_attachment(self, context, **kwargs):
            return {"preparation_receipt_id": "prep-1", "source_revision": {"kind": "hostFingerprint", "value": "s1"}, "target_identity": {"resource_ref": "sealed-target", "kind": "copal_document"}, "target_revision": {"kind": "copalHead", "value": "t1"}, "asset": {"resource_ref": "sealed-asset", "resource_key": "asset-key", "revision": {"kind": "hostFingerprint", "value": "a1"}, "mime_type": "text/plain", "name": "source.txt"}, "insertion": {"format": "markdown", "link_target": "asset-key", "label": "source.txt", "media_kind": "text"}, "action_receipt": {"receipt_id": "prep-1", "status": "complete", "receipt": {"outcome": "committed"}}}

    context = _context()
    source = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="source", kind="file", capabilities=("stat", "download"), policy_generation=4).token
    target = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="target", kind="file", capabilities=("stat", "write"), policy_generation=4).token
    result = await FilesFacade([Provider()]).prepare_attachment(context, operation_id="attach", generation=4, source={"resource_ref": source, "expected_revision": {"kind": "hostFingerprint", "value": "s1"}}, target={"kind": "copal_document", "resource_ref": target, "expected_revision": {"kind": "copalHead", "value": "t1"}}, mode="embed")
    assert result["preparation_receipt_id"] == "prep-1"
    assert result["asset"]["resource_ref"] == "sealed-asset"
    assert result["history"]["receipt"]["outcome"] == "committed"


@pytest.mark.asyncio
async def test_import_attachment_receipt_is_workspace_bound_and_replays_in_same_workspace():
    class Provider:
        name = "host"
        def __init__(self):
            self.recovery_calls = 0
            self.preparation = None

        async def stat(self, context, *, origin_id):
            if origin_id == "source":
                return ProviderResource(origin_id, "source.txt", "file", ("stat", "download"), revision={"kind": "hostFingerprint", "value": "s1"})
            return ProviderResource(origin_id, "lesson.doc", "file", ("stat", "write"), revision={"kind": "copalHead", "value": "t1"})
        async def prepare_attachment(self, context, **kwargs):
            self.preparation = {"preparation_receipt_id": "prep-import", "source_revision": {"kind": "hostFingerprint", "value": "s1"}, "target_identity": {"resource_ref": "target", "kind": "copal_document"}, "target_revision": {"kind": "copalHead", "value": "t1"}, "asset": {"resource_ref": "asset", "resource_key": "asset-key", "revision": {"kind": "hostFingerprint", "value": "a1"}, "mime_type": "text/plain", "name": "source.txt"}, "insertion": {"format": "markdown", "link_target": "asset-key", "label": "source.txt", "media_kind": "text"}, "action_receipt": {"receipt_id": "prep-import", "status": "complete", "receipt": {"outcome": "committed"}}}
            return self.preparation
        async def attachment_status(self, context, *, operation_id):
            self.recovery_calls += 1
            return self.preparation

    context = _context(workspace_id="workspace-a")
    source_ref = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="source", kind="file", capabilities=("stat", "download"), policy_generation=4).token
    target_ref = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider="host", origin_id="target", kind="file", capabilities=("stat", "write"), policy_generation=4).token
    store = {(context.owner_subject_id, "import-1"): {"owner": context.owner_subject_id, "digest": "upload-digest", "generation": 4, "receipt": {"operation_id": "import-1", "generation": 4, "state": "complete", "items": [{"item_id": "item-1", "outcome": "committed", "resource_ref": source_ref}], "_workspace_id": "workspace-a"}}}
    facade = FilesFacade([Provider()], operation_store=store)
    source = {"import_receipt_id": "import-1", "item_id": "item-1"}
    target = {"kind": "copal_document", "resource_ref": target_ref, "expected_revision": {"kind": "copalHead", "value": "t1"}}
    first = await facade.prepare_attachment(context, operation_id="attach-import", generation=4, source=source, target=target, mode="embed")
    replay = await facade.prepare_attachment(context, operation_id="attach-import", generation=4, source=source, target=target, mode="embed")
    assert replay == first
    recovery_calls_before_cross_workspace = facade._providers["host"].recovery_calls
    with pytest.raises(FilesFacadeError) as cross_workspace:
        await facade.prepare_attachment(_context(workspace_id="workspace-b"), operation_id="attach-import", generation=4, source=source, target=target, mode="embed")
    assert cross_workspace.value.code == "resource_unavailable"
    assert facade._providers["host"].recovery_calls == recovery_calls_before_cross_workspace


@pytest.mark.asyncio
async def test_host_import_aborts_when_numeric_stage_capability_is_malformed_in_both_paths():
    class Registry:
        def app_scope(self, username, *, is_admin=False): return {"host": True, "generation": 4}
        def visibility_for_subject(self, username): return []

    class Client:
        def __init__(self): self.aborts = []
        async def stage_begin(self, path): return {"data": {"stage_id": "opaque", "max_chunk_bytes": "not-a-number", "max_total_bytes": 4}}
        async def stage_abort(self, stage_id): self.aborts.append(stage_id)
        async def stage_chunk(self, *args, **kwargs): raise AssertionError("chunk should not be sent")
        async def stage_finish(self, *args, **kwargs): raise AssertionError("finish should not be sent")

    class Upload:
        content_type = "text/plain"
        async def read(self, size=-1): return b""

    client = Client()
    provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: client)
    context = _context()
    with pytest.raises(FilesFacadeError):
        await provider.stage_import(context, destination_origin_id=_origin("/tmp"), name="x.txt", upload=Upload(), operation_id="x", item_id="i")
    with pytest.raises(FilesFacadeError):
        await provider.import_file(context, destination_origin_id=_origin("/tmp"), name="x.txt", collision="fail", upload=Upload(), operation_id="x", item_id="i")
    assert client.aborts == ["opaque", "opaque"]


@pytest.mark.asyncio
async def test_create_directory_is_durable_and_reconciles_a_lost_response():
    class DurableProvider:
        name = "host"

        def __init__(self, state):
            self.state = state
            self.calls = 0

        async def stat(self, context, *, origin_id):
            if origin_id == "root":
                return ProviderResource(
                    "root", "Drop", "folder", ("children", "stat", "write"),
                    revision={"kind": "hostFingerprint", "value": "parent-v1"},
                )
            row = self.state.get(origin_id)
            if row is None:
                raise FilesFacadeError("missing", code="resource_unavailable")
            return row

        async def create_directory(self, context, *, parent_origin_id, name, operation_id,
                                    request_digest, parent_revision, collision):
            self.calls += 1
            assert parent_revision == {"kind": "hostFingerprint", "value": "parent-v1"}
            assert collision == "fail"
            child = ProviderResource(
                "child", name, "folder", ("children", "stat", "write"), parent_origin_id="root",
                revision={"kind": "hostFingerprint", "value": "child-v1"},
            )
            self.state["child"] = child
            self.state["status"] = {
                "state": "complete",
                "items": [{
                    "item_id": operation_id,
                    "outcome": "committed",
                    "resource_ref": issue_resource_ref(
                        owner_subject_id=context.owner_subject_id, provider="host", origin_id="child",
                        kind="folder", capabilities=child.capabilities,
                        policy_generation=context.policy_generation,
                    ).token,
                }],
                "_request_digest": request_digest,
            }
            raise RuntimeError("lost response after mkdir")

        async def operation_status(self, context, *, operation_id):
            return self.state.get("status")

    context = _context()
    parent = issue_resource_ref(
        owner_subject_id=context.owner_subject_id, provider="host", origin_id="root", kind="folder",
        capabilities=("children", "stat", "write"), policy_generation=context.policy_generation,
    ).token
    state = {}
    provider = DurableProvider(state)
    store = {}
    facade = FilesFacade([provider], operation_store=store)
    request = {
        "parent_ref": parent, "name": "nested", "operation_id": "mkdir-once",
        "generation": context.policy_generation,
        "expected_revision": {"kind": "hostFingerprint", "value": "parent-v1"},
    }
    with pytest.raises(FilesFacadeError) as lost:
        await facade.create_directory(context, **request)
    assert lost.value.code == "provider_unavailable"
    restarted = FilesFacade([DurableProvider(state)], operation_store=store)
    replay = await restarted.create_directory(context, **request)
    assert replay["action"] == "create-directory"
    assert replay["resource"]["name"] == "nested"
    assert state["child"].origin_id == "child"

    with pytest.raises(FilesFacadeError) as stale:
        await restarted.create_directory(context, **{
            **request, "operation_id": "mkdir-stale",
            "expected_revision": {"kind": "hostFingerprint", "value": "old"},
        })
    assert stale.value.code == "resource_changed"


@pytest.mark.asyncio
async def test_host_create_directory_marker_survives_service_response_loss_and_rejects_collisions(tmp_path):
    class Registry:
        def app_scope(self, username, *, is_admin=False): return {"host": True, "generation": 4}
        def visibility_for_subject(self, username): return []

    class Store:
        def __init__(self): self.rows = {}
        def get_operation(self, *, owner_subject_id, operation_id): return self.rows.get((owner_subject_id, operation_id))
        def record_operation(self, *, owner_subject_id, operation_id, request_digest, generation, receipt):
            self.rows[(owner_subject_id, operation_id)] = {"receipt": dict(receipt)}

    class Client:
        def __init__(self): self.directories = {str(tmp_path)}; self.lose_reply = False
        async def request(self, operation, path, payload=None):
            if operation == "stat":
                if path not in self.directories:
                    raise FilesServiceError("missing", code="denied")
                return {"data": {"path": path, "kind": "Directory", "size": 0,
                                  "fingerprint": {"kind": "hostFingerprint", "value": f"fp-{path}"}}}
            if operation == "mkdir":
                if path in self.directories:
                    raise FilesServiceError("exists", code="conflict")
                self.directories.add(path)
                if self.lose_reply:
                    self.lose_reply = False
                    raise FilesServiceError("reply lost", code="root_unavailable")
                return {"data": {"path": path}}
            raise AssertionError(operation)

    context = _context()
    client = Client()
    store = Store()
    provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: client, operation_store=store)
    parent = _origin(str(tmp_path))
    client.lose_reply = True
    with pytest.raises(FilesFacadeError) as lost:
        await provider.create_directory(
            context, parent_origin_id=parent, name="nested", operation_id="mkdir-loss",
            request_digest="digest-1", parent_revision={"kind": "hostFingerprint", "value": f"fp-{tmp_path}"},
        )
    assert lost.value.code == "provider_unavailable"
    restarted_provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kwargs: client, operation_store=store)
    recovered = await restarted_provider.create_directory(
        context, parent_origin_id=parent, name="nested", operation_id="mkdir-loss",
        request_digest="digest-1", parent_revision={"kind": "hostFingerprint", "value": f"fp-{tmp_path}"},
    )
    assert recovered.kind == "folder"
    assert store.rows[(context.owner_subject_id, "__host_directory__mkdir-loss")]["receipt"]["phase"] == "complete"

    with pytest.raises(FilesFacadeError) as collision:
        await provider.create_directory(
            context, parent_origin_id=parent, name="nested", operation_id="mkdir-fail",
            request_digest="digest-2", collision="fail",
        )
    assert collision.value.code == "resource_changed"
    reused = await provider.create_directory(
        context, parent_origin_id=parent, name="nested", operation_id="mkdir-reuse",
        request_digest="digest-3", collision="reuse",
    )
    assert reused.kind == "folder"
