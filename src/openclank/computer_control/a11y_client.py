"""Host side of the accessibility helper (`--a11y-v1`): read an application window's element tree and act on ONE element.

No portal, no input injection. Two kinds of call, both bound to a human approval of the exact call:
  * `inspect_desktop` (handshake needs none; list_apps / list_windows / app_state need a consume-once approval per call),
  * element actions of `control_desktop` (click_element, secondary, set_value, type_element), one approval per call.
A snapshot lives in the helper process; this module keeps the matching claim table so the approval text shown to the human
(`target`) is checked against what the helper really returned BEFORE anything is sent. Typed text is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from . import action_log
from . import windows
from . import protocol as P

logger = logging.getLogger(__name__)
IDLE_S = 60.0
SNAP_TTL_S = 120.0
MAX_SNAPS = 8
START_TIMEOUT_S = 10.0
REQUEST_TIMEOUT_S = 45.0
APPROVE_TIMEOUT_S = 10.0
PERFORMED = "performed, effect not verified; read the window again with inspect_desktop app_state to check. Actions are never repeated automatically."


def _helper_argv() -> Optional[list[str]]:
    if windows.is_windows():
        return windows.helper_argv("a11y")
    if sys.platform == "darwin":
        from src.openclank.computer_control import macos
        return macos.helper_argv("control" if __name__.endswith(".linux") else "a11y")
    from src.openclank.native_capture_linux import _helper
    helper = _helper()
    return [str(helper), "--a11y-v1"] if helper.is_file() and os.access(helper, os.X_OK) else None


def _platform_ok() -> bool:
    if windows.is_windows():
        return windows.platform_ok()
    if sys.platform == "darwin": return True
    return sys.platform.startswith("linux") and bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")) and bool(os.environ.get("XDG_RUNTIME_DIR"))


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if sys.platform == "darwin":
        if proc.stdin is not None: proc.stdin.close()
        try: await asyncio.wait_for(proc.wait(), 1)
        except asyncio.TimeoutError: pass  # never signal an unverified PID
        return
    if proc.returncode is None and windows.is_windows():
        try: proc.terminate()  # asyncio owns the original Windows process handle
        except ProcessLookupError: pass
        await proc.wait()
        return
    if proc.returncode is None:
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: await asyncio.wait_for(proc.wait(), 1)
        except asyncio.TimeoutError:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            await proc.wait()


class _Helper:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.snaps: dict[str, dict[str, Any]] = {}
        self.last = 0.0
        self.watch: Optional[asyncio.Task] = None
        self.epoch = 0          # bumped by a human takeover; an action that started before it never runs after it
        self.died = False       # the helper vanished without being closed by us
        self.proof_token: Optional[str] = None
        self.registration: Optional[str] = None
        self.state: Any = None

    def active(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def close(self) -> None:
        proc, self.proc, self.snaps = self.proc, None, {}
        state, self.state = self.state, None
        self.registration = None
        token, self.proof_token = self.proof_token, None
        if proc is not None and state is not None:
            try:
                if sys.platform == "darwin" and token: await state.end_proved_helper(token)
                elif sys.platform != "darwin": await state.unregister_helper(proc.pid)
            except Exception: pass
        watch, self.watch = self.watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        if proc is not None:
            await _kill(proc)

    async def ensure(self, state: Any = None) -> bool:
        if self.active():
            return True
        if self.proc is not None:                       # it exited by itself: its snapshots died with it
            self.died = True
            await self.close()
        argv = _helper_argv()
        if argv is None:
            return False
        reservation = None
        if windows.is_windows() or sys.platform == "darwin":
            if state is None: return False
            try: reservation = await (state.reserve_macos_launch("a11y") if sys.platform == "darwin" else state.reserve_windows_launch("a11y"))
            except Exception: return False
        try:
            self.proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                             stderr=asyncio.subprocess.DEVNULL, limit=P.A11Y_STREAM_MAX, **windows.spawn_kwargs())
        except BaseException as exc:
            if reservation is not None:
                try: await (state.cancel_macos_launch(reservation["reservation"]) if sys.platform == "darwin" else state.cancel_windows_launch(reservation["reservation"]))
                except Exception: pass
            if isinstance(exc, OSError): return False
            raise
        if windows.is_windows() or sys.platform == "darwin":
            self.state, self.proof_token = state, reservation["reservation"]
            try: proved = await windows.bootstrap_helper(self.proc, state, reservation, "macos" if sys.platform == "darwin" else "windows")
            except BaseException:
                await self.close()
                raise
            if not proved:
                await self.close()
                return False
        self.state = state
        self.snaps, self.died, self.last = {}, False, time.monotonic()
        self.watch = asyncio.create_task(self._idle(self.proc))
        return True

    async def exchange(self, line: bytes, expect: str, timeout: float = REQUEST_TIMEOUT_S) -> Optional[dict]:
        """One request, one reply; None when the outcome is unknown (EOF, timeout, write failure)."""
        proc = self.proc
        try:
            proc.stdin.write(line)
            await proc.stdin.drain()
            reply = await asyncio.wait_for(proc.stdout.readline(), timeout)
        except (OSError, asyncio.TimeoutError, ValueError, AttributeError):
            return None
        self.last = time.monotonic()
        return P.parse_a11y_reply(reply, expect) if reply else None

    async def _idle(self, proc: asyncio.subprocess.Process) -> None:
        while True:
            await asyncio.sleep(max(0.0, self.last + IDLE_S - time.monotonic()))
            if self.proc is not proc:
                return
            if time.monotonic() >= self.last + IDLE_S and not self.lock.locked():
                await self.close()
                return


A = _Helper()


def _done(call: Optional[dict], outcome: str, approved: bool, reply: Optional[dict] = None, started: Optional[float] = None, stages: Optional[dict] = None, **extra: Any) -> dict:
    ok = outcome in ("performed", "ready", "read", "entered")
    op_id = uuid.uuid4().hex
    timings = dict(stages or {})
    if started is not None:
        timings["host"] = int((time.monotonic() - started) * 1000)
    rec = P.receipt(call, outcome, approved) if call and call["action"] in P.ELEMENT_ACTIONS else {"action": (call or {}).get("action", "invalid"), "outcome": outcome, "human_approved": bool(approved)}
    obs = P.observation_receipt(call, outcome, reply, op_id, approved, timings) if call and call["action"] in P.ELEMENT_ACTIONS else None
    logger.info("a11y receipt %s", json.dumps(obs or rec, sort_keys=True))
    return {"ok": ok, "outcome": outcome, "receipt": obs or rec, "exit_code": 0 if ok else 1, **extra}


def _remember(reply: dict) -> None:
    """Claim table for one snapshot: element id -> (role, name), plus app and window, so approvals can be checked against reality."""
    nodes: dict[str, tuple[str, str]] = {}
    stack = [reply["tree"]]
    while stack and len(nodes) < 1300:
        n = stack.pop()
        if type(n) is dict and type(n.get("id")) is str:
            nodes[n["id"]] = (str(n.get("role", "")), str(n.get("name", "")))
            stack.extend(c for c in (n.get("children") or []) if type(c) is dict)
    app = reply.get("app") if type(reply.get("app")) is dict else {}
    window = reply.get("window") if type(reply.get("window")) is dict else {}
    if len(A.snaps) >= MAX_SNAPS:
        A.snaps.pop(next(iter(A.snaps)))
    A.snaps[reply["snapshot_id"]] = {"nodes": nodes, "app": str(app.get("name", "")), "window": str(window.get("title", "")), "made": time.monotonic()}


async def _ask(approve: Callable[[str], Awaitable[bool]], args: Any, stages: Optional[dict] = None) -> bool:
    t0 = time.monotonic()
    try:
        return bool(await asyncio.wait_for(approve(P.args_digest(args)), APPROVE_TIMEOUT_S))
    except Exception:
        return False
    finally:
        if stages is not None:
            stages["permission_wait"] = int((time.monotonic() - t0) * 1000)


async def _spawn_noted(state: Any, stages: dict) -> bool:
    """Start (or find) the helper, record the process with the Host so a human Stop can end it, and time the acquisition."""
    t0 = time.monotonic()
    fresh = not A.active()
    ok = await A.ensure(state)
    stages["acquisition"] = int((time.monotonic() - t0) * 1000)
    if ok and fresh and state is not None and A.proc is not None and not windows.is_windows() and sys.platform != "darwin":
        try:
            registration = await state.register_helper(A.proc.pid, "a11y")
            if not isinstance(registration, str) or not registration: raise RuntimeError("helper registration unavailable")
            A.registration = registration
        except Exception:
            await A.close()
            return False
    return ok


async def inspect_action(args: Any, approve: Callable[[str], Awaitable[bool]], allow: Optional[dict] = None, state: Any = None) -> dict:
    """The tool entry; reads are logged like actions when the actions-only log is On."""
    result = await _inspect_action(args, approve, allow, state)
    action_log.record(result, allow)
    return result


async def _inspect_action(args: Any, approve: Callable[[str], Awaitable[bool]], allow: Optional[dict] = None, state: Any = None) -> dict:
    """handshake / list_apps / list_windows / app_state. Everything except handshake needs a consume-once approval.
    `allow` (the owner's settings, from the Host) says whether reading the accessibility tree is switched on; when given and Off,
    nothing starts and the refusal is typed (`setting_off`)."""
    started = time.monotonic()
    try:
        call = P.parse_inspect(args)
    except P.ProtocolError as exc:
        return _done(None, exc.code, False)
    if allow is not None and allow.get("accessibility") is not True:
        return _done(call, "setting_off", False, reason="desktop_accessibility_disabled")
    if not _platform_ok():
        return _done(call, "unavailable", False, reason="no_interactive_session")
    async with A.lock:
        if _helper_argv() is None:
            return _done(call, "unavailable", False, reason="helper_missing")
        epoch = A.epoch
        host_epoch = await state.epoch() if state is not None else 0
        stages: dict = {}
        if state is not None and call["action"] != "handshake":      # one agent per app unless the owner allowed several
            code = await state.precheck(call, allow)
            if code:
                return _done(call, code, False, reason=code)
        approved = False
        if call["action"] != "handshake":
            approved = await _ask(approve, args, stages)
            if not approved:
                return _done(call, "approval_required", False, stages=stages)
            if A.epoch != epoch or (state is not None and await state.epoch() != host_epoch):
                return _done(call, "taken_over", True, stages=stages)
            if state is not None:
                code = await state.precheck(call, allow)
                if code:
                    return _done(call, code, True, stages=stages, reason=code)
        if not await _spawn_noted(state, stages):
            return _done(call, "unavailable", approved, stages=stages, reason="helper_start_failed")
        expect = {"handshake": "handshake", "list_apps": "apps", "list_windows": "windows", "app_state": "state"}[call["action"]]
        t1 = time.monotonic()
        reply = await A.exchange(P.a11y_request_line(call), expect)
        stages["transport"] = int((time.monotonic() - t1) * 1000)
        if reply is None:
            await A.close()
            return _done(call, "unavailable", approved, stages=stages, reason="helper_no_reply")
        if not reply["ok"]:
            return _done(call, reply["code"], approved, stages=stages)
        if call["action"] == "app_state":
            _remember(reply)
            if state is not None:
                tgt = reply.get("target") if isinstance(reply.get("target"), dict) else {}
                app = dict(reply.get("app") or {})
                try: app["start"] = int(str(tgt.get("processStartIdentity", "0:0")).split(":")[1])
                except (ValueError, IndexError): app["start"] = 0
                lease = await state.window_observed(reply["snapshot_id"], app, reply.get("window") or {"index": 0})
                if lease is not None:
                    reply = {**reply, "lease": lease, "target": {**tgt, "owner": lease.get("owner"), "session": lease.get("session"), "capturedResource": lease.get("capturedResource")}}
        return {**_done(call, "ready" if call["action"] == "handshake" else "read", approved, stages=stages), "ok": True, "exit_code": 0, "result": reply}


async def element_action(call: dict, args: Any, approve: Callable[[str], Awaitable[bool]], allow: Optional[dict] = None, state: Any = None) -> dict:
    """click_element / secondary / set_value / type_element. Validation and the claim check come BEFORE the approval prompt;
    the helper re-resolves the element again after it, and a stale element or changed window refuses without acting."""
    started = time.monotonic()
    if allow is not None and (allow.get("accessibility") is not True or allow.get("control") is not True):
        return _done(call, "setting_off", False, reason="desktop_accessibility_disabled" if allow.get("control") is True else "desktop_control_disabled")
    if not _platform_ok():
        return _done(call, "unavailable", False, reason="no_interactive_session")
    async with A.lock:
        if _helper_argv() is None:
            return _done(call, "unavailable", False, reason="helper_missing")
        snap = A.snaps.get(call["snapshot_id"])
        if snap is None or time.monotonic() - snap["made"] > SNAP_TTL_S or not A.active():
            A.snaps.pop(call["snapshot_id"], None)
            return _done(call, "snapshot_expired", False)
        node = snap["nodes"].get(call["element"])
        if node is None:
            return _done(call, "no_such_element", False)
        t = call["target"]
        if (t["app"], t["window"], t["role"], t["name"]) != (snap["app"], snap["window"], node[0], node[1]):
            return _done(call, "stale_element", False)      # the words shown to the human do not describe this element
        epoch = A.epoch
        host_epoch = await state.epoch() if state is not None else 0
        stages: dict = {"acquisition": 0}
        if not await _ask(approve, args, stages):
            return _done(call, "approval_required", False, stages=stages)
        # After the human decided: a takeover, an expired snapshot or a dead helper means nothing is sent.
        snap = A.snaps.get(call["snapshot_id"])
        if A.epoch != epoch or (state is not None and await state.epoch() != host_epoch):
            return _done(call, "taken_over", True, stages=stages)
        if snap is None or time.monotonic() - snap["made"] > SNAP_TTL_S or not A.active():
            A.snaps.pop(call["snapshot_id"], None)
            return _done(call, "snapshot_expired", True, stages=stages)
        if state is not None:
            code = await state.precheck(call, allow)
            if code:
                return _done(call, code, True, stages=stages, reason=code)
        t1 = time.monotonic()
        reply = await A.exchange(P.a11y_request_line(call), "act", REQUEST_TIMEOUT_S)
        stages["transport"] = int((time.monotonic() - t1) * 1000)
        if reply is None:
            await A.close()
            return _done(call, "outcome_unknown", True, started=started, stages=stages, message="Not repeated. Read the window again to see what happened.")
        if reply["ok"]:
            return _done(call, "performed", True, reply, started, stages, message=PERFORMED, window_changed=reply["window_changed"], verified=reply.get("verified", "unknown"))
        code = reply["code"]
        if code in ("timeout", "action_failed", "invalid_reply"):
            return _done(call, "outcome_unknown", True, None, started, stages, message="Not repeated. Read the window again to see what happened.")
        return _done(call, code, True, None, started, stages)


async def chooser_action(call: dict, args: Any, approve: Callable[[str], Awaitable[bool]], allow: Optional[dict], state: Any) -> dict:
    """Enter one selected snapshot in the already observed Linux chooser."""
    if not sys.platform.startswith("linux") or state is None or not _platform_ok():
        return _done(call, "file_dialog_unproven", False)
    if allow is not None and (allow.get("accessibility") is not True or allow.get("control") is not True):
        return _done(call, "setting_off", False)
    async with A.lock:
        snap = A.snaps.get(call["snapshot_id"])
        if snap is None or time.monotonic() - snap["made"] > SNAP_TTL_S or not A.active() or not A.registration:
            return _done(call, "snapshot_expired", False)
        node = snap["nodes"].get(call["element"])
        target = call["target"]
        if node is None or (target["app"], target["window"], target["role"], target["name"]) != (snap["app"], snap["window"], node[0], node[1]):
            return _done(call, "stale_element", False)
        # Older helpers can accept arbitrary editable chooser fields.  Never
        # issue the private operation unless this exact closed profile is present.
        capability = await A.exchange(P.a11y_request_line({"action": "handshake"}), "handshake")
        if not capability or capability.get("ok") is not True or capability.get("chooser_field_profile") != "gtk3-location-v1":
            return _done(call, "file_dialog_unproven", False)
        epoch, host_epoch = A.epoch, await state.epoch()
        code = await state.precheck(call, allow)
        if code: return _done(call, code, False)
        if not await _ask(approve, args): return _done(call, "approval_required", False)
        if A.epoch != epoch or await state.epoch() != host_epoch or not A.active():
            return _done(call, "taken_over", True)
        if time.monotonic() - snap["made"] > SNAP_TTL_S or A.snaps.get(call["snapshot_id"]) is not snap:
            return _done(call, "snapshot_expired", True)
        code = await state.precheck(call, allow)
        if code: return _done(call, code, True)
        try:
            snapshot = await state.selected_snapshot(A.registration, call["resource_ref"], call["expected_revision"])
        except Exception:
            snapshot = None
        if not snapshot: return _done(call, "file_dialog_unproven", True)
        # Revalidate the original source and actor immediately before issuing.
        # The Host keeps the snapshot even if this request or a reply is lost.
        try:
            if (A.epoch != epoch or await state.epoch() != host_epoch or not A.active()
                    or await state.precheck(call, allow) or not await state.selected_snapshot_current(A.registration)):
                return _done(call, "file_dialog_unproven", True)
        except Exception:
            return _done(call, "file_dialog_unproven", True)
        try:
            line = P.chooser_request_line(call["snapshot_id"], call["element"], snapshot["path"])
        except P.ProtocolError:
            return _done(call, "file_dialog_unproven", True)
        task = asyncio.create_task(A.exchange(line, "chooser", REQUEST_TIMEOUT_S))
        try:
            reply = await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
                except Exception: break
            raise
        try:
            current = (A.epoch == epoch and await state.epoch() == host_epoch and A.active()
                       and not await state.precheck(call, allow) and await state.selected_snapshot_current(A.registration))
        except Exception:
            current = False
        if (reply is None or reply.get("code") in ("outcome_unknown", "timeout", "invalid_reply") or not current):
            return _done(call, "outcome_unknown", True, message="Not repeated; file entry may have occurred.", lease_max_seconds=snapshot["lease_max_seconds"])
        if reply.get("ok") is True:
            return _done(call, "entered", True, message="The field read back the entry; selection and opening are not verified. Snapshot custody lasts at most 60 seconds from reservation and can end sooner.", lease_max_seconds=snapshot["lease_max_seconds"])
        return _done(call, str(reply.get("code") or "file_dialog_unproven"), True)


async def take_over() -> None:
    """Human takeover: drop every snapshot and end the helper NOW (no waiting for an action in flight, no approval)."""
    A.epoch += 1
    proc, A.proc, A.snaps = A.proc, None, {}
    watch, A.watch = A.watch, None
    if watch is not None and watch is not asyncio.current_task():
        watch.cancel()
    if proc is not None:
        if sys.platform == "darwin" and A.state is not None and A.proof_token:
            try: await A.state.end_proved_helper(A.proof_token)
            except Exception: pass
            A.proof_token = None
        await _kill(proc)


def readiness() -> dict:
    """Plan readiness vocabulary for the accessibility capability, from facts that need no helper start."""
    if not _platform_ok():
        return {"state": "no_interactive_session"}
    if sys.platform == "darwin":
        from src.openclank.computer_control import macos
        return {"state": "unverified" if macos.permission_status()["accessibility"] else "permission_required",
                "note": "AX and helper launch are not yet qualified on macOS."}
    if _helper_argv() is None:
        return {"state": "driver_missing"}
    if A.lock.locked():
        return {"state": "busy"}
    if A.died:
        return {"state": "disconnected"}
    return {"state": "ready" if A.active() else "permission_required", "note": "Reading a window needs your approval for each call; handshake tells whether the accessibility bus is on."}
