import asyncio
import codecs
import os
import re
import sys
import time
import collections
from typing import Optional, Callable, Awaitable, Tuple, Dict
from core.platform_compat import find_bash, kill_process_tree
from src.constants import MAX_OUTPUT_CHARS
from src.shell_policy import (
    ShellApprovalError,
    ShellContainmentError,
    append_shell_audit,
    contained_argv,
    destructive_actions,
    minimal_shell_env,
    pop_sudo_secret,
    require_shell_approval,
    shell_command_argv,
    StreamingRedactor,
)
from src.openclank.history_capture import declared_root_baseline

DEFAULT_BASH_TIMEOUT = 60 * 60     # 1 hour
DEFAULT_PYTHON_TIMEOUT = 60 * 60

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12
TMUX_CAPTURE_LINES = 2000


def _declared_root_receipt(workspace: str) -> dict:
    """Return an honest pre/post baseline for configured subprocess roots."""
    configured = os.environ.get("OPENCLANK_HISTORY_DECLARED_ROOTS", "")
    roots = [workspace]
    if configured.strip():
        roots.extend(item for item in configured.split(os.pathsep) if item.strip())
    return declared_root_baseline(roots)

_ASKPASS_BODY = "#!/bin/sh\nprintf '%s\\n' \"$OPEN_CLANK_SUDO_SECRET\"\n"
_askpass_path: Optional[str] = None


def _sudo_askpass_env(secret: str) -> Dict[str, str]:
    """Environment that lets the approved command's sudo read the password once.

    The helper script is written lazily (mode 0700) and contains no secret —
    the password only travels in the worker's environment, never in the command
    line, the job spec, or the audit log.
    """
    global _askpass_path
    if _askpass_path is None:
        from src.constants import BG_JOBS_DIR

        path = os.path.join(BG_JOBS_DIR, "sudo-askpass.sh")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_ASKPASS_BODY)
        os.chmod(path, 0o700)
        _askpass_path = path
    return {"SUDO_ASKPASS": _askpass_path, "OPEN_CLANK_SUDO_SECRET": secret}


def _tmux_session_name(session_id: Optional[str]) -> str:
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or "default")).strip("-")
    return f"ody-agent-{raw[:80] or 'default'}"


async def _run_exec(*args: str, timeout: float = 10) -> Tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "", "timeout", 124
    return (
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
        proc.returncode or 0,
    )


async def _tmux_has_session(name: str) -> bool:
    _, _, rc = await _run_exec("tmux", "has-session", "-t", name, timeout=3)
    return rc == 0


async def _tmux_capture(name: str) -> str:
    out, _, _ = await _run_exec(
        "tmux", "capture-pane", "-p", "-J", "-S", f"-{TMUX_CAPTURE_LINES}", "-t", name,
        timeout=5,
    )
    return out


async def _tmux_send_line(name: str, line: str) -> None:
    if line:
        await _run_exec("tmux", "send-keys", "-t", name, "-l", line, timeout=5)
    await _run_exec("tmux", "send-keys", "-t", name, "C-m", timeout=5)


async def _ensure_tmux_session(name: str, cwd: str, env: Optional[dict]) -> None:
    if await _tmux_has_session(name):
        await _run_exec("tmux", "send-keys", "-t", name, "stty -echo", "C-m", timeout=5)
        return
    await _run_exec(
        "tmux", "new-session", "-d", "-s", name, "-c", cwd,
        "env",
        f"TERM={env.get('TERM', 'xterm-256color') if env else 'xterm-256color'}",
        f"COLUMNS={env.get('COLUMNS', '120') if env else '120'}",
        f"LINES={env.get('LINES', '40') if env else '40'}",
        "/bin/bash",
        "--noprofile",
        "--norc",
        timeout=10,
    )
    if not await _tmux_has_session(name):
        raise RuntimeError(f"failed to create tmux session {name}")
    await _run_exec("tmux", "send-keys", "-t", name, "stty -echo", "C-m", timeout=5)


def _output_after_marker(capture: str, start_marker: str, end_marker: str) -> Tuple[str, bool]:
    lines = capture.splitlines()
    start_idx = -1
    for idx, line in enumerate(lines):
        if line.strip() == start_marker:
            start_idx = idx
    if start_idx < 0:
        return capture, False
    end_idx = -1
    for idx in range(start_idx + 1, len(lines)):
        if lines[idx].strip().startswith(end_marker):
            end_idx = idx
    if end_idx < 0:
        return "\n".join(lines[start_idx + 1:]), False
    return "\n".join(lines[start_idx + 1:end_idx]), True


