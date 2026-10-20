"""Real adapter parity and interrupted-import safety; disposable stores only."""
import asyncio
import io
import json
import multiprocessing
import os
import secrets
import zipfile
from pathlib import Path

import pytest

from src.openclank.copal_errors import CopalBridgeError
from src.openclank.copal_loose import LooseCopalBridge
from src.openclank import copal_loose_import as importer

SCOPE = {"owner":"alice", "workspace_id":"home"}


def note(text="hello", **properties):
    return json.dumps({"schemaVersion":1,"body":{"type":"doc","blocks":[{"id":"p","type":"paragraph","text":text}]},"properties":properties,"relations":[]})


def write(root, name, content):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if isinstance(content, bytes) else content.encode())
    return target


def call(bridge, operation, **args):
    return bridge._call_sync(operation, {**SCOPE, **args})


def _crash_import(store, archive, phase):
    bridge = LooseCopalBridge(store)
    original = importer._install
    calls = 0
    def install(repo, source, relative, expected):
        nonlocal calls
        calls += 1
        if phase == "prepared" and calls == 1:
            os._exit(77)
        original(repo, source, relative, expected)
        if (phase == "mid-files" and calls == 2) or (phase == "committed" and relative.endswith("/.copal/manifest.json")):
            os._exit(77)
    importer._install = install
    atomic_write = bridge._atomic_write
    def publish(path,content):
        if phase == 'before-journal' and path.name == importer.JOURNAL:
            os._exit(77)
        return atomic_write(path,content)
    bridge._atomic_write = publish
    if phase == 'after-unlink':
        importer._cleanup = lambda repo,directory: os._exit(77)
    if phase == 'during-cleanup':
        remove = importer.shutil.rmtree
        def partial(directory):
            remove(directory)
            os._exit(77)
        importer.shutil.rmtree = partial
    call(bridge, "import_vault", path=str(archive), note_kind="note")


def _read_after_crash(store, identifier, pipe):
    bridge = LooseCopalBridge(store)
    document = call(bridge, "get", id=identifier)
    manifest = bridge._load(bridge._vault(SCOPE["owner"], SCOPE["workspace_id"]))
    pipe.send({"text":document["text"],"names":[d["name"] for d in manifest["documents"].values()]})
    pipe.close()


def test_scoped_prepared_import_repeats_without_changing_ids_or_heads(tmp_path):
    bridge, archive = LooseCopalBridge(tmp_path / "store"), tmp_path / "archive"
    write(archive, "Nested/A.md", note("typed", flag=True, rank=1))
    write(archive, "B.base", '{"views":[]}')
    write(archive, "C.canvas", '{"nodes":[]}')
    first = call(bridge, "import_vault", path=str(archive), note_kind="note")
    assert first["notes"] == 3 and len(first["entries"]) == 3 and first["op"]
    manifest = bridge._load(bridge._vault("alice", "home"))
    identities = {d["name"]:(d["id"],d["head"]) for d in manifest["documents"].values()}
    again = call(bridge, "import_vault", path=str(archive), note_kind="note")
    assert again["unchanged"] == 3 and again["notes"] == 0
    assert {d["name"]:(d["id"],d["head"]) for d in bridge._load(bridge._vault("alice","home"))["documents"].values()} == identities
    assert (bridge._vault("alice","home") / "Nested/A.md").read_text() == note("typed", flag=True, rank=1)
    assert not bridge._load(bridge._vault("bob","home"))["documents"]
    assert not bridge._load(bridge._vault("alice","other"))["documents"]
    assert not (bridge.data_dir / importer.JOURNAL).exists()


