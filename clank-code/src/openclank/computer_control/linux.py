"""One Linux control session (a `--control-v1` helper child) per process.

Every action that reaches the helper first passes `approve(args_digest)`: a Host-verified,
consume-once human approval bound to that exact call. No approval, nothing is written to the
helper. Turn end is NOT observed here; a session ends on stop, helper exit/error/caps, a
30 s idle timeout after the last action, or process exit.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Awaitable, Callable, Optional

from . import a11y_client
from . import windows
from . import action_log
from . import hoststate
from . import protocol as P

logger = logging.getLogger(__name__)
IDLE_S = 30.0
BEGIN_TIMEOUT_S = 125.0
ACTION_TIMEOUT_S = 30.0
APPROVE_TIMEOUT_S = 10.0
SENT = "sent, effect not verified; capture again with capture_desktop to check. Actions are never repeated automatically."


def _helper_argv() -> Optional[list[str]]:
    if windows.is_windows():
        return windows.helper_argv("control")
    if sys.platform == "darwin":
        from src.openclank.computer_control import macos
        return macos.helper_argv("control" if __name__.endswith(".linux") else "a11y")
    from src.openclank.native_capture_linux import _helper
    helper = _helper()
    return [str(helper), "--control-v1"] if helper.is_file() and os.access(helper, os.X_OK) else None


def _platform_ok() -> bool:
    if windows.is_windows():
        return windows.platform_ok()
    if sys.platform == "darwin": return True
    return sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland"


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


class _Session:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.stream: Optional[tuple[int, int]] = None
        self.takeover_shortcut: Optional[str] = None
        self.cursor_overlay_at_begin: Optional[bool] = None
        self.control_target: Optional[dict] = None
        self.clipboard_enabled = False
        self.last = 0.0
        self.watch: Optional[asyncio.Task] = None
        self.epoch = 0          # bumped by a human takeover: an action that started before it never runs after it
        self.died = False       # the helper vanished without being closed by us
        self.last_code = ""     # last helper refusal code of a begin (sticky "denied")
        self.proof_token: Optional[str] = None
        self.registration: Optional[str] = None
        self.exit_watch: Optional[asyncio.Task] = None
        self.host_epoch_at_begin: Optional[int] = None
        self.takeover_unconfirmed: Optional[int] = None
        self.takeover_notice = False
        self.state: Any = None  # the Host target state handle of the session that began it (leases, observations, takeover epoch)

    def active(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def close(self, graceful: bool = True) -> None:
        proc, self.proc, self.stream = self.proc, None, None
        self.takeover_shortcut = None
        self.cursor_overlay_at_begin = None
        self.control_target = None
        self.clipboard_enabled = False
        exit_watch, self.exit_watch = self.exit_watch, None
        if exit_watch is not None and exit_watch is not asyncio.current_task(): exit_watch.cancel()
        self.registration = None
        if proc is not None and self.state is not None and sys.platform != "darwin" and self.takeover_unconfirmed is None:
            try: await self.state.unregister_helper(proc.pid)
            except Exception: pass
        watch, self.watch = self.watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        if proc is None:
            return
        try:
            if graceful and proc.returncode is None and proc.stdin is not None:
                proc.stdin.write(P.build_stop())
                await proc.stdin.drain()
                await asyncio.wait_for(proc.stdout.readline(), 2)
        except Exception:
            pass
        if sys.platform == "darwin" and self.state is not None and self.proof_token:
            try: await self.state.end_proved_helper(self.proof_token)
            except Exception: pass
        self.proof_token = None
        await _kill(proc)

    async def notice_takeover(self, proc: asyncio.subprocess.Process, state: Any, registration: str) -> None:
        if self.proc is not proc or self.takeover_notice: return
        self.takeover_notice = True
        self.epoch += 1
        self.died = True
        self.takeover_unconfirmed = self.host_epoch_at_begin if self.host_epoch_at_begin is not None else -1
        try:
            if await state.helper_takeover(registration): self.takeover_unconfirmed = None
        except Exception: pass

    async def watch_exit(self, proc: asyncio.subprocess.Process, state: Any, registration: str) -> None:
        try: code = await proc.wait()
        except asyncio.CancelledError: return
        if code == 73: await self.notice_takeover(proc, state, registration)

    async def exchange(self, line: bytes, expect: str, timeout: float) -> Optional[dict]:
        """One request, one reply; None when the outcome is unknown (EOF, timeout, write failure)."""
        proc = self.proc
        try:
            proc.stdin.write(line)
            await proc.stdin.drain()
            reply = await asyncio.wait_for(proc.stdout.readline(), timeout)
        except (OSError, asyncio.TimeoutError, ValueError):
            return None
        return P.parse_reply(reply, expect) if reply else None

    async def _idle(self, proc: asyncio.subprocess.Process) -> None:
        while True:
            await asyncio.sleep(max(0.0, self.last + IDLE_S - time.monotonic()))
            async with self.lock:
                if self.proc is not proc:
                    return
                if time.monotonic() >= self.last + IDLE_S:
                    await self.close()
                    return


S = _Session()


def _kill_sync() -> None:
    proc = S.proc
    if proc is not None and proc.returncode is None and sys.platform == "darwin":
        if proc.stdin is not None: proc.stdin.close()
    elif proc is not None and proc.returncode is None and windows.is_windows():
        try: proc.terminate()
        except ProcessLookupError: pass
    elif proc is not None and proc.returncode is None:
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: return
        time.sleep(0.3)
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass


atexit.register(_kill_sync)


def _done(call: Optional[dict], outcome: str, approved: bool, timings: Optional[dict] = None, **extra: Any) -> dict:
    rec = P.receipt(call, outcome, approved)
    if timings is not None:
        rec["stageTimingsMs"] = P.stage_timings(timings)
    logger.info("computer_control receipt %s", json.dumps(rec, sort_keys=True))
    ok = outcome == "sent" or outcome == "active" or outcome == "performed"
    return {"ok": ok, "outcome": outcome, "receipt": rec, "exit_code": 0 if ok else 1, **extra}


_AGAIN = {"observation_required", "observation_stale", "lease_expired"}


def _precheck(call: dict, stream: Optional[tuple[int, int]]) -> None:
    """Host-side refusal before the helper (which checks again): pixel actions must match the shared stream."""
    a = call["action"]
    if a in ("click", "scroll", "move"):
        P.check_click(call["x"], call["y"], call["frame"], stream)
    elif a == "drag":
        P.check_click(call["from"]["x"], call["from"]["y"], call["frame"], stream)
        P.check_click(call["to"]["x"], call["to"]["y"], call["frame"], stream)


async def control_action(args: Any, approve: Callable[[str], Awaitable[bool]], allow: Optional[dict] = None, state: Any = None) -> dict:
    """The tool entry; the actions-only log (setting Off by default) records every finished result, refused or stopped ones included.
    `state` is the Host target-state handle (hoststate.LocalHandle / the lifetools RemoteHandle): leases, observations, holds, takeover epoch."""
    result = await _control_action(args, approve, allow, state)
    action_log.record(result, allow)
    return result


async def _file_action(call: dict, args: Any, approve: Callable[[str], Awaitable[bool]], state: Any, allow: Optional[dict] = None) -> dict:
    """Chosen Files resources only (an opaque token the person's Files picker issued, never a path). Opening goes through the Host's existing
    Files facade open-on-host authority; handing a file over through a live file dialog needs a real desktop to mean anything: refused, typed."""
    if call["action"] == "hand_over_file":
        if call.get("direction") == "upload" and "expected_revision" in call:
            return await a11y_client.chooser_action(call, args, approve, allow, state)
        return _done(call, "file_dialog_unproven", False, reason="needs_a_real_file_dialog")
    if state is None:
        return _done(call, "files_open_unavailable", False, reason="no_host_state")
    try:
        approved = bool(await asyncio.wait_for(approve(P.args_digest(call)), APPROVE_TIMEOUT_S))
    except Exception:
        approved = False
    if not approved:
        return _done(call, "approval_required", False)
    try:
        r = await state.open_resource(call["resource_ref"], call["app_id"])
    except Exception:
        r = {"ok": False, "code": "outcome_unknown"}
    if r.get("ok") is True:
        return _done(call, "performed", True)
    return _done(call, str(r.get("code") or "files_open_unavailable"), True)


async def _control_action(args: Any, approve: Callable[[str], Awaitable[bool]], allow: Optional[dict] = None, state: Any = None) -> dict:
    """begin/click/key/hotkey/type/scroll/drag/paste (pixel path) and the element actions (accessibility path).
    `approve(digest)` must consume a Host-verified one-call approval. `allow` is the owner's settings from the Host
    ({"control": bool, "accessibility": bool}); when given and the needed one is Off the refusal is typed (`setting_off`).
    Control does not depend on the capture setting."""
    try:
        call = P.parse_call(args)
    except P.ProtocolError as exc:
        return _done(None, exc.code, False)
    if call["action"] == "paste" and call.get("clipboard") != "replace":
        return _done(call, "unsupported_clipboard", False)
    if allow is not None and allow.get("control") is not True:
        return _done(call, "setting_off", False, reason="desktop_control_disabled")
    if (sys.platform == "darwin" or sys.platform.startswith("linux")) and call["action"] in P.PIXEL_ACTIONS and state is None:
        return _done(call, "unavailable", False, reason="capture_target_unbound")
    if call["action"] == "begin" and S.takeover_unconfirmed is not None:
        if state is None: return _done(call, "unavailable", False, reason="takeover_unconfirmed")
        try: current_epoch = await state.epoch()
        except Exception: return _done(call, "unavailable", False, reason="takeover_unconfirmed")
        if current_epoch <= S.takeover_unconfirmed:
            return _done(call, "unavailable", False, reason="takeover_unconfirmed")
        S.takeover_unconfirmed = None
    if call["action"] in P.FILE_ACTIONS:
        return await _file_action(call, args, approve, state, allow)
    if state is not None:                                   # fresh observation, lease scope, holds: a typed refusal before any approval
        code = await state.precheck(call, allow)
        if code:
            return _done(call, code, False, reason="observe_again" if code in _AGAIN else code)
    if call["action"] in P.ELEMENT_ACTIONS:
        return await a11y_client.element_action(call, args, approve, allow, state)
    if not _platform_ok():
        return _done(call, "unavailable", False, reason="no_wayland_session")
    begin = call["action"] == "begin"
    async with S.lock:
        if begin and S.active():
            return _done(call, "already_active", False)
        if begin and _helper_argv() is None:
            return _done(call, "unavailable", False, reason="helper_missing")
        if not begin:
            if S.proc is not None and not S.active():
                S.died = True
                await S.close(False)
            if not S.active():
                return _done(call, "unavailable", False, reason="no_session")
            try: _precheck(call, S.stream)
            except P.ProtocolError as exc: return _done(call, exc.code, False)
        epoch = S.epoch
        host_epoch = await state.epoch() if state is not None else 0
        t0 = time.monotonic()
        try:
            approved = bool(await asyncio.wait_for(approve(P.args_digest(args)), APPROVE_TIMEOUT_S))
        except Exception:
            approved = False
        timings = {"permission_wait": int((time.monotonic() - t0) * 1000)}
        if not approved:
            return _done(call, "approval_required", False, timings)
        if S.epoch != epoch or (state is not None and await state.epoch() != host_epoch):   # the human took over while the approval was open
            return _done(call, "taken_over", True, timings)
        if state is not None:                               # the world may have changed while the person decided: check again, then spend the observation
            code = await state.precheck(call, allow)
            if code:
                return _done(call, code, True, timings, reason="observe_again" if code in _AGAIN else code)
            await state.consume_observation(call)
        if begin:
            argv = _helper_argv()
            if argv is None or S.active():
                return _done(call, "unavailable", True, reason="helper_missing")
            t1 = time.monotonic()
            reservation = None
            if windows.is_windows() or sys.platform == "darwin":
                if state is None:
                    return _done(call, "unavailable", True, reason="helper_registration_required")
                try: reservation = await (state.reserve_macos_launch("control") if sys.platform == "darwin" else state.reserve_windows_launch("control"))
                except Exception: return _done(call, "unavailable", True, reason="helper_registration_refused")
            try:
                S.proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                              stderr=asyncio.subprocess.DEVNULL, limit=8192, **windows.spawn_kwargs())
            except BaseException as exc:
                if reservation is not None:
                    try: await (state.cancel_macos_launch(reservation["reservation"]) if sys.platform == "darwin" else state.cancel_windows_launch(reservation["reservation"]))
                    except Exception: pass
                if isinstance(exc, OSError):
                    return _done(call, "unavailable", True, reason="helper_start_failed")
                raise
            S.state = state
            S.host_epoch_at_begin = host_epoch
            S.takeover_notice = False
            if windows.is_windows() or sys.platform == "darwin":
                S.proof_token = reservation["reservation"]
                try: proved = await windows.bootstrap_helper(S.proc, state, reservation, "macos" if sys.platform == "darwin" else "windows")
                except BaseException:
                    await S.close(False)
                    raise
                if not proved:
                    await S.close(False)
                    return _done(call, "unavailable", True, reason="helper_registration_refused")
            elif state is not None:
                try:
                    S.registration = await state.register_helper(S.proc.pid, "control")
                    if not isinstance(S.registration, str) or not S.registration:
                        raise RuntimeError("helper registration unavailable")
                    S.exit_watch = asyncio.create_task(S.watch_exit(S.proc, state, S.registration))
                except Exception:
                    await S.close(False)
                    return _done(call, "unavailable", True, reason="helper_registration_refused")
            timings["acquisition"] = int((time.monotonic() - t1) * 1000)
            stream_handoff = None
            if sys.platform.startswith("linux") and S.registration and state is not None:
                try: stream_handoff = await state.reserve_recording_stream(S.registration)
                except Exception: pass  # recording is optional; control remains available
            reply = await S.exchange(P.build_begin(stream_handoff=stream_handoff, clipboard=call.get("clipboard")), "begin", BEGIN_TIMEOUT_S)
            if reply is None or not reply["ok"]:
                await S.close(False)
                S.last_code = "" if reply is None else reply["code"]
                return _done(call, "unavailable" if reply is None else reply["code"], True, timings)
            S.last_code, S.died = "", False
            S.stream, S.last = reply["stream"], time.monotonic()
            S.clipboard_enabled = reply.get("clipboard_enabled", False)
            S.takeover_shortcut = reply.get("takeover_shortcut")
            S.cursor_overlay_at_begin = reply.get("cursor_overlay_at_begin")
            S.control_target = reply.get("control_target")
            if S.registration and state is not None:
                try: await state.helper_capabilities(S.registration, S.takeover_shortcut, S.cursor_overlay_at_begin, S.control_target)
                except Exception: pass
            S.watch = asyncio.create_task(S._idle(S.proc))
            return _done(call, "active", True, timings, stream={"width": S.stream[0], "height": S.stream[1]}, clipboard_enabled=S.clipboard_enabled, takeover_shortcut=S.takeover_shortcut, cursor_overlay_at_begin=S.cursor_overlay_at_begin)
        if S.epoch != epoch:
            return _done(call, "taken_over", True)
        if not S.active():  # stopped or exited while the human was deciding
            await S.close(False)
            return _done(call, "unavailable", True, reason="no_session")
        if call["action"] == "paste" and not S.clipboard_enabled:
            return _done(call, "unsupported_clipboard", True, clipboard="not_issued", input="not_issued")
        t2 = time.monotonic()
        reply = await S.exchange(P.request_line(call), "paste" if call["action"] == "paste" else "action", ACTION_TIMEOUT_S)
        S.last = time.monotonic()
        timings["transport"] = int((S.last - t2) * 1000)
        timings["host"] = int((S.last - t0) * 1000)
        if call["action"] == "paste":
            if reply is None or reply.get("code") == "invalid_reply":
                await S.close(False)
                return _done(call, "outcome_unknown", True, timings, clipboard="unknown", input="unknown", message="Not repeated; clipboard and input outcomes are unknown.")
            if "clipboard" in reply:
                if not reply["ok"]: await S.close(False)
                return _done(call, "sent" if reply["ok"] else "outcome_unknown", True, timings, clipboard=reply["clipboard"], input=reply["input"], clipboard_lifetime=reply["lifetime"], message=SENT if reply["ok"] else "Not repeated; inspect the target before further action.")
            code = reply.get("code", "outcome_unknown")
            if not P.keeps_session(code): await S.close(False)
            effect = "unknown" if code == "outcome_unknown" else "not_issued"
            return _done(call, code, True, timings, clipboard=effect, input=effect)
        if reply is not None and reply["ok"]:
            return _done(call, "sent", True, timings, message=SENT)
        code = "outcome_unknown" if reply is None or reply["code"] == "invalid_reply" else reply["code"]
        if code == "taken_over" and S.proc is not None and S.state is not None and S.registration:
            await S.notice_takeover(S.proc, S.state, S.registration)
        if not P.keeps_session(code):
            await S.close(False)
        return _done(call, code, True, timings, message="Not repeated. Capture again to see what happened." if code == "outcome_unknown" else "")


def _input_readiness() -> dict:
    """Plan readiness vocabulary for the input capability, from facts that need no helper start."""
    if not _platform_ok():
        return {"state": "no_interactive_session"}
    if _helper_argv() is None:
        return {"state": "driver_missing"}
    if S.lock.locked():
        return {"state": "busy"}
    if S.last_code == "denied":
        return {"state": "permission_denied"}
    if S.died:
        return {"state": "disconnected"}
    if S.active():
        return {"state": "ready"}
    return {"state": "permission_required", "note": "The system share dialog appears at begin."}


async def probe_input() -> dict:
    """Ask the portal what it offers (version, keyboard, pointer) through the helper's `probe` op: no session, no dialog."""
    if not _platform_ok():
        return {"state": "no_interactive_session"}
    if sys.platform == "darwin":
        from src.openclank.computer_control import macos
        return {"state": "unverified" if macos.permission_status()["accessibility"] else "permission_required"}
    if windows.is_windows():     # no portal here; SendInput needs no consent dialog
        return {"state": "permission_required" if _helper_argv() else "driver_missing", "windows": windows.facts()}
    argv = _helper_argv()
    if argv is None:
        return {"state": "driver_missing"}
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, limit=8192, **windows.spawn_kwargs())
    except OSError:
        return {"state": "driver_missing"}
    try:
        proc.stdin.write(P.build_probe()); await proc.stdin.drain()
        reply = P.parse_reply(await asyncio.wait_for(proc.stdout.readline(), 8), "probe")
    except Exception:
        reply = {"ok": False, "code": "timeout"}
    finally:
        await _kill(proc)
    if reply["ok"]:
        both = reply["keyboard"] and reply["pointer"]
        return {"state": "permission_required" if both else "backend_unavailable", "portal_version": reply["version"], "keyboard": reply["keyboard"], "pointer": reply["pointer"]}
    return {"state": "no_interactive_session" if reply["code"] == "no_wayland_session" else "backend_unavailable", "code": reply["code"]}


