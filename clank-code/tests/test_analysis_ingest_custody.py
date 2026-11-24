import asyncio
import hashlib
import os
import pathlib
import types

from src.openclank.analysis_projects import AnalysisProjects, manifest, project_identity
from src.openclank.files_facade import FilesFacadeError
from src.openclank.files_host_provider import _origin

FIX = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'analysis'
SOURCE = (FIX / 'ingest-source.bin').read_bytes()
STATIC = (FIX / 'ingest-static-v1.jsonl').read_bytes()
TRACE = (FIX / 'ingest-trace-v1.jsonl').read_bytes()
WS = '/ws'


def _fp(data, version):
    return {'kind': 'hostFingerprint', 'value': 'sha256:' + hashlib.sha256(data).hexdigest() + ':' + str(version)}


class Fs:
    """Disposable in-memory stand-in for the Files facade, Host provider and Host: path -> [bytes, version]."""
    def __init__(self, identity):
        self.identity, self.files, self.dirs, self.ok = identity, {}, set(), True
        path = WS
        self.dirs.add(path)
        for part in ('.clanker', 'robonotes', identity['projectKey'], 'analysis'):
            path = os.path.join(path, part)
            self.dirs.add(path)
        self.analysis = path
        self.files[os.path.join(path, '00-index.md')] = [manifest(identity).encode(), 1]
        self.files[WS + '/src.bin'] = [SOURCE, 1]
        self.mutation_scope = object()

    def entry(self, path, inspect=False):
        if path in self.dirs:
            return types.SimpleNamespace(origin_id=_origin(path), kind='folder', size=0, capabilities={'children', 'write', 'open'}, revision=_fp(b'', 0))
        data, version = self.files[path]
        revision = {'kind': 'hostObjectIdentity', 'value': 'oid:%d' % version} if inspect else _fp(data, version)
        return types.SimpleNamespace(origin_id=_origin(path), kind='file', size=len(data), capabilities={'open', 'download'}, revision=revision)

    def path(self, origin):
        return next(p for p in list(self.files) + list(self.dirs) if _origin(p) == origin)

    # host
    async def resource_for_path(self, context, path):
        return self.entry(path)

    async def stat_inspection(self, context, origin_id):
        return self.entry(self.path(origin_id), inspect=True)

    # provider
    async def stat(self, context, origin_id):
        return self.entry(self.path(origin_id))

    async def content(self, context, origin_id):
        path = self.path(origin_id)
        data = self.files[path][0]

        async def stream(start, end):
            yield data[start:end]
        return types.SimpleNamespace(origin_id=origin_id, size=len(data), etag='oid:%d' % self.files[path][1], stream=stream, data=None, path=None)

    # facade
    def _resource(self, context, provider, resource):
        return {'ref': 'ref:' + resource.origin_id, 'id': resource.origin_id, 'revision': resource.revision}

    def _provider_for_ref(self, context, ref, capability=None):
        origin = _origin(WS + '/src.bin') if ref == 'src-ref' else ref[4:]
        return self, types.SimpleNamespace(provider='host', stable_id='src-1' if ref == 'src-ref' else origin, origin_id=origin)

    @staticmethod
    def _provider_revision(entry):
        return dict(entry.revision)

    def _generation_current(self, context):
        return True

    async def open_payload(self, context, resource_ref, expected_revision=None):
        return {'payload': {'text': self.files[self.path(resource_ref[4:])][0].decode('utf-8')}}

    async def create(self, context, parent_ref, name, text, before_dispatch=None):
        path = os.path.join(self.path(parent_ref[4:]), name)
        if path in self.files:
            raise FilesFacadeError('exists', code='resource_changed')
        if before_dispatch:
            await before_dispatch()
        self.files[path] = [text.encode(), 1]
        return {'outcome': 'created', 'name': name}

    async def save_resource(self, context, resource_ref, expected_revision, text, before_dispatch=None):
        path = self.path(resource_ref[4:])
        if self.entry(path).revision != expected_revision:
            raise FilesFacadeError('stale', code='resource_changed')
        if before_dispatch:
            await before_dispatch()
        self.files[path] = [text.encode(), self.files[path][1] + 1]
        return {'outcome': 'saved', 'name': os.path.basename(path)}

    def set_source(self, data):
        self.files[WS + '/src.bin'] = [data, self.files[WS + '/src.bin'][1] + 1]


