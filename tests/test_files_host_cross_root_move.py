"""B2-P3b: Host cross-root managed document moves.

Real FilePolicyRepository and the real Files service (stat/content/capabilities) on temp dirs.
The managed native move is simulated at the client boundary by Native.request, following the B2-P2
wire contract (the real service needs a History hook the Python harness does not have).
Plain functions with bare asserts.
"""
import asyncio, hashlib, json, os, re, shutil, tempfile
from pathlib import Path

from src.openclank.file_policy import FilePolicyRepository
from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContext
from src.openclank.files_host_provider import HostFilesProvider, _device_of, _origin, _service_error
from src.openclank.files_service_client import FilesServiceError, OdysseusFilesClient, set_history_binding_provider
from src.openclank.media_ownership import MediaProvenance, binary_digest
from src.openclank.resource_refs import issue_resource_ref

BINARY = os.environ.get("OPEN_CLANK_FILES_SERVICE_BIN") or os.path.expanduser(
    "~/.cache/openclank-devtools/terra-rust/target/debug/odysseus-files-service")
OWNER = "account-alice"


class Registry:
    def app_scope(self, username, *, is_admin=False):
        return {"host": True}

    def visibility_for_subject(self, username):
        return []


class Native:
    """Real service for stat/content/capabilities; managed_file_move simulated per the B2-P2 wire contract."""

    def __init__(self, client):
        self.client, self.calls, self.committed, self.mutations, self.last_payload = client, [], {}, 0, None
        self.fail_after_commit = 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def request(self, operation, path, payload=None, root_operation_id=None):
        self.calls.append(operation)
        if operation == "capabilities":
            r = await self.client.request(operation, path, payload)
            return {**r, "operations": list(r.get("operations") or []) + ["managed_file_move", "managed_file_cleanup", "managed_file_move_status"]}
        if operation == "managed_file_move_status":
            self.status_calls = getattr(self, "status_calls", 0) + 1
            return {"data": {"operation_id": payload["operation_id"], "status": "committed" if payload["operation_id"] in self.committed else "unknown"}}
        if operation == "managed_file_cleanup":
            return {"data": {"operation_id": root_operation_id, "status": "committed", "cleanup": "complete"}}
        if operation != "managed_file_move":
            return await self.client.request(operation, path, payload)
        self.last_payload = json.loads(json.dumps(payload))
        self.last_target, self.last_root_operation = path, root_operation_id
        opid = payload["operation_id"]
        if opid in self.committed:
            return {"data": {"operation_id": opid, "status": "committed"}}
        parts = payload["participants"]
        for g in payload["guards"] + parts:
            p = g.get("source")
            if hashlib.sha256(Path(p).read_bytes()).hexdigest() != (g["expected_fingerprint"].split(":")[1]):
                raise FilesServiceError("filesystem operation failed", code="conflict")
        for part in parts:
            if part["destination"] is not None:
                d = os.path.dirname(part["destination"])
                while not os.path.isdir(d):
                    d = os.path.dirname(d)
                if os.stat(d).st_dev != os.stat(part["source"]).st_dev:
                    raise FilesServiceError("managed_cross_filesystem_unsupported", code="unsupported")
        self.mutations += 1
        for part in parts:
            src, dst, rep = part["source"], part["destination"], part["replacement"]
            data = bytes(rep) if rep is not None else Path(src).read_bytes()
            if dst is None:
                Path(src).write_bytes(data)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                Path(dst).write_bytes(data)
                os.unlink(src)
        self.committed[opid] = True
        if self.fail_after_commit:
            self.fail_after_commit -= 1
            raise FilesServiceError("transport lost", code="root_unavailable")
        return {"data": {"operation_id": opid, "status": "committed"}}


class World:
    def __init__(self, base=None):
        self.base = Path(base or tempfile.mkdtemp(prefix="b2p3b-")).resolve()
        for n in ("data", "xdg"):
            (self.base / n).mkdir(exist_ok=True)
        self.repo = FilePolicyRepository(str(self.base / "policy.db"))
        self.dirs = {}
        self.client = OdysseusFilesClient("alice", binary=BINARY, transport="framed")
        self.native = Native(self.client)
        self.provider = HostFilesProvider(registry=Registry(), client_factory=lambda username, **kw: self.native,
                                          operation_store=self.repo)
        self.locations, self.n = {}, 0
        set_history_binding_provider(self._binding)

    def _binding(self, owner, lane, scope=None):
        rid = (scope or {}).get("selected_history_root_id")
        loc = self.repo.get_location(rid)
        return {"account_id": OWNER, "workspace_id": f"files:{rid}", "workspace_root": loc.canonical_path,
                "root_bindings": [{"root_id": rid, "canonical_path": loc.canonical_path}]}

    def root(self, name, parent=None):
        path = (Path(parent) if parent else self.base) / name
        path.mkdir(parents=True, exist_ok=True)
        loc = self.repo.create_location(actor_subject_id="admin", path=str(path), kind="directory", capabilities=["read", "write"])
        self.locations[name] = loc
        return str(path.resolve())

    def workspace(self, location_name, folder="", wid="default"):
        self.repo.create_workspace(actor_subject_id="admin", owner_subject_id=OWNER, location_id=self.locations[location_name].id,
                                   name=wid, relative_folder=folder, workspace_id=wid)

    def ctx(self):
        return ProviderContext(OWNER, "alice", self.repo.generation(), workspace_id="default")

    def reserve(self, operation_id, item_id="i1"):
        g = self.repo.generation()
        self.repo.reserve_operation(owner_subject_id=OWNER, operation_id=operation_id, request_digest="d-" + operation_id,
                                    generation=g, receipt={"operation_id": operation_id, "_item_ids": [item_id], "state": "pending"})

    def token(self, path):
        return issue_resource_ref(owner_subject_id=OWNER, provider="host", origin_id=_origin(path), kind="file",
                                  capabilities=("stat", "open", "download", "move", "write"), policy_generation=self.repo.generation()).token

    def seed(self, doc, text, assets, *, origin, layout, root, workspace_id=None, media_dir=None, document_path=None):
        """Write a document with owned assets and one provenance row per asset."""
        Path(doc).parent.mkdir(parents=True, exist_ok=True)
        Path(doc).write_text(text, encoding="utf-8")
        for name, data in assets.items():
            p = Path(root) / media_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        for name, data in assets.items():
            self.n += 1
            record = MediaProvenance(canonical_root=root, origin=origin, owner_subject_id=OWNER, workspace_id=workspace_id,
                document_id=self.token(doc), document_path=document_path, asset_name=name, asset_digest=binary_digest(data),
                references=(), asset_id=f"asset-{self.n}", created_unix_ms=1, layout_version=layout, media_dir=media_dir)
            self.repo.reserve_operation(owner_subject_id=OWNER, operation_id=f"__host_media__seed-{self.n}",
                request_digest=f"dg{self.n}", generation=self.repo.generation(), receipt={"phase": "complete", "provenance": record.as_receipt()})

    async def revision(self, path):
        return dict((await self.provider.stat(self.ctx(), origin_id=_origin(path))).revision)

    def close(self):
        for extra in getattr(self, "extra", []):
            shutil.rmtree(extra, ignore_errors=True)
        set_history_binding_provider(None)
        self.client.close()
        shutil.rmtree(self.base, ignore_errors=True)