def test_binary_hidden_invalid_formats_and_reserved_documents(tmp_path):
    bridge, archive = LooseCopalBridge(tmp_path / "store"), tmp_path / "archive"
    write(archive, "A.md", b"\xff\x00")
    write(archive, "Raw.md", "unprepared markdown")
    write(archive, "Image.png", b"\x89PNG\x00\xff")
    write(archive, ".git/config", "inert metadata")
    write(archive, ".clanker/private.txt", "synthetic private canary")
    write(archive, ".copal/manifest.json", "inert non-authority")
    write(archive, ".copal/tracks.json", '{"schemaVersion":2,"tracks":[]}')
    write(archive, ".copal/planning-migration.json", '{"schemaVersion":99}')
    write(archive, ".copal/treehouse-state.json", '{}')
    planning = write(archive, ".copal/planning.json", '{}')
    result = call(bridge,"import_vault",path=str(archive),note_kind="note",planning_path=str(planning))
    assert result["notes"] == 1 and result["assets"] == 1 and result["compatibility"] == 6
    assert result["planning"] and result["treehouse"]
    vault = bridge._vault("alice","home")
    manifest = bridge._load(vault)
    compat = [d for d in manifest["documents"].values() if d["kind"] == "compatibility"]
    assert all(d["readOnly"] and d["path"].startswith('.copal/imported-compat/') for d in compat)
    assert not (vault / ".git").exists() and not (vault / ".clanker").exists()
    assert json.loads((vault / ".copal/manifest.json").read_text())["schemaVersion"] == 1
    again = call(bridge,"import_vault",path=str(archive),note_kind="note",planning_path=str(planning))
    assert again["unchanged"] == 10


def test_update_preserves_history_and_restore_guards(tmp_path):
    bridge, archive = LooseCopalBridge(tmp_path / "store"), tmp_path / "archive"
    write(archive,"A.md",note("before"))
    identity = {"A.md":{"id":"restored-id","corpus":"notes","kind":"note"}}
    result = call(bridge,"import_vault",path=str(archive),note_kind="note",restore_ids=identity)
    assert result["restored_identities"] == 1
    before = call(bridge,"get",id="restored-id")
    write(archive,"A.md",note("after"))
    with pytest.raises(CopalBridgeError, match="stale"):
        call(bridge,"import_vault",path=str(archive),note_kind="note",restore_ids=identity,expected_heads={"restored-id":"wrong"})
    assert call(bridge,"get",id="restored-id")["text"] == before["text"]
    changed = call(bridge,"import_vault",path=str(archive),note_kind="note",restore_ids=identity,expected_heads={"restored-id":before["head"]})
    assert changed["entries"][0]["status"] == "updated"
    history = bridge._history_dir(bridge._vault("alice","home"),before) / (before["head"] + '.json')
    assert json.loads(history.read_text())["content"] == before["text"]
    with pytest.raises(CopalBridgeError, match="outside"):
        bridge._call_sync("import_vault",{**SCOPE,"owner":"bob","path":str(archive),"note_kind":"note","restore_ids":identity})
    with pytest.raises(CopalBridgeError, match="outside"):
        bridge._call_sync("import_vault",{**SCOPE,"workspace_id":"other","path":str(archive),"note_kind":"note","restore_ids":identity})


@pytest.mark.parametrize("bad",["root-link","file-link","directory-link","fifo","outside-planning"])
def test_source_preflight_rejects_unsafe_tree_without_canonical_mutation(tmp_path,bad):
    bridge, archive = LooseCopalBridge(tmp_path / "store"), tmp_path / "archive"
    write(archive,"A.md",note())
    outside = write(tmp_path,"outside","must not be read")
    args = {"path":str(archive),"note_kind":"note"}
    if bad == "root-link":
        alias = tmp_path / "alias"
        alias.symlink_to(archive,target_is_directory=True)
        args["path"] = str(alias)
    elif bad == "file-link": (archive / "link").symlink_to(outside)
    elif bad == "directory-link": (archive / "link").symlink_to(tmp_path,target_is_directory=True)
    elif bad == "fifo": os.mkfifo(archive / "pipe")
    else: args["planning_path"] = str(outside)
    with pytest.raises((CopalBridgeError,OSError)):
        call(bridge,"import_vault",**args)
    assert not bridge._vault("alice","home").exists()
    assert outside.read_text() == "must not be read"


