"""Durable native Hex shell owner; actual platform qualification is external."""
from __future__ import annotations

import codecs
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import tempfile
import threading
import uuid

from core.atomic_io import atomic_write_text
from src.shell_policy import ShellApprovalError, StreamingRedactor, redact_text, shell_approval_binding, shell_command_argv
from src.project_hex import require_hex_activation, require_executable_trust, validate_project_file_candidates, _policy_file_list, policy_executable_manifest
from .hex_windows import PrivateHexProfile, prepare_runtime, runtime_sources, _same
from .windows_shell_stage import NativeShellStage
from .windows_shell_cancel import CancelEndpoint, wait_owned

CONTAINMENT = "windows-lpac-stage"


def system_shell():
    if os.name != "nt":
        raise OSError("native Windows shell requires Windows")
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.GetWindowsDirectoryW.argtypes = [W.LPWSTR, W.UINT]
    kernel.GetWindowsDirectoryW.restype = W.UINT
    buffer = C.create_unicode_buffer(32768)
    count = kernel.GetWindowsDirectoryW(buffer, len(buffer))
    if not count or count >= len(buffer):
        raise C.WinError(C.get_last_error())
    root = Path(buffer.value).resolve(strict=True)
    if os.path.normcase(str(Path(os.environ["SystemRoot"]).resolve(strict=True))) != os.path.normcase(str(root)):
        raise ShellApprovalError("SystemRoot does not identify the OS Windows directory")
    executable = root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    before = executable.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
        raise ShellApprovalError("native OS PowerShell executable is unavailable or reparsed")
    from .windows_history_io import read_file
    digest = hashlib.sha256(read_file(executable, before, _same)).hexdigest()
    return executable, root, before, digest


def native_policy_active(*, workspace, owner, db_path):
    from src.project_hex import require_project_mutation_admission
    project = require_project_mutation_admission(owner=owner, workspace=workspace, db_path=db_path)
    if not project:
        return False
    resolution = require_hex_activation(workspace, owner=owner, project_id=project["project_id"], db_path=db_path, workspace_root=workspace)
    return bool(resolution.contract_path and resolution.state == "active")