def tree(*roots):
    out = {}
    for root in roots:
        for folder, _d, files in os.walk(root):
            for f in files:
                p = os.path.join(folder, f)
                out[p] = hashlib.sha256(Path(p).read_bytes()).hexdigest()
    return out


def norm(o, w):
    s = json.dumps(o, sort_keys=True)
    s = s.replace(str(w.base), "<BASE>")
    import re
    # tokens are base64url: the trailing = padding varies with the token length
    s = re.sub(r"rr1\.[A-Za-z0-9_\-\.=]+", "<TOKEN>", s)
    s = re.sub(r'"created_unix_ms": \d+', '"created_unix_ms": 1', s)
    s = re.sub(r"location-[0-9a-f]{32}", "<LOC>", s)
    def fix(x):
        if isinstance(x, dict):
            return {k: (bytes(v).decode() if k == "replacement" and isinstance(v, list) else fix(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [fix(v) for v in x]
        return x
    return fix(json.loads(s))


async def ws_case(w):
    W = w.root("W"); w.workspace("W")
    doc = W + "/Note.md"
    w.seed(doc, "![i](.media/Note/img.png) [[.media/Note/img.png]]", {"img.png": b"PNGDATA"}, origin="workspace", layout=2, root=W,
           workspace_id="default", media_dir=".media/Note", document_path="Note.md")
    (Path(W) / "sub").mkdir()
    ctx = w.ctx(); w.reserve("op-ws")
    rev = await w.revision(doc)
    item = await w.provider.prepare_managed_workspace_move(ctx, source_origin_id=_origin(doc), destination_origin_id=_origin(W + "/sub/Moved.md"),
        expected_revision=rev, operation_id="op-ws", item_id="i1")
    r = await w.provider.execute_managed_workspace_item(ctx, operation_id="op-ws", item_id="i1")
    final = w.provider.load_managed_workspace_item(ctx, operation_id="op-ws", item_id="i1")
    return {"plan": item["plan"], "payload": w.native.last_payload, "result": r, "phase": final["phase"], "target": w.native.last_target,
            "tree": {k: v for k, v in tree(W).items()}, "text": Path(W + "/sub/Moved.md").read_text()}

async def loose_case(w):
    L = w.root("L")
    doc = L + "/a/Loose.md"
    w.seed(doc, "![i](media/Loose/img.png)", {"img.png": b"LOOSE"}, origin="loose", layout=1, root=L + "/a", workspace_id=None,
           media_dir="media/Loose", document_path="Loose.md")
    # loose assets live under dirname(doc)
    (Path(L) / "b").mkdir()
    ctx = w.ctx(); w.reserve("op-l")
    managed = dict(await w.provider.authorize_managed_root(ctx, origin_id=_origin(L), expected_revision=None))
    rev = await w.revision(doc)
    item = await w.provider.prepare_managed_workspace_move(ctx, source_origin_id=_origin(doc), destination_origin_id=_origin(L + "/b/Loose2.md"),
        expected_revision=rev, operation_id="op-l", item_id="i1", managed_root=managed)
    r = await w.provider.execute_managed_workspace_item(ctx, operation_id="op-l", item_id="i1", fresh_selected=managed)
    final = w.provider.load_managed_workspace_item(ctx, operation_id="op-l", item_id="i1")
    return {"plan": item["plan"], "payload": w.native.last_payload, "result": r, "phase": final["phase"], "target": w.native.last_target,
            "tree": tree(L), "text": Path(L + "/b/Loose2.md").read_text()}



_IDENTITY = {'loose': {'payload': {'guards': [],
                       'operation_id': 'host-managed-7c29426c78bdaa575a9be973ecab8f322d69ccf496fb9403494df2bf5cb35f21',
                       'participants': [{'destination': '<BASE>/L/b/Loose2.md',
                                         'expected_fingerprint': 'sha256:2a48e55990a63e0753ac302f932f3ab1f4d4a798e663309ad3c1d0231036149f:25',
                                         'kind': 'document',
                                         'replacement': '![i](<media/Loose2/img.png>)',
                                         'source': '<BASE>/L/a/Loose.md'},
                                        {'destination': '<BASE>/L/b/media/Loose2/img.png',
                                         'expected_fingerprint': 'sha256:7e3846c00b3be76a977b2456d3e3014c28a333deb87ee57d55921a854dfd4034:5',
                                         'kind': 'asset',
                                         'replacement': None,
                                         'source': '<BASE>/L/a/media/Loose/img.png'}]},
           'phase': 'complete',
           'plan': {'asset_proofs': [{'fingerprint': 'sha256:7e3846c00b3be76a977b2456d3e3014c28a333deb87ee57d55921a854dfd4034:5',
                                      'movable': True,
                                      'path': '<BASE>/L/a/media/Loose/img.png'}],
                    'coverage': 'app_known_declared_references',
                    'destination': '<BASE>/L/b/Loose2.md',
                    'generation': 1,
                    'location_id': '<LOC>',
                    'location_revision': 1,
                    'managed_root': {'generation': 1,
                                     'location_revision': 1,
                                     'root': '<BASE>/L',
                                     'root_id': '<LOC>',
                                     'scope': {'active_folder': None,
                                               'capabilities': ['read', 'write'],
                                               'generation': 1,
                                               'host': False,
                                               'root_capabilities': {'<LOC>': ['read', 'write']},
                                               'selected_history_root_id': '<LOC>',
                                               'visible_root_ids': ['<LOC>']}},
                    'native_operation_id': 'host-managed-7c29426c78bdaa575a9be973ecab8f322d69ccf496fb9403494df2bf5cb35f21',
                    'payload': {'guards': [],
                                'operation_id': 'host-managed-7c29426c78bdaa575a9be973ecab8f322d69ccf496fb9403494df2bf5cb35f21',
                                'participants': [{'destination': '<BASE>/L/b/Loose2.md',
                                                  'expected_fingerprint': 'sha256:2a48e55990a63e0753ac302f932f3ab1f4d4a798e663309ad3c1d0231036149f:25',
                                                  'kind': 'document',
                                                  'replacement': '![i](<media/Loose2/img.png>)',
                                                  'source': '<BASE>/L/a/Loose.md'},
                                                 {'destination': '<BASE>/L/b/media/Loose2/img.png',
                                                  'expected_fingerprint': 'sha256:7e3846c00b3be76a977b2456d3e3014c28a333deb87ee57d55921a854dfd4034:5',
                                                  'kind': 'asset',
                                                  'replacement': None,
                                                  'source': '<BASE>/L/a/media/Loose/img.png'}]},
                    'provenance_rows': [{'after': {'phase': 'complete',
                                                   'provenance': {'asset_digest': 'sha256:7e3846c00b3be76a977b2456d3e3014c28a333deb87ee57d55921a854dfd4034:5',
                                                                  'asset_id': 'asset-1',
                                                                  'asset_name': 'img.png',
                                                                  'canonical_root': '<BASE>/L/b',
                                                                  'created_unix_ms': 1,
                                                                  'document_id': '<TOKEN>',
                                                                  'document_path': 'Loose2.md',
                                                                  'layout_version': 1,
                                                                  'media_dir': 'media/Loose2',
                                                                  'origin': 'loose',
                                                                  'owner_subject_id': 'account-alice',
                                                                  'references': [],
                                                                  'workspace_id': None}},
                                         'before': {'phase': 'complete',
                                                    'provenance': {'asset_digest': 'sha256:7e3846c00b3be76a977b2456d3e3014c28a333deb87ee57d55921a854dfd4034:5',
                                                                   'asset_id': 'asset-1',
                                                                   'asset_name': 'img.png',
                                                                   'canonical_root': '<BASE>/L/a',
                                                                   'created_unix_ms': 1,
                                                                   'document_id': '<TOKEN>',
                                                                   'document_path': 'Loose.md',
                                                                   'layout_version': 1,
                                                                   'media_dir': 'media/Loose',
                                                                   'origin': 'loose',
                                                                   'owner_subject_id': 'account-alice',
                                                                   'references': [],
                                                                   'workspace_id': None}},
                                         'digest': 'dg1',
                                         'operation_id': '__host_media__seed-1'}],
                    'reference_proofs': [],
                    'root': '<BASE>/L',
                    'selected_folder': None,
                    'source': '<BASE>/L/a/Loose.md',
                    'source_revision': {'kind': 'hostFingerprint',
                                        'value': 'sha256:2a48e55990a63e0753ac302f932f3ab1f4d4a798e663309ad3c1d0231036149f:25'},
                    'workspace_id': 'default',
                    'workspace_revision': None},
           'result': {'cleanup': 'pending', 'outcome': 'committed'},
           'target': '<BASE>/L/a/Loose.md',
           'text': '![i](<media/Loose2/img.png>)',
           'tree': {'<BASE>/L/b/Loose2.md': '9751f7b04eb6dc4721d63bfe91d5af9bc0732be29924ceba6bf3634acf3ff838',
                    '<BASE>/L/b/media/Loose2/img.png': '7e3846c00b3be76a977b2456d3e3014c28a333deb87ee57d55921a854dfd4034'}},
 'workspace': {'payload': {'guards': [],
                           'operation_id': 'host-managed-672d0b30a7e855d3ad689823056b160ddf24e21fc172d693c1ca0d4f8ba0ad22',
                           'participants': [{'destination': '<BASE>/W/sub/Moved.md',
                                             'expected_fingerprint': 'sha256:e7f9a6d7f9800694b72e7208d7856b04b0de41ae64c41152a9862a737a54ce98:49',
                                             'kind': 'document',
                                             'replacement': '![i](<../.media/sub/Moved/img.png>) '
                                                            '[[../.media/sub/Moved/img.png]]',
                                             'source': '<BASE>/W/Note.md'},
                                            {'destination': '<BASE>/W/.media/sub/Moved/img.png',
                                             'expected_fingerprint': 'sha256:2d4566582844690f8634a8b2534ea5221560038c6c0650c99140759bad603ae2:7',
                                             'kind': 'asset',
                                             'replacement': None,
                                             'source': '<BASE>/W/.media/Note/img.png'}]},
               'phase': 'complete',
               'plan': {'asset_proofs': [{'fingerprint': 'sha256:2d4566582844690f8634a8b2534ea5221560038c6c0650c99140759bad603ae2:7',
                                          'movable': True,
                                          'path': '<BASE>/W/.media/Note/img.png'}],
                        'coverage': 'app_known_declared_references',
                        'destination': '<BASE>/W/sub/Moved.md',
                        'generation': 2,
                        'location_id': '<LOC>',
                        'location_revision': 1,
                        'managed_root': None,
                        'native_operation_id': 'host-managed-672d0b30a7e855d3ad689823056b160ddf24e21fc172d693c1ca0d4f8ba0ad22',
                        'payload': {'guards': [],
                                    'operation_id': 'host-managed-672d0b30a7e855d3ad689823056b160ddf24e21fc172d693c1ca0d4f8ba0ad22',
                                    'participants': [{'destination': '<BASE>/W/sub/Moved.md',
                                                      'expected_fingerprint': 'sha256:e7f9a6d7f9800694b72e7208d7856b04b0de41ae64c41152a9862a737a54ce98:49',
                                                      'kind': 'document',
                                                      'replacement': '![i](<../.media/sub/Moved/img.png>) '
                                                                     '[[../.media/sub/Moved/img.png]]',
                                                      'source': '<BASE>/W/Note.md'},
                                                     {'destination': '<BASE>/W/.media/sub/Moved/img.png',
                                                      'expected_fingerprint': 'sha256:2d4566582844690f8634a8b2534ea5221560038c6c0650c99140759bad603ae2:7',
                                                      'kind': 'asset',
                                                      'replacement': None,
                                                      'source': '<BASE>/W/.media/Note/img.png'}]},
                        'provenance_rows': [{'after': {'phase': 'complete',
                                                       'provenance': {'asset_digest': 'sha256:2d4566582844690f8634a8b2534ea5221560038c6c0650c99140759bad603ae2:7',
                                                                      'asset_id': 'asset-1',
                                                                      'asset_name': 'img.png',
                                                                      'canonical_root': '<BASE>/W',
                                                                      'created_unix_ms': 1,
                                                                      'document_id': '<TOKEN>',
                                                                      'document_path': 'sub/Moved.md',
                                                                      'layout_version': 2,
                                                                      'media_dir': '.media/sub/Moved',
                                                                      'origin': 'workspace',
                                                                      'owner_subject_id': 'account-alice',
                                                                      'references': [],
                                                                      'workspace_id': 'default'}},
                                             'before': {'phase': 'complete',
                                                        'provenance': {'asset_digest': 'sha256:2d4566582844690f8634a8b2534ea5221560038c6c0650c99140759bad603ae2:7',
                                                                       'asset_id': 'asset-1',
                                                                       'asset_name': 'img.png',
                                                                       'canonical_root': '<BASE>/W',
                                                                       'created_unix_ms': 1,
                                                                       'document_id': '<TOKEN>',
                                                                       'document_path': 'Note.md',
                                                                       'layout_version': 2,
                                                                       'media_dir': '.media/Note',
                                                                       'origin': 'workspace',
                                                                       'owner_subject_id': 'account-alice',
                                                                       'references': [],
                                                                       'workspace_id': 'default'}},
                                             'digest': 'dg1',
                                             'operation_id': '__host_media__seed-1'}],
                        'reference_proofs': [],
                        'root': '<BASE>/W',
                        'selected_folder': None,
                        'source': '<BASE>/W/Note.md',
                        'source_revision': {'kind': 'hostFingerprint',
                                            'value': 'sha256:e7f9a6d7f9800694b72e7208d7856b04b0de41ae64c41152a9862a737a54ce98:49'},
                        'workspace_id': 'default',
                        'workspace_revision': 1},
               'result': {'cleanup': 'pending', 'outcome': 'committed'},
               'target': '<BASE>/W/Note.md',
               'text': '![i](<../.media/sub/Moved/img.png>) [[../.media/sub/Moved/img.png]]',
               'tree': {'<BASE>/W/.media/sub/Moved/img.png': '2d4566582844690f8634a8b2534ea5221560038c6c0650c99140759bad603ae2',
                        '<BASE>/W/sub/Moved.md': '7490fab6ce7abe521265acfd8c3563bf32a1eb06b8f133c29bc7faa1e025bc5a'}}}


def run(body, *, shm=False):
    w = World()
    if shm:  # a second filesystem: /dev/shm is a different device from the home volume
        w.shm = Path(tempfile.mkdtemp(prefix="b2p3b-", dir="/dev/shm"))
        w.extra = [w.shm]
    try:
        return asyncio.run(body(w))
    finally:
        w.close()


def moves(native):
    return native.calls.count("managed_file_move")


def recorded(w, operation_id):
    receipt = (w.repo.get_operation(owner_subject_id=OWNER, operation_id=operation_id) or {}).get("receipt") or {}
    return receipt.get("_managed_items") or {}


def rows(w):
    return [r["receipt"]["provenance"] for r in w.repo.list_host_media_operations(owner_subject_id=OWNER, select=lambda r: True)]


async def directory(w, path):
    return dict(await w.revision(path))


async def seed_workspace(w, W, name="Note", text=None):
    doc = f"{W}/{name}.md"
    w.seed(doc, text or f"![i](.media/{name}/img.png) and [[.media/{name}/img.png]]", {"img.png": b"PNGDATA"}, origin="workspace",
           layout=2, root=W, workspace_id="default", media_dir=f".media/{name}", document_path=f"{name}.md")
    return doc


async def go(w, doc, dest, operation_id, *, managed_root=None, destination_root=None):
    ctx = w.ctx()
    w.reserve(operation_id)
    item = await w.provider.prepare_managed_workspace_move(ctx, source_origin_id=_origin(doc), destination_origin_id=_origin(dest),
        expected_revision=await w.revision(doc), operation_id=operation_id, item_id="i1", managed_root=managed_root,
        **({"destination_root": destination_root} if destination_root is not None else {}))
    result = await w.provider.execute_managed_workspace_item(ctx, operation_id=operation_id, item_id="i1",
        fresh_selected=managed_root, fresh_destination=destination_root)
    return item, result, w.provider.load_managed_workspace_item(ctx, operation_id=operation_id, item_id="i1")


async def refused(w, doc, dest, operation_id, code, **kw):
    roots = [loc.canonical_path for loc in w.locations.values()]
    before, calls = tree(*roots), moves(w.native)
    try:
        await go(w, doc, dest, operation_id, **kw)
    except FilesFacadeError as exc:
        assert exc.code == code, (exc.code, str(exc))
    else:
        raise AssertionError("not refused")
    assert moves(w.native) == calls == 0 and tree(*roots) == before and not recorded(w, operation_id)


def test_workspace_to_selected_root():
    async def body(w):
        W, S = w.root("W"), w.root("S")
        w.workspace("W")
        (Path(S) / "Sel").mkdir()
        doc = await seed_workspace(w, W)
        dest_root = dict(await w.provider.authorize_destination_root(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
        assert dest_root["selected_folder"]["path"] == S + "/Sel"
        item, result, final = await go(w, doc, S + "/Sel/Moved.md", "op-1", destination_root=dest_root)
        assert result["outcome"] == "committed" and final["phase"] == "complete" and moves(w.native) == 1
        assert not os.path.exists(doc) and not os.path.exists(W + "/.media/Note/img.png")
        assert Path(S + "/Sel/.media/Moved/img.png").read_bytes() == b"PNGDATA"
        text = Path(S + "/Sel/Moved.md").read_text()
        assert text == "![i](<.media/Moved/img.png>) and [[.media/Moved/img.png]]" and os.path.exists(S + "/Sel/" + ".media/Moved/img.png")
        assert w.native.last_target == doc and item["plan"]["destination_frame"]["origin"] == "selected_root"
        assert [r["origin"] for r in rows(w)] == ["selected_root"]
        r = rows(w)[0]
        assert (r["layout_version"], r["workspace_id"], r["canonical_root"], r["document_path"], r["media_dir"]) == (2, None, S + "/Sel", "Moved.md", ".media/Moved")
    run(body)


def test_selected_root_to_workspace():
    async def body(w):
        W, S = w.root("W"), w.root("S")
        w.workspace("W")
        (Path(S) / "Sel").mkdir()
        doc = S + "/Sel/Doc.md"
        w.seed(doc, "![i](.media/Doc/img.png)", {"img.png": b"X1"}, origin="selected_root", layout=2, root=S + "/Sel",
               media_dir=".media/Doc", document_path="Doc.md")
        managed = dict(await w.provider.authorize_selected_folder(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
        item, result, final = await go(w, doc, W + "/Moved.md", "op-2", managed_root=managed)
        assert result["outcome"] == "committed" and final["phase"] == "complete"
        assert not os.path.exists(doc) and not os.path.exists(S + "/Sel/.media/Doc/img.png")
        assert Path(W + "/.media/Moved/img.png").read_bytes() == b"X1" and Path(W + "/Moved.md").read_text() == "![i](<.media/Moved/img.png>)"
        r = rows(w)[0]
        assert (r["origin"], r["layout_version"], r["workspace_id"], r["canonical_root"], r["document_path"]) == ("workspace", 2, "default", W, "Moved.md")
    run(body)


def test_loose_to_workspace_changes_layout_v1_to_v2():
    async def body(w):
        W, L = w.root("W"), w.root("L")
        w.workspace("W")
        doc = L + "/a/Loose.md"
        w.seed(doc, "![i](media/Loose/img.png)", {"img.png": b"LOOSE"}, origin="loose", layout=1, root=L + "/a", media_dir="media/Loose", document_path="Loose.md")
        managed = dict(await w.provider.authorize_managed_root(w.ctx(), origin_id=_origin(L), expected_revision=None))
        item, result, final = await go(w, doc, W + "/Moved.md", "op-3", managed_root=managed)
        assert result["outcome"] == "committed" and not os.path.exists(doc) and not os.path.exists(L + "/a/media/Loose/img.png")
        assert Path(W + "/.media/Moved/img.png").read_bytes() == b"LOOSE" and Path(W + "/Moved.md").read_text() == "![i](<.media/Moved/img.png>)"
        r = rows(w)[0]
        assert (r["origin"], r["layout_version"], r["canonical_root"]) == ("workspace", 2, W)
    run(body)


def test_workspace_to_loose_changes_layout_v2_to_v1():
    async def body(w):
        W, L = w.root("W"), w.root("L")
        w.workspace("W")
        (Path(L) / "b").mkdir()
        doc = await seed_workspace(w, W)
        dest_root = dict(await w.provider.authorize_destination_root(w.ctx(), origin_id=_origin(L), expected_revision=None))
        assert dest_root.get("selected_folder") is None  # a registered root exactly is a loose root
        item, result, final = await go(w, doc, L + "/b/Loose2.md", "op-4", destination_root=dest_root)
        assert result["outcome"] == "committed" and not os.path.exists(doc) and not os.path.exists(W + "/.media/Note/img.png")
        assert Path(L + "/b/media/Loose2/img.png").read_bytes() == b"PNGDATA"
        assert Path(L + "/b/Loose2.md").read_text() == "![i](<media/Loose2/img.png>) and [[media/Loose2/img.png]]"
        r = rows(w)[0]
        assert (r["origin"], r["layout_version"], r["workspace_id"], r["canonical_root"], r["document_path"], r["media_dir"]) == (
            "loose", 1, None, L + "/b", "Loose2.md", "media/Loose2")
    run(body)


def test_destination_root_required_is_reported_and_missing_or_wrong_root_is_refused():
    async def body(w):
        W, S, L = w.root("W"), w.root("S"), w.root("L")
        w.workspace("W")
        (Path(S) / "Sel").mkdir()
        doc = await seed_workspace(w, W)
        ctx = w.ctx()
        assert w.provider.managed_destination_root_required(ctx, source_origin_id=_origin(doc), destination_origin_id=_origin(S + "/Sel/x.md"), source_is_directory=False)
        assert not w.provider.managed_destination_root_required(ctx, source_origin_id=_origin(doc), destination_origin_id=_origin(W + "/sub/x.md"), source_is_directory=False)
        await refused(w, doc, S + "/Sel/Moved.md", "op-5", "managed_media_move_unsupported")
        # a destination root that does not contain the destination
        other = dict(await w.provider.authorize_destination_root(ctx, origin_id=_origin(L), expected_revision=None))
        await refused(w, doc, S + "/Sel/Moved.md", "op-6", "managed_media_move_unsupported", destination_root=other)
        # a stale revision is refused by the existing selected-folder authorisation
        good = await directory(w, S + "/Sel")
        (Path(S) / "Sel" / "new.txt").write_text("changes the directory revision")
        try:
            await w.provider.authorize_destination_root(ctx, origin_id=_origin(S + "/Sel"), expected_revision=good)
        except FilesFacadeError as exc:
            assert exc.code == "resource_changed", exc.code
        else:
            raise AssertionError("stale revision accepted")
        # classification for a loose document is left to the client's root
        assert os.path.exists(doc)
    run(body)


def test_folder_three_roots_and_nested_roots_are_refused_before_any_native_call():
    async def body(w):
        W, S, L, Q = w.root("W"), w.root("S"), w.root("L"), w.root("Q")
        w.workspace("W")
        (Path(S) / "Sel").mkdir()
        (Path(L) / "dirA").mkdir()
        ctx = w.ctx()
        # folder across roots: loose root L -> selected folder in S
        managed = dict(await w.provider.authorize_managed_root(ctx, origin_id=_origin(L), expected_revision=None))
        dest = dict(await w.provider.authorize_destination_root(ctx, origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
        w.reserve("op-f")
        try:
            await w.provider.prepare_managed_loose_item(ctx, source_origin_id=_origin(L + "/dirA"), destination_origin_id=_origin(S + "/Sel"),
                expected_revision=await directory(w, L + "/dirA"), operation_id="op-f", item_id="i1", collision="fail",
                managed_root=managed, destination_root=dest)
        except FilesFacadeError as exc:
            assert exc.code == "managed_media_move_unsupported", exc.code
        else:
            raise AssertionError("folder moved across roots")
        assert not recorded(w, "op-f")
        # three roots: a declared reference lives in a third root
        doc = W + "/Note.md"
        third = Q + "/other.md"
        Path(third).write_text("third")
        w.seed(doc, "![i](.media/Note/img.png)", {"img.png": b"P"}, origin="workspace", layout=2, root=W, workspace_id="default",
               media_dir=".media/Note", document_path="Note.md")
        row = [r for r in w.repo.list_host_media_operations(owner_subject_id=OWNER, select=lambda r: True)][-1]
        provenance = dict(row["receipt"]["provenance"])
        provenance["references"] = [w.token(third)]
        w.repo.reserve_operation(owner_subject_id=OWNER, operation_id="__host_media__third", request_digest="dg-third",
            generation=w.repo.generation(), receipt={"phase": "complete", "provenance": {**provenance, "asset_name": "img.png", "asset_id": "a-third"}})
        await refused(w, doc, S + "/Sel/Moved.md", "op-3r", "resource_unavailable", destination_root=dest)
    run(body)


def test_nested_roots_are_refused():
    async def body(w):
        S = w.root("S")
        w.workspace("S")  # workspace root is S, which contains the selected folder
        (Path(S) / "Sel").mkdir()
        doc = S + "/Sel/Doc.md"
        w.seed(doc, "![i](.media/Doc/img.png)", {"img.png": b"X"}, origin="selected_root", layout=2, root=S + "/Sel", media_dir=".media/Doc", document_path="Doc.md")
        managed = dict(await w.provider.authorize_selected_folder(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
        (Path(S) / "other").mkdir()
        await refused(w, doc, S + "/other/Moved.md", "op-n", "managed_media_move_unsupported", managed_root=managed)
    run(body)


def test_planner_refusal_surfaces_its_code_and_prepares_nothing():
    async def body(w):
        W, S = w.root("W"), w.root("S")
        w.workspace("W")
        (Path(S) / "Sel").mkdir()
        doc = await seed_workspace(w, W, text="![i](.media/Note/img.png) ![x](keep.png)")
        Path(W + "/keep.png").write_bytes(b"k")
        dest = dict(await w.provider.authorize_destination_root(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
        await refused(w, doc, S + "/Sel/Moved.md", "op-p", "unmanaged_local_reference", destination_root=dest)
    run(body)


def claims(w):
    with w.repo._connect() as c:
        return c.execute("SELECT count(*) FROM file_operations WHERE lifecycle_phase='host_path_claim'").fetchone()[0]


async def two_disk_world(w):
    W = w.root("W")
    S = w.root("S", parent=w.shm)
    assert os.stat(W).st_dev != os.stat(S).st_dev
    w.workspace("W")
    (Path(S) / "Sel").mkdir()
    doc = await seed_workspace(w, W)
    dest = dict(await w.provider.authorize_destination_root(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
    return W, S, doc, dest


def test_cross_disk_is_refused_before_anything_is_prepared_or_recorded():
    async def body(w):
        W, S, doc, dest = await two_disk_world(w)
        before = tree(W, S)
        try:
            await go(w, doc, S + "/Sel/Moved.md", "op-d", destination_root=dest)
        except FilesFacadeError as exc:
            assert exc.code == "managed_cross_filesystem_unsupported" and str(exc).startswith("This move crosses disks."), (exc.code, str(exc))
        else:
            raise AssertionError("cross-disk move was prepared")
        # no native call of any managed kind, no item, no plan, no path claim, files untouched
        assert moves(w.native) == 0 and "managed_file_move" not in w.native.calls
        assert not recorded(w, "op-d") and claims(w) == 0 and tree(W, S) == before
        # the destination's media directory does not exist yet: the nearest existing ancestor decides
        assert not os.path.exists(S + "/Sel/.media") and _device_of(S + "/Sel/.media/Moved") == os.stat(S).st_dev != os.stat(W).st_dev
    run(body, shm=True)


def test_native_cross_disk_refusal_is_mapped_when_the_precheck_is_bypassed():
    import src.openclank.files_host_provider as module

    async def body(w):
        W, S, doc, dest = await two_disk_world(w)
        before = tree(W, S)
        original, module._device_of = module._device_of, (lambda path: 0)
        try:
            item, result, final = await go(w, doc, S + "/Sel/Moved.md", "op-d2", destination_root=dest)
        finally:
            module._device_of = original
        # Past the dispatched mark every error is pending reconciliation (Codex baseline); never `failed`.
        assert result == {"outcome": "pending", "code": "managed_reconciliation_required"}, result
        assert tree(W, S) == before and w.native.mutations == 0 and final["phase"] == "dispatched"
        error = _service_error(FilesServiceError("managed_cross_filesystem_unsupported", code="unsupported"))
        assert error.code == "managed_cross_filesystem_unsupported" and str(error).startswith("This move crosses disks.")
        assert _service_error(FilesServiceError("filesystem operation failed", code="conflict")).code == "resource_changed"
    run(body, shm=True)


def test_same_disk_cross_root_still_moves_with_the_precheck():
    async def body(w):
        W, S = w.root("W"), w.root("S")
        w.workspace("W")
        (Path(S) / "Sel").mkdir()
        doc = await seed_workspace(w, W)
        dest = dict(await w.provider.authorize_destination_root(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
        item, result, final = await go(w, doc, S + "/Sel/Moved.md", "op-ok", destination_root=dest)
        assert result["outcome"] == "committed" and _device_of(S + "/x/y/z") == os.stat(S).st_dev
    run(body)


def test_interrupted_after_dispatch_resumes_without_a_second_mutation():
    async def body(w):
        W, S = w.root("W"), w.root("S")
        w.workspace("W")
        (Path(S) / "Sel").mkdir()
        doc = await seed_workspace(w, W)
        dest = dict(await w.provider.authorize_destination_root(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
        w.native.fail_after_commit = 1
        item, first, mid = await go(w, doc, S + "/Sel/Moved.md", "op-i", destination_root=dest)
        assert first == {"outcome": "pending", "code": "managed_reconciliation_required"} and mid["phase"] == "dispatched"
        assert w.native.mutations == 1 and os.path.exists(S + "/Sel/Moved.md")
        again = await w.provider.execute_managed_workspace_item(w.ctx(), operation_id="op-i", item_id="i1", fresh_destination=dest)
        final = w.provider.load_managed_workspace_item(w.ctx(), operation_id="op-i", item_id="i1")
        assert again["outcome"] == "committed" and final["phase"] == "complete" and w.native.mutations == 1
        assert rows(w)[0]["origin"] == "selected_root"
    run(body)


def test_same_root_moves_are_identical_to_the_unmodified_code():
    for name, fn in (("workspace", ws_case), ("loose", loose_case)):
        w = World()
        try:
            assert norm(asyncio.run(fn(w)), w) == _IDENTITY[name], name
        finally:
            w.close()


def test_facade_reports_destination_root_and_moves_with_it():
    async def body(w):
        W, S = w.root("W"), w.root("S")
        w.workspace("W")
        (Path(S) / "Sel").mkdir()
        doc = await seed_workspace(w, W)
        ctx = w.ctx()
        facade = FilesFacade([w.provider], operation_store=w.repo)
        async def ref(path):
            return facade._resource(ctx, "host", await w.provider.stat(ctx, origin_id=_origin(path)))
        src, folder = await ref(doc), await ref(S + "/Sel")
        sources = [{"resource_ref": src["ref"], "expected_revision": src.get("revision")}]
        wanted = await facade.transfer_root_requirement(ctx, generation=ctx.policy_generation, sources=sources, destination_ref=folder["ref"])
        assert wanted == {"root_required": False, "destination_root_required": True}, wanted
        inside = await ref(W)
        assert (await facade.transfer_root_requirement(ctx, generation=ctx.policy_generation, sources=sources,
                                                        destination_ref=inside["ref"]))["destination_root_required"] is False
        sources = [{"item_id": "i1", "resource_ref": src["ref"], "expected_revision": src.get("revision")}]
        receipt = await facade.transfer_resources(ctx, operation_id="op-fx", kind="move", sources=sources, destination_ref=folder["ref"],
            destination_root={"resource_ref": folder["ref"], "expected_revision": folder.get("revision")})
        assert receipt["items"][0]["outcome"] == "committed", receipt
        assert Path(S + "/Sel/Note.md").exists() and Path(S + "/Sel/.media/Note/img.png").exists() and not os.path.exists(doc)
        # without the destination root the managed document is refused and nothing moves
        doc2 = await seed_workspace(w, W, "Two")
        src2 = await ref(doc2)
        receipt = await facade.transfer_resources(ctx, operation_id="op-fy", kind="move",
            sources=[{"item_id": "i1", "resource_ref": src2["ref"], "expected_revision": src2.get("revision")}], destination_ref=folder["ref"])
        assert receipt["items"][0]["outcome"] != "committed" and os.path.exists(doc2), receipt
    run(body)



# --- BQ-13: Host cross-root status, post-dispatch state, collisions, lost replies ---------------------------------------------------

async def selected_destination(w):
    W, S = w.root("W"), w.root("S")
    w.workspace("W")
    (Path(S) / "Sel").mkdir()
    doc = await seed_workspace(w, W)
    dest = dict(await w.provider.authorize_destination_root(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
    return W, S, doc, dest


async def prepare(w, doc, dest_path, operation_id, dest):
    ctx = w.ctx(); w.reserve(operation_id)
    item = await w.provider.prepare_managed_workspace_move(ctx, source_origin_id=_origin(doc), destination_origin_id=_origin(dest_path),
        expected_revision=await w.revision(doc), operation_id=operation_id, item_id="i1", destination_root=dest)
    return ctx, item


def phase_of(w, operation_id):
    return recorded(w, operation_id)["i1"]["phase"]


def test_lost_reply_at_claimed_retries_with_the_same_operation_id_and_one_dispatch():
    async def body(w):
        W, S, doc, dest = await selected_destination(w)
        ctx, item = await prepare(w, doc, S + "/Sel/Moved.md", "op-lc", dest)
        native_id = item["plan"]["native_operation_id"]
        w.repo.claim_managed_transfer_item(owner_subject_id=OWNER, operation_id="op-lc", item_id="i1", native_operation_id=native_id,
                                           generation=int(ctx.policy_generation))      # the process died right after the claim
        assert phase_of(w, "op-lc") == "claimed" and moves(w.native) == 0
        result = await w.provider.execute_managed_workspace_item(ctx, operation_id="op-lc", item_id="i1", fresh_destination=dest)
        assert result["outcome"] == "committed" and phase_of(w, "op-lc") == "complete"
        assert moves(w.native) == 1 and w.native.mutations == 1 and w.native.last_root_operation == native_id
        assert Path(S + "/Sel/.media/Moved/img.png").read_bytes() == b"PNGDATA" and not os.path.exists(doc)
    run(body)


def test_lost_reply_at_native_committed_settles_without_a_second_dispatch():
    async def body(w):
        W, S, doc, dest = await selected_destination(w)
        ctx, item = await prepare(w, doc, S + "/Sel/Moved.md", "op-ln", dest)
        real, failures = w.repo.settle_managed_transfer_item, [1]
        def settle(**kw):
            if failures[0]:
                failures[0] -= 1
                raise OSError("store lost")
            return real(**kw)
        w.repo.settle_managed_transfer_item = settle
        first = await w.provider.execute_managed_workspace_item(ctx, operation_id="op-ln", item_id="i1", fresh_destination=dest)
        assert first == {"outcome": "pending", "code": "managed_metadata_pending"} and phase_of(w, "op-ln") == "native_committed"
        assert moves(w.native) == 1 and w.native.mutations == 1
        again = await w.provider.execute_managed_workspace_item(ctx, operation_id="op-ln", item_id="i1", fresh_destination=dest)
        assert again["outcome"] == "committed" and phase_of(w, "op-ln") == "complete"
        assert moves(w.native) == 1 and w.native.mutations == 1 and w.native.last_root_operation == item["plan"]["native_operation_id"]
        assert len(rows(w)) == 1 and rows(w)[0]["origin"] == "selected_root"
    run(body)


def test_lost_reply_at_metadata_pending_settles_without_a_second_dispatch():
    from src.openclank import file_policy as fp
    async def body(w):
        W, S, doc, dest = await selected_destination(w)
        ctx, item = await prepare(w, doc, S + "/Sel/Moved.md", "op-lm", dest)
        real, drifted = w.repo.settle_managed_transfer_item, [1]
        def settle(**kw):
            if not drifted[0]:
                return real(**kw)
            drifted[0] = 0
            with w.repo._connect() as c:      # at settlement time the provenance row has drifted: the exact update cannot publish
                raw = c.execute("SELECT receipt_ciphertext FROM file_operations WHERE operation_id=?", ("__host_media__seed-1",)).fetchone()[0]
                c.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE operation_id=?",
                          (fp.encrypt(fp._json({"phase": "complete", "provenance": {"drift": True}})), "__host_media__seed-1"))
            try:
                return real(**kw)
            finally:
                with w.repo._connect() as c:
                    c.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE operation_id=?", (raw, "__host_media__seed-1"))
        w.repo.settle_managed_transfer_item = settle
        first = await w.provider.execute_managed_workspace_item(ctx, operation_id="op-lm", item_id="i1", fresh_destination=dest)
        assert first == {"outcome": "pending", "code": "managed_metadata_pending"} and phase_of(w, "op-lm") == "metadata_pending"
        assert moves(w.native) == 1 and w.native.mutations == 1
        again = await w.provider.execute_managed_workspace_item(ctx, operation_id="op-lm", item_id="i1", fresh_destination=dest)
        assert again["outcome"] == "committed" and phase_of(w, "op-lm") == "complete"
        assert moves(w.native) == 1 and w.native.mutations == 1 and w.native.last_root_operation == item["plan"]["native_operation_id"]
        assert rows(w)[0]["origin"] == "selected_root"
    run(body)


def test_existing_destination_document_or_asset_is_refused_before_any_native_move():
    async def body(w):
        W, S, doc, dest = await selected_destination(w)
        before = tree(W, S)
        # a file already at the destination document path
        Path(S + "/Sel/Moved.md").write_text("someone else's file")
        await refused(w, doc, S + "/Sel/Moved.md", "op-c1", "resource_changed", destination_root=dest)
        os.unlink(S + "/Sel/Moved.md")
        # a file already at the destination asset path
        (Path(S) / "Sel/.media/Moved").mkdir(parents=True)
        Path(S + "/Sel/.media/Moved/img.png").write_bytes(b"not ours")
        await refused(w, doc, S + "/Sel/Moved.md", "op-c2", "resource_changed", destination_root=dest)
        assert Path(doc).read_text() == "![i](.media/Note/img.png) and [[.media/Note/img.png]]" and Path(W + "/.media/Note/img.png").read_bytes() == b"PNGDATA"
        assert Path(S + "/Sel/.media/Moved/img.png").read_bytes() == b"not ours"
    run(body)


def test_same_name_neighbour_assets_and_unrelated_references_are_untouched():
    async def body(w):
        W, S = w.root("W"), w.root("S")
        w.workspace("W")
        (Path(S) / "Sel/.media/Other").mkdir(parents=True)
        (Path(W) / ".media/Other").mkdir(parents=True)
        Path(S + "/Sel/.media/Other/img.png").write_bytes(b"dest neighbour")       # same name, destination area, not a participant
        Path(W + "/.media/Other/img.png").write_bytes(b"source neighbour")        # same name, another document's media
        text = "![i](.media/Note/img.png) [site](https://example.com/img.png) [top](#top)"
        doc = await seed_workspace(w, W, text=text)
        dest = dict(await w.provider.authorize_destination_root(w.ctx(), origin_id=_origin(S + "/Sel"), expected_revision=await directory(w, S + "/Sel")))
        item, result, final = await go(w, doc, S + "/Sel/Moved.md", "op-n", destination_root=dest)
        assert result["outcome"] == "committed" and final["phase"] == "complete"
        assert Path(S + "/Sel/.media/Other/img.png").read_bytes() == b"dest neighbour"
        assert Path(W + "/.media/Other/img.png").read_bytes() == b"source neighbour"
        assert Path(S + "/Sel/.media/Moved/img.png").read_bytes() == b"PNGDATA"
        assert Path(S + "/Sel/Moved.md").read_text() == "![i](<.media/Moved/img.png>) [site](https://example.com/img.png) [top](#top)"
        assert {part["kind"] for part in w.native.last_payload["participants"]} == {"document", "asset"} and len(w.native.last_payload["participants"]) == 2
    run(body)


def test_a_device_change_between_plan_and_claim_is_refused_and_the_item_is_retired():
    import src.openclank.files_host_provider as module

    async def body(w):
        W, S, doc, dest = await two_disk_world(w)
        before = tree(W, S)
        original, module._device_of = module._device_of, (lambda path: 0)       # planning saw one device
        try:
            ctx, item = await prepare(w, doc, S + "/Sel/Moved.md", "op-dc", dest)
        finally:
            module._device_of = original                                        # by the claim the real devices differ
        result = await w.provider.execute_managed_workspace_item(ctx, operation_id="op-dc", item_id="i1", fresh_destination=dest)
        assert result == {"outcome": "failed", "code": "managed_cross_filesystem_unsupported"}, result
        assert phase_of(w, "op-dc") == "rejected" and claims(w) == 0
        assert moves(w.native) == 0 and tree(W, S) == before
    run(body, shm=True)


def tamper_plan(w, operation_id, change):
    from src.openclank import file_policy as fp
    receipt = w.repo.get_operation(owner_subject_id=OWNER, operation_id=operation_id)["receipt"]
    change(receipt["_managed_items"]["i1"]["plan"])
    with w.repo._connect() as c:
        c.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE operation_id=?", (fp.encrypt(fp._json(receipt)), operation_id))


def test_status_of_a_dispatched_cross_root_item_observes_native_and_never_moves():
    async def body(w):
        W, S, doc, dest = await selected_destination(w)
        w.native.fail_after_commit = 1
        item, first, mid = await go(w, doc, S + "/Sel/Moved.md", "op-s", destination_root=dest)
        assert first["outcome"] == "pending" and mid["phase"] == "dispatched" and moves(w.native) == 1
        ctx = w.ctx()
        status = await w.provider.operation_status(ctx, operation_id="op-s")
        assert [row["outcome"] for row in status["items"]] == ["committed"], status
        assert phase_of(w, "op-s") == "complete" and moves(w.native) == 1 and w.native.mutations == 1 and w.native.status_calls == 1
        assert rows(w)[0]["origin"] == "selected_root"
    run(body)


def test_status_returns_the_native_state_unknown_without_a_move():
    async def body(w):
        W, S, doc, dest = await selected_destination(w)
        w.native.fail_after_commit = 1
        await go(w, doc, S + "/Sel/Moved.md", "op-u", destination_root=dest)
        del w.native.committed[next(iter(w.native.committed))]                  # Native has no committed journal for this id
        status = await w.provider.operation_status(w.ctx(), operation_id="op-u")
        assert status["items"][0]["outcome"] == "pending" and status["items"][0]["code"] == "managed_reconciliation_required"
        assert phase_of(w, "op-u") == "dispatched" and moves(w.native) == 1 and w.native.status_calls == 1
    run(body)


def test_status_with_a_changed_destination_authority_needs_authority_and_sends_nothing():
    async def body(w):
        W, S, doc, dest = await selected_destination(w)
        w.native.fail_after_commit = 1
        await go(w, doc, S + "/Sel/Moved.md", "op-a", destination_root=dest)
        tamper_plan(w, "op-a", lambda plan: plan["destination_root"].update(location_revision=99))
        status = await w.provider.operation_status(w.ctx(), operation_id="op-a")
        assert status["items"][0] == {"item_id": "i1", "outcome": "pending", "code": "managed_needs_authority"}, status
        assert getattr(w.native, "status_calls", 0) == 0 and moves(w.native) == 1 and phase_of(w, "op-a") == "dispatched"
    run(body)