def session_state() -> dict:
    """What a Stop button needs to know: is anything of ours running that a human could stop?"""
    local = S.active()
    host = hoststate.STATE.helper_status()
    return {"session_active": local or host["session_active"],
            "accessibility_active": a11y_client.A.active() or host["accessibility_active"],
            "busy": S.lock.locked() or a11y_client.A.lock.locked() or host.get("pending_stop_reconciliation", False),
            "takeover_shortcut": S.takeover_shortcut if local else host.get("takeover_shortcut"),
            "cursor_overlay_at_begin": S.cursor_overlay_at_begin if local else host.get("cursor_overlay_at_begin"),
            "pending_stop_reconciliation": host.get("pending_stop_reconciliation", False)}


async def take_over_if_active(owner: Optional[str] = None, session_id: Optional[str] = None) -> Optional[dict]:
    """Turn cancel: the same takeover, but only when a control or accessibility session exists (an idle cancel changes nothing)."""
    st = session_state()
    if not (st["session_active"] or st["accessibility_active"] or st["busy"]):
        return None
    return await take_over(owner, session_id)


async def take_over(owner: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """Human takeover, called by the Host (a Stop route or a turn-cancel hook): no model, no approval, no waiting for an action in flight.
    Pending approvals are revoked, the control helper is signalled (it releases held buttons/keys and closes the portal session),
    the accessibility helper is ended, and any action that was waiting for its approval refuses `taken_over` instead of running."""
    S.epoch += 1
    hoststate.STATE.take_over(owner)           # the Host-side epoch and every registered helper process group (they live in the lifetools process)
    P.REGISTRY.revoke(owner, session_id)
    proc, S.proc, S.stream = S.proc, None, None
    had_macos_input = sys.platform == "darwin" and proc is not None
    watch, S.watch = S.watch, None
    if watch is not None and watch is not asyncio.current_task():
        watch.cancel()
    if proc is not None:
        if sys.platform == "darwin" and S.state is not None and S.proof_token:
            try: await S.state.end_proved_helper(S.proof_token)
            except Exception: pass
            S.proof_token = None
        await _kill(proc)
    await a11y_client.take_over()
    result = {"outcome": "stopped", "session_active": False, "taken_over": True, "exit_code": 0}
    if had_macos_input: result["input_release"] = "unknown"
    return result


async def control_status(args: Any) -> dict:
    """readiness / probe / stop. Never needs approval: stop must be immediate."""
    act = args.get("action") if type(args) is dict and set(args) == {"action"} else None
    if act == "readiness":
        if sys.platform == "darwin":
            from src.openclank.computer_control import macos
            granted = macos.permission_status()["accessibility"]
            return {"state": "unverified", "helper_present": _helper_argv() is not None,
                    "session_active": S.active(), "exit_code": 0,
                    "capabilities": {"input": {"state": "unverified" if granted else "permission_required"},
                                     "accessibility": a11y_client.readiness(),
                                     "clipboard": {"state": "unverified" if S.active() and S.clipboard_enabled else "permission_required", "note": "Explicit replacement mode does not restore the previous clipboard; platform execution is unqualified."},
                                     "file_transfer": {"state": "backend_unavailable"},
                                     "recording": {"state": "permission_required"}},
                    "note": "macOS control requires Accessibility permission and verified helper launch; platform execution remains unqualified."}
        helper = _platform_ok() and _helper_argv() is not None
        return {"state": "unverified" if helper else "unavailable", "wayland": _platform_ok(), "helper_present": _helper_argv() is not None,
                "session_active": S.active(), "exit_code": 0,
                "capabilities": {"input": _input_readiness(), "accessibility": a11y_client.readiness(),
                                 "clipboard": {"state": "ready" if S.active() and S.clipboard_enabled else "permission_required", "note": "Explicit replacement access must be requested at begin; prior clipboard is not restored."},
                                 "file_transfer": {"state": "permission_required", "open_resource": "ready if the Host exposes Files open-on-host to the agent; hand_over_file: conditional on the supported GTK chooser profile and an explicit selected Files source; target acceptance is not proven"},
                                 "recording": {"state": "permission_required", "note": "recording requires explicit opt-in, desktop capture permission, a selected destination and the system chooser; platform execution is not established by readiness"}},
                "note": "Needs desktop_control_enabled, a human approval for every action, and the system share dialog at begin (pixel input). "
                        "Capturing the screen does not prove input or accessibility works; 'probe' asks the portal without any dialog."}
    if act == "probe":
        return {"input": await probe_input(), "exit_code": 0}
    if act == "stop":
        await S.close(graceful=not S.lock.locked())
        await a11y_client.take_over()
        return {"outcome": "stopped", "session_active": False, "exit_code": 0}
    return {"error": "action must be readiness, probe or stop", "exit_code": 1}


async def legacy_control_tool(content: str, ctx: Any) -> dict:
    """TOOL_HANDLERS entry: this path has no Host-verified approval, so it never acts."""
    return {"error": "control_desktop requires a Host-verified approval; unavailable here", "outcome": "approval_required", "exit_code": 1}


async def legacy_inspect_tool(content: str, ctx: Any) -> dict:
    """TOOL_HANDLERS entry: same rule as control, this path has no Host-verified approval, so it never reads."""
    return {"error": "inspect_desktop requires a Host-verified approval; unavailable here", "outcome": "approval_required", "exit_code": 1}


async def status_tool(content: str, ctx: Any) -> dict:
    try:
        args = json.loads(content or "{}")
    except ValueError:
        args = None
    return await control_status(args)
