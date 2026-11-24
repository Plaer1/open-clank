"""Bounded private control frames, independent of a cell's stdout/stderr."""
import json
import struct
import time

MAX_FRAME = 4 * 1024 * 1024
MAX_CODE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024


def send_frame(sock, value):
    data = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    if len(data) > MAX_FRAME:
        raise ValueError('kernel frame exceeds limit')
    sock.sendall(struct.pack('!I', len(data)) + data)


def _read_exact(sock, size, deadline=None):
    parts = []
    while size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('kernel deadline exceeded')
            sock.settimeout(remaining)
        value = sock.recv(size)
        if not value:
            raise EOFError('kernel control channel closed')
        parts.append(value)
        size -= len(value)
    return b''.join(parts)


def receive_frame(sock, deadline=None):
    size = struct.unpack('!I', _read_exact(sock, 4, deadline))[0]
    if size > MAX_FRAME:
        raise ValueError('kernel frame exceeds limit')
    value = json.loads(_read_exact(sock, size, deadline))
    if not isinstance(value, dict):
        raise ValueError('invalid kernel frame')
    return value


def project_model_result(value):
    """Closed model-facing result; never expose the host executable path."""
    if not isinstance(value, dict):
        raise ValueError('invalid kernel result')
    allowed = {'kernel_id', 'generation', 'status', 'state_survives', 'active_cell_id', 'python',
               'cell_id', 'stdout', 'stderr', 'stdout_truncated', 'stderr_truncated', 'exception',
               'duration_ms', 'artifacts', 'artifact_status', 'output_scope', 'effects'}
    result = {key: value[key] for key in allowed if key in value}
    if not isinstance(result.get('kernel_id'), str) or len(result['kernel_id']) > 128:
        raise ValueError('invalid kernel identity')
    if type(result.get('generation')) is not int or result['generation'] < 1:
        raise ValueError('invalid kernel generation')
    if result.get('status') not in {'ready', 'busy', 'lost', 'completed', 'error', 'interrupted', 'unknown', 'termination_requested', 'closed'}:
        raise ValueError('invalid kernel status')
    for key in ('stdout', 'stderr'):
        if key in result and (not isinstance(result[key], str) or len(result[key].encode('utf-8')) > MAX_OUTPUT_BYTES):
            raise ValueError('invalid kernel output')
    for key in ('cell_id', 'active_cell_id', 'python', 'exception', 'output_scope', 'effects'):
        if key in result and result[key] is not None and (not isinstance(result[key], str) or len(result[key]) > 2048):
            raise ValueError('invalid kernel result field')
    for key in ('state_survives', 'stdout_truncated', 'stderr_truncated'):
        if key in result and type(result[key]) is not bool:
            raise ValueError('invalid kernel result flag')
    if 'duration_ms' in result and (type(result['duration_ms']) is not int or result['duration_ms'] < 0):
        raise ValueError('invalid kernel duration')
    artifacts = result.get('artifacts', [])
    if not isinstance(artifacts, list) or len(artifacts) > 1:
        raise ValueError('invalid artifact result')
    for artifact in artifacts:
        if (not isinstance(artifact, dict) or set(artifact) != {'resource_ref','sha256','mime_type','size'}
            or not isinstance(artifact['resource_ref'], str) or not 1 <= len(artifact['resource_ref']) <= 16384
            or not isinstance(artifact['sha256'], str) or len(artifact['sha256']) != 64 or any(c not in '0123456789abcdef' for c in artifact['sha256'])
            or artifact['mime_type'] != 'image/png' or type(artifact['size']) is not int or not 0 < artifact['size'] <= 1048576):
            raise ValueError('invalid artifact descriptor')
    if result.get('artifact_status') not in (None,'none','committed','unavailable','unknown') or (bool(artifacts) != (result.get('artifact_status') == 'committed')):
        raise ValueError('invalid artifact outcome')
    return result
