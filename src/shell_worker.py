"""Detached shell worker: contain the child and redact its bounded log."""

from __future__ import annotations

import codecs
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO, Optional

from core.atomic_io import (
    AtomicFileChange,
    atomic_write_batch,
    atomic_write_text,
    file_fingerprint,
)
from core.platform_compat import kill_process_tree
from src.shell_policy import (
    ShellApprovalError,
    append_shell_audit,
    contained_argv,
    inject_sudo_askpass,
    minimal_shell_env,
    redact_text,
    shell_approval_binding,
    shell_command_argv,
    StreamingRedactor,
)
from src.project_hex import (
    HexResolution,
    require_executable_trust,
    require_hex_activation,
    validate_project_file_candidates,
    verified_contract_snapshot,
)
from src.openclank.history_capture import context_from_mapping

_MAX_LOG_BYTES = 4 * 1024 * 1024
_MAX_COMMAND_BYTES = 1024 * 1024
_TRUNCATED = b"\n...[output capped at 4 MiB]...\n"
_MAX_POLICY_CHANGE_BYTES = 256 * 1024 * 1024
_MAX_POLICY_FILES = 100_000
_POLICY_OVERLAY_SKIP = {
    ".git",
    ".references",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "data",
}


def _workspace_state(workspace: str) -> dict[str, tuple[int, int, int, int, int]]:
    """Pin source identities before an overlay command can race another writer."""
    root = Path(workspace).resolve(strict=True)
    state: dict[str, tuple[int, int, int, int, int]] = {}
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if listed.returncode == 0:
        relative_paths = sorted(
            {
                value.decode("utf-8", errors="surrogateescape")
                for value in listed.stdout.split(b"\0")
                if value
            }
        )
    else:
        relative_paths = []
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = [name for name in names if name not in _POLICY_OVERLAY_SKIP]
            base = Path(directory)
            relative_paths.extend(
                (base / name).relative_to(root).as_posix() for name in files
            )
    if len(relative_paths) > _MAX_POLICY_FILES:
        raise ShellApprovalError(
            "active project policy file set exceeds the 100,000-file limit"
        )
    for relative in relative_paths:
        if any(part in _POLICY_OVERLAY_SKIP for part in Path(relative).parts):
            continue
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        state[Path(relative).as_posix()] = (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
        )
    return state


def _overlay_candidates(workspace: str, upper: str) -> tuple[dict[str, Optional[bytes]], dict[str, int]]:
    """Decode one bubblewrap overlay upperdir into exact file candidates."""
    root = Path(workspace).resolve(strict=True)
    upper_root = Path(upper).resolve(strict=True)
    candidates: dict[str, Optional[bytes]] = {}
    modes: dict[str, int] = {}
    total = 0
    for directory, names, files in os.walk(upper_root, followlinks=False):
        current = Path(directory)
        relative_dir = current.relative_to(upper_root)
        names[:] = [name for name in names if name not in _POLICY_OVERLAY_SKIP]
        if any(part in _POLICY_OVERLAY_SKIP for part in relative_dir.parts):
            continue
        try:
            opaque = os.getxattr(current, "user.overlay.opaque") == b"y"
        except (AttributeError, OSError):
            opaque = False
        if opaque:
            raise ShellApprovalError(
                "active project policy cannot publish a shell directory replacement atomically"
            )
        for name in tuple(names):
            entry = current / name
            info = entry.lstat()
            relative = entry.relative_to(upper_root)
            if any(part in _POLICY_OVERLAY_SKIP for part in relative.parts):
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise ShellApprovalError(
                    "active project policy supports only regular-file shell changes"
                )
        for name in files:
            entry = current / name
            relative = entry.relative_to(upper_root)
            if any(part in _POLICY_OVERLAY_SKIP for part in relative.parts):
                continue
            target = root / relative
            lexical = Path(os.path.abspath(target))
            try:
                lexical.relative_to(root)
            except ValueError as exc:
                raise ShellApprovalError("shell candidate escapes the active project") from exc
            info = entry.lstat()
            key = relative.as_posix()
            if stat.S_ISCHR(info.st_mode):
                if target.is_dir():
                    raise ShellApprovalError(
                        "active project policy cannot publish a shell directory deletion atomically"
                    )
                candidates[key] = None
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ShellApprovalError(
                    "active project policy supports only regular-file shell changes"
                )
            total += int(info.st_size)
            if total > _MAX_POLICY_CHANGE_BYTES:
                raise ShellApprovalError("shell candidate set exceeds the 256 MiB limit")
            candidates[key] = entry.read_bytes()
            modes[key] = stat.S_IMODE(info.st_mode)
    return candidates, modes