def _extract_marker_rc(capture: str, end_marker: str) -> int:
    for line in reversed(capture.splitlines()):
        stripped = line.strip()
        if stripped.startswith(end_marker):
            suffix = stripped[len(end_marker):].strip()
            if suffix.isdigit():
                return int(suffix)
    return 0


async def _run_tmux_bash(
    content: str,
    *,
    session_id: str,
    cwd: str,
    env: Optional[dict],
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Tuple[str, str, Optional[int], bool]:
    name = _tmux_session_name(session_id)
    await _ensure_tmux_session(name, cwd, env)

    stamp = f"{int(time.time() * 1000)}-{abs(hash(content)) % 1000000}"
    start_marker = f"__ODYSSEUS_CMD_START_{stamp}__"
    end_prefix = f"__ODYSSEUS_CMD_END_{stamp}__:"
    wrapped = (
        f"printf '\\n{start_marker}\\n'\n"
        f"{content}\n"
        f"__ody_rc=$?\n"
        f"printf '\\n{end_prefix}%s\\n' \"$__ody_rc\"\n"
    )
    for line in wrapped.splitlines():
        await _tmux_send_line(name, line)

    started = time.time()
    last_tail = ""
    while True:
        capture = await _tmux_capture(name)
        body, done = _output_after_marker(capture, start_marker, end_prefix)
        tail = "\n".join(body.splitlines()[-PROGRESS_TAIL_LINES:])
        if progress_cb and tail != last_tail:
            last_tail = tail
            try:
                await progress_cb({
                    "elapsed_s": round(time.time() - started, 1),
                    "tail": tail,
                    "tmux_session": name,
                })
            except Exception:
                pass
        if done:
            rc = _extract_marker_rc(capture, end_prefix)
            cleaned = _clean_tmux_command_output(body, wrapped)
            return cleaned, "", rc, False
        if time.time() - started > timeout:
            try:
                await _run_exec("tmux", "send-keys", "-t", name, "C-c", timeout=3)
            except Exception:
                pass
            cleaned = _clean_tmux_command_output(body, wrapped)
            return cleaned, "", 124, True
        await asyncio.sleep(0.5)


def _clean_tmux_command_output(text: str, wrapped_command: str) -> str:
    lines = text.splitlines()
    wrapped_lines = {ln.rstrip() for ln in wrapped_command.splitlines() if ln.strip()}
    cleaned = []
    for line in lines:
        raw = line.rstrip()
        stripped = raw.strip()
        if not stripped:
            cleaned.append(raw)
            continue
        if stripped in wrapped_lines:
            continue
        if stripped.startswith("__ody_rc=") or stripped.startswith("printf "):
            continue
        if re.fullmatch(r"(?:bash|sh)-[\d.]+\$ ?", stripped):
            continue
        if re.fullmatch(r"[\w.@:/~+-]+[#$] ?", stripped):
            continue
        cleaned.append(raw)
    return "\n".join(cleaned).strip()

async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Tuple[str, str, Optional[int], bool]:
    started = time.time()
    stdout_full = _BoundedCapture(MAX_OUTPUT_CHARS * 2)
    stderr_full = _BoundedCapture(MAX_OUTPUT_CHARS * 2)
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        redactor = StreamingRedactor()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def retain(value: str) -> None:
            full_buf.append_chunk(value)
            for decoded in value.rstrip("\n").rsplit(
                "\n",
                PROGRESS_TAIL_LINES,
            )[-PROGRESS_TAIL_LINES:]:
                if label == "err":
                    tail.append(f"! {decoded}")
                else:
                    tail.append(decoded)

        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            safe = redactor.feed(decoder.decode(chunk))
            if safe:
                retain(safe)
        decoded_tail = decoder.decode(b"", final=True)
        if decoded_tail:
            safe = redactor.feed(decoded_tail)
            if safe:
                retain(safe)
        final = redactor.finish()
        if final:
            retain(final)

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        kill_process_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
    except asyncio.CancelledError:
        kill_process_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        raise
    finally:
        if prog_task is not None and not prog_task.done():
            prog_task.cancel()
            try:
                await prog_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        stdout_full.text(),
        stderr_full.text(),
        proc.returncode,
        timed_out,
    )