def run_native_shell(spec, command, *, capture_factory, publish, excluded, audit):
    """Only this process owns the native Job, IO, cancellation and publication."""
    from .windows_confined_process import launch_confined
    workspace = Path(spec["workspace"]).resolve(strict=True)
    cwd = Path(spec.get("cwd") or workspace).resolve(strict=True)
    relative_cwd = cwd.relative_to(workspace)
    executable, windows, shell_before, shell_digest = system_shell()
    if os.path.normcase(str(spec["shell"])) != os.path.normcase(str(executable)):
        raise ShellApprovalError("active native Hex shell supports the OS PowerShell profile")
    network = str(spec.get("network") or "enabled")
    if network not in {"enabled", "disabled"}:
        raise ShellApprovalError("invalid trusted native shell network profile")
    if spec.get("destructive_actions") and spec.get("approval_binding") != shell_approval_binding(
            command, cwd=str(cwd), workspace=str(workspace), containment=CONTAINMENT, network=network):
        raise ShellApprovalError("native shell approval no longer matches execution tuple")
    authority = dict(owner=str(spec["owner"]), project_id=str(spec["project_id"]), db_path=str(spec["hex_db_path"]))
    resolution = require_hex_activation(str(workspace), workspace_root=str(workspace), **authority)
    if not resolution.contract_path:
        raise ShellApprovalError("native Hex shell requires its activated contract")

    def verify_authority():
        current = require_hex_activation(str(workspace), workspace_root=str(workspace), **authority)
        if (current.contract_path, current.contract_hash, current.engine_version) != (
                resolution.contract_path, resolution.contract_hash, resolution.engine_version):
            raise ShellApprovalError("native shell activated authority changed")
        require_executable_trust(current, **authority)

    verify_authority()
    profile = process = endpoint = capture = stage = None
    receipt = None
    publication_started = False
    published = False
    cancelled = False
    code = 1
    stop_input = threading.Event()
    listener = None
    root = Path(tempfile.gettempdir()) / ("open-clank-native-shell-" + uuid.uuid4().hex)
    log_path = Path(spec["log_path"])
    try:
        endpoint = CancelEndpoint(spec["native_cancel_path"], job_id=str(spec["job_id"]),
                                  action_id=str(spec["action_id"]), owner=str(spec["owner"]))
        checked = validate_project_file_candidates(resolution, candidates={}, stage="check", **authority)
        if not checked.get("allowed"):
            raise ShellApprovalError("active native Hex shell precheck rejected project: " + json.dumps(checked.get("findings") or [])[:4096])
        if endpoint.requested():
            raise ShellApprovalError("native shell cancelled during preflight")
        profile = PrivateHexProfile(root)
        sources = runtime_sources(Path(__file__).resolve().parents[2])
        runtime_bytes = sum(path.stat().st_size for path, _ in sources)
        if len(sources) > 5000 or runtime_bytes > 128 * 1024 ** 2:
            raise ShellApprovalError("native managed runtime exceeds reviewed copy budget")
        if shutil.disk_usage(profile.root).free < runtime_bytes * 2 + 64 * 1024 ** 2:
            raise ShellApprovalError("insufficient space for native shell managed runtime")
        python = prepare_runtime(profile, sources)
        policy_paths = {Path(resolution.contract_path).relative_to(workspace).as_posix()}
        policy_paths.update(Path(item["path"]).as_posix()
                            for item in policy_executable_manifest(resolution)["files"])
        stage = NativeShellStage(profile, workspace,
            list_files=lambda: sorted(set(name for name in _policy_file_list(workspace)
                                          if not excluded(name)) | policy_paths), excluded=excluded)
        active_cwd = stage.root / relative_cwd
        if not active_cwd.is_dir():
            raise ShellApprovalError("native shell cwd is not represented in independent stage")
        profile.protect()
        stage.protect()
        verify_authority()
        stage.verify_source()
        profile.verify()
        if not _same(executable.lstat(), shell_before) or system_shell()[3] != shell_digest:
            raise ShellApprovalError("native OS shell runtime changed before launch")
        if endpoint.requested():
            raise ShellApprovalError("native shell cancelled before launch")
        capture = capture_factory(spec, str(workspace), command)
        if capture is not None:
            capture._state("prepared")
        environment = {"SystemRoot": str(windows), "WINDIR": str(windows),
                       "TEMP": str(profile.root / "stage"), "TMP": str(profile.root / "stage"),
                       "PATH": os.pathsep.join((str(python.parent), str(windows / "System32"), str(executable.parent))),
                       "PSModulePath": str(executable.parent / "Modules")}
        if endpoint.requested():
            raise ShellApprovalError("native shell cancelled during capture preparation")
        profile.child_started, profile.empty = True, False
        try:
            process = launch_confined(shell_command_argv(str(executable), command),
                cwd=str(active_cwd), env=environment, appcontainer_sid=profile.sid, network=network)
        except BaseException as error:
            receipt = getattr(error, "tree_receipt", None)
            profile.empty = bool(receipt is not None and receipt.empty)
            raise
        if capture is not None:
            capture._state("running", native_owner=True)
        atomic_write_text(spec["child_pid_path"], str(process.pid))
        # Signals latch the same irreversible Event; lifetime calls stay in the
        # main owner supervisor, never the signal handler or IO threads.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: endpoint.kernel.SetEvent(endpoint.handle))
        from multiprocessing.connection import Listener
        listener = Listener(spec["stdin_path"], family="AF_PIPE", authkey=None)
        atomic_write_text(spec["stdin_ready_path"], "ready")

        def input_pump():
            while not stop_input.is_set():
                try:
                    connection = listener.accept()
                    try:
                        value = connection.recv_bytes(1024 * 1024)
                    finally:
                        connection.close()
                    if value:
                        process.stdin.write(value)
                        process.stdin.flush()
                except (OSError, EOFError):
                    return

        threading.Thread(target=input_pump, daemon=True, name="native-shell-input").start()
        lock = threading.Lock()
        total = [0]
        errors = []
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            def drain(stream):
                redactor = StreamingRedactor()
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                try:
                    while chunk := stream.read(8192):
                        value = redactor.feed(decoder.decode(chunk)).encode("utf-8")
                        with lock:
                            keep = value[:max(0, 4 * 1024 * 1024 - total[0])]
                            log.write(keep)
                            log.flush()
                            total[0] += len(keep)
                    tail = (redactor.feed(decoder.decode(b"", final=True)) + redactor.finish()).encode()
                    with lock:
                        keep = tail[:max(0, 4 * 1024 * 1024 - total[0])]
                        log.write(keep)
                        total[0] += len(keep)
                except BaseException as error:
                    errors.append(error)
            threads = [threading.Thread(target=drain, args=(stream,), daemon=True)
                       for stream in (process.stdout, process.stderr)]
            for thread in threads:
                thread.start()
            receipt, cancelled = wait_owned(process, endpoint,
                timeout=max(1, int(spec.get("reconciliation_timeout_s") or 600)))
            if not receipt.empty or receipt.active_processes != 0 or receipt.direct_exit_code is None:
                raise ShellApprovalError("native shell owned Job did not become empty")
            profile.empty = True
            for thread in threads:
                thread.join(5)
            if any(thread.is_alive() for thread in threads) or errors:
                raise ShellApprovalError("native shell output drain did not complete")
            log.flush()
            os.fsync(log.fileno())
        if cancelled or endpoint.requested():
            cancelled = True
            raise ShellApprovalError("native shell cancelled before publication")
        candidates, modes = stage.candidates(receipt)

        def before_commit():
            nonlocal publication_started
            verify_authority()
            stage.verify_source()
            profile.verify()
            if endpoint.requested():
                raise ShellApprovalError("native shell cancelled before atomic publication")
            publication_started = True

        baseline = {name: tuple(int(getattr(info, key)) for key in
                    ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))
                    for name, (info, _) in stage.original.items()}
        publish(str(workspace), candidates, modes, spec, baseline, before_commit=before_commit)
        published = True
        if capture is not None:
            result = capture.finish(committed=True)
            if result.get("history_status") not in {"complete", "paused", "unavailable", "unconfigured"}:
                raise ShellApprovalError("native shell History reconciliation remains pending")
        code = receipt.direct_exit_code if receipt.direct_exit_code is not None else 1
    except BaseException as error:
        if process is not None and not profile.empty:
            try:
                receipt = process.stop_tree(timeout=10)
                profile.empty = bool(receipt.empty)
            except BaseException:
                profile.empty = False
        if capture is not None and (not publication_started or profile.empty):
            try:
                if publication_started and not published:
                    try:
                        stage.verify_source()
                    except BaseException:
                        capture._state("after_failed", error="atomic publication failed; source reconciliation unknown")
                        # This is solely the capture-owned root journal. Atomic
                        # publication/recovery journals remain owned by AtomicIO.
                        capture._journal_abandon("atomic_publication_failed")
                        capture.handle.close()
                    else:
                        capture.finish(committed=False)
                else:
                    capture.finish(committed=published)
            except Exception:
                pass
        message = "\nworker error: " + redact_text(error) + "\n"
        if profile is not None and profile.child_started and not profile.empty:
            message += "native resources retained: " + str(profile.root) + " profile=" + profile.name + "\n"
        with log_path.open("ab") as log:
            log.write(message.encode("utf-8")[:4096])
    finally:
        stop_input.set()
        if listener is not None:
            listener.close()
        if endpoint is not None:
            endpoint.close()
        if process is not None:
            process.close()
        if profile is not None and (not profile.child_started or profile.empty):
            try:
                if stage is not None:
                    stage.release_readonly_attributes()
                profile.cleanup()
            except Exception as error:
                code = 1
                with log_path.open("ab") as log:
                    log.write(("\nnative cleanup pending: " + redact_text(error) + " " + str(profile.root) + "\n").encode()[:4096])
        for name in ("child_pid_path", "stdin_ready_path"):
            Path(spec[name]).unlink(missing_ok=True)
    atomic_write_text(spec["exit_path"], str(code))
    audit(command=command, workspace=str(workspace), containment=CONTAINMENT,
          network=network, event="exit", actions=list(spec.get("destructive_actions") or ()),
          owner=spec.get("owner"), session_id=spec.get("session_id"))
    return code
