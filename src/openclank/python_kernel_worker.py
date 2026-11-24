"""Private subprocess entry point; control socket never shares user output."""
import contextlib
import base64
import io
import socket
import sys
import time

# Isolated Python has no repository import path; import the adjacent protocol.
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from python_kernel_protocol import receive_frame, send_frame


class LimitedOutput(io.TextIOBase):
    def __init__(self, limit):
        self.limit, self.used, self.chunks, self.truncated = limit, 0, [], False

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError('output must be text')
        raw = text.encode('utf-8', 'replace')
        if not self.truncated:
            accepted = raw[:max(0, self.limit - self.used)].decode('utf-8', 'ignore')
            self.chunks.append(accepted)
            self.used += len(accepted.encode('utf-8'))
            self.truncated = len(raw) > self.limit - (self.used - len(accepted.encode('utf-8')))
        return len(text)

    def value(self):
        return ''.join(self.chunks)


def main():
    sock = socket.socket(fileno=int(sys.argv[1]))
    namespace = {'__name__': '__main__'}
    send_frame(sock, {'operation': 'ready', 'python': sys.version.split()[0], 'executable': sys.executable})
    while True:
        try:
            request = receive_frame(sock)
        except (EOFError, OSError, ValueError):
            return
        if request.get('operation') != 'execute':
            return
        out, err = LimitedOutput(request['output_limit']), LimitedOutput(request['output_limit'])
        started = time.monotonic()
        status, error = 'completed', None
        emitted, rejected = [], []
        def emit_artifact(data, mime):
            if not request.get('artifact_enabled'):
                rejected.append(True); raise RuntimeError('Artifact destination is unavailable')
            if emitted or not isinstance(data, bytes) or not 0 < len(data) <= 1024 * 1024 or mime != 'image/png':
                rejected.append(True); raise ValueError('Only one bounded PNG artifact is supported')
            emitted.append(base64.b64encode(data).decode('ascii'))
        namespace['emit_artifact'] = emit_artifact
        try:
            send_frame(sock, {'operation': 'cell_started', 'cell_id': request['cell_id']})
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exec(compile(request['code'], '<kernel-cell>', 'exec'), namespace, namespace)
        except KeyboardInterrupt:
            status = 'interrupted'
        except BaseException as exc:
            status, error = 'error', f'{type(exc).__name__}: {str(exc)[:1024]}'
        send_frame(sock, {'cell_id': request['cell_id'], 'status': status, 'stdout': out.value(),
                          'stderr': err.value(), 'stdout_truncated': out.truncated,
                          'stderr_truncated': err.truncated, 'exception': error,
                          'duration_ms': int((time.monotonic() - started) * 1000),
                          '_artifact_payload': emitted[0] if status == 'completed' and emitted and not rejected else None,
                          '_artifact_rejected': bool(rejected)})


if __name__ == '__main__':
    main()