class _BoundedCapture:
    def __init__(self, max_chars: int):
        self._max_chars = max(1024, max_chars)
        self._head = ""
        self._tail = ""
        self._seen_chars = 0

    def append(self, line: str) -> None:
        self.append_chunk(line + "\n")

    def append_chunk(self, chunk: str) -> None:
        self._seen_chars += len(chunk)
        half = self._max_chars // 2
        head_room = max(0, half - len(self._head))
        self._head += chunk[:head_room]
        self._tail = (self._tail + chunk[head_room:])[-half:]

    def text(self) -> str:
        if self._seen_chars <= self._max_chars:
            return (self._head + self._tail).rstrip("\n")
        omitted = self._seen_chars - len(self._head) - len(self._tail)
        return (
            f"{self._head.rstrip()}\n"
            f"...[{omitted} chars omitted]...\n"
            f"{self._tail.lstrip()}"
        ).rstrip()


def _bounded_text(value: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    capture = _BoundedCapture(max_chars)
    capture.append_chunk(value)
    return capture.text()


class BashTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import bg_jobs
        from src.tool_execution import agent_cwd

        network = ctx.get("shell_network")
        if isinstance(content, dict):
            network = content.get("network", network)
            content = str(content.get("command") or content.get("cmd") or content.get("code") or "")
        progress_cb = ctx.get("progress_cb")
        cwd = agent_cwd()
        workspace = os.path.realpath(str(ctx.get("workspace") or cwd))
        owner = str(ctx.get("owner") or "")
        session_id = str(ctx.get("session_id") or "")
        if not owner or not session_id:
            return {
                "error": "bash: an authenticated owner and chat session are required",
                "exit_code": 1,
            }
        shell = (
            os.getenv("OPEN_CLANK_SHELL")
            or find_bash()
            or (os.environ.get("ComSpec", "cmd.exe") if os.name == "nt" else "/bin/bash")
        )
        baseline_before = _declared_root_receipt(workspace)
        base_argv = shell_command_argv(shell, content)
        try:
            argv, containment = contained_argv(
                base_argv,
                workspace=workspace,
                cwd=cwd,
                network=network,
            )
        except ShellContainmentError as exc:
            return {"error": f"bash: {exc}", "exit_code": 1}
        network_mode = str(network or os.getenv("OPEN_CLANK_SHELL_NETWORK", "enabled")).lower()
        actions = destructive_actions(content)
        try:
            approval_binding = await require_shell_approval(
                content,
                ctx=ctx,
                cwd=cwd,
                containment=containment,
                network=network_mode,
            )
            sudo_secret = pop_sudo_secret(approval_binding)
            rec = bg_jobs.launch(
                content,
                session_id=session_id,
                owner=owner,
                workspace=workspace,
                cwd=cwd,
                max_runtime_s=DEFAULT_BASH_TIMEOUT,
                network=network_mode,
                approval_binding=approval_binding,
                extra_env=_sudo_askpass_env(sudo_secret) if sudo_secret else None,
                history_context=bg_jobs.history_context_mapping(ctx.get("history_context")),
            )
        except ShellApprovalError as exc:
            return {"error": f"bash: {exc}", "exit_code": 126}
        except (ShellContainmentError, ValueError) as exc:
            return {"error": f"bash: {exc}", "exit_code": 1}

        try:
            foreground_wait_s = max(
                0.0,
                min(float(ctx.get("shell_foreground_wait_s", 30.0)), 300.0),
            )
        except (TypeError, ValueError):
            foreground_wait_s = 30.0
        deadline = time.monotonic() + foreground_wait_s
        cursor = 0
        capture = _BoundedCapture(MAX_OUTPUT_CHARS)
        current = rec
        while True:
            current = bg_jobs.get_scoped(
                rec["id"],
                session_id=session_id,
                owner=owner,
                workspace=workspace,
            )
            if current is None:
                return {
                    "error": "bash: durable shell session left its authenticated scope",
                    "exit_code": 1,
                }
            frame = bg_jobs.tail(
                rec["id"],
                cursor=cursor,
                limit=64 * 1024,
                session_id=session_id,
                owner=owner,
                workspace=workspace,
            )
            cursor = frame["next_cursor"]
            if frame["output"]:
                capture.append_chunk(frame["output"])
                if progress_cb:
                    try:
                        await progress_cb(
                            {
                                "elapsed_s": round(
                                    time.time() - rec["started_at"],
                                    1,
                                ),
                                "tail": "\n".join(
                                    frame["output"].splitlines()[-PROGRESS_TAIL_LINES:]
                                ),
                                "shell_session": rec["id"],
                            }
                        )
                    except Exception:
                        pass
            if current.get("status") != "running" and frame["eof"]:
                break
            if current.get("status") == "running" and time.monotonic() >= deadline:
                partial = capture.text()
                return {
                    "output": (
                        partial + "\n\n" if partial else ""
                    ) + (
                        f"Command is still running in shell session `{rec['id']}`. "
                        "Use manage_bg_jobs to poll, write, wait, tail, or terminate it."
                    ),
                    "exit_code": None,
                    "state": "running",
                    "job_id": rec["id"],
                    "promoted": True,
                    "cursor": cursor,
                    "containment": containment,
                    "network": network_mode,
                    "destructive_actions": actions,
                    "coverage": {
                        "kind": "DeclaredRootsBaseline",
                        "before": baseline_before,
                        "after": None,
                        "honest_limit": "running process has no atomic after baseline yet",
                    },
                }
            await asyncio.sleep(0 if not frame["eof"] else 0.1)

        output = capture.text()
        rc = current.get("exit_code")
        if current.get("timed_out"):
            return {
                "error": f"bash: timed out after {DEFAULT_BASH_TIMEOUT}s — process tree killed",
                "exit_code": 124,
                "stdout": output,
                "containment": containment,
                "network": network_mode,
                "job_id": rec["id"],
                "coverage": {
                    "kind": "DeclaredRootsBaseline",
                    "before": baseline_before,
                    "after": _declared_root_receipt(workspace),
                    "honest_limit": "baseline cannot reconstruct writes by concurrent external writers",
                },
            }
        return {
            "output": output or "(no output)",
            "exit_code": rc or 0,
            "state": current.get("status"),
            "job_id": rec["id"],
            "containment": containment,
            "network": network_mode,
            "destructive_actions": actions,
            "coverage": {
                "kind": "DeclaredRootsBaseline",
                "before": baseline_before,
                "after": _declared_root_receipt(workspace),
                "honest_limit": "baseline cannot reconstruct writes by concurrent external writers",
            },
        }

class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd
        network = ctx.get("shell_network")
        if isinstance(content, dict):
            network = content.get("network", network)
            content = str(content.get("code") or content.get("command") or "")
        progress_cb = ctx.get("progress_cb")
        cwd = agent_cwd()
        baseline_before = _declared_root_receipt(cwd)
        try:
            argv, containment = contained_argv(
                [(sys.executable or "python"), "-I", "-c", content],
                workspace=cwd,
                cwd=cwd,
                network=network,
            )
        except ShellContainmentError as exc:
            return {"error": f"python: {exc}", "exit_code": 1}
        network_mode = str(network or os.getenv("OPEN_CLANK_SHELL_NETWORK", "enabled")).lower()
        append_shell_audit(
            command=f"python -I -c {content}",
            owner=ctx.get("owner"),
            session_id=ctx.get("session_id"),
            workspace=cwd,
            containment=containment,
            network=network_mode,
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_shell_env(ctx.get("subproc_env"), cwd=cwd),
            cwd=cwd,
            start_new_session=os.name != "nt",
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_PYTHON_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {
                "error": f"python: timed out after {DEFAULT_PYTHON_TIMEOUT}s — process tree killed",
                "exit_code": 124,
                "stdout": _bounded_text(stdout),
                "stderr": _bounded_text(stderr),
                "containment": containment,
                "network": network_mode,
                "coverage": {
                    "kind": "DeclaredRootsBaseline",
                    "before": baseline_before,
                    "after": _declared_root_receipt(cwd),
                    "honest_limit": "baseline cannot reconstruct writes by concurrent external writers",
                },
            }
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _bounded_text(output)
        return {
            "output": output or "(no output)",
            "exit_code": rc or 0,
            "containment": containment,
            "network": network_mode,
            "coverage": {
                "kind": "DeclaredRootsBaseline",
                "before": baseline_before,
                "after": _declared_root_receipt(cwd),
                "honest_limit": "baseline cannot reconstruct writes by concurrent external writers",
            },
        }
