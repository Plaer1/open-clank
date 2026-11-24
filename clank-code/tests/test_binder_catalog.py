from types import SimpleNamespace

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_catalog_cas_is_owner_scoped_and_preserves_preferences(tmp_path, monkeypatch):
    from routes import prefs_routes as prefs
    from src.openclank.binder_catalog import empty_catalog

    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    manager = SimpleNamespace(users={"alice": {}, "bob": {}}, account_id=lambda user: {"alice": "account-a", "bob": "account-b"}[user])
    request = lambda user: SimpleNamespace(state=SimpleNamespace(authenticated=True, current_user=user, api_token=False), app=SimpleNamespace(state=SimpleNamespace(auth_manager=manager)))
    router = prefs.setup_prefs_routes()
    endpoint = lambda path, method: next(route.endpoint for route in router.routes if route.path == path and method in route.methods)
    get = endpoint("/api/prefs/binder-catalog", "GET")
    put = endpoint("/api/prefs/binder-catalog", "PUT")
    generic = endpoint("/api/prefs/{key}", "PUT")
    prefs._save_for_user("alice", {"theme": "dark"})
    prefs._save_for_user("bob", {"theme": "light"})
    catalog = empty_catalog("account-a")
    catalog["configuredIds"] = ["one"]
    catalog["defaultId"] = "one"
    catalog["descriptors"] = {"one": {"id": "one", "displayName": "Notes", "provider": "copal", "source": {"workspaceId": "default", "corpus": "notes", "root": ["BrainVault"]}, "enabled": True, "revision": 0}}
    body = {"expectedAccountId": "account-a", "expectedRevision": 0, "catalog": catalog}
    saved = await put(request("alice"), body)
    assert saved["revision"] == 1 and saved["descriptors"]["one"]["revision"] == 1
    assert (await get(request("bob"))) == empty_catalog("account-b")
    with pytest.raises(HTTPException) as stale:
        await put(request("alice"), body)
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException):
        await generic(request("alice"), "binder_catalog", {"value": {}})
    prefs._update_for_user("alice", {"theme": "blue"})
    assert (await get(request("alice"))) == saved
    assert prefs._load_for_user("alice")["theme"] == "blue"
    assert prefs._load_for_user("bob")["theme"] == "light"


@pytest.mark.asyncio
async def test_binder_folder_resolves_only_current_authorized_copal_path():
    from src.openclank.files_facade import FilesFacadeError, ProviderContext
    from src.openclank.files_managed_providers import CopalFilesProvider

    class Bridge:
        async def call(self, operation, args):
            assert operation == "metadata_page"
            assert args["owner"] == "alice" and args["workspace_id"] == "personal"
            return {"snapshot": "one", "docs": [{"name": "Section/Page.md"}], "next_cursor": None}

    provider = CopalFilesProvider(Bridge())
    ctx = ProviderContext(owner_subject_id="account-a", owner_username="alice", policy_generation=1, workspace_id="personal")
    folder = await provider.resolve_binder_folder(ctx, workspace="personal", corpus="notes", root=("Section",))
    assert "children" in folder.capabilities
    assert await provider.describe_binder_folder(ctx, origin_id=folder.origin_id) == {"workspaceId": "personal", "corpus": "notes", "root": ["Section"]}
    with pytest.raises(FilesFacadeError):
        await provider.resolve_binder_folder(ctx, workspace="personal", corpus="notes", root=("Missing",))


@pytest.mark.asyncio
async def test_binder_resolution_rechecks_selected_identity_across_catalog_changes(tmp_path, monkeypatch):
    import asyncio
    import copy
    from routes import files_facade_routes as files, prefs_routes as prefs
    from src.openclank.binder_catalog import empty_catalog, prepare_update

    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    account = {"id": "account-a"}
    manager = SimpleNamespace(users={"alice": {}}, account_id=lambda _: account["id"])
    request = SimpleNamespace(state=SimpleNamespace(authenticated=True, current_user="alice", api_token=False), app=SimpleNamespace(state=SimpleNamespace(auth_manager=manager, copal_bridge=object(), treehouse_repository=object())))
    monkeypatch.setattr(files, "files_principal", lambda *args, **kwargs: ("account-a", "alice", False))
    entered, release = asyncio.Event(), asyncio.Event()
    class FakeFacade:
        def __init__(self, *args, **kwargs): pass
        async def resolve_binder_folder(self, context, *, source):
            entered.set(); await release.wait(); return {"ref": "sealed"}
    monkeypatch.setattr(files, "FilesFacade", FakeFacade)
    repository = SimpleNamespace(generation=lambda: 1)
    router = files.setup_files_facade_routes(policy_repository=repository, filesystem_registry=object(), session_factory=lambda: None)
    resolve = next(route.endpoint for route in router.routes if route.path == "/api/files-v1/binders/resolve")
    catalog=empty_catalog("account-a")
    descriptor=lambda key, root: {"id":key,"displayName":key,"provider":"copal","source":{"workspaceId":"default","corpus":"notes","root":[root]},"enabled":True,"revision":0}
    catalog["configuredIds"]=["one"]; catalog["descriptors"]={"one":descriptor("one","One")}; catalog=prepare_update(None,catalog,"account-a",0)
    prefs._save_for_user("alice", {"binder_catalog":catalog}, allow_binder_catalog=True)
    async def race(change):
        entered.clear(); release.clear(); selected=copy.deepcopy(catalog["descriptors"]["one"])
        body=files.BinderResolveRequest(binder_id="one",expected_catalog_revision=catalog["revision"],expected_descriptor_revision=selected["revision"],expected_descriptor=selected)
        task=asyncio.create_task(resolve(body,request)); await entered.wait(); change(); release.set(); return await task
    def add_other():
        nonlocal catalog
        proposed=copy.deepcopy(catalog); proposed["configuredIds"].append("two"); proposed["descriptors"]["two"]=descriptor("two","Two")
        catalog=prepare_update(catalog,proposed,"account-a",catalog["revision"]); prefs._save_for_user("alice", {"binder_catalog":catalog}, allow_binder_catalog=True)
    assert (await race(add_other))["catalogRevision"] == 2
    def change_selected():
        nonlocal catalog
        proposed=copy.deepcopy(catalog); proposed["descriptors"]["one"]["displayName"]="changed"
        catalog=prepare_update(catalog,proposed,"account-a",catalog["revision"]); prefs._save_for_user("alice", {"binder_catalog":catalog}, allow_binder_catalog=True)
    with pytest.raises(HTTPException) as changed: await race(change_selected)
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as owner: await race(lambda: account.update(id="account-b"))
    assert owner.value.status_code == 409
