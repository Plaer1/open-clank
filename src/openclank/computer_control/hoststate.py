"""Host-side authority for desktop-control targets: leases, observations, holds and the takeover epoch.

The lifetools server (where the helpers run) and the Host (approval card, Stop route) are different processes, so this state lives in
the Host and the lifetools side talks to it through the existing loopback broker (`control_lease` op); in-process callers and tests use
`LocalHandle` on a `ControlState` directly. Nothing here starts a helper or touches the desktop.

Leases REUSE the browser target lease registry (src/openclank/browser_control.py `BrowserTargetRegistry`: owner, host session, binding,
connection generation, observation generation, stale handling): a native window is registered as the selected "tab" (its identity
is pid:process-start:window-index, its "URL" is about:blank). A newer observation of the same window bumps the observation generation
and thereby makes every older snapshot of it stale, exactly as for browser snapshot refs.
"""
from __future__ import annotations

import os
import asyncio
import secrets
import signal
import sys
import threading
import time
from typing import Any, Callable, Mapping, Optional

from src.openclank.computer_control import windows, macos
from src.openclank.browser_control import BrowserTargetError, BrowserTargetNotFound, BrowserTargetRegistry, BrowserTargetScopeError, BrowserTargetStale

OBS_MAX_AGE_S = 30.0          # a pixel observation backs an action only this long
HOLD_TTL_S = 120.0            # an agent keeps its hold on an app/screen this long after its last use
WORKSPACE = "native"
PIXEL_ACTIONS = ("click", "move", "key", "hotkey", "type", "scroll", "drag", "paste")


class HelperRefused(ValueError):
    """A pid offered as a control helper failed the Host's own check; `code` is the typed reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _helper_exes() -> set[str]:
    """Real paths of the executables a registered helper may be (the configured capture/control helper binary)."""
    from src.openclank.native_capture_linux import _helper
    return {os.path.realpath(str(_helper()))}


HELPER_EXES: Callable[[], set[str]] = _helper_exes      # tests point this at their own harmless child's executable


def _identity(pid: int) -> Optional[tuple[int, int, int, str]]:
    """(start time in clock ticks, process-group id, uid, real executable path) read by the Host from /proc, or None when the process is gone."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            stat = f.read()
        fields = stat.rsplit(")", 1)[1].split()
        pgrp, start = int(fields[2]), int(fields[19])
        uid = os.stat(f"/proc/{pid}").st_uid
        exe = os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
    except (OSError, ValueError, IndexError):
        return None
    return start, pgrp, uid, exe


def _still(pid: int, entry: Mapping[str, Any]) -> bool:
    """The very same process we registered: same start time, still our uid, still its own group leader, still the helper executable."""
    ident = _identity(pid)
    return ident is not None and ident[0] == entry["start"] and ident[1] == pid and ident[2] == os.getuid() and ident[3] == entry["exe"]


class NativeTargetRegistry(BrowserTargetRegistry):
    """The browser lease registry, used unchanged for native windows (see module docstring)."""


