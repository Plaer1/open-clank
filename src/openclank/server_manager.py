"""Cross-platform lifecycle management for the complete Open Clank service."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from core.atomic_io import atomic_write_json
from src.openclank.client_profiles import ClientProfile, ProfileError, default_config_dir


class ServerManagerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServerStatus:
    running: bool
    pid: int | None
    url: str
    ready: bool
    detail: str


class LocalServerManager:
    def __init__(self, *, repo_root: Path | None = None, state_dir: Path | None = None):
        self.repo_root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
        self.state_dir = state_dir or (default_config_dir() / "runtime")
        self.pid_path = self.state_dir / "server.json"
        self.log_path = self.state_dir / "server.log"
        self.start_lock_path = self.state_dir / "server.start.lock"

    @staticmethod
    def _address(profile: ClientProfile) -> tuple[str, int]:
        if not profile.local:
            raise ProfileError("server lifecycle commands are available only for loopback profiles")
        parts = urlsplit(profile.url)
        if parts.scheme != "http" or parts.path not in {"", "/"}:
            raise ProfileError("local auto-start requires a root HTTP loopback profile")
        return str(parts.hostname), int(parts.port or 80)

    def _read_state(self) -> dict:
        try:
            data = json.loads(self.pid_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 1:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _looks_like_server(pid: int) -> bool:
        if not LocalServerManager._alive(pid):
            return False
        if os.name == "nt":
            # Do not trust a possibly stale PID on Windows: PID reuse could
            # otherwise make `server stop` terminate an unrelated process.
            # CIM is part of Tier-1 Windows and exposes both executable and
            # command line without adding a pywin32 runtime dependency.
            command = (
                f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}';"
                "if ($null -eq $p) { exit 3 };"
                "[ordered]@{ExecutablePath=$p.ExecutablePath;CommandLine=$p.CommandLine}"
                "|ConvertTo-Json -Compress"
            )
            try:
                result = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        command,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                details = json.loads(result.stdout) if result.returncode == 0 else {}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return False
            executable = os.path.normcase(str(details.get("ExecutablePath") or ""))
            expected = os.path.normcase(str(Path(sys.executable).resolve()))
            process_command = str(details.get("CommandLine") or "")
            if executable != expected:
                return False
            if getattr(sys, "frozen", False):
                return "__managed-server" in process_command
            return "openclank_bootstrap.py" in process_command and "serve" in process_command
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        command = result.stdout
        return (
            "uvicorn" in command and "app:app" in command
        ) or (
            "openclank_bootstrap.py" in command and "serve" in command
        )

    @staticmethod
    def _probe(url: str, path: str, *, timeout: float = 1.5) -> tuple[bool, str]:
        try:
            response = httpx.get(url.rstrip("/") + path, timeout=timeout, follow_redirects=False)
        except httpx.ConnectError:
            return False, "connection refused"
        except httpx.HTTPError as exc:
            return False, str(exc)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            detail = (
                payload.get("detail") or payload.get("checks") or payload
                if isinstance(payload, dict)
                else response.text[:200]
            )
            return False, f"HTTP {response.status_code}: {detail}"
        if (
            not isinstance(payload, dict)
            or payload.get("ready") is not True
            or not isinstance(payload.get("checks"), dict)
            or not str(payload.get("version") or "")
        ):
            return False, "unexpected readiness response"
        return True, "ready"

    @staticmethod
    def _presence_probe(url: str, *, timeout: float = 1.5) -> tuple[bool, str]:
        """Distinguish a reachable authenticated app from an empty port.

        `/api/ready` intentionally sits behind the normal auth middleware, so
        an unauthenticated lifecycle client receives 401 even when the app is
        fully started.  The public auth-status shape is a narrow Open Clank
        identity probe; it does not claim readiness or expose session data.
        """

        try:
            response = httpx.get(
                url.rstrip("/") + "/api/auth/status",
                timeout=timeout,
                follow_redirects=False,
            )
        except httpx.ConnectError:
            return False, "connection refused"
        except httpx.HTTPError as exc:
            return False, str(exc)
        if response.status_code in {401, 403}:
            return True, f"authentication endpoint returned HTTP {response.status_code}"
        if response.status_code != 200:
            return False, f"authentication endpoint returned HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return False, "unexpected authentication response"
        if not isinstance(payload, dict) or not isinstance(payload.get("configured"), bool):
            return False, "unexpected authentication response"
        return True, "authentication endpoint reachable"

    def status(self, profile: ClientProfile) -> ServerStatus:
        self._address(profile)
        state = self._read_state()
        try:
            pid = int(state.get("pid"))
        except (TypeError, ValueError):
            pid = None
        ready, detail = self._probe(profile.url, "/api/ready")
        present = False
        if not ready:
            present, presence_detail = self._presence_probe(profile.url)
            if present:
                detail = f"reachable; readiness unavailable without authentication; {presence_detail}"
        recorded_alive = bool(pid and self._alive(pid))
        verified = bool(pid and self._looks_like_server(pid))
        running = ready or recorded_alive or present
        if recorded_alive and not verified and not ready:
            detail = f"recorded process identity is not verifiable; {detail}"
        return ServerStatus(running=running, pid=pid, url=profile.url, ready=ready, detail=detail)

    @contextmanager
    def _start_lock(self, deadline: float):
        token = uuid.uuid4().hex
        while True:
            try:
                descriptor = os.open(
                    self.start_lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    lock = json.loads(self.start_lock_path.read_text(encoding="utf-8"))
                    owner_pid = int(lock.get("pid"))
                    age = max(0.0, time.time() - self.start_lock_path.stat().st_mtime)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    owner_pid = 0
                    age = 0.0
                if owner_pid > 1 and self._alive(owner_pid):
                    if time.monotonic() >= deadline:
                        raise ServerManagerError("another Open Clank start is still in progress")
                    time.sleep(0.1)
                    continue
                # Do not tear away a just-created lock while its owner is still
                # writing the small identity record.
                if age < 5.0:
                    if time.monotonic() >= deadline:
                        raise ServerManagerError("Open Clank start lock could not be verified")
                    time.sleep(0.1)
                    continue
                try:
                    self.start_lock_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise ServerManagerError(f"could not clear stale start lock: {exc}") from exc
                continue
            except OSError as exc:
                raise ServerManagerError(f"could not create Open Clank start lock: {exc}") from exc
            try:
                payload = json.dumps({"pid": os.getpid(), "token": token}).encode("utf-8")
                os.write(descriptor, payload)
                os.fsync(descriptor)
            except OSError as exc:
                try:
                    self.start_lock_path.unlink()
                except OSError:
                    pass
                raise ServerManagerError(f"could not persist Open Clank start lock: {exc}") from exc
            finally:
                os.close(descriptor)
            break
        try:
            yield
        finally:
            try:
                current = json.loads(self.start_lock_path.read_text(encoding="utf-8"))
                if current.get("token") == token:
                    self.start_lock_path.unlink()
            except (OSError, ValueError, json.JSONDecodeError):
                pass

    def start(self, profile: ClientProfile, *, wait_seconds: float = 60.0) -> ServerStatus:
        host, port = self._address(profile)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(1.0, wait_seconds)
        with self._start_lock(deadline):
            remaining = max(1.0, deadline - time.monotonic())
            return self._start_locked(profile, host=host, port=port, wait_seconds=remaining)

    def _start_locked(
        self,
        profile: ClientProfile,
        *,
        host: str,
        port: int,
        wait_seconds: float,
    ) -> ServerStatus:
        existing = self.status(profile)
        if existing.ready:
            return existing
        if existing.running:
            raise ServerManagerError(f"Open Clank process {existing.pid} is running but not ready: {existing.detail}")

        instance_id = str(uuid.uuid4())
        process_env = os.environ.copy()
        process_env["APP_BIND"] = host
        process_env["APP_PORT"] = str(port)
        process_env["OPENCLANK_SERVER_INSTANCE"] = instance_id
        if getattr(sys, "frozen", False):
            # The public portable executable also contains a deliberately
            # hidden server entrypoint.  Passing a .py file to a frozen
            # executable would re-enter the public CLI instead of running it.
            command = [
                sys.executable,
                "__managed-server",
                "--host",
                host,
                "--port",
                str(port),
            ]
        else:
            command = [
                sys.executable,
                str(self.repo_root / "scripts" / "openclank_bootstrap.py"),
                "serve",
                "--host",
                host,
                "--port",
                str(port),
            ]
        flags = 0
        popen_options: dict = {}
        if os.name == "nt":
            flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
                getattr(subprocess, "DETACHED_PROCESS", 0)
            )
            popen_options["creationflags"] = flags
        else:
            popen_options["start_new_session"] = True
        try:
            log_handle = self.log_path.open("ab", buffering=0)
            process = subprocess.Popen(
                command,
                cwd=self.repo_root,
                env=process_env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **popen_options,
            )
        except OSError as exc:
            raise ServerManagerError(f"could not start Open Clank: {exc}") from exc
        finally:
            if "log_handle" in locals():
                log_handle.close()

        try:
            atomic_write_json(
                str(self.pid_path),
                {
                    "schema_version": 1,
                    "pid": process.pid,
                    "instance_id": instance_id,
                    "url": profile.url,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
            try:
                os.chmod(self.pid_path, 0o600)
            except OSError:
                pass

            deadline = time.monotonic() + max(1.0, wait_seconds)
            detail = "starting"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ServerManagerError(
                        f"Open Clank exited during startup (code {process.returncode}); see {self.log_path}"
                    )
                ready, detail = self._probe(profile.url, "/api/ready")
                if ready:
                    return ServerStatus(True, process.pid, profile.url, True, "ready")
                time.sleep(0.25)
            raise ServerManagerError(f"Open Clank did not become ready: {detail}; see {self.log_path}")
        except Exception:
            if process.poll() is None:
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            try:
                state = self._read_state()
                if int(state.get("pid") or 0) == process.pid:
                    self.pid_path.unlink()
            except (OSError, TypeError, ValueError):
                pass
            raise

    def stop(self, profile: ClientProfile, *, wait_seconds: float = 15.0) -> ServerStatus:
        self._address(profile)
        state = self._read_state()
        try:
            pid = int(state.get("pid"))
        except (TypeError, ValueError):
            pid = 0
        if not pid or not self._looks_like_server(pid):
            raise ServerManagerError("no verified Open Clank server process is recorded")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise ServerManagerError(f"could not stop Open Clank process {pid}: {exc}") from exc
        deadline = time.monotonic() + max(1.0, wait_seconds)
        while time.monotonic() < deadline and self._alive(pid):
            time.sleep(0.1)
        if self._alive(pid):
            raise ServerManagerError(f"Open Clank process {pid} did not stop cleanly")
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass
        return ServerStatus(False, None, profile.url, False, "stopped")


__all__ = ["LocalServerManager", "ServerManagerError", "ServerStatus"]