def _env(owner='owner-1', production=False):
    identity = project_identity(owner, 'ws-1')
    fs = Fs(identity)
    context = types.SimpleNamespace(owner_subject_id=owner, workspace_id='ws-1', policy_generation=1)

    async def current(binding, ident, ctx):
        return fs.ok
    projects = AnalysisProjects(facade=fs, host=fs, context=context, binding=types.SimpleNamespace(path=WS), identity=identity, current=current)
    if not production:   # the fixture adapter is unqualified: only an explicit test-only adapter object may use it
        import functools
        from src.openclank.analysis_adapters.ingest import REGISTRY
        projects.save_ingest = functools.partial(projects.save_ingest, _test_adapter=REGISTRY['fixture_jsonl'])
        projects.read_ingest = functools.partial(projects.read_ingest, _test_adapter=REGISTRY['fixture_jsonl'])
    return projects, fs


def _body(fs, data=STATIC, **extra):
    return {'workspaceID': 'ws-1', 'sourceRef': 'src-ref', 'sourceRevision': fs.entry(WS + '/src.bin').revision,
            'adapterID': 'fixture_jsonl', 'data': data, **extra}


def _run(coro):
    return asyncio.run(coro)


def _refused(coro, text=None):
    try:
        _run(coro)
    except (ValueError, FilesFacadeError) as exc:
        return text is None or str(exc) == text
    return False


def _names(fs):
    return sorted(os.path.basename(p) for p in fs.files if p.startswith(fs.analysis + '/'))


def test_round_trip_static_and_trace_fixtures():
    for data, kind in ((STATIC, 'static_db'), (TRACE, 'trace')):
        projects, fs = _env()
        saved = _run(projects.save_ingest(_body(fs, data)))
        assert saved['sourceCurrent'] is True and saved['receipt']['outcome'] == 'created'
        read = _run(projects.read_ingest(saved['ingestID'], 'src-ref'))
        assert read['state'] == 'verified' and read['sourceState'] == 'current'
        assert read['result']['kind'] == kind and read['result']['rows'] and read['result']['receiptKind'] == 'ingest'
        assert read['resultIdentity']['rawSHA256'] == hashlib.sha256(data).hexdigest()


def test_stored_file_is_markdown_in_analysis_folder_only():
    projects, fs = _env()
    saved = _run(projects.save_ingest(_body(fs)))
    name = 'ingest-' + saved['ingestID'] + '.md'
    assert name in _names(fs) and fs.files[fs.analysis + '/' + name][0].startswith(b'```json\n')
    assert all('provider-db' not in p for p in list(fs.files) + list(fs.dirs))
    assert all(p.startswith(fs.analysis) or p == WS + '/src.bin' for p in fs.files)
    assert STATIC not in fs.files[fs.analysis + '/' + name][0]


def test_id_is_content_derived():
    a, fa = _env()
    b, fb = _env()
    first = _run(a.save_ingest(_body(fa)))['ingestID']
    assert first == _run(b.save_ingest(_body(fb)))['ingestID']
    assert _run(a.save_ingest(_body(fa, TRACE)))['ingestID'] != first


def test_changed_source_reads_historical_and_no_source_is_unchecked():
    projects, fs = _env()
    ingest_id = _run(projects.save_ingest(_body(fs)))['ingestID']
    assert _run(projects.read_ingest(ingest_id))['sourceState'] == 'unchecked'
    fs.set_source(b'x' * 16)
    read = _run(projects.read_ingest(ingest_id, 'src-ref'))
    assert read['sourceState'] == 'historical' and read['result']['sourceState'] == 'historical'