def test_limits_identity_duplicates_portable_collision_and_destination_symlink(tmp_path,monkeypatch):
    bridge, archive = LooseCopalBridge(tmp_path / "store"), tmp_path / "archive"
    write(archive,"A.md",note())
    write(archive,"B.md",note())
    monkeypatch.setattr(importer,"MAX_FILES",1)
    with pytest.raises(CopalBridgeError,match="limits"):
        call(bridge,"import_vault",path=str(archive),note_kind="note")
    monkeypatch.setattr(importer,"MAX_FILES",10_000)
    with pytest.raises(CopalBridgeError,match="duplicate"):
        call(bridge,"import_vault",path=str(archive),note_kind="note",restore_ids={n:{"id":"same","corpus":"notes","kind":"note"} for n in ['A.md','B.md']})
    with pytest.raises(CopalBridgeError,match="paths"):
        call(bridge,"import_vault",path=str(archive),restore_ids={"../bad":{"id":"x","corpus":"notes","kind":"note"}})
    write(archive,'.copal/wiki/a.md',note())
    with pytest.raises(CopalBridgeError,match="portable"):
        call(bridge,"import_vault",path=str(archive),note_kind="wiki")
    assert not bridge._vault("alice","home").exists()
    parent = bridge._vault("alice","home").parent
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    bridge._vault("alice","home").symlink_to(outside,target_is_directory=True)
    with pytest.raises(CopalBridgeError,match='unsafe'):
        call(bridge,"import_vault",path=str(archive),note_kind="note")
    assert not list(outside.iterdir())


def test_immutable_official_reference_never_becomes_an_import_target(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "store")
    docs = call(bridge,"index")["docs"]
    reference = next(d for d in docs if d.get("officialRef"))
    archive = tmp_path / "archive"
    write(archive,reference["name"],note("overwrite attempt"))
    with pytest.raises(CopalBridgeError,match="outside the mutable requested scope"):
        call(bridge,"import_vault",path=str(archive),note_kind="wiki",restore_ids={reference['name']:{'id':reference['id'],'kind':reference['kind'],'corpus':reference['corpus']}})
    assert call(bridge,"get",id=reference["id"])["head"] == reference["head"]


def test_fault_mid_publish_rolls_back_and_fault_after_manifest_is_committed(tmp_path,monkeypatch):
    bridge, archive = LooseCopalBridge(tmp_path / "store"), tmp_path / "archive"
    before = call(bridge,"create",name="A.md",kind="note",content=note("before"))["doc"]
    write(archive,"A.md",note("after"));write(archive,"B.md",note("second"))
    original, injected = importer._install, False
    def fail(repo,source,relative,expected):
        nonlocal injected
        original(repo,source,relative,expected)
        if relative.endswith('/A.md') and not injected:
            injected = True
            raise OSError("synthetic IO failure")
    monkeypatch.setattr(importer,"_install",fail)
    with pytest.raises(CopalBridgeError,match="rolled back"):
        call(bridge,"import_vault",path=str(archive),note_kind="note")
    assert call(bridge,"get",id=before["id"])["text"] == before["text"]
    assert not (bridge._vault("alice","home") / 'B.md').exists()
    injected = False
    def after_commit(repo,source,relative,expected):
        nonlocal injected
        original(repo,source,relative,expected)
        if relative.endswith('/.copal/manifest.json') and not injected:
            injected = True
            raise OSError("synthetic postcommit failure")
    monkeypatch.setattr(importer,"_install",after_commit)
    result = call(bridge,"import_vault",path=str(archive),note_kind="note")
    assert result["notes"] == 2
    assert call(bridge,"get",id=before["id"])["text"] == note("after")


@pytest.mark.parametrize("phase",["before-journal","prepared","mid-files","committed","after-unlink","during-cleanup"])
def test_real_subprocess_crash_and_fresh_reader_recovery(tmp_path,phase):
    bridge, archive = LooseCopalBridge(tmp_path / "store"), tmp_path / "archive"
    before = call(bridge,"create",name="A.md",kind="note",content=note("before"))["doc"]
    write(archive,"A.md",note("after"));write(archive,"B.md",note("second"))
    context = multiprocessing.get_context('spawn')
    process = context.Process(target=_crash_import,args=(str(bridge.data_dir),str(archive),phase))
    process.start();process.join(30)
    if process.is_alive(): process.terminate();process.join(5)
    assert process.exitcode == 77
    receiver,sender = context.Pipe(duplex=False)
    reader = context.Process(target=_read_after_crash,args=(str(bridge.data_dir),before["id"],sender))
    reader.start();reader.join(30)
    if reader.is_alive(): reader.terminate();reader.join(5)
    assert reader.exitcode == 0 and receiver.poll(1)
    result = receiver.recv()
    assert result["text"] == note("after" if phase in {'committed','after-unlink','during-cleanup'} else "before")
    assert ('B.md' in result["names"]) is (phase in {'committed','after-unlink','during-cleanup'})
    assert not (bridge.data_dir / importer.JOURNAL).exists()
    assert not list(bridge.data_dir.glob('.copal-import-*'))


