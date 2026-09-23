"""Authenticated client for the local Open Clank history service.

Mutation owners pass one action envelope through this client. It is intentionally a small IPC
adapter: provider code remains responsible for capturing the authoritative preimage and live
result at its mutation boundary.
"""

from __future__ import annotations

import asyncio
import json
import base64
import binascii
import hashlib
import os
import secrets
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class HistoryClientError(RuntimeError):
    """The history worker rejected or failed an IPC request."""


@dataclass(frozen=True)
class ScopedHistoryCredential:
    """A supervisor-issued account binding; the token is never serialized to logs."""

    actor_id: str
    account_id: str
    capabilities: frozenset[str] = frozenset({"capture", "read"})
    owner_accounts: frozenset[str] = frozenset()
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    def wire(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "account_id": self.account_id,
            "token": self.token,
            "capabilities": sorted(self.capabilities),
            "owner_accounts": sorted(self.owner_accounts),
        }


class HistoryServiceSupervisor:
    """Own one per-install history worker and distribute immutable scoped clients."""

    def __init__(
        self,
        binary: str | Path,
        *,
        socket_path: str | Path,
        catalog_path: str | Path,
        lore_root: str | Path,
        credential_file: str | Path,
        credentials: list[ScopedHistoryCredential],
        host_root: str | Path | None = None,
        receipt_root: str | Path | None = None,
        resource_map: str | Path | None = None,
        authorized_roots: list[Mapping[str, Any]] | None = None,
        startup_timeout: float = 10.0,
    ) -> None:
        self.binary = str(binary)
        self.socket_path = Path(socket_path)
        self.catalog_path = Path(catalog_path)
        self.lore_root = Path(lore_root)
        self.credential_file = Path(credential_file)
        self.credentials = tuple(credentials)
        self.host_root = Path(host_root) if host_root is not None else None
        self.receipt_root = Path(receipt_root) if receipt_root is not None else None
        self.resource_map = Path(resource_map) if resource_map is not None else None
        self.authorized_roots = tuple(dict(item) for item in (authorized_roots or ()))
        self.authorized_roots_file = self.credential_file.with_name(
            f"{self.credential_file.stem}.roots.json"
        )
        self.authority_file = self.credential_file.with_name(
            f"{self.credential_file.stem}.authority.json"
        )
        self._authority_generation = 0
        self._rotation_lock = asyncio.Lock()
        self.startup_timeout = startup_timeout
        self.process: Any = None

    def client_for(self, actor_id: str, account_id: str) -> HistoryClient:
        for credential in self.credentials:
            if credential.account_id == account_id and credential.actor_id in {actor_id, "*"}:
                return HistoryClient(
                    str(self.socket_path),
                    actor_id=actor_id,
                    account_id=credential.account_id,
                    token=credential.token,
                )
        raise HistoryClientError("no supervisor-issued history credential for this owner")

    def credential_token(self, actor_id: str, account_id: str) -> str:
        for credential in self.credentials:
            if credential.account_id == account_id and credential.actor_id in {actor_id, "*"}:
                return credential.token
        raise HistoryClientError("no supervisor-issued history credential for this owner")

    async def _cleanup_failed_start(self) -> None:
        """Reap a worker that failed health negotiation before publication."""
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 2)
            except __import__("subprocess").TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 2)
        set_history_credential_provider(None)
        self.socket_path.unlink(missing_ok=True)
        self.socket_path.with_name(self.socket_path.name + ".lock").unlink(missing_ok=True)

    async def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if self.socket_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                if probe.connect_ex(str(self.socket_path)) == 0:
                    raise HistoryClientError("history worker socket is already in use")
            finally:
                probe.close()
            self.socket_path.unlink(missing_ok=True)
            self.socket_path.with_name(self.socket_path.name + ".lock").unlink(missing_ok=True)
        self._write_credentials()
        self._write_authorized_roots()
        self._write_authority()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        args = [
            self.binary,
            str(self.socket_path),
            str(self.catalog_path),
            str(self.lore_root),
            "",  # legacy account slot; credentials file owns account bindings
        ]
        if self.host_root is not None:
            args.append(str(self.host_root))
            if self.receipt_root is not None:
                args.append(str(self.receipt_root))
            if self.resource_map is not None:
                if self.receipt_root is None:
                    args.append(str(self.host_root / ".openclank-history-receipts"))
                args.append(str(self.resource_map))
        env = dict(os.environ)
        env.pop("OPENCLANK_HISTORY_TOKEN", None)
        env.pop("OPENCLANK_HISTORY_OWNER_GRANTS", None)
        env["OPENCLANK_HISTORY_CREDENTIALS_FILE"] = str(self.credential_file)
        env["OPENCLANK_HISTORY_AUTHORIZED_ROOTS_FILE"] = str(self.authorized_roots_file)
        env["OPENCLANK_HISTORY_AUTHORITY_FILE"] = str(self.authority_file)
        if self.authorized_roots:
            env["OPENCLANK_HISTORY_AUTHORIZED_ROOTS"] = json.dumps(
                list(self.authorized_roots), separators=(",", ":"), sort_keys=True
            )
        else:
            env.pop("OPENCLANK_HISTORY_AUTHORIZED_ROOTS", None)
        self.process = await asyncio.to_thread(
            __import__("subprocess").Popen,
            args,
            env=env,
            stdin=__import__("subprocess").DEVNULL,
            stdout=__import__("subprocess").DEVNULL,
            # Keep startup diagnostics available when a packaged binary is
            # stale or built for the wrong protocol.  The service is quiet in
            # steady state, so this bounded pipe cannot become a log sink.
            stderr=__import__("subprocess").PIPE,
        )
        set_history_credential_provider(self.credential_token)
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = ""
                if self.process.stderr is not None:
                    detail = (self.process.stderr.read() or b"").decode("utf-8", "replace").strip()
                suffix = f": {detail[-800:]}" if detail else ""
                await self._cleanup_failed_start()
                raise HistoryClientError(f"history worker exited during startup{suffix}")
            if self.socket_path.exists():
                # Existence alone can be a stale endpoint or a worker that
                # has not entered its accept loop.  Negotiate the protocol
                # before publishing the supervisor to the application.
                binding = self.credentials[0]
                probe_actor = binding.actor_id if binding.actor_id != "*" else "supervisor"
                try:
                    health = HistoryClient(
                        str(self.socket_path),
                        actor_id=probe_actor,
                        account_id=binding.account_id,
                        token=binding.token,
                        timeout=min(1.0, self.startup_timeout),
                    )._call(
                        {
                            "Health": {
                                "protocol_version": 1,
                                "auth": {
                                    "actor_id": probe_actor,
                                    "account_id": binding.account_id,
                                    "token": binding.token,
                                },
                                "action_id": "supervisor-health",
                            }
                        }
                    )
                except Exception:
                    await asyncio.sleep(0.02)
                    continue
                health_body = health.get("Health")
                if not isinstance(health_body, dict) or health_body.get("protocol_version") != 1:
                    await self._cleanup_failed_start()
                    raise HistoryClientError("history worker protocol negotiation failed")
                return
            await asyncio.sleep(0.02)
        await self._cleanup_failed_start()
        raise HistoryClientError("history worker did not create its private socket")

    async def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            set_history_credential_provider(None)
            self.socket_path.unlink(missing_ok=True)
            self.socket_path.with_name(self.socket_path.name + ".lock").unlink(missing_ok=True)
            return
        admin = next((item for item in self.credentials if "admin" in item.capabilities), None)
        if admin is not None:
            try:
                await asyncio.to_thread(
                    HistoryClient(
                        str(self.socket_path),
                        actor_id=admin.actor_id,
                        account_id=admin.account_id,
                        token=admin.token,
                    )._call,
                    {"Shutdown": {"envelope": {"protocol_version": 1, "auth": {"actor_id": admin.actor_id, "account_id": admin.account_id, "token": admin.token}, "action_id": "supervisor-shutdown"}}},
                )
            except Exception:
                process.terminate()
        else:
            process.terminate()
        try:
            await asyncio.to_thread(process.wait, 10)
        except __import__("subprocess").TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 2)
        self.process = None
        set_history_credential_provider(None)
        self.socket_path.unlink(missing_ok=True)
        self.socket_path.with_name(self.socket_path.name + ".lock").unlink(missing_ok=True)

    async def _rotate_scope(
        self,
        credentials: tuple[ScopedHistoryCredential, ...],
        authorized_roots: tuple[Mapping[str, Any], ...],
    ) -> None:
        """Publish credentials and roots while keeping a live worker available."""
        async with self._rotation_lock:
            self._publish_scope(credentials, authorized_roots)
            if self.process is None or self.process.poll() is not None:
                await self.start()

    def _publish_scope(
        self,
        credentials: tuple[ScopedHistoryCredential, ...],
        authorized_roots: tuple[Mapping[str, Any], ...],
    ) -> None:
        self.credentials = tuple(credentials)
        self.authorized_roots = tuple(dict(item) for item in authorized_roots)
        self._authority_generation += 1
        self._write_authority()

    async def rotate_credentials(self, credentials: list[ScopedHistoryCredential]) -> None:
        """Apply account create/revoke/rotation in the live authority snapshot."""
        await self._rotate_scope(tuple(credentials), self.authorized_roots)

    async def sync_authorized_roots(self, authorized_roots: list[Mapping[str, Any]]) -> None:
        """Refresh provider root authority when the Files registry generation changes."""
        async with self._rotation_lock:
            projected = tuple(dict(item) for item in authorized_roots)
            if projected != self.authorized_roots:
                self._publish_scope(self.credentials, projected)
                if self.process is None or self.process.poll() is not None:
                    await self.start()

    async def sync_accounts(
        self,
        users: Mapping[str, Any],
        authorized_roots: list[Mapping[str, Any]] | None = None,
    ) -> None:
        async with self._rotation_lock:
            await self._sync_accounts_locked(users, authorized_roots)

    async def _sync_accounts_locked(
        self,
        users: Mapping[str, Any],
        authorized_roots: list[Mapping[str, Any]] | None = None,
    ) -> None:
        """Rotate the writer binding after account create/delete/rename.

        Account IDs come from the authenticated record, while usernames remain
        display keys. Existing account tokens survive a rename; deleted
        accounts are removed from the live worker authority snapshot.
        """
        previous = {item.account_id: item for item in self.credentials}
        refreshed: list[ScopedHistoryCredential] = []
        for _username, record in users.items():
            if not isinstance(record, Mapping):
                continue
            account_id = str(record.get("account_id") or "").strip()
            if not account_id:
                continue
            # Restore and registry publication are owner-bound history
            # capabilities too.  Keep them through account lifecycle rotation;
            # silently dropping restore here would make a renamed or recreated
            # account unable to recover its own files after the worker restarts.
            capabilities = {
                "capture",
                "read",
                "restore",
                "settings-read",
                "settings-write",
            }
            if record.get("is_admin") is True:
                capabilities.add("admin")
            old = previous.get(account_id)
            refreshed.append(
                ScopedHistoryCredential(
                    actor_id="*",
                    account_id=account_id,
                    capabilities=frozenset(capabilities),
                    token=old.token if old is not None else secrets.token_urlsafe(32),
                )
            )
        if not refreshed:
            old = previous.get("local-installation")
            refreshed.append(
                ScopedHistoryCredential(
                    actor_id="*",
                    account_id="local-installation",
                    capabilities=frozenset(
                        {
                            "admin",
                            "capture",
                            "read",
                            "restore",
                            "settings-read",
                            "settings-write",
                        }
                    ),
                    token=old.token if old is not None else secrets.token_urlsafe(32),
                )
            )
        projected_roots = (
            tuple(dict(item) for item in authorized_roots)
            if authorized_roots is not None
            else self.authorized_roots
        )
        changed = (
            [item.wire() for item in refreshed] != [item.wire() for item in self.credentials]
            or projected_roots != self.authorized_roots
        )
        if changed:
            self._publish_scope(tuple(refreshed), projected_roots)
            if self.process is None or self.process.poll() is not None:
                await self.start()

    def _write_credentials(self) -> None:
        self.credential_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.credential_file.with_name(f".{self.credential_file.name}.tmp")
        payload = json.dumps([item.wire() for item in self.credentials], separators=(",", ":"))
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.credential_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _write_authorized_roots(self) -> None:
        """Atomically publish the current Files root snapshot for a live worker."""
        self.authorized_roots_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.authorized_roots_file.with_name(
            f".{self.authorized_roots_file.name}.tmp"
        )
        payload = json.dumps(
            list(self.authorized_roots), separators=(",", ":"), sort_keys=True
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.authorized_roots_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _write_authority(self) -> None:
        """Atomically publish credentials and roots as one generation."""
        self.authority_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.authority_file.with_name(f".{self.authority_file.name}.tmp")
        payload = json.dumps(
            {
                "generation": self._authority_generation,
                "credentials": [item.wire() for item in self.credentials],
                "authorized_roots": list(self.authorized_roots),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.authority_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


MAX_FRAME_BYTES = 1024 * 1024
# Keep inline control frames comfortably below the IPC ceiling. Larger
# payloads use the service-owned chunk staging protocol; this is a wire-shape
# threshold, not a file eligibility limit.
_INLINE_CONTENT_BYTES = 640 * 1024
_STAGE_CHUNK_BYTES = 512 * 1024
_CREDENTIAL_PROVIDER: Any = None


def set_history_credential_provider(provider: Any) -> None:
    """Install the supervisor's immutable per-account credential lookup."""
    global _CREDENTIAL_PROVIDER
    _CREDENTIAL_PROVIDER = provider


class HistoryClient:
    def __init__(self, socket_path: str, *, actor_id: str, account_id: str, token: str | None = None, timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.actor_id = actor_id
        self.account_id = account_id
        if token is None and callable(_CREDENTIAL_PROVIDER):
            token = _CREDENTIAL_PROVIDER(actor_id, account_id)
        self.token = token if token is not None else os.environ.get("OPENCLANK_HISTORY_TOKEN", "")
        self.timeout = timeout

    def _auth(self) -> dict[str, str]:
        return {"actor_id": self.actor_id, "account_id": self.account_id, "token": self.token}

    def _call(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
        if len(raw) > MAX_FRAME_BYTES:
            raise HistoryClientError("history frame exceeds the 1 MiB service limit; use a provider chunk transport")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(self.timeout)
            channel.connect(self.socket_path)
            channel.sendall(raw)
            response = bytearray()
            while not response.endswith(b"\n"):
                chunk = channel.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_FRAME_BYTES:
                    raise HistoryClientError("history response exceeds the 1 MiB service limit; use bounded readback")
        if not response:
            raise HistoryClientError("history worker returned no response")
        decoded = json.loads(response)
        # Unit and production callers use a mapping-shaped response. Serde's
        # externally tagged Rust enum emits unit variants such as `Accepted`
        # as a JSON string, so normalize that wire form at this boundary.
        if decoded == "Accepted":
            return {"Accepted": None}
        if not isinstance(decoded, dict):
            raise HistoryClientError("history worker returned an invalid response")
        if "Error" in decoded or decoded.get("error"):
            raise HistoryClientError(str(decoded.get("Error", decoded.get("error"))))
        return decoded

    def prepare(self, envelope: Mapping[str, Any], *, content: bytes | None, fingerprint: str) -> dict[str, Any]:
        self._check_content(content)
        if content is not None and len(content) > _INLINE_CONTENT_BYTES:
            fingerprint = self._canonical_staged_fingerprint(content, fingerprint)
            action_id = str(envelope.get("action_id") or "")
            upload_id = self._stage_content(action_id, content, fingerprint)
            try:
                return self._prepare_staged(envelope, upload_id, fingerprint)
            except Exception:
                self._abort_staged(action_id, upload_id)
                raise
        return self._call({"Prepare": {"envelope": self._request_envelope(envelope), "content": self._wire_content(content), "fingerprint": fingerprint}})

    def prepare_batch(
        self,
        envelope: Mapping[str, Any],
        entries: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Durably prepare every exact resource preimage in one parent action.

        Batch entries use the same bounded inline encoding as ``Prepare``. A
        provider with a larger preimage must use its staged/chunk owner path;
        silently dropping secondary bytes would violate the batch contract.
        """
        wire_entries: list[dict[str, Any]] = []
        staged: list[tuple[str, str]] = []
        action_id = str(envelope.get("action_id") or "")
        try:
            for entry in entries:
                item = dict(entry)
                content = item.get("content")
                item["staged_upload_id"] = None
                if content is not None:
                    if not isinstance(content, (bytes, bytearray)):
                        raise HistoryClientError("batch preimage content must be bytes")
                    content = bytes(content)
                    self._check_content(content)
                    fingerprint = str(item.get("fingerprint") or "missing")
                    if len(content) > _INLINE_CONTENT_BYTES:
                        fingerprint = self._canonical_staged_fingerprint(content, fingerprint)
                        upload_id = self._stage_content(action_id, content, fingerprint)
                        staged.append((upload_id, action_id))
                        item["staged_upload_id"] = upload_id
                        item["fingerprint"] = fingerprint
                        item["content"] = None
                    else:
                        item["content"] = self._wire_content(content)
                wire_entries.append(item)
            response = self._call(
                {
                    "PrepareBatch": {
                        "envelope": self._request_envelope(envelope),
                        "batch_version": 1,
                        "entries": wire_entries,
                    }
                }
            )
            return response
        except Exception:
            for upload_id, owner_action in staged:
                self._abort_staged(owner_action, upload_id)
            raise

    def record_live(self, action_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        return self._call({"RecordLive": {"envelope": self._control_envelope(action_id), "receipt": dict(receipt)}})

    def rebind_resource(self, action_id: str, resource_id: str) -> dict[str, Any]:
        """Bind a create action to the provider id allocated by its commit."""
        return self._call(
            {
                "RebindResource": {
                    "envelope": self._control_envelope(action_id),
                    "resource_id": resource_id,
                }
            }
        )

    def complete(self, action_id: str, *, content: bytes | None, fingerprint: str) -> dict[str, Any]:
        self._check_content(content)
        if content is not None and len(content) > _INLINE_CONTENT_BYTES:
            fingerprint = self._canonical_staged_fingerprint(content, fingerprint)
            upload_id = self._stage_content(action_id, content, fingerprint)
            try:
                return self._complete_staged(action_id, upload_id, fingerprint)
            except Exception:
                self._abort_staged(action_id, upload_id)
                raise
        return self._call({"Complete": {"envelope": self._control_envelope(action_id), "content": self._wire_content(content), "fingerprint": fingerprint}})

    def abort(self, action_id: str) -> dict[str, Any]:
        return self._call({"Abort": {"envelope": self._control_envelope(action_id)}})

    def read_version(self, action_id: str, receipt: Mapping[str, Any]) -> bytes | None:
        """Read the exact authenticated version payload from the history worker."""
        envelope = self._control_envelope(action_id)
        info_response = self._call(
            {
                "ReadVersionInfo": {
                    "envelope": envelope,
                    "receipt": dict(receipt),
                }
            }
        )
        info = info_response.get("VersionInfo")
        if not isinstance(info, Mapping) or "content_length" not in info:
            raise HistoryClientError("history worker returned invalid version metadata")
        content_length = info.get("content_length")
        if content_length is None:
            return None
        if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length < 0:
            raise HistoryClientError("history worker returned invalid version length")
        content = bytearray()
        offset = 0
        while offset < content_length or (content_length == 0 and offset == 0):
            requested = min(_STAGE_CHUNK_BYTES, content_length - offset)
            response = self._call(
                {
                    "ReadVersionChunk": {
                        "envelope": envelope,
                        "receipt": dict(receipt),
                        "offset": offset,
                        "length": requested,
                    }
                }
            )
            chunk = response.get("Chunk")
            if not isinstance(chunk, Mapping):
                raise HistoryClientError("history worker returned invalid version chunk")
            returned_offset = chunk.get("offset")
            if returned_offset != offset:
                raise HistoryClientError("history worker returned an out-of-order version chunk")
            raw = chunk.get("content")
            if raw is None:
                data = b""
            else:
                data = self._decode_bytes(raw)
            if len(data) > requested or offset + len(data) > content_length:
                raise HistoryClientError("history worker returned an oversized version chunk")
            content.extend(data)
            offset += len(data)
            eof = chunk.get("eof") is True
            if content_length == 0:
                if data or not eof:
                    raise HistoryClientError("history worker returned an invalid empty version chunk")
                break
            if eof:
                if offset != content_length:
                    raise HistoryClientError("history worker ended version read before its declared length")
                break
            if not data:
                raise HistoryClientError("history worker returned an empty non-terminal version chunk")
        if len(content) != content_length:
            raise HistoryClientError("history worker returned an incomplete version payload")
        return bytes(content)

    @staticmethod
    def _decode_bytes(value: Any) -> bytes:
        if isinstance(value, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
            return bytes(value)
        if isinstance(value, str):
            try:
                return base64.b64decode(value, validate=True)
            except (ValueError, binascii.Error) as error:
                raise HistoryClientError("history worker returned invalid version bytes") from error
        raise HistoryClientError("history worker returned invalid version payload")

    def _stage_content(self, action_id: str, content: bytes, fingerprint: str) -> str:
        if not action_id:
            raise HistoryClientError("history staged capture requires an action id")
        upload_id = f"stage-{secrets.token_hex(24)}"
        try:
            self._call(
                {
                    "StageBegin": {
                        "envelope": self._control_envelope(action_id),
                        "upload_id": upload_id,
                        "content_length": len(content),
                        "fingerprint": fingerprint,
                    }
                }
            )
            for offset in range(0, len(content), _STAGE_CHUNK_BYTES):
                chunk = content[offset : offset + _STAGE_CHUNK_BYTES]
                self._call(
                    {
                        "StageChunk": {
                            "envelope": self._control_envelope(action_id),
                            "upload_id": upload_id,
                            "offset": offset,
                            "content": self._wire_content(chunk),
                        }
                    }
                )
            self._call(
                {
                    "StageFinish": {
                        "envelope": self._control_envelope(action_id),
                        "upload_id": upload_id,
                    }
                }
            )
            return upload_id
        except Exception:
            self._abort_staged(action_id, upload_id)
            raise

    @staticmethod
    def _canonical_staged_fingerprint(content: bytes | None, fingerprint: str) -> str:
        """Give the service's staging protocol a verifiable content claim.

        Inline captures retain their provider fingerprint semantics.  Once a
        payload crosses the bounded staging transport, the worker must hash
        the bytes it receives, so legacy callers that supplied a short hint
        are upgraded at this boundary instead of bypassing integrity checks.
        """
        if content is None:
            return fingerprint
        expected = f"sha256:{hashlib.sha256(content).hexdigest()}:{len(content)}"
        if fingerprint == expected:
            return fingerprint
        return expected

    def _abort_staged(self, action_id: str, upload_id: str) -> None:
        if not action_id or not upload_id:
            return
        try:
            self._call(
                {
                    "StageAbort": {
                        "envelope": self._control_envelope(action_id),
                        "upload_id": upload_id,
                    }
                }
            )
        except Exception:
            # Startup cleanup removes abandoned service-owned stage files;
            # an IPC failure must not hide the original capture error.
            pass

    def _prepare_staged(
        self,
        envelope: Mapping[str, Any],
        upload_id: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        action_id = str(envelope.get("action_id") or "")
        return self._call(
            {
                "PrepareStaged": {
                    "envelope": self._request_envelope(envelope),
                    "upload_id": upload_id,
                    "fingerprint": fingerprint,
                }
            }
        )

    def _complete_staged(self, action_id: str, upload_id: str, fingerprint: str) -> dict[str, Any]:
        return self._call(
            {
                "CompleteStaged": {
                    "envelope": self._control_envelope(action_id),
                    "upload_id": upload_id,
                    "fingerprint": fingerprint,
                }
            }
        )

    def register_resource(
        self,
        *,
        workspace_id: str,
        root_id: str | None = None,
        root_path: str,
        relative_path: str,
        resource_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish a provider-resolved path through the authenticated registry."""
        payload: dict[str, Any] = {
            "RegisterResource": {
                "envelope": self._control_envelope("resource-registry"),
                "account_id": self.account_id,
                "workspace_id": workspace_id,
                "root_id": root_id,
                "root_path": root_path,
                "relative_path": relative_path,
                "resource_id": resource_id,
            }
        }
        return self._call(payload)

    def update_resource(
        self,
        resource_id: str,
        *,
        workspace_id: str,
        root_id: str | None = None,
        root_path: str,
        relative_path: str,
    ) -> dict[str, Any]:
        """Retain an opaque resource id through a provider rename or move."""
        return self._call(
            {
                "UpdateResource": {
                    "envelope": self._control_envelope("resource-registry"),
                    "resource_id": resource_id,
                    "account_id": self.account_id,
                    "workspace_id": workspace_id,
                    "root_id": root_id,
                    "root_path": root_path,
                    "relative_path": relative_path,
                }
            }
        )

    def move_resource(
        self,
        resource_id: str,
        *,
        replaced_resource_id: str | None,
        workspace_id: str,
        root_id: str | None,
        root_path: str,
        relative_path: str,
    ) -> dict[str, Any]:
        """Commit a rename atomically while retaining the source identity."""
        return self._call(
            {
                "MoveResource": {
                    "envelope": self._control_envelope("resource-registry"),
                    "resource_id": resource_id,
                    "replaced_resource_id": replaced_resource_id,
                    "account_id": self.account_id,
                    "workspace_id": workspace_id,
                    "root_id": root_id,
                    "root_path": root_path,
                    "relative_path": relative_path,
                }
            }
        )

    def revoke_resource(self, resource_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Tombstone a deleted resource so a later create gets a new id."""
        return self._call(
            {
                "RevokeResource": {
                    "envelope": self._control_envelope("resource-registry"),
                    "resource_id": resource_id,
                    "reason": reason,
                }
            }
        )

    def resolve_resource(self, resource_id: str) -> dict[str, Any]:
        """Resolve an opaque id without returning the service's private path map."""
        return self._call(
            {
                "ResolveResource": {
                    "envelope": self._control_envelope("resource-registry"),
                    "resource_id": resource_id,
                }
            }
        )

    def get_policy(self) -> dict[str, Any]:
        return self._call({"GetPolicy": self._control_envelope("settings")})

    def set_policy(self, policy: Mapping[str, Any], *, expected_revision: int) -> dict[str, Any]:
        return self._call({
            "SetPolicy": {
                "envelope": self._control_envelope("settings"),
                "expected_revision": int(expected_revision),
                "policy": dict(policy),
            }
        })

    def get_usage(self) -> dict[str, Any]:
        return self._call({"GetUsage": self._control_envelope("settings")})

    def get_status(self) -> dict[str, Any]:
        return self._call({"GetStatus": self._control_envelope("settings")})

    def restore_host(
        self,
        request: Mapping[str, Any],
        source: Mapping[str, Any],
        *,
        destination_path: str,
        source_host_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply an authorized host restore through the Rust provider boundary."""
        restore_id = str(request.get("restore_id") or "")
        if not restore_id or not destination_path:
            raise HistoryClientError("restore_id and destination_path are required")
        envelope = self._control_envelope(restore_id)
        payload: dict[str, Any] = {
            "RestoreHost": {
                "envelope": envelope,
                "request": dict(request),
                "source": dict(source),
                "destination_path": destination_path,
            }
        }
        if source_host_metadata is not None:
            payload["RestoreHost"]["source_host_metadata"] = dict(source_host_metadata)
        return self._call(payload)

    def _request_envelope(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(envelope)
        request.pop("physical_lease_keys", None)
        # The Rust service computes and binds the digest from the decoded ActionRequest.  Clients
        # may provide a tested Rust-compatible digest as an integrity hint, but do not need to
        # implement serde_json's byte-level representation merely to submit a mutation.
        claimed_digest = str(request.pop("claimed_digest", ""))
        return {"protocol_version": 1, "auth": self._auth(), "claimed_digest": claimed_digest, "request": request}

    @staticmethod
    def _check_content(content: bytes | None) -> None:
        if content is not None and not isinstance(content, bytes):
            raise HistoryClientError("history capture content must be bytes")

    @staticmethod
    def _wire_content(content: bytes | None) -> str | None:
        return base64.b64encode(content).decode("ascii") if content is not None else None

    def _control_envelope(self, action_id: str) -> dict[str, Any]:
        return {"protocol_version": 1, "auth": self._auth(), "action_id": action_id}