def _publish_overlay(
    workspace: str,
    upper: str,
    spec: dict,
    baseline: dict[str, tuple[int, int, int, int, int]],
) -> None:
    """Validate and atomically publish all regular-file overlay changes."""
    from src.project_hex import validate_registered_project_candidates

    candidates, modes = _overlay_candidates(workspace, upper)
    if not candidates:
        return
    result = validate_registered_project_candidates(
        owner=str(spec.get("owner") or ""),
        workspace=workspace,
        db_path=str(spec.get("hex_db_path") or ""),
        candidates=candidates,
    )
    if not result.get("allowed"):
        raise ShellApprovalError(
            "active project policy rejected shell changes: "
            + json.dumps(result.get("findings") or [], ensure_ascii=False)[:4096]
        )
    root = Path(workspace).resolve(strict=True)
    history_context = None
    raw_history = spec.get("history_context")
    if isinstance(raw_history, dict):
        history_context = context_from_mapping(raw_history)
    changes: list[AtomicFileChange] = []
    for relative, payload in candidates.items():
        target = root / relative
        lexical = Path(os.path.abspath(target))
        resolved = target.resolve(strict=False)
        if resolved != lexical:
            raise ShellApprovalError("shell candidate traverses a symlink")
        expected_state = baseline.get(relative)
        try:
            current_info = target.lstat()
        except FileNotFoundError:
            current_state = None
        else:
            current_state = (
                int(current_info.st_dev),
                int(current_info.st_ino),
                int(current_info.st_size),
                int(current_info.st_mtime_ns),
                int(current_info.st_ctime_ns),
            )
        if current_state != expected_state:
            raise ShellApprovalError(
                "project source changed while the isolated shell command was running"
            )
        before = file_fingerprint(str(target))
        if payload is None and before is None:
            continue
        changes.append(
            AtomicFileChange(
                str(target),
                payload,
                expected_fingerprint=before,
                require_missing=payload is not None and before is None,
                mode=modes.get(relative),
                history_context=history_context,
            )
        )
    if changes:
        atomic_write_batch(changes)


def _run_hex_check(
    resolution: HexResolution,
    *,
    snapshot: BinaryIO,
    spec: dict,
    workspace: str,
    cwd: str,
) -> None:
    result = validate_project_file_candidates(
        resolution,
        owner=str(spec.get("owner") or ""),
        project_id=str(spec.get("project_id") or ""),
        db_path=str(spec.get("hex_db_path") or ""),
        candidates={},
        stage="check",
    )
    if not result.get("allowed"):
        raise ShellApprovalError(
            "active Open Clank Hexes contract rejected the project worker: "
            + redact_text(json.dumps(result.get("findings") or [], ensure_ascii=False))[:4096]
        )