def test_recovery_fault_and_external_change_fail_closed(tmp_path,monkeypatch):
    bridge, archive = LooseCopalBridge(tmp_path / "store"), tmp_path / "archive"
    before = call(bridge,"create",name="A.md",kind="note",content=note("before"))["doc"]
    write(archive,"A.md",note("after"));write(archive,"B.md",note("second"))
    original = importer._install
    class Crash(BaseException): pass
    def crash(repo,source,relative,expected):
        original(repo,source,relative,expected)
        if relative.endswith('/A.md'): raise Crash()
    monkeypatch.setattr(importer,"_install",crash)
    with pytest.raises(Crash): call(bridge,"import_vault",path=str(archive),note_kind="note")
    target = bridge._vault('alice','home') / 'A.md'
    target.write_text('external change')
    monkeypatch.setattr(importer,"_install",original)
    with pytest.raises(CopalBridgeError,match='changed externally'): call(bridge,'get',id=before['id'])
    assert target.read_text() == 'external change'
    target.write_text(note('after'))
    def io_fail(*_args): raise OSError('synthetic recovery failure')
    monkeypatch.setattr(importer,'_install',io_fail)
    with pytest.raises(OSError): call(bridge,'get',id=before['id'])
    assert (bridge.data_dir / importer.JOURNAL).exists()
    monkeypatch.setattr(importer,'_install',original)
    assert call(LooseCopalBridge(bridge.data_dir),'get',id=before['id'])['text'] == before['text']


