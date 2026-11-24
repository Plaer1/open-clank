"""Focused one-shot contract against the actual HTTP client and a fake host."""
import io
import json
import httpx
from src.openclank.client_profiles import ClientProfile
from src.openclank.cli_run import run_one_shot
from src.openclank.cli_output import clean_text, bounded_chunk, _LIMIT
from src.openclank.tui_client import OpenClankTuiClient


def test_quiet_run_host_boundary():
    assert clean_text('a\u0085b\u009fc') == 'abc'
    accepted, used, truncated = bounded_chunk('😀x', _LIMIT - 4)
    assert accepted == '😀' and used == _LIMIT and truncated
    accepted, used, truncated = bounded_chunk('😀', _LIMIT - 3)
    assert accepted == '' and used == _LIMIT - 3 and truncated
    calls = []
    mode = ['complete']
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            raise KeyboardInterrupt
            yield b''
    class ClosedOutput(io.StringIO):
        def write(self, value):
            raise BrokenPipeError
    def host(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith('/stop'):
            return httpx.Response(200, json={'run_id':'run-a', 'stopped':True})
        if request.method == 'POST':
            if mode[0] == 'interrupt':
                return httpx.Response(200, headers={'X-Agent-Run-ID':'run-a'}, stream=Interrupted())
            if mode[0] == 'pending':
                body = 'id: 1\ndata: {"type":"permission_request","data":{}}\n\n'
            else:
                body = 'id: 1\ndata: {"delta":"hello"}\n\nid: 2\ndata: [DONE]\n\n'
            return httpx.Response(200, headers={'X-Agent-Run-ID':'run-a'}, text=body)
        return httpx.Response(200, json={'run_id':'run-a','status':'done'})
    client = OpenClankTuiClient(ClientProfile.create('test', 'https://example.test'), token='oct_fake', transport=httpx.MockTransport(host))
    try:
        out, err = io.StringIO(), io.StringIO()
        assert run_one_shot(client, 'chat', 'prompt', 'json', out, err, 'owner') == 0
        result = json.loads(out.getvalue())
        assert result['run_id'] == 'run-a' and result['cursor'] == 2 and result['output'] == 'hello'
        mode[0] = 'pending'
        out, err = io.StringIO(), io.StringIO()
        assert run_one_shot(client, 'chat', 'prompt', 'json', out, err, 'owner') == 4
        assert json.loads(out.getvalue())['status'] == 'human_action_required'
        assert sum(method == 'POST' for method, _ in calls) == 2
        assert 'existing chat' in err.getvalue()
        mode[0] = 'interrupt'
        out, err = io.StringIO(), io.StringIO()
        assert run_one_shot(client, 'chat', 'prompt', 'json', out, err, 'owner') == 5
        assert json.loads(out.getvalue())['status'] == 'cancel_requested'
        assert calls[-1][1].endswith('/stop')
        mode[0] = 'complete'
        before = len(calls)
        assert run_one_shot(client, 'chat', 'prompt', 'text', ClosedOutput(), io.StringIO(), 'owner') == 7
        assert not any(path.endswith('/stop') for _, path in calls[before:])
    finally:
        client.close()