def _load_spec(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def run(spec_path: str) -> int:
    spec = _load_spec(Path(spec_path))
    command_size = int(spec["command_size"])
    if command_size < 0 or command_size > _MAX_COMMAND_BYTES:
        raise ValueError("invalid background shell command size")
    raw_command = sys.stdin.buffer.read(command_size + 1)
    if len(raw_command) != command_size:
        raise ValueError("background shell command transport was incomplete")
    command = raw_command.decode("utf-8")
    workspace = os.path.realpath(str(spec["workspace"]))
    cwd = os.path.realpath(str(spec.get("cwd") or workspace))
    log_path = Path(spec["log_path"])
    exit_path = Path(spec["exit_path"])
    child_pid_path = Path(spec["child_pid_path"])
    stdin_path = str(spec["stdin_path"])
    stdin_ready_path = Path(
        spec.get("stdin_ready_path") or f"{spec['stdin_path']}.ready"
    )
    shell = str(spec["shell"])
    network = str(spec.get("network") or "enabled")

    pipe_listener = None
    if os.name != "nt":
        fifo_path = Path(stdin_path)
        fifo_path.parent.mkdir(parents=True, exist_ok=True)
        if not fifo_path.exists():
            os.mkfifo(fifo_path, 0o600)
        stdin_handle = open(fifo_path, "r+b", buffering=0)
    else:
        from multiprocessing.connection import Listener

        pipe_listener = Listener(
            stdin_path,
            family="AF_PIPE",
            authkey=None,
        )
        stdin_handle = subprocess.PIPE
    atomic_write_text(str(stdin_ready_path), "ready")
    # The approval binding below is computed on `command` exactly as the owner
    # approved it; the askpass rewrite only shapes the argv we execute.
    exec_command = inject_sudo_askpass(command) if spec.get("sudo_askpass") else command
    base_argv = shell_command_argv(shell, exec_command)
    snapshot_context = None
    snapshot = None
    overlay_context = None
    overlay_upper = None
    overlay_work = None
    workspace_baseline = None
    if spec.get("hex_check"):
        resolution = require_hex_activation(
            spec.get("hex_target") or workspace,
            owner=str(spec.get("owner") or ""),
            project_id=str(spec.get("project_id") or ""),
            db_path=str(spec.get("hex_db_path") or ""),
            workspace_root=workspace,
        )
        # A workspace without a declared project contract remains a valid
        # ordinary shell workspace. If an Open Clank `.hex` contract exists, the
        # active hash is mandatory and the exact contract is checked inside a
        # network-disabled worker before the user command starts. The same
        # verified bytes stay mounted for the user command's sandbox.
        if resolution.contract_path:
            require_executable_trust(
                resolution,
                owner=str(spec.get("owner") or ""),
                project_id=str(spec.get("project_id") or ""),
                db_path=str(spec.get("hex_db_path") or ""),
            )
            snapshot_context = verified_contract_snapshot(resolution)
            snapshot = snapshot_context.__enter__()
            overlay_context = tempfile.TemporaryDirectory(
                prefix="open-clank-shell-policy-"
            )
            overlay_root = Path(overlay_context.name)
            overlay_upper = overlay_root / "upper"
            overlay_work = overlay_root / "work"
            overlay_upper.mkdir()
            overlay_work.mkdir()
            try:
                argv, containment = contained_argv(
                    base_argv,
                    workspace=workspace,
                    cwd=cwd,
                    network=network,
                    owner=spec.get("owner"),
                    project_id=spec.get("project_id"),
                    hex_target=spec.get("hex_target") or workspace,
                    hex_db_path=spec.get("hex_db_path"),
                    workspace_overlay=(str(overlay_upper), str(overlay_work)),
                    readonly_data_mounts=((snapshot.fileno(), resolution.contract_path),),
                )
                _run_hex_check(
                    resolution,
                    snapshot=snapshot,
                    spec=spec,
                    workspace=workspace,
                    cwd=cwd,
                )
                snapshot.seek(0)
                workspace_baseline = _workspace_state(workspace)
            except BaseException:
                snapshot_context.__exit__(*sys.exc_info())
                snapshot_context = None
                snapshot = None
                raise
    if snapshot is None:
        argv, containment = contained_argv(
            base_argv,
            workspace=workspace,
            cwd=cwd,
            network=network,
            owner=spec.get("owner"),
            project_id=spec.get("project_id"),
            hex_target=spec.get("hex_target") or workspace,
            hex_db_path=spec.get("hex_db_path"),
        )
    actions = list(spec.get("destructive_actions") or ())
    approval_mismatch = bool(actions) and spec.get(
        "approval_binding"
    ) != shell_approval_binding(
        command,
        cwd=cwd,
        containment=containment,
        network=network,
        workspace=workspace,
    )
    child: Optional[subprocess.Popen] = None
    input_thread: Optional[threading.Thread] = None
    input_stop = threading.Event()

    def stop_child(_signum=None, _frame=None):
        if child is not None:
            kill_process_tree(child.pid)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop_child)

    try:
        if approval_mismatch:
            raise ShellApprovalError(
                "destructive background approval no longer matches the execution tuple"
            )
        try:
            child_env = minimal_shell_env(cwd=workspace)
            # Askpass delivery (sudo escalation): the launcher puts these in the
            # worker's own environment; forward them to the child shell. The
            # StreamingRedactor masks the secret's exact value in the job log.
            for name in ("SUDO_ASKPASS", "OPEN_CLANK_SUDO_SECRET"):
                if os.environ.get(name):
                    child_env[name] = os.environ[name]
            child = subprocess.Popen(
                argv,
                stdin=stdin_handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=child_env,
                start_new_session=True if os.name != "nt" else False,
                pass_fds=(snapshot.fileno(),) if snapshot is not None else (),
            )
        finally:
            if snapshot_context is not None:
                snapshot_context.__exit__(*sys.exc_info())
                snapshot_context = None
                snapshot = None
        if pipe_listener is not None:
            def pump_windows_input():
                while not input_stop.is_set() and child is not None and child.poll() is None:
                    try:
                        connection = pipe_listener.accept()
                        try:
                            payload = connection.recv_bytes()
                        finally:
                            connection.close()
                        if payload and child.stdin is not None:
                            child.stdin.write(payload)
                            child.stdin.flush()
                    except (BrokenPipeError, EOFError, OSError):
                        if child is None or child.poll() is not None:
                            return

            input_thread = threading.Thread(
                target=pump_windows_input,
                name="open-clank-shell-stdin",
                daemon=True,
            )
            input_thread.start()
        atomic_write_text(str(child_pid_path), str(child.pid))
        written = 0
        capped = False
        redactor = StreamingRedactor()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "wb") as log:
            assert child.stdout is not None
            while True:
                chunk = child.stdout.read(8192)
                if not chunk:
                    break
                safe = redactor.feed(
                    decoder.decode(chunk)
                ).encode("utf-8")
                remaining = max(0, _MAX_LOG_BYTES - written)
                keep = safe[:remaining]
                if keep:
                    log.write(keep)
                    log.flush()
                    written += len(keep)
                if len(keep) < len(safe):
                    capped = True
            decoded_tail = decoder.decode(b"", final=True)
            final = (
                redactor.feed(decoded_tail) + redactor.finish()
            ).encode("utf-8")
            remaining = max(0, _MAX_LOG_BYTES - written)
            keep = final[:remaining]
            if keep:
                log.write(keep)
                written += len(keep)
            if len(keep) < len(final):
                capped = True
            if capped and written + len(_TRUNCATED) <= _MAX_LOG_BYTES + len(_TRUNCATED):
                log.write(_TRUNCATED)
            log.flush()
            os.fsync(log.fileno())
        code = child.wait()
        if overlay_upper is not None:
            _publish_overlay(
                workspace,
                str(overlay_upper),
                spec,
                workspace_baseline or {},
            )
    except BaseException as exc:
        stop_child()
        code = 1
        error = (
            "\nworker error: " + redact_text(exc) + "\n"
        ).encode("utf-8")[:4096]
        with open(log_path, "a+b") as log:
            log.seek(0, os.SEEK_END)
            if log.tell() + len(error) > _MAX_LOG_BYTES:
                log.seek(max(0, _MAX_LOG_BYTES - len(error)))
                log.truncate()
            log.write(error)
    finally:
        if snapshot_context is not None:
            snapshot_context.__exit__(None, None, None)
        if overlay_context is not None:
            try:
                os.chmod(Path(overlay_work) / "work", 0o700)
            except OSError:
                pass
            overlay_context.cleanup()
        input_stop.set()
        if pipe_listener is not None:
            pipe_listener.close()
        if child is not None and child.stdin is not None:
            child.stdin.close()
        if (
            hasattr(stdin_handle, "close")
            and (child is None or stdin_handle is not child.stdin)
        ):
            stdin_handle.close()
        try:
            child_pid_path.unlink()
        except FileNotFoundError:
            pass
        try:
            stdin_ready_path.unlink()
        except FileNotFoundError:
            pass
    atomic_write_text(str(exit_path), str(code))
    append_shell_audit(
        command=command,
        owner=spec.get("owner"),
        session_id=spec.get("session_id"),
        workspace=workspace,
        containment=containment,
        network=network,
        actions=actions,
        event="exit",
    )
    return code


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