def test_normal_authenticated_public_zip_import406_and_typed_catalog(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.auth import AuthManager
    from core.database import ApiToken, SessionLocal
    from routes.auth_routes import setup_auth_routes, SESSION_COOKIE
    from routes.copal_routes import setup_copal_routes
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.request_auth import install_request_auth
    manager = AuthManager(str(tmp_path / 'auth.json'))
    policy = FilePolicyRepository(tmp_path / 'policy.db')
    app = FastAPI()
    app.state.auth_manager = manager
    app.state.files_policy_repository = policy
    app.state.copal_bridge = LooseCopalBridge(tmp_path / 'notes')
    async def account(request,next_call,_owner): return await next_call(request)
    install_request_auth(app,auth_manager=manager,session_factory=SessionLocal,api_token_model=ApiToken,account_call_next=account,trusted_loopback=lambda request:False,desktop_ready_path='/test-ready',session_cookie=SESSION_COOKIE)
    app.include_router(setup_auth_routes(manager))
    app.include_router(setup_copal_routes(policy_repository=policy))
    client = TestClient(app)
    data = io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        for index in range(406):
            flag = 'true' if index % 2 == 0 else '"true"'
            archive.writestr(f'Synthetic/doc-{index:03d}.md',f'---\nflag: {flag}\nrank: {index}\n---\n# Synthetic {index}\n')
    upload = lambda: {'file':('synthetic.zip',data.getvalue(),'application/zip')}
    assert client.post('/api/copal/import/obsidian',files=upload()).status_code == 401
    password = secrets.token_urlsafe(20)
    assert client.post('/api/auth/setup',json={'username':'alice','password':password}).status_code == 200
    assert client.post('/api/auth/login',json={'username':'alice','password':password}).status_code == 200
    response = client.post('/api/copal/import/obsidian?workspace=home&corpus=notes',files=upload())
    assert response.status_code == 200,response.text
    result = response.json()
    assert result['imported']['notes'] == result['preparation']['preparedDatabaseNotes'] == 406
    listed = client.get('/api/copal/documents?workspace=home').json()['docs']
    assert len([d for d in listed if d['name'].startswith('Synthetic/')]) == 406
    facets = client.get('/api/copal/graph/facets?workspace=home&facet_version=2&category=properties&property_key=flag').json()
    values = facets['facets']['properties']['flag']['values']
    assert {(v['type'],v['count']) for v in values} == {('boolean',203),('string',203)}
    repeated = client.post('/api/copal/import/obsidian?workspace=home&corpus=notes',files=upload())
    assert repeated.status_code == 200 and repeated.json()['imported']['unchanged'] == 406
    other_password = secrets.token_urlsafe(20)
    assert client.post('/api/auth/users',json={'username':'bob','password':other_password}).status_code == 200
    client.post('/api/auth/logout')
    assert client.post('/api/auth/login',json={'username':'bob','password':other_password}).status_code == 200
    assert not any(d['name'].startswith('Synthetic/') for d in client.get('/api/copal/documents?workspace=home').json()['docs'])
    assert not any(d['name'].startswith('Synthetic/') for d in client.get('/api/copal/documents?workspace=other').json()['docs'])


@pytest.mark.parametrize('limit',['MAX_BYTES','MAX_MEMBER_BYTES','MAX_TRANSACTION_BYTES','disk'])
def test_source_and_cumulative_staging_limits_precede_publication(tmp_path,monkeypatch,limit):
    from collections import namedtuple
    bridge, archive = LooseCopalBridge(tmp_path/'store'), tmp_path/'archive'
    before = call(bridge,'create',name='A.md',kind='note',content=note('old'))['doc']
    write(archive,'A.md',note('new'))
    manifest_path = bridge._manifest_path(bridge._vault('alice','home'))
    manifest_before = manifest_path.read_bytes()
    if limit == 'disk':
        usage = namedtuple('usage','total used free')
        monkeypatch.setattr(importer.shutil,'disk_usage',lambda _:usage(100,99,1))
    else:
        monkeypatch.setattr(importer,limit,1)
    with pytest.raises(CopalBridgeError,match='limit|headroom'):
        call(bridge,'import_vault',path=str(archive),note_kind='note')
    assert manifest_path.read_bytes() == manifest_before
    assert call(bridge,'get',id=before['id'])['text'] == note('old')
    assert not list(bridge.data_dir.glob('.copal-import-*'))


def test_attachment_ownership_and_settlement_are_not_bypassed(tmp_path,monkeypatch):
    from src.upload_handler import UploadHandler
    from src import tool_utils
    handler = UploadHandler(str(tmp_path),str(tmp_path/'uploads'))
    identifier = 'a'*32+'.txt'
    uploaded = write(Path(handler.upload_dir),identifier,'owned attachment')
    row = {'id':identifier,'owner':'bob','path':str(uploaded),'name':identifier,'size':uploaded.stat().st_size,'mime':'text/plain'}
    handler._atomic_write_json(str(Path(handler.upload_dir)/'uploads.json'),{'bob:synthetic':row})
    monkeypatch.setattr(tool_utils,'get_upload_handler',lambda:handler)
    bridge, archive = LooseCopalBridge(tmp_path/'store'), tmp_path/'archive'
    write(archive,'A.md',note('/api/upload/'+identifier))
    with pytest.raises(CopalBridgeError,match='rolled back'):
        call(bridge,'import_vault',path=str(archive),note_kind='note')
    assert not bridge._manifest_path(bridge._vault('alice','home')).exists()
    assert not handler._load_upload_index()['bob:synthetic'].get('reference_claims')
    row['owner'] = 'alice'
    handler._atomic_write_json(str(Path(handler.upload_dir)/'uploads.json'),{'alice:synthetic':row})
    assert call(bridge,'import_vault',path=str(archive),note_kind='note')['notes'] == 1
    assert handler._load_upload_index()['alice:synthetic']['reference_claims'] == ['domain:copal']


def test_unknown_staging_lookalikes_are_not_reclaimed(tmp_path):
    bridge = LooseCopalBridge(tmp_path/'store')
    unknown = bridge.data_dir/'.copal-import-unknown'
    unknown.mkdir(mode=0o700)
    write(unknown,'private','not our transaction')
    call(bridge,'index')
    assert (unknown/'private').read_text() == 'not our transaction'


def _crash_recovery(store,identifier):
    original = importer._install
    def install(repo,source,relative,expected):
        original(repo,source,relative,expected)
        os._exit(78)
    importer._install = install
    call(LooseCopalBridge(store),'get',id=identifier)


def test_real_process_crash_during_recovery_is_recoverable_again(tmp_path):
    bridge,archive = LooseCopalBridge(tmp_path/'store'),tmp_path/'archive'
    before = call(bridge,'create',name='A.md',kind='note',content=note('before'))['doc']
    write(archive,'A.md',note('after'));write(archive,'B.md',note('second'))
    context = multiprocessing.get_context('spawn')
    writer = context.Process(target=_crash_import,args=(str(bridge.data_dir),str(archive),'mid-files'))
    writer.start();writer.join(30)
    if writer.is_alive(): writer.terminate();writer.join(5)
    assert writer.exitcode == 77
    recovery = context.Process(target=_crash_recovery,args=(str(bridge.data_dir),before['id']))
    recovery.start();recovery.join(30)
    if recovery.is_alive(): recovery.terminate();recovery.join(5)
    assert recovery.exitcode == 78
    assert call(LooseCopalBridge(bridge.data_dir),'get',id=before['id'])['text'] == before['text']
    assert not (bridge.data_dir/importer.JOURNAL).exists()
    assert not list(bridge.data_dir.glob('.copal-import-*'))


def _held_import(store,archive,entered,release):
    original = importer._install
    def install(repo,source,relative,expected):
        original(repo,source,relative,expected)
        if relative.endswith('/A.md'):
            entered.set()
            if not release.wait(10): raise RuntimeError('synthetic lock test timed out')
    importer._install = install
    call(LooseCopalBridge(store),'import_vault',path=str(archive),note_kind='note')


def test_other_process_reader_cannot_observe_partially_published_batch(tmp_path):
    bridge,archive = LooseCopalBridge(tmp_path/'store'),tmp_path/'archive'
    before = call(bridge,'create',name='A.md',kind='note',content=note('before'))['doc']
    write(archive,'A.md',note('after'));write(archive,'B.md',note('second'))
    context = multiprocessing.get_context('spawn')
    entered,release = context.Event(),context.Event()
    writer = context.Process(target=_held_import,args=(str(bridge.data_dir),str(archive),entered,release))
    receiver,sender = context.Pipe(duplex=False)
    reader = context.Process(target=_read_after_crash,args=(str(bridge.data_dir),before['id'],sender))
    writer.start()
    try:
        assert entered.wait(10)
        reader.start()
        assert not receiver.poll(0.5), 'reader escaped cross-process commit lock'
        release.set()
        writer.join(15);reader.join(15)
        assert writer.exitcode == reader.exitcode == 0
        assert receiver.poll(1)
        result = receiver.recv()
        assert result['text'] == note('after') and 'B.md' in result['names']
    finally:
        release.set()
        for process in [writer,reader]:
            if process.pid is not None and process.is_alive(): process.terminate();process.join(5)


def test_source_swap_and_changed_preimage_fail_before_publication(tmp_path,monkeypatch):
    bridge,archive = LooseCopalBridge(tmp_path/'store'),tmp_path/'archive'
    before = call(bridge,'create',name='A.md',kind='note',content=note('before'))['doc']
    write(archive,'A.md',note('after'))
    original = importer._copy
    def changed(root,relative,target,**kwargs):
        if target.parent.name == 'before':
            write(root,relative,note('external independent edit'))
        return original(root,relative,target,**kwargs)
    monkeypatch.setattr(importer,'_copy',changed)
    with pytest.raises(CopalBridgeError,match='preserving preimage'):
        call(bridge,'import_vault',path=str(archive),note_kind='note')
    assert (bridge._vault('alice','home')/'A.md').read_text() == note('external independent edit')
    assert not (bridge.data_dir/importer.JOURNAL).exists()
    monkeypatch.setattr(importer,'_copy',original)
    original_collect = importer._collect
    outside = write(tmp_path,'outside','private outside')
    def swapped(root):
        result = original_collect(root)
        (root/'A.md').unlink();(root/'A.md').symlink_to(outside)
        return result
    monkeypatch.setattr(importer,'_collect',swapped)
    with pytest.raises(CopalBridgeError,match='rolled back'):
        call(bridge,'import_vault',path=str(archive),note_kind='note')
    assert outside.read_text() == 'private outside'


def test_updated_binary_assets_keep_history_and_repeat_unchanged(tmp_path):
    bridge,archive = LooseCopalBridge(tmp_path/'store'),tmp_path/'archive'
    write(archive,'image.bin',b'\x00before')
    call(bridge,'import_vault',path=str(archive))
    record = next(iter(bridge._load(bridge._vault('alice','home'))['documents'].values()))
    write(archive,'image.bin',b'\x00after')
    assert call(bridge,'import_vault',path=str(archive))['assets'] == 1
    history = bridge._vault('alice','home')/'.copal/history'/record['id']/(record['head']+'.bin')
    assert history.read_bytes() == b'\x00before'
    assert call(bridge,'import_vault',path=str(archive))['unchanged'] == 1


def test_restore_stale_heads_trashed_and_schema_future_guards(tmp_path):
    bridge,archive = LooseCopalBridge(tmp_path/'store'),tmp_path/'archive'
    before = call(bridge,'create',name='A.md',kind='note',content=note('before'))['doc']
    write(archive,'A.md',note('after'))
    with pytest.raises(CopalBridgeError,match='stale'):
        call(bridge,'import_vault',path=str(archive),note_kind='note',expected_heads={before['id']:'sha256:'+'0'*64+':0'})
    call(bridge,'trash',id=before['id'])
    with pytest.raises(CopalBridgeError,match='outside'):
        call(bridge,'import_vault',path=str(archive),note_kind='note',restore_ids={'A.md':{'id':before['id'],'kind':'note','corpus':'notes'}})
    (archive/'A.md').unlink()
    write(archive,'.events/current.md','---\ncopal_type: event\ncopal_schema: 2\n---\n# Synthetic event')
    write(archive,'.events/future.md','---\ncopal_type: event\ncopal_schema: 999\n---\n# Future inert')
    result = call(bridge,'import_vault',path=str(archive),note_kind='note')
    entries = {item['path']:item for item in result['entries']}
    assert entries['.events/current.md']['kind'] == 'copal-event'
    assert entries['.events/future.md']['kind'] == 'compatibility'


def test_large_prior_asset_counts_against_staging_budget(tmp_path,monkeypatch):
    bridge,archive = LooseCopalBridge(tmp_path/'store'),tmp_path/'archive'
    write(archive,'asset.bin',b'\0'*100_000)
    call(bridge,'import_vault',path=str(archive))
    write(archive,'asset.bin',b'\1')
    monkeypatch.setattr(importer,'MAX_TRANSACTION_BYTES',4096)
    with pytest.raises(CopalBridgeError,match='staging exceeds'):
        call(bridge,'import_vault',path=str(archive))
    assert (bridge._vault('alice','home')/'asset.bin').read_bytes() == b'\0'*100_000
    assert not list(bridge.data_dir.glob('.copal-import-*'))


@pytest.mark.parametrize('field,value',[('id',123),('kind',123),('corpus',123)])
def test_restore_identity_fields_must_be_strings_before_any_mutation(tmp_path,field,value):
    bridge,archive = LooseCopalBridge(tmp_path/'store'),tmp_path/'archive'
    write(archive,'A.md',note('new'))
    identity = {'id':'valid-id','kind':'note','corpus':'notes',field:value}
    with pytest.raises(CopalBridgeError,match='invalid or duplicate'):
        call(bridge,'import_vault',path=str(archive),note_kind='note',restore_ids={'A.md':identity})
    assert not bridge._vault('alice','home').exists()


@pytest.mark.parametrize('window',['before-old-head','after-verified-preimage'])
def test_expected_head_cannot_rebase_over_external_edit(tmp_path,monkeypatch,window):
    bridge,archive = LooseCopalBridge(tmp_path/'store'),tmp_path/'archive'
    before = call(bridge,'create',name='A.md',kind='note',content=note('before'))['doc']
    write(archive,'A.md',note('import wants to replace'))
    canonical = bridge._vault('alice','home')/'A.md'
    external = note('independent external edit must survive')
    manifest_path = bridge._manifest_path(bridge._vault('alice','home'))
    original_manifest = manifest_path.read_bytes()
    if window == 'before-old-head':
        fingerprint = importer._fingerprint
        seen = 0
        def changed(root,relative):
            nonlocal seen
            if root == bridge.data_dir and relative.endswith('/A.md'):
                seen += 1
                if seen == 2: canonical.write_text(external)
            return fingerprint(root,relative)
        monkeypatch.setattr(importer,'_fingerprint',changed)
    else:
        copy_file = importer._copy
        def changed(root,relative,target,**kwargs):
            result = copy_file(root,relative,target,**kwargs)
            if target.name.startswith('preimage-'): canonical.write_text(external)
            return result
        monkeypatch.setattr(importer,'_copy',changed)
    with pytest.raises(CopalBridgeError,match='stale import target'):
        call(bridge,'import_vault',path=str(archive),note_kind='note',expected_heads={before['id']:before['head']})
    assert canonical.read_text() == external
    assert manifest_path.read_bytes() == original_manifest
    assert not (bridge.data_dir/importer.JOURNAL).exists()
    assert not list(bridge.data_dir.glob('.copal-import-*'))