def test_stale_and_missing_cas_refused():
    projects, fs = _env()
    ingest_id = _run(projects.save_ingest(_body(fs)))['ingestID']
    before = dict(fs.files)
    assert _refused(projects.save_ingest(_body(fs)))  # create over existing
    stale = dict(fs.entry(fs.analysis + '/ingest-' + ingest_id + '.md').revision)
    stale['value'] = stale['value'][:-1] + '9'
    assert _refused(projects.save_ingest(_body(fs, expectedRevision=stale)), 'ingest changed')
    assert fs.files == before
    good = _run(projects.read_ingest(ingest_id))['revision']
    assert _run(projects.save_ingest(_body(fs, expectedRevision=good)))['receipt']['outcome'] == 'saved'
    assert _refused(projects.save_ingest(_body(fs, expectedRevision=good)))  # revision moved on


def test_tampered_stored_file_refused():
    projects, fs = _env()
    ingest_id = _run(projects.save_ingest(_body(fs)))['ingestID']
    path = fs.analysis + '/ingest-' + ingest_id + '.md'
    good = fs.files[path][0].decode()
    for bad in (good.replace('"entry"', '"entrx"', 1), good.replace('"rawSHA256":"', '"rawSHA256":"0', 1), good.replace('"normalizedSHA256":"', '"normalizedSHA256":"0', 1),
                good + '\n', good.replace('"version":1', '"version":2', 1), good.replace('"ingestID":"', '"ingestID":"0', 1), 'junk'):
        assert bad != good or bad == 'junk'
        fs.files[path] = [bad.encode(), fs.files[path][1] + 1]
        assert _refused(projects.read_ingest(ingest_id)), bad[:300]
    fs.files[path] = [good.encode(), fs.files[path][1] + 1]
    assert _run(projects.read_ingest(ingest_id))['state'] == 'verified'


def test_foreign_scope_refused():
    projects, fs = _env()
    body = _body(fs)
    assert _refused(projects.save_ingest({**body, 'workspaceID': 'ws-2'}), 'workspace changed')
    ingest_id = _run(projects.save_ingest(body))['ingestID']
    path = fs.analysis + '/ingest-' + ingest_id + '.md'
    other = AnalysisProjects._parse_ingest
    assert _refused(_wrap(lambda: other(fs.files[path][0].decode(), project_identity('owner-2', 'ws-1'), ingest_id)))
    fs.ok = False
    assert _refused(projects.read_ingest(ingest_id), 'workspace or account changed')
    assert _refused(projects.save_ingest(body), 'workspace or account changed')


async def _wrap_async(fn):
    return fn()


def _wrap(fn):
    return _wrap_async(fn)


def test_invalid_ingest_refused_with_nothing_written():
    projects, fs = _env()
    before = dict(fs.files)
    for data in (b'', b'not json\n', STATIC.replace(b'"size":32', b'"size":32,"extra":1', 1), STATIC + STATIC.split(b'\n')[1] + b'\n', 'text'):
        assert _refused(projects.save_ingest(_body(fs, data)))
    assert _refused(projects.save_ingest({**_body(fs), 'adapterID': 'nope'}), 'invalid ingest')
    assert _refused(projects.save_ingest({**_body(fs), 'extra': 1}))
    assert fs.files == before


def test_foreign_source_hash_saves_as_historical():
    projects, fs = _env()
    fs.set_source(b'y' * 16)
    saved = _run(projects.save_ingest(_body(fs)))
    assert _run(projects.read_ingest(saved['ingestID'], 'src-ref'))['sourceState'] == 'historical'


def test_production_mode_refuses_the_unqualified_fixture_adapter_before_reading_the_source_or_writing():
    projects, fs = _env()           # test double: the fixture adapter is injected through the instance's partials
    production_save, production_read = (lambda *a, **k: AnalysisProjects.save_ingest(projects, *a, **k),
                                        lambda *a, **k: AnalysisProjects.read_ingest(projects, *a, **k))   # no _test_adapter: as the routes call it
    names = _names(fs)
    assert _refused(production_save(_body(fs)), 'invalid ingest')
    assert _names(fs) == names                      # nothing was written
    # a document stored earlier by the fixture adapter cannot be overlaid in production either
    ingest_id = _run(projects.save_ingest(_body(fs)))['ingestID']
    assert _refused(production_read(ingest_id), 'invalid ingest')