class ControlState:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self.epoch = 0
        self.leases = NativeTargetRegistry(clock=clock)
        self.helpers: dict[int, dict[str, Any]] = {}
        self.launches: dict[str, dict[str, Any]] = {}
        self.pixel: dict[str, dict[str, Any]] = {}
        self.snaps: dict[str, dict[str, Any]] = {}
        self.windows: dict[tuple, dict[str, Any]] = {}
        self.holds: dict[str, dict[str, Any]] = {}

    # ---- helpers and takeover
    def register_helper(self, pid: int, kind: str, scope: tuple = (), holder: Any = None, holder_current: Any = None) -> str:
        """Accept a pid only after the Host has checked it itself: it exists, is ours (same uid), leads its own process group and runs the
        configured helper executable. Stores pid + start time so a reused pid is never mistaken for the helper later."""
        if type(pid) is not int or pid <= 1:
            raise HelperRefused("helper_pid_invalid")
        if windows.is_windows() or sys.platform == "darwin":
            # A validated image/owner alone does not bind this PID to the caller's
            # launch. Refuse until process-bound launch proof is available.
            raise HelperRefused("helper_launch_unverified")
        ident = _identity(pid)
        if ident is None:
            raise HelperRefused("helper_not_running")
        start, pgrp, uid, exe = ident
        if uid != os.getuid():
            raise HelperRefused("helper_not_ours")
        if pgrp != pid:
            raise HelperRefused("helper_not_group_leader")
        if exe not in HELPER_EXES():
            raise HelperRefused("helper_not_the_helper")
        with self._lock:
            for old in [p for p, e in self.helpers.items() if not _still(p, e)]:
                stale = self.helpers.pop(old, None)        # never retain a stale stream or recorder
                if stale is not None: self._close_recording_stream(stale)
            token = secrets.token_hex(32)
            self.helpers[pid] = {"kind": str(kind)[:16], "start": start, "exe": exe, "scope": scope,
                                 "registration": token, "epoch": self.epoch, "holder": holder, "holder_current": holder_current}
            return token

    def reserve_windows_launch(self, owner: str, session: str, actor: str, kind: str, turn: tuple = ()) -> dict:
        return self._reserve_launch(owner, session, actor, kind, "windows", turn)

    def reserve_macos_launch(self, owner: str, session: str, actor: str, kind: str, turn: tuple = ()) -> dict:
        return self._reserve_launch(owner, session, actor, kind, "macos", turn)

    def _reserve_launch(self, owner: str, session: str, actor: str, kind: str, platform: str, turn: tuple) -> dict:
        if not ((platform == "windows" and windows.is_windows()) or (platform == "macos" and sys.platform == "darwin")) or kind not in ("control", "a11y") or not all((owner, session, actor)):
            raise HelperRefused("helper_launch_unavailable")
        secret, token = secrets.token_hex(32), secrets.token_hex(32)
        with self._lock:
            for key, old in list(self.launches.items()):
                if old["deadline"] <= time.monotonic():
                    old["pipe"].cancel(); self.launches.pop(key, None)
            if len(self.launches) >= 32: raise HelperRefused("helper_launch_unavailable")
            try: pipe = windows.LaunchPipe(secret, kind) if platform == "windows" else macos.LaunchSocket(secret, kind)
            except (OSError, RuntimeError): raise HelperRefused("helper_launch_unavailable")
            self.launches[token] = {"scope": (owner, session, actor, *turn), "epoch": self.epoch,
                                    "kind": kind, "platform": platform, "pipe": pipe, "deadline": pipe.deadline}
            return {"reservation": token, "pipe": pipe.name, "secret": secret}

    def cancel_windows_launch(self, owner: str, session: str, actor: str, token: str, turn: tuple = ()) -> None:
        with self._lock:
            record = self.launches.get(token)
            if record is not None and record["scope"] == (owner, session, actor, *turn):
                self.launches.pop(token, None); record["pipe"].cancel()

    cancel_macos_launch = cancel_windows_launch

    def prepare_launch(self, owner: str, session: str, actor: str, token: str, turn: tuple = ()) -> dict:
        with self._lock:
            record = self.launches.get(token)
            if record is None or record["scope"] != (owner, session, actor, *turn):
                raise HelperRefused("helper_launch_unverified")
            return record

    @staticmethod
    def wait_launch_ready(record: dict) -> bool:
        return record["pipe"].done.wait(max(0.0, record["deadline"] - time.monotonic()))

    def commit_launch(self, owner: str, session: str, actor: str, token: str, record: dict, turn: tuple = ()) -> None:
        scope = (owner, session, actor, *turn)
        # Caller may invoke only after wait_launch_ready. No blocking wait here.
        if not record["pipe"].done.is_set(): raise HelperRefused("helper_launch_unverified")
        entry = record["pipe"].take()
        backend = windows if record["platform"] == "windows" else macos
        with self._lock:
            valid = (self.launches.get(token) is record and record["scope"] == scope and record["epoch"] == self.epoch
                     and time.monotonic() < record["deadline"] and entry is not None and entry.get("platform") == record["platform"] and backend.helper_alive(entry))
            if self.launches.get(token) is record: self.launches.pop(token, None)
            record["pipe"].cancel()
            if not valid:
                if entry is not None: backend.close_helper(entry)
                raise HelperRefused("helper_launch_unverified")
            entry["scope"] = scope
            entry["launch_token"] = token
            previous = self.helpers.pop(entry["pid"], None)
            if previous is not None and previous.get("platform") in ("windows", "macos"):
                (windows if previous["platform"] == "windows" else macos).close_helper(previous)
            self.helpers[entry["pid"]] = entry

    async def complete_launch(self, owner: str, session: str, actor: str, token: str, turn: tuple = ()) -> None:
        record = self.prepare_launch(owner, session, actor, token, turn)
        try:
            if not await asyncio.to_thread(self.wait_launch_ready, record):
                raise HelperRefused("helper_launch_unverified")
            self.commit_launch(owner, session, actor, token, record, turn)
        except BaseException:
            self.cancel_windows_launch(owner, session, actor, token, turn)
            raise

    def end_proved_helper(self, owner: str, session: str, actor: str, token: str, turn: tuple = ()) -> bool:
        scope = (owner, session, actor, *turn)
        with self._lock:
            pair = next(((pid, entry) for pid, entry in self.helpers.items()
                         if entry.get("launch_token") == token and entry.get("scope") == scope), None)
            if pair is None: return False
            pid, entry = pair
            self.helpers.pop(pid, None)
        backend = windows if entry.get("platform") == "windows" else macos
        try: return backend.terminate_helper(entry)
        finally: backend.close_helper(entry)

    def helper_capabilities(self, token: str, scope: tuple, shortcut: Any, overlay: Any, target: Any = None, holder: Any = None) -> bool:
        if shortcut is not None and (not isinstance(shortcut, str) or not 1 <= len(shortcut) <= 128 or not shortcut.isprintable()): return False
        if overlay is not None and type(overlay) is not bool: return False
        if target is not None:
            from src.openclank.computer_control.protocol import valid_control_target
            rect = target.get("rect") if isinstance(target, dict) else None
            if not isinstance(rect, dict) or not valid_control_target(target, rect): return False
        with self._lock:
            entry = next((item for item in self.helpers.values()
                          if item.get("registration") == token and item.get("scope") == scope
                          and item.get("epoch") == self.epoch and item.get("kind") == "control" and item.get("holder") is holder), None)
            if entry is None: return False
            entry["takeover_shortcut"], entry["cursor_overlay_at_begin"] = shortcut, overlay
            entry["control_target"] = dict(target) if target is not None else None
            return True

    @staticmethod
    def _recording_holder_live(entry: dict) -> bool:
        holder = entry.get('holder')
        if holder is None: return True  # in-process LocalHandle
        try:
            current=entry.get('holder_current')
            return holder.is_alive() is True and (current is None or current() is True)
        except Exception: return False

    def reserve_recording_stream(self, token: str, scope: tuple, holder: Any = None) -> dict:
        """Offer one private endpoint to the already registered Linux helper.

        A received descriptor remains Host-owned until a human recording job
        consumes it. A reservation never starts a capture or a recorder.
        """
        import socket, tempfile
        with self._lock:
            pair = next(((pid, e) for pid, e in self.helpers.items()
                if e.get('registration') == token and e.get('scope') == scope
                and e.get('holder') is holder and e.get('epoch') == self.epoch
                and e.get('kind') == 'control' and not e.get('platform') and self._recording_holder_live(e) and _still(pid, e)), None)
            if pair is None: raise HelperRefused('recording_stream_unavailable')
            pid, entry = pair
            if entry.get('stream_reservation') is not None: raise HelperRefused('recording_stream_unavailable')
            host = _identity(os.getpid())
            if host is None: raise HelperRefused('recording_stream_unavailable')
            directory = tempfile.TemporaryDirectory(prefix='oc-control-stream-')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            path = os.path.join(directory.name, 'fd')
            try:
                listener.bind(path); listener.listen(1); listener.settimeout(1.0)
            except BaseException:
                listener.close(); directory.cleanup(); raise
            secret = secrets.token_hex(32)
            reservation = {'listener':listener,'directory':directory,'secret':secret,
                           'epoch':self.epoch,'cancel':threading.Event(),'deadline':time.monotonic()+125}
            entry['stream_reservation'] = reservation
        def receive():
            import array, socket, struct
            fd = None
            try:
                while not reservation['cancel'].is_set() and time.monotonic() < reservation['deadline']:
                    try: conn, _ = listener.accept(); break
                    except socket.timeout: continue
                    except OSError: return
                else: return
                with conn:
                    conn.settimeout(3)
                    peer = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                    peer_pid, peer_uid, _ = struct.unpack('3i', peer)
                    if peer_pid != pid or peer_uid != os.getuid() or not _still(pid, entry): return
                    conn.sendall(secret.encode('ascii'))
                    payload, ancillary, flags, _ = conn.recvmsg(8, socket.CMSG_SPACE(array.array('i').itemsize), socket.MSG_CMSG_CLOEXEC)
                    received = array.array('i')
                    for level, kind, data in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            received.frombytes(data[:len(data)-(len(data)%received.itemsize)])
                    if len(received) != 1:
                        for item in received: os.close(item)
                        return
                    fd = received[0]
                    if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or len(payload) != 8: return
                    serial = int.from_bytes(payload, 'big')
                    if serial == 0: return
                    with self._lock:
                        if (self.helpers.get(pid) is not entry or entry.get('stream_reservation') is not reservation
                            or reservation['cancel'].is_set() or self.epoch != reservation['epoch'] or not self._recording_holder_live(entry) or not _still(pid, entry)):
                            return
                        entry['stream_fd'], entry['stream_serial'], entry['stream_id'] = fd, serial, secrets.token_hex(32)
                        fd = None
            except (OSError, ValueError): pass
            finally:
                if fd is not None: os.close(fd)
                listener.close(); directory.cleanup()
        threading.Thread(target=receive, name='control-stream-handoff', daemon=True).start()
        return {'path':path, 'secret':secret, 'host_pid':os.getpid(), 'host_start':host[0]}

    def reserve_selected_snapshot(self, token: str, scope: tuple, holder: Any) -> dict:
        """Reserve one bounded handoff on the already registered a11y helper."""
        with self._lock:
            pair = next(((pid, e) for pid, e in self.helpers.items()
                if e.get("registration") == token and e.get("kind") == "a11y"
                and len(scope) == 7 and len(e.get("scope", ())) == 7
                and e["scope"][:6] == scope[:6] and e.get("holder") is holder
                and e.get("epoch") == self.epoch and not e.get("platform")
                and self._recording_holder_live(e) and _still(pid, e)), None)
            if pair is None:
                raise HelperRefused("file_dialog_unavailable")
            pid, entry = pair
            if entry.get("selected_snapshot") is not None:
                raise HelperRefused("file_dialog_unavailable")
            reservation = {"pid": pid, "entry": entry, "scope": scope, "holder": holder,
                           "epoch": self.epoch, "snapshot": None, "deadline": time.monotonic() + 60.0}
            entry["selected_snapshot"] = reservation
        def retire():
            while time.monotonic() < reservation["deadline"]:
                if not self.selected_snapshot_current(reservation): break
                time.sleep(0.25)
            self.retire_selected_snapshot(reservation)
        threading.Thread(target=retire, name="selected-file-lease", daemon=True).start()
        return reservation

    def selected_snapshot_current(self, reservation: dict) -> bool:
        with self._lock:
            pid, entry = reservation["pid"], reservation["entry"]
            return bool(self.helpers.get(pid) is entry and entry.get("selected_snapshot") is reservation
                and self.epoch == reservation["epoch"] and entry.get("holder") is reservation["holder"]
                and time.monotonic() < reservation["deadline"] and self._recording_holder_live(entry) and _still(pid, entry))

    def selected_snapshot_for(self, token: str, scope: tuple, holder: Any) -> Optional[dict]:
        with self._lock:
            for entry in self.helpers.values():
                reservation = entry.get("selected_snapshot")
                if (entry.get("registration") == token and isinstance(reservation, dict)
                        and reservation.get("scope") == scope and reservation.get("holder") is holder
                        and reservation.get("snapshot") is not None and self.selected_snapshot_current(reservation)):
                    return reservation
            return None

    def admit_selected_snapshot(self, reservation: dict, snapshot: Any) -> str:
        with self._lock:
            if not self.selected_snapshot_current(reservation) or reservation["snapshot"] is not None:
                raise HelperRefused("file_dialog_unavailable")
            reservation["snapshot"] = snapshot
            # Returning this private address is treated as possible issue.  It remains
            # held even when the following helper transport fails or is cancelled.
            return snapshot.path

    def retire_selected_snapshot(self, reservation: dict) -> None:
        with self._lock:
            entry = reservation["entry"]
            if entry.get("selected_snapshot") is not reservation: return
            entry.pop("selected_snapshot", None)
            snapshot, reservation["snapshot"] = reservation.get("snapshot"), None
        if snapshot is not None: snapshot.close()

    @staticmethod
    def _close_recording_stream(entry: dict) -> None:
        ControlState._close_control_frame(entry)
        selected = entry.pop("selected_snapshot", None)
        if selected is not None:
            snapshot, selected["snapshot"] = selected.get("snapshot"), None
            if snapshot is not None: snapshot.close()
        revoke = entry.pop('recording_revoke', None)
        if revoke is not None: revoke()
        reservation = entry.pop('stream_reservation', None)
        if reservation is not None:
            reservation['cancel'].set()
            try: reservation['listener'].close()
            except OSError: pass
        fd = entry.pop('stream_fd', None)
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        entry.pop('stream_serial', None)

    def reserve_control_frame(self, token: str, scope: tuple, holder: Any = None) -> dict:
        """Reserve a fresh remote for one still without spending the recording FD."""
        import socket, tempfile
        with self._lock:
            # A fresh admitted inner turn may use the same retained actor execution;
            # the reservation itself still binds that new complete turn scope.
            pair=next(((pid,e) for pid,e in self.helpers.items() if e.get('registration')==token
                and len(scope)==7 and len(e.get('scope',()))==7 and e['scope'][:6]==scope[:6]
                and e.get('holder') is holder and e.get('epoch')==self.epoch and e.get('kind')=='control'
                and not e.get('platform') and self._recording_holder_live(e) and _still(pid,e)),None)
            if pair is None: raise HelperRefused('frame_unavailable')
            pid,entry=pair
            existing=entry.get('frame_reservation')
            if existing is not None and (existing.get('admitted') is True or existing['cancel'].is_set() or time.monotonic()>=existing['deadline']):self._close_control_frame(entry)
            if entry.get('frame_reservation') is not None: raise HelperRefused('frame_unavailable')
            host=_identity(os.getpid())
            if host is None: raise HelperRefused('frame_unavailable')
            directory=tempfile.TemporaryDirectory(prefix='oc-control-frame-');listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            path=os.path.join(directory.name,'fd')
            try: listener.bind(path);listener.listen(1);listener.settimeout(.25)
            except BaseException: listener.close();directory.cleanup();raise
            secret=secrets.token_hex(32);token_id=secrets.token_hex(32)
            reservation={'listener':listener,'directory':directory,'secret':secret,'token':token_id,'scope':scope,
                         'epoch':self.epoch,'cancel':threading.Event(),'ready':threading.Event(),'deadline':time.monotonic()+30,
                         'holder':holder,'fd':None,'claimed':False}
            entry['frame_reservation']=reservation
        def receive():
            import array,socket,struct
            fd=None
            try:
                while not reservation['cancel'].is_set() and time.monotonic()<reservation['deadline']:
                    try: conn,_=listener.accept();break
                    except socket.timeout:continue
                    except OSError:return
                else:return
                with conn:
                    conn.settimeout(3);peer=conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i'))
                    peer_pid,peer_uid,_=struct.unpack('3i',peer)
                    if peer_pid!=pid or peer_uid!=os.getuid() or not _still(pid,entry):return
                    conn.sendall(secret.encode('ascii'))
                    payload,ancillary,flags,_=conn.recvmsg(16,socket.CMSG_SPACE(array.array('i').itemsize),socket.MSG_CMSG_CLOEXEC)
                    received=array.array('i')
                    for level,kind,data in ancillary:
                        if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:received.frombytes(data[:len(data)-(len(data)%received.itemsize)])
                    if len(received)!=1:
                        for item in received:os.close(item)
                        return
                    fd=received[0]
                    if flags & (socket.MSG_CTRUNC|socket.MSG_TRUNC) or len(payload)!=16:return
                    serial=int.from_bytes(payload[:8],'big');width=int.from_bytes(payload[8:12],'big');height=int.from_bytes(payload[12:],'big')
                    if not serial or not 1<=width<=4096 or not 1<=height<=4096 or width*height>16000000:return
                    with self._lock:
                        if not self._control_frame_current_locked(pid,entry,reservation):return
                        reservation.update(fd=fd,serial=serial,size=(width,height));fd=None
            except (OSError,ValueError):pass
            finally:
                if fd is not None:os.close(fd)
                listener.close();directory.cleanup();reservation['ready'].set()
        threading.Thread(target=receive,name='control-frame-handoff',daemon=True).start()
        return {'path':path,'secret':secret,'host_pid':os.getpid(),'host_start':host[0],'frame_token':token_id}

    def _control_frame_current_locked(self,pid,entry,reservation):
        return (self.helpers.get(pid) is entry and entry.get('frame_reservation') is reservation
            and not reservation['cancel'].is_set() and time.monotonic()<reservation['deadline']
            and self.epoch==reservation['epoch']==entry.get('epoch') and entry.get('holder') is reservation.get('holder')
            and len(entry.get('scope',()))==7 and len(reservation.get('scope',()))==7
            and entry['scope'][:6]==reservation['scope'][:6] and self._recording_holder_live(entry) and _still(pid,entry))

    def cancel_control_frame_token(self,token,scope,holder):
        with self._lock:
            for entry in self.helpers.values():
                r=entry.get("frame_reservation")
                if isinstance(r,dict) and r.get("token")==token and r.get("scope")==scope and r.get("holder") is holder:
                    self._close_control_frame(entry);return True
        return False

    def control_frame_waiter(self,token,scope,holder):
        with self._lock:
            for entry in self.helpers.values():
                r=entry.get("frame_reservation")
                if isinstance(r,dict) and r.get("token")==token and r.get("scope")==scope and r.get("holder") is holder:return r["ready"]
        raise HelperRefused("frame_unavailable")

    def claim_control_frame(self,token,scope,holder):
        with self._lock:
            for pid,entry in self.helpers.items():
                r=entry.get('frame_reservation')
                if (isinstance(r,dict) and r.get('token')==token and r.get('scope')==scope and r.get('holder') is holder
                    and not r.get('claimed') and r['ready'].is_set() and r.get('fd') is not None
                    and self._control_frame_current_locked(pid,entry,r)):
                    r['claimed']=True;fd=r.pop('fd');return {'fd':fd,'serial':r['serial'],'size':r['size'],'record':r,'pid':pid,'entry':entry}
        raise HelperRefused('frame_unavailable')

    def control_frame_current(self,claim):
        with self._lock:
            try:return self._control_frame_current_locked(claim['pid'],claim['entry'],claim['record']) and claim['record'].get('claimed') is True
            except (KeyError,TypeError):return False

    @staticmethod
    def _close_control_frame(entry):
        r=entry.pop('frame_reservation',None)
        if not isinstance(r,dict):return
        r['cancel'].set();r['ready'].set()
        try:r['listener'].close()
        except OSError:pass
        fd=r.pop('fd',None)
        if fd is not None:
            try:os.close(fd)
            except OSError:pass

    def cancel_control_frame(self,claim):
        with self._lock:
            if claim.get('entry',{}).get('frame_reservation') is claim.get('record'):
                self._close_control_frame(claim['entry'])

    def admit_control_frame(self,claim,owner,session,actor,width,height,resource):
        with self._lock:
            if not self.control_frame_current(claim) or claim['record']['size']!=(width,height) or not resource:return None
            claim['record']['deadline']=time.monotonic()+OBS_MAX_AGE_S
            claim['record']['admitted']=True
            oid=secrets.token_hex(8)
            self.pixel[oid]={'owner':owner,'session':session,'epoch':self.epoch,'made':self.clock(),'used':False,'size':(width,height),
                             'resource':resource,'actor':actor,'control_frame':claim}
            if len(self.pixel)>32:self.pixel.pop(next(iter(self.pixel)))
            self._claim(actor,['screen'])
            return {'observationId':oid,'epoch':self.epoch,'capturedResource':resource,'maxAgeS':OBS_MAX_AGE_S}


    @staticmethod
    def native_session_for_chat(owner: str, host_session: str) -> Optional[str]:
        """Resolve the Host chat to the current engine session used by helper proofs."""
        from src.openclank.transcript_projection import get_managed_binding

        try:
            binding = get_managed_binding(host_session, owner=owner)
            if (binding.get("owner") == owner and binding.get("stableChatID") == host_session
                    and binding.get("transition") is None and type(binding.get("engineSessionID")) is str):
                return binding["engineSessionID"] or None
        except (KeyError, ValueError, RuntimeError):
            pass
        return None

    def _recording_entries(self, owner: str, account: str, native_session: str):
        return [entry for pid, entry in self.helpers.items()
                if entry.get('kind') == 'control' and entry.get('epoch') == self.epoch
                and len(entry.get('scope', ())) >= 4 and entry['scope'][0] == owner
                and entry['scope'][1] == native_session and entry['scope'][3] == account
                and self._recording_holder_live(entry) and _still(pid, entry)]

    def recording_stream_current(self, owner: str, account: str, session: str, binding: dict) -> bool:
        native_session = self.native_session_for_chat(owner, session)
        if not native_session:
            return False
        with self._lock:
            return self.epoch == binding.get('epoch') and any(entry.get('stream_id') == binding.get('id')
                for entry in self._recording_entries(owner, account, native_session))

    def recording_stream_binding(self, owner: str, account: str, session: str) -> Optional[dict]:
        native_session = self.native_session_for_chat(owner, session)
        if not native_session:
            return None
        with self._lock:
            choices = [entry for entry in self._recording_entries(owner, account, native_session)
                       if entry.get('stream_fd') is not None]
            return {'id': choices[0]['stream_id'], 'epoch': self.epoch} if len(choices) == 1 else None

    def claim_recording_stream(self, owner: str, account: str, session: str, binding: dict, revoke: Callable[[], None]) -> Optional[dict]:
        """Atomically spend the one control stream and attach its retirement hook."""
        native_session = self.native_session_for_chat(owner, session)
        if not native_session:
            return None
        with self._lock:
            if self.epoch != binding.get('epoch'):
                return None
            choices = [entry for entry in self._recording_entries(owner, account, native_session)
                       if entry.get('stream_id') == binding.get('id')]
            if len(choices) != 1:
                return None
            entry = choices[0]
            if entry.get('stream_fd') is None or entry.get('recording_revoke') is not None:
                return None
            entry['recording_revoke'] = revoke
            return {'fd': entry.pop('stream_fd'), 'serial': entry.pop('stream_serial')}

    @staticmethod
    def _helper_live(pid: int, entry: Mapping[str, Any]) -> bool:
        if entry.get("platform") == "windows":
            return windows.helper_alive(entry)
        if entry.get("platform") == "macos":
            return macos.helper_alive(entry)
        return _still(pid, entry)

    def scoped_helper_status(self, owner: str, account: str, native_session: str) -> dict[str, Any]:
        """Content-free readback for the exact chat, without exposing helper identifiers."""
        with self._lock:
            activity = []
            pending = False
            for pid, entry in self.helpers.items():
                scope = entry.get("scope", ())
                if (len(scope) < 4 or scope[0] != owner or scope[1] != native_session or scope[3] != account):
                    continue
                if not self._helper_live(pid, entry):
                    pending = True
                    continue
                target = entry.get("control_target")
                dimensions = target["rect"] if isinstance(target, dict) else None
                activity.append({
                    "actorId": scope[2], "kind": entry["kind"], "platform": entry.get("platform") or "linux",
                    "target": {"kind": "shared_display", "width": dimensions["width"], "height": dimensions["height"]}
                    if dimensions else {"kind": "unavailable"},
                    "takeoverShortcut": entry.get("takeover_shortcut"),
                    "cursorOverlayAtBegin": entry.get("cursor_overlay_at_begin"),
                })
            return {"session_active": any(row["kind"] == "control" for row in activity),
                    "accessibility_active": any(row["kind"] == "a11y" for row in activity),
                    "busy": pending, "pending_stop_reconciliation": pending, "activity": activity,
                    "takeoverScope": "host"}

    def helper_status(self) -> dict[str, Any]:
        with self._lock:
            active = [entry for pid, entry in self.helpers.items() if entry.get("kind") == "control" and self._helper_live(pid, entry)]
            pending = any(entry.get("kind") == "control" and not self._helper_live(pid, entry) for pid, entry in self.helpers.items())
            a11y = any(entry.get("kind") == "a11y" and self._helper_live(pid, entry) for pid, entry in self.helpers.items())
            if not active: return {"session_active": False, "accessibility_active": a11y, "pending_stop_reconciliation": pending}
            entry = active[-1]
            return {"session_active": True, "accessibility_active": a11y, "pending_stop_reconciliation": pending,
                    "takeover_shortcut": entry.get("takeover_shortcut"),
                    "cursor_overlay_at_begin": entry.get("cursor_overlay_at_begin"),
                    "recording_target_available": False}

    def helper_takeover(self, token: str, scope: tuple, holder: Any = None) -> bool:
        """A registered helper observed an OS stop. The token can only revoke."""
        with self._lock:
            valid = any(entry.get("registration") == token and entry.get("scope") == scope
                        and entry.get("epoch") == self.epoch and entry.get("kind") == "control"
                        and entry.get("holder") is holder for entry in self.helpers.values())
            if not valid: return False
            self.take_over(scope[0] if scope else None)
            return True

    def unregister_helper(self, pid: int, scope: Optional[tuple[str, str, str]] = None) -> None:
        with self._lock:
            entry = self.helpers.get(int(pid))
            if entry is not None and entry.get("platform") in ("windows", "macos") and entry.get("scope") != scope:
                return
            entry = self.helpers.pop(int(pid), None)
            if entry is not None: self._close_recording_stream(entry)
            if entry is not None and entry.get("platform") in ("windows", "macos"):
                (windows if entry["platform"] == "windows" else macos).close_helper(entry)

    def take_over(self, owner: Optional[str] = None) -> dict[str, Any]:
        """Human Stop: bump the epoch (every earlier observation is now older than the Stop), end every registered helper process group
        at once, drop leases, observations and holds. Idempotent."""
        with self._lock:
            self.epoch += 1
            entries = list(self.helpers.items())
            self.helpers.clear()
            launches = list(self.launches.values())
            self.launches.clear()
            self.leases = NativeTargetRegistry(clock=self.clock)
            self.pixel.clear(); self.snaps.clear(); self.windows.clear(); self.holds.clear()
        for launch in launches:
            launch["pipe"].cancel()
        ended = 0
        release_unknown = False
        for pid, entry in entries:
            self._close_recording_stream(entry)
            if entry.get("platform") in ("windows", "macos"):
                backend = windows if entry["platform"] == "windows" else macos
                try:
                    if backend.terminate_helper(entry):
                        ended += 1
                        release_unknown = True
                finally:
                    backend.close_helper(entry)
                continue
            if not _still(pid, entry):                   # gone, or the pid now belongs to something else: signal nothing
                continue
            try:
                os.killpg(pid, signal.SIGTERM)       # the helper releases held input and closes the portal session
            except (ProcessLookupError, PermissionError):
                continue
            ended += 1
            threading.Timer(1.0, _kill_hard, (pid, entry)).start()
        result = {"epoch": self.epoch, "ended": ended}
        if release_unknown:
            result["input_release"] = "unknown"
        return result

    # ---- holds (row 24: one agent per app unless the owner allowed several)
    def _hold_blocked(self, actor: Optional[str], keys: list[str], multi: bool) -> bool:
        if multi or actor is None:
            return False
        now = self.clock()
        return any((h := self.holds.get(k)) and h["until"] > now and h["actor"] != actor for k in keys)

    def _claim(self, actor: Optional[str], keys: list[str]) -> None:
        if actor is not None:
            for k in keys:
                self.holds[k] = {"actor": actor, "until": self.clock() + HOLD_TTL_S}

    # ---- observations
    def window_observed(self, owner: str, session: str, actor: str, snapshot_id: str, app: Mapping[str, Any], window: Mapping[str, Any], helper_gen: str) -> dict[str, Any]:
        """A window tree was read: register or refresh its lease and bind the snapshot to the lease generation and the current epoch."""
        tab = f"{app['pid']}:{app.get('start', 0)}:{window['index']}"
        title = str(window.get("title", ""))[:200]
        with self._lock:
            key = (owner, session, tab, helper_gen)
            known = self.windows.get(key)
            if known is None:
                view = self.leases.register_selected_tab(owner_id=owner, host_session_id=session, workspace_id=WORKSPACE, binding_revision=0, connection_id=helper_gen,
                                                         connection_generation=helper_gen, tab_id=tab, title=title or "window", url="about:blank")
                self.windows[key] = {"target": view["targetId"], "nav": view["navigationGeneration"]}
                known = self.windows[key]
            else:
                view = self.leases.record_observation(target_id=known["target"], owner_id=owner, host_session_id=session, workspace_id=WORKSPACE, binding_revision=0,
                                                      connection_generation=helper_gen, navigation_generation=known["nav"], observed_tab_id=tab, title=title or "window", url="about:blank")
            self.snaps[snapshot_id] = {"target": known["target"], "nav": known["nav"], "gen": view["observationGeneration"], "epoch": self.epoch, "owner": owner,
                                       "session": session, "helper_gen": helper_gen, "keys": self._app_keys(app)}
            if len(self.snaps) > 64:
                self.snaps.pop(next(iter(self.snaps)))
            self._claim(actor, self._app_keys(app))
            return {"targetId": view["targetId"], "observationGeneration": view["observationGeneration"], "epoch": self.epoch, "owner": owner, "session": session,
                    "capturedResource": snapshot_id}

    @staticmethod
    def _app_keys(app: Mapping[str, Any]) -> list[str]:
        keys = []
        if app.get("pid"):
            keys.append(f"pid:{app['pid']}")
        if app.get("name"):
            keys.append("name:" + str(app["name"]).lower())
        return keys

    def capture_observed(self, owner: str, session: str, actor: str, width: int, height: int, resource: str = "", target: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """A screen capture happened: pixel actions may name this observation."""
        oid = secrets.token_hex(8)
        with self._lock:
            self.pixel[oid] = {"owner": owner, "session": session, "epoch": self.epoch, "made": self.clock(), "used": False, "size": (int(width), int(height)), "resource": str(resource) if len(str(resource)) <= 16384 else "",
                               "capture_target": dict(target) if isinstance(target, Mapping) else None}
            if len(self.pixel) > 32:
                self.pixel.pop(next(iter(self.pixel)))
            self._claim(actor, ["screen"])
        return {"observationId": oid, "epoch": self.epoch, "capturedResource": resource, "maxAgeS": OBS_MAX_AGE_S}

    # ---- the checks (typed codes; nothing here acts)
    def precheck(self, owner: str, session: str, actor: Optional[str], call: Mapping[str, Any], settings: Optional[Mapping[str, Any]], *, scope: Optional[tuple] = None, holder: Any = None) -> Optional[str]:
        """None when the call may go on to approval; else the typed refusal code. Used before the approval card (Host) and again by the tool."""
        multi = bool(settings and settings.get("multi_agent") is True)
        act = call.get("action")
        with self._lock:
            if act in PIXEL_ACTIONS or act == "begin":
                if self._hold_blocked(actor, ["screen"], multi):
                    return "app_held"
                if act == "begin":
                    return None
                obs = self.pixel.get(str(call.get("observation") or ""))
                if obs is None:
                    return "observation_required"
                if obs["owner"] != owner or obs["session"] != session:
                    return "lease_scope"
                if obs["used"] or obs["epoch"] != self.epoch or self.clock() - obs["made"] > OBS_MAX_AGE_S:
                    return "observation_stale"
                if sys.platform.startswith("linux"):
                    proof = obs.get("control_frame")
                    if (obs.get("actor") != actor or not isinstance(proof,dict) or not self.control_frame_current(proof)
                        or not isinstance(scope,tuple) or len(scope)!=7 or len(proof["record"].get("scope",()))!=7
                        or scope[:6]!=proof["record"]["scope"][:6] or proof["record"].get("holder") is not holder):
                        return "capture_target_unbound"
                if sys.platform == "darwin":
                    target = obs.get("capture_target") or {}
                    try:
                        geometry = macos.primary_geometry()
                        current = macos.primary_display_id()
                        valid = (target.get("source") == "display" and type(target.get("id")) is int
                                 and target["id"] == current and bool(obs["resource"])
                                 and obs["size"] == (geometry[4], geometry[5])
                                 and target.get("bounds") == list(geometry[:4]))
                    except (OSError, RuntimeError, AttributeError, TypeError, IndexError):
                        valid = False
                    if not valid: return "capture_target_unbound"
                return None
            if act in ("click_element", "secondary", "set_value", "type_element", "select_text") or (act == "hand_over_file" and call.get("direction") == "upload" and "snapshot_id" in call):
                snap = self.snaps.get(str(call.get("snapshot_id") or ""))
                if snap is None:
                    return "lease_expired"
                if snap["owner"] != owner or snap["session"] != session:
                    return "lease_scope"
                if snap["epoch"] != self.epoch:
                    return "observation_stale"
                try:
                    self.leases.resolve(target_id=snap["target"], owner_id=owner, host_session_id=session, workspace_id=WORKSPACE, binding_revision=0,
                                        connection_generation=snap["helper_gen"], navigation_generation=snap["nav"], observation_generation=snap["gen"])
                except BrowserTargetNotFound:
                    return "lease_expired"
                except BrowserTargetScopeError:
                    return "lease_scope"
                except BrowserTargetStale:
                    return "observation_stale"
                return "app_held" if self._hold_blocked(actor, snap["keys"], multi) else None
            if act in ("list_apps", "list_windows", "app_state"):
                sel = call.get("app") or {}
                keys = ([f"pid:{sel['pid']}"] if "pid" in sel else []) + (["name:" + str(sel["name"]).lower()] if "name" in sel else [])
                return "app_held" if self._hold_blocked(actor, keys, multi) else None
        return None

    def consume_observation(self, call: Mapping[str, Any]) -> None:
        """An action is going out: its pixel observation is spent (one observation backs one action)."""
        with self._lock:
            obs = self.pixel.get(str(call.get("observation") or ""))
            if obs is not None:
                obs["used"] = True
                proof=obs.get("control_frame")
                if isinstance(proof,dict): self.cancel_control_frame(proof)


def _kill_hard(pid: int, entry: Mapping[str, Any]) -> None:
    if not _still(pid, entry):                           # re-check before the hard kill: the helper may be gone and the pid reused
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


STATE = ControlState()          # the Host's state (also the default for in-process use and tests)


class LocalHandle:
    """The async interface the control path uses, bound to one owner/session/actor; here directly on a ControlState."""

    def __init__(self, state: ControlState, owner: str, session: str, actor: str) -> None:
        self.state, self.owner, self.session, self.actor = state, owner, session, actor
        self.helper_gen = secrets.token_hex(8)

    async def epoch(self) -> int:
        return self.state.epoch

    async def register_helper(self, pid: int, kind: str) -> str:
        return self.state.register_helper(pid, kind, (self.owner, self.session, self.actor))

    async def reserve_recording_stream(self, token: str) -> dict:
        return self.state.reserve_recording_stream(token, (self.owner, self.session, self.actor))

    async def helper_capabilities(self, token: str, shortcut: Optional[str], overlay: Optional[bool], target: Optional[dict] = None) -> bool:
        return self.state.helper_capabilities(token, (self.owner, self.session, self.actor), shortcut, overlay, target)

    async def helper_takeover(self, token: str) -> bool:
        accepted = self.state.helper_takeover(token, (self.owner, self.session, self.actor))
        if accepted:
            from src.openclank.computer_control.protocol import REGISTRY
            REGISTRY.revoke(self.owner, self.session)
        return accepted

    async def reserve_windows_launch(self, kind: str) -> dict:
        return self.state.reserve_windows_launch(self.owner, self.session, self.actor, kind)

    async def complete_windows_launch(self, token: str) -> None:
        await self.state.complete_launch(self.owner, self.session, self.actor, token)

    async def cancel_windows_launch(self, token: str) -> None:
        self.state.cancel_windows_launch(self.owner, self.session, self.actor, token)

    async def reserve_macos_launch(self, kind: str) -> dict:
        return self.state.reserve_macos_launch(self.owner, self.session, self.actor, kind)

    async def complete_macos_launch(self, token: str) -> None:
        await self.state.complete_launch(self.owner, self.session, self.actor, token)

    async def cancel_macos_launch(self, token: str) -> None:
        self.state.cancel_macos_launch(self.owner, self.session, self.actor, token)

    async def end_proved_helper(self, token: str) -> bool:
        return self.state.end_proved_helper(self.owner, self.session, self.actor, token)

    async def unregister_helper(self, pid: int) -> None:
        self.state.unregister_helper(pid, (self.owner, self.session, self.actor))

    async def window_observed(self, snapshot_id: str, app: Mapping[str, Any], window: Mapping[str, Any]) -> Optional[dict]:
        return self.state.window_observed(self.owner, self.session, self.actor, snapshot_id, app, window, self.helper_gen)

    async def capture_observed(self, width: int, height: int, resource: str = "", target: Optional[dict] = None) -> Optional[dict]:
        return self.state.capture_observed(self.owner, self.session, self.actor, width, height, resource, target)

    async def precheck(self, call: Mapping[str, Any], settings: Optional[Mapping[str, Any]]) -> Optional[str]:
        return self.state.precheck(self.owner, self.session, self.actor, call, settings)

    async def consume_observation(self, call: Mapping[str, Any]) -> None:
        self.state.consume_observation(call)

    async def open_resource(self, resource_ref: str, app_id: str) -> dict:
        return {"ok": False, "code": "files_open_unavailable"}


def handle_broker(state: ControlState, owner: str, identity: Mapping[str, Any], request: Mapping[str, Any], account: str = "", holder: Any = None, holder_current: Any = None) -> dict[str, Any]:
    """The Host end of the `control_lease` broker op (owner comes from the authenticated request, never from the body)."""
    session, actor = str(identity.get("sessionID", "")), str(identity.get("actorID", ""))
    turn = (account, identity.get("rootOperationID"), identity.get("executionRevision"), identity.get("innerTurnID"))
    op = request.get("op")
    if op == "epoch":
        return {"epoch": state.epoch}
    if op in ("reserve_windows_launch", "reserve_macos_launch"):
        try:
            reserve = state.reserve_windows_launch if op == "reserve_windows_launch" else state.reserve_macos_launch
            return {"ok": True, **reserve(owner, session, actor, str(request.get("kind", "")), turn)}
        except HelperRefused as refused:
            return {"ok": False, "code": refused.code}
    if op in ("complete_windows_launch", "complete_macos_launch"):
        return {"ok": False, "code": "helper_launch_async_required"}
    if op in ("cancel_windows_launch", "cancel_macos_launch") and isinstance(request.get("reservation"), str):
        state.cancel_windows_launch(owner, session, actor, request["reservation"], turn)
        return {"ok": True}
    if op == "register_helper" and type(request.get("pid")) is int:
        try:
            registration = state.register_helper(request["pid"], str(request.get("kind", "")), (owner, session, actor, *turn), holder, holder_current)
        except HelperRefused as refused:
            return {"ok": False, "code": refused.code}
        return {"ok": True, "registration": registration}
    if op == "cancel_control_frame" and isinstance(request.get("frame_token"), str):
        return {"ok":state.cancel_control_frame_token(request["frame_token"],(owner,session,actor,*turn),holder)}
    if op == "reserve_control_frame" and isinstance(request.get("registration"), str):
        from src.settings import get_user_setting
        if get_user_setting("desktop_capture_enabled",owner,False) is not True or get_user_setting("desktop_control_enabled",owner,False) is not True: return {"ok":False,"code":"frame_unavailable"}
        try: return {"ok":True,**state.reserve_control_frame(request["registration"],(owner,session,actor,*turn),holder)}
        except (HelperRefused,OSError): return {"ok":False,"code":"frame_unavailable"}
    if op == "reserve_recording_stream" and isinstance(request.get("registration"), str):
        from src.settings import get_user_setting
        if get_user_setting("desktop_control_recording_enabled", owner, False) is not True: return {"ok": False, "code": "recording_stream_unavailable"}
        try: return {"ok": True, **state.reserve_recording_stream(request["registration"], (owner, session, actor, *turn), holder)}
        except (HelperRefused, OSError): return {"ok": False, "code": "recording_stream_unavailable"}
    if op == "helper_capabilities" and isinstance(request.get("registration"), str):
        accepted = state.helper_capabilities(request["registration"], (owner, session, actor, *turn), request.get("shortcut"), request.get("overlay"), request.get("target"), holder)
        return {"ok": True, "accepted": accepted}
    if op == "helper_takeover" and isinstance(request.get("registration"), str):
        accepted = state.helper_takeover(request["registration"], (owner, session, actor, *turn), holder)
        if accepted:
            from src.openclank.computer_control.protocol import REGISTRY
            REGISTRY.revoke(owner, session)
        return {"ok": True, "accepted": accepted}
    if op == "end_proved_helper" and isinstance(request.get("reservation"), str):
        return {"ok": True, "ended": state.end_proved_helper(owner, session, actor, request["reservation"], turn)}
    if op == "unregister_helper" and type(request.get("pid")) is int:
        state.unregister_helper(request["pid"], (owner, session, actor, *turn))
        return {"ok": True}
    if op == "window_observed" and isinstance(request.get("app"), dict) and isinstance(request.get("window"), dict) and isinstance(request.get("snapshot_id"), str):
        return {"lease": state.window_observed(owner, session, actor, request["snapshot_id"], request["app"], request["window"], str(request.get("helper_gen", "")))}
    if op == "capture_observed" and type(request.get("width")) is int and type(request.get("height")) is int:
        return {"observation": state.capture_observed(owner, session, actor, request["width"], request["height"], str(request.get("resource", "")), request.get("target") if isinstance(request.get("target"), dict) else None)}
    if op == "precheck" and isinstance(request.get("call"), dict):
        return {"code": state.precheck(owner, session, actor, request["call"], request.get("settings") if isinstance(request.get("settings"), dict) else None, scope=(owner,session,actor,*turn), holder=holder)}
    if op == "consume_observation" and isinstance(request.get("call"), dict):
        state.consume_observation(request["call"])
        return {"ok": True}
    raise ValueError("invalid control lease request")
