"""Session-scoped Python subprocess owner; admission is supplied by its caller.

This is process lifecycle management, not an OS sandbox or an authorization
service. Do not expose it without the host's existing scoped admission.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

from src.openclank.python_kernel_protocol import MAX_CODE_BYTES, MAX_OUTPUT_BYTES, receive_frame, send_frame


@dataclass(frozen=True)
class KernelScope:
    account_id: str
    workspace_id: str
    chat_id: str
    engine_session_id: str
    actor_id: str
    holder_id: str
    holder_generation: int

    def __post_init__(self):
        identities = (self.account_id, self.workspace_id, self.chat_id, self.engine_session_id, self.actor_id, self.holder_id)
        if (not all(isinstance(x, str) and 0 < len(x) <= 256 for x in identities) or
                isinstance(self.holder_generation, bool) or not isinstance(self.holder_generation, int) or self.holder_generation < 0):
            raise ValueError('a complete admitted scope is required')


class KernelError(RuntimeError):
    pass


@dataclass(eq=False)
class _CallReservation:
    scope: KernelScope
    call_id: str
    turn_key: str
    phase: str = 'reserved'
    revoked: bool = False
    completed: bool = False
    operation: str | None = None
    item: dict | None = None


class KernelManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._closed = False
        self._retired = set()
        self._launching = {}
        self._kernels = {}
        self._calls = {}

    def reserve_call(self, scope, call_id, *, turn_key):
        if (not isinstance(scope, KernelScope) or not isinstance(call_id, str) or not 0 < len(call_id) <= 256
                or not isinstance(turn_key, str) or not 0 < len(turn_key.encode('utf-8')) <= 2048):
            raise KernelError('invalid admitted call identity')
        with self._condition:
            key = (scope, turn_key, call_id)
            if self._closed or scope in self._retired or key in self._calls or len(self._calls) >= 4096:
                raise KernelError('call unavailable or already reserved')
            reservation = _CallReservation(scope, call_id, turn_key)
            self._calls[key] = reservation
            return reservation

    def _check_call(self, reservation):
        if not isinstance(reservation, _CallReservation) or self._calls.get((reservation.scope, reservation.turn_key, reservation.call_id)) is not reservation:
            raise KernelError('unknown call reservation')

    def revoke_call(self, reservation):
        """Short tombstone operation; it never waits on a process or cell."""
        with self._condition:
            self._check_call(reservation)
            if reservation.completed:
                return False
            reservation.revoked = True
            if reservation.phase == 'reserved':
                reservation.phase = 'finished'
            self._condition.notify_all()
            return True

    def complete_call(self, reservation):
        """Commit the host's final same-turn publication check exactly once."""
        item = reservation.item if isinstance(reservation, _CallReservation) else None
        lock = item['state_lock'] if item is not None else self._lock
        with lock:
            with self._condition:
                self._check_call(reservation)
                if reservation.revoked or reservation.completed or reservation.phase != 'finished':
                    raise KernelError('call cannot be published')
                reservation.completed = True
                if item is not None and item.get('claim') is reservation:
                    item['claim'] = None
                reservation.item = None
                self._condition.notify_all()

    def drain_call(self, reservation):
        """Outside the C2 lock, revoke and drain only this call's process."""
        self.revoke_call(reservation)
        while True:
            with self._condition:
                self._check_call(reservation)
                if reservation.completed:
                    return
                item = reservation.item
                finished = reservation.phase == 'finished'
                if item is None and finished:
                    return
                if item is None:
                    self._condition.wait()
                    continue
            with item['state_lock']:
                with self._condition:
                    owned = reservation.revoked and not reservation.completed and item.get('claim') is reservation
                if owned and item['process'].poll() is None:
                    try:
                        item['process'].kill()
                    except ProcessLookupError:
                        pass
            if owned and finished:
                self._terminate(item)
                with self._condition:
                    if item.get('claim') is reservation:
                        item['claim'] = None
                    if reservation.operation == 'start' and self._kernels.get(item['id']) is item:
                        del self._kernels[item['id']]
                    reservation.item = None
                    self._condition.notify_all()
                return
            with self._condition:
                if reservation.phase != 'finished':
                    self._condition.wait()

    def invoke_reserved(self, reservation, operation, **args):
        with self._condition:
            self._check_call(reservation)
            if self._closed or reservation.scope in self._retired or reservation.revoked or reservation.phase != 'reserved':
                raise KernelError('call reservation is no longer active')
            if operation not in {'start', 'status', 'execute', 'interrupt', 'reset', 'close'}:
                raise KernelError('unknown kernel operation')
            reservation.phase = 'running'
            reservation.operation = operation
        try:
            if operation == 'start':
                return self.start(reservation.scope, _call=reservation, **args)
            if operation == 'execute':
                return self.execute(reservation.scope, _call=reservation, **args)
            if operation == 'reset':
                return self.reset(reservation.scope, _call=reservation, **args)
            with self._condition:
                if reservation.revoked:
                    raise KernelError('call reservation was revoked')
            return getattr(self, operation)(reservation.scope, **args)
        finally:
            with self._condition:
                reservation.phase = 'finished'
                self._condition.notify_all()

    def retire_turn(self, scope, *, turn_key):
        """Forget drained call records after the host has sealed this C2 turn.

        The host's existing closed-turn authority prevents old-call replay.
        This method never terminates a successfully published kernel.
        """
        if not isinstance(scope, KernelScope) or not isinstance(turn_key, str) or not 0 < len(turn_key.encode('utf-8')) <= 2048:
            raise KernelError('invalid admitted turn identity')
        with self._condition:
            keys = [key for key in self._calls if key[0] == scope and key[1] == turn_key]
            calls = [self._calls[key] for key in keys]
            if any(call.phase != 'finished' or not (call.completed or call.revoked and call.item is None) for call in calls):
                raise KernelError('turn has undrained or unpublished calls')
            for key in keys:
                del self._calls[key]

    def _get(self, scope, kernel_id):
        with self._lock:
            item = self._kernels.get(kernel_id)
            if item is None or item['scope'] != scope:
                raise KernelError('kernel unavailable for this scope')
            return item

    def _launch(self, scope, directory, kernel_id, generation):
        if (getattr(sys, 'frozen', False) or sys.implementation.name != 'cpython'
                or not re.fullmatch(r'python(?:3(?:\.\d+)?)?', Path(sys.executable).name)):
            raise KernelError('Python kernel is unavailable in this packaged runtime')
        if os.name != 'posix':
            raise KernelError('Python kernel control channel is unavailable on this platform')
        path = Path(directory).resolve(strict=True)
        if not path.is_dir():
            raise KernelError('workspace directory unavailable')
        parent, child = socket.socketpair()
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
        try:
            process = subprocess.Popen([sys.executable, '-I', '-u', str(Path(__file__).with_name('python_kernel_worker.py')), str(child.fileno())],
                cwd=str(path), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(child.fileno(),), start_new_session=True)
        except BaseException:
            parent.close(); child.close()
            raise
        child.close()
        try:
            parent.settimeout(10)
            ready = receive_frame(parent)
            if ready.get('operation') != 'ready':
                raise KernelError('invalid kernel startup response')
            parent.settimeout(None)
        except BaseException:
            process.kill(); process.wait(); parent.close()
            raise
        return {'scope': scope, 'directory': str(path), 'id': kernel_id, 'generation': generation,
                'process': process, 'socket': parent, 'cell_lock': threading.Lock(), 'state_lock': threading.Lock(), 'active': None, 'claim': None, 'seen': set(),
                'python': ready.get('python'), 'executable': ready.get('executable')}

    def start(self, scope, directory, *, _call=None):
        if not isinstance(scope, KernelScope):
            raise KernelError('admitted scope required')
        kernel_id = uuid.uuid4().hex
        with self._condition:
            if self._closed or scope in self._retired or (_call is not None and _call.revoked):
                raise KernelError('kernel owner or call has retired')
            self._launching[scope] = self._launching.get(scope, 0) + 1
        try:
            item = self._launch(scope, directory, kernel_id, 1)
            with self._condition:
                rejected = self._closed or scope in self._retired or (_call is not None and _call.revoked)
                if not rejected:
                    if _call is not None:
                        item['claim'] = _call
                        _call.item = item
                    self._kernels[kernel_id] = item
                    self._condition.notify_all()
            if rejected:
                self._terminate(item)
                raise KernelError('kernel owner or call retired during startup')
            return self.status(scope, kernel_id)
        finally:
            with self._condition:
                self._launching[scope] -= 1
                if not self._launching[scope]:
                    del self._launching[scope]
                self._condition.notify_all()

    def status(self, scope, kernel_id):
        item = self._get(scope, kernel_id)
        alive = item['process'].poll() is None
        return {'kernel_id': kernel_id, 'generation': item['generation'], 'status': 'busy' if alive and item['active'] else 'ready' if alive else 'lost',
                'state_survives': alive, 'active_cell_id': item['active'], 'python': item['python'], 'executable': item['executable']}

    def execute(self, scope, kernel_id, generation, cell_id, code, *, timeout=30, output_limit=65536, artifact_enabled=False, _call=None):
        item = self._get(scope, kernel_id)
        if generation != item['generation']:
            raise KernelError('kernel generation changed')
        if not isinstance(cell_id, str) or not 0 < len(cell_id) <= 128 or not isinstance(code, str) or len(code.encode('utf-8')) > MAX_CODE_BYTES:
            raise KernelError('invalid cell')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 120 or isinstance(output_limit, bool) or not isinstance(output_limit, int) or not 0 <= output_limit <= MAX_OUTPUT_BYTES:
            raise KernelError('invalid execution limits')
        if type(artifact_enabled) is not bool:
            raise KernelError('invalid artifact selection')
        if not item['cell_lock'].acquire(blocking=False):
            raise KernelError('kernel already executing')
        try:
            if item['process'].poll() is not None:
                raise KernelError('kernel process lost; reset required')
            if cell_id in item['seen'] or len(item['seen']) >= 4096:
                raise KernelError('cell identity already used or kernel cell limit reached')
            with item['state_lock']:
                with self._condition:
                    if item.get('claim') is not None or (_call is not None and _call.revoked):
                        raise KernelError('kernel call is unavailable or awaiting publication')
                    if _call is not None:
                        item['claim'] = _call
                        _call.item = item
                        self._condition.notify_all()
            item['seen'].add(cell_id)
            started = time.monotonic()
            try:
                item['socket'].settimeout(timeout)
                send_frame(item['socket'], {'operation': 'execute', 'cell_id': cell_id, 'code': code, 'output_limit': output_limit, 'artifact_enabled': artifact_enabled})
                acknowledgement = receive_frame(item['socket'], deadline=started + timeout)
                if acknowledgement != {'operation': 'cell_started', 'cell_id': cell_id}:
                    raise ValueError('invalid cell acknowledgement')
                with item['state_lock']:
                    item['active'] = cell_id
                result = receive_frame(item['socket'], deadline=started + timeout)
                if result.get('cell_id') != cell_id or result.get('status') not in ('completed', 'error', 'interrupted'):
                    raise ValueError('kernel result does not match the submitted cell')
                payload = result.pop('_artifact_payload', None)
                rejected = result.pop('_artifact_rejected', False)
                if type(rejected) is not bool or (payload is not None and (not artifact_enabled or not isinstance(payload,str) or len(payload) > 1398104 or result['status'] != 'completed')):
                    raise ValueError('invalid artifact response')
                return {**result, 'kernel_id': kernel_id, 'generation': generation, 'state_survives': item['process'].poll() is None, 'output_scope': 'python_text_streams_only', 'artifacts': [],
                        **({'_artifact_payload':payload} if payload is not None else {}), **({'artifact_status':'unavailable'} if rejected else {})}
            except (OSError, EOFError, ValueError):
                self._terminate(item)
                return {'kernel_id': kernel_id, 'generation': generation, 'cell_id': cell_id, 'status': 'unknown',
                        'stdout': '', 'stderr': '', 'stdout_truncated': False, 'stderr_truncated': False,
                        'exception': 'Control channel lost or deadline exceeded; effects may have occurred.',
                        'duration_ms': int((time.monotonic()-started)*1000), 'state_survives': False, 'output_scope': 'python_text_streams_only', 'artifacts': []}
            except BaseException:
                # A caller interruption cannot leave a running cell behind a
                # released serialization lock.
                self._terminate(item)
                raise
            finally:
                with item['state_lock']:
                    with self._condition:
                        item['active'] = None
                        # The host releases a reserved call only after final publication/currentness.
                        self._condition.notify_all()
        finally:
            item['cell_lock'].release()

    def interrupt(self, scope, kernel_id, generation, cell_id):
        item = self._get(scope, kernel_id)
        with item['state_lock']:
            if item['generation'] != generation or item['active'] != cell_id or item['process'].poll() is not None:
                raise KernelError('active cell changed')
            # A delayed signal could hit the following cell. Terminate this
            # exact process instead; effects and cell outcome remain unknown.
            try:
                item['process'].kill()
            except ProcessLookupError:
                raise KernelError('active cell ended before termination')
        return {'kernel_id': kernel_id, 'generation': generation, 'cell_id': cell_id,
                'status': 'termination_requested', 'state_survives': False, 'effects': 'unknown'}

    @staticmethod
    def _terminate(item):
        process = item['process']
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
        item['socket'].close()

    def reset(self, scope, kernel_id, generation, *, _call=None):
        item = self._get(scope, kernel_id)
        if not item['cell_lock'].acquire(blocking=False):
            raise KernelError('kernel already executing; interrupt first')
        reserved = False
        try:
            with self._condition:
                if self._closed or scope in self._retired or self._kernels.get(kernel_id) is not item or generation != item['generation'] or item.get('claim') is not None or (_call is not None and _call.revoked):
                    raise KernelError('kernel owner or generation changed')
                self._launching[scope] = self._launching.get(scope, 0) + 1
                reserved = True
            self._terminate(item)
            replacement = self._launch(scope, item['directory'], kernel_id, generation + 1)
            with self._condition:
                rejected = self._closed or scope in self._retired or self._kernels.get(kernel_id) is not item or (_call is not None and _call.revoked)
                if not rejected:
                    if _call is not None:
                        replacement['claim'] = _call
                        _call.item = replacement
                    self._kernels[kernel_id] = replacement
                    self._condition.notify_all()
            if rejected:
                self._terminate(replacement)
                raise KernelError('kernel owner or call retired during reset')
        finally:
            if reserved:
                with self._condition:
                    self._launching[scope] -= 1
                    if not self._launching[scope]:
                        del self._launching[scope]
                    self._condition.notify_all()
            item['cell_lock'].release()
        return self.status(scope, kernel_id)

    def close(self, scope, kernel_id, generation):
        item = self._get(scope, kernel_id)
        if not item['cell_lock'].acquire(blocking=False):
            raise KernelError('kernel already executing; interrupt first')
        try:
            with self._lock:
                if self._kernels.get(kernel_id) is not item or item['generation'] != generation or item.get('claim') is not None:
                    raise KernelError('kernel generation changed')
                del self._kernels[kernel_id]
            self._terminate(item)
        finally:
            item['cell_lock'].release()
        return {'kernel_id': kernel_id, 'generation': generation, 'status': 'closed', 'state_survives': False}

    def retire_scope(self, scope):
        if not isinstance(scope, KernelScope):
            raise KernelError('admitted scope required')
        with self._condition:
            self._retired.add(scope)
            for call in self._calls.values():
                if call.scope == scope and not call.completed:
                    call.revoked = True
                    if call.phase == 'reserved':
                        call.phase = 'finished'
            items = [item for item in self._kernels.values() if item['scope'] == scope]
            for item in items:
                del self._kernels[item['id']]
        for item in items:
            self._terminate(item)
        with self._condition:
            while self._launching.get(scope, 0) or any(call.scope == scope and call.phase == 'running' for call in self._calls.values()):
                self._condition.wait()

    def close_all(self):
        with self._condition:
            self._closed = True
            for call in self._calls.values():
                if not call.completed:
                    call.revoked = True
                    if call.phase == 'reserved':
                        call.phase = 'finished'
            items = list(self._kernels.values())
            self._kernels.clear()
        for item in items:
            self._terminate(item)
        with self._condition:
            while self._launching or any(call.phase == 'running' for call in self._calls.values()):
                self._condition.wait()
