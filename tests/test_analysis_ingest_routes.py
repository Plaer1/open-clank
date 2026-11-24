import base64
import json
import pathlib

from routes.files_facade_routes import (AnalysisIngestOverlayRequest, AnalysisIngestSaveRequest, _analysis_ingest_bytes)
from src.openclank.analysis_adapters.ingest import REGISTRY
from src.openclank.analysis_ingest import trace_overlay

FIX = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'analysis'
GOOD_REV = {'kind': 'hostFingerprint', 'value': 'sha256:' + '0' * 64 + ':1'}


def _body(**over):
    body = {'workspaceID': 'ws', 'sourceRef': 'opaque-source-ref', 'sourceRevision': GOOD_REV, 'adapterID': 'fixture_jsonl',
            'data_b64': base64.b64encode(b'{"a":1}\n').decode()}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def _refused(**over):
    try:
        AnalysisIngestSaveRequest.model_validate(_body(**over))
    except ValueError:  # pydantic ValidationError subclasses ValueError
        return True
    return False


def test_model_refuses_bad_bodies():
    assert not _refused()
    assert _refused(data_b64='not base64!!')
    assert _refused(data_b64='QUJD\nREVG')  # whitespace is not strict base64
    assert _refused(data_b64='QUJDRA')  # bad padding
    assert _refused(data_b64='')
    assert _refused(data_b64=base64.b64encode(b'x' * 1048577).decode())
    assert not _refused(data_b64=base64.b64encode(b'x' * 1048576).decode())
    assert _refused(extra='x')
    for key in ('workspaceID', 'sourceRef', 'sourceRevision', 'adapterID', 'data_b64'):
        assert _refused(**{key: None})  # None drops the key: missing key refused
    assert _refused(adapterID='Bad Adapter')
    assert _refused(adapterID=7)
    assert _refused(sourceRevision='x')
    assert _refused(data_b64=12)


def test_bytes_helper_decodes_exactly():
    assert _analysis_ingest_bytes(base64.b64encode(b'\x00\xffabc').decode()) == b'\x00\xffabc'


def test_overlay_request_closed():
    AnalysisIngestOverlayRequest.model_validate({'workspaceID': 'ws', 'ingestID': 'abc'})
    for bad in ({'workspaceID': 'ws'}, {'workspaceID': 'ws', 'ingestID': 'a', 'x': 1}, {'workspaceID': 'ws', 'ingestID': 3}):
        try:
            AnalysisIngestOverlayRequest.model_validate(bad)
        except ValueError:
            continue
        assert False, bad


def test_overlay_payload_keeps_state_and_labels():
    from tests.test_analysis_ingest_custody import STATIC, TRACE
    from src.openclank.analysis_ingest import normalize
    import hashlib
    sha = json.loads(STATIC.split(b'\n')[0])['source']['sha256']
    for data, label in ((STATIC, 'static'), (TRACE, 'runtime_observed')):
        normalized = normalize(data, 'fixture_jsonl', expected_source_sha256=sha, _test_adapter=REGISTRY['fixture_jsonl'])
        for state in ('current', 'historical', 'unchecked'):
            out = trace_overlay({**normalized, 'sourceState': state})
            assert out['sourceState'] == state and out['label'] == label
            assert all(r['id'].startswith('overlay:') for k in ('functions', 'edges', 'hits') for r in out[k])


def test_body_parser_refuses_before_any_backend_call():
    # The route closures are not importable alone (and the neighbouring route test needs pytest), so drive the shared
    # body parser they call first; a refusal here (HTTP 422) happens before _analysis_projects/save_ingest exist.
    import asyncio
    from fastapi import HTTPException
    from routes.files_facade_routes import _analysis_request_body

    class Req:
        def __init__(self, raw, ctype='application/json'):
            self.headers, self._raw = {'content-type': ctype}, raw

        async def stream(self):
            for i in range(0, len(self._raw), 65536):
                yield self._raw[i:i + 65536]

    def run(raw, model=AnalysisIngestSaveRequest, **kw):
        try:
            return asyncio.run(_analysis_request_body(Req(raw), model, **kw))
        except HTTPException as error:
            assert error.status_code == 422
            return None

    big = json.dumps(_body(data_b64=base64.b64encode(b'x' * 1048576).decode())).encode()
    assert 1_048_576 < len(big) <= 1_572_864
    assert run(big, limit=1_572_864) is not None
    assert run(big) is None  # default 256 KiB cap still applies to every other route
    assert run(b' ' * 1_600_000, limit=1_572_864) is None
    assert run(b'{"workspaceID":"a","workspaceID":"b"}') is None
    assert run(json.dumps(_body(extra=1)).encode()) is None
    assert run(json.dumps(_body(data_b64='***')).encode()) is None
    assert run(b'{"workspaceID":"ws"}', AnalysisIngestOverlayRequest) is None


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
    print('ok')
