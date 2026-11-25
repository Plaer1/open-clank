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
import re
import secrets
import socket
import threading
from .history_transport import cleanup_endpoint, exchange, private_file_fd, publish_private_file
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


class HistoryClientError(RuntimeError):
    """The history worker rejected or failed an IPC request."""


class HistoryAvailabilityError(HistoryClientError):
    """A typed missing, revoked, pruned, or invalid History selection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "history_unavailable")


async def registered_resource_pages(fetch_page):
    """One complete bounded private registry enumeration; no authority granted."""
    cursor = None
    seen = set()
    for _ in range(16):
        page = await fetch_page(cursor)
        if not isinstance(page,Mapping) or not isinstance(page.get("items"),list) or len(page["items"])>100:
            raise HistoryAvailabilityError("resource_unavailable","History registry page unavailable")
        yield page
        next_cursor = page.get("next_cursor")
        if next_cursor is None: return
        if (not isinstance(next_cursor,str) or not next_cursor.isdecimal() or len(next_cursor)>20
            or next_cursor in seen or int(next_cursor)<=int(cursor or "0")):
            raise HistoryAvailabilityError("resource_unavailable","History registry cursor unavailable")
        seen.add(next_cursor); cursor = next_cursor
    raise HistoryAvailabilityError("resource_unavailable","History registry enumeration exceeds its bound")


@dataclass(frozen=True)
class ScopedHistoryCredential:
    """A supervisor-issued account binding; the token is never serialized to logs."""

    actor_id: str
    account_id: str
    capabilities: frozenset[str] = frozenset({"capture", "read"})
    owner_accounts: frozenset[str] = frozenset()
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    checkpoint_root_id: str | None = None
    drain_session_id: str | None = None

    def wire(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "account_id": self.account_id,
            "token": self.token,
            "capabilities": sorted(self.capabilities),
            "owner_accounts": sorted(self.owner_accounts),
            **({"checkpoint_root_id": self.checkpoint_root_id} if self.checkpoint_root_id is not None else {}),
            **({"drain_session_id": self.drain_session_id} if self.drain_session_id is not None else {}),
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
        self._base_credentials = tuple(credentials)
        self.credentials = self._base_credentials
        self.host_root = Path(host_root) if host_root is not None else None
        self.receipt_root = Path(receipt_root) if receipt_root is not None else None
        self.resource_map = Path(resource_map) if resource_map is not None else None
        self.authorized_roots = tuple(dict(item) for item in (authorized_roots or ()))
        self._root_actor_tokens: dict[tuple[str, str], str] = {}
        self._checkpoint_credentials: dict[tuple[str, str, str, str], ScopedHistoryCredential] = {}
        self._checkpoint_process = None
        self._checkpoint_unavailable: set[tuple] = set()
        self._checkpoint_verified: set[tuple] = set()
        self._scope_lock = threading.RLock()
        self.credentials = self._root_actor_bindings(self._base_credentials, self.authorized_roots)
        self.authorized_roots_file = self.credential_file.with_name(
            f"{self.credential_file.stem}.roots.json"
        )
        self.authority_file = self.credential_file.with_name(
            f"{self.credential_file.stem}.authority.json"
        )
        self._authority_generation = 0
        self._rotation_lock = asyncio.Lock()
        self._fenced = False
        self._temporary_scope_count = 0
        self.startup_timeout = startup_timeout
        self.process: Any = None

    def _root_actor_bindings(self, credentials, roots):
        """Merge admitted root capabilities into exact human read identities."""
        projected = list(credentials)
        active = set()
        for root in roots:
            accounts = root.get("account_ids") or ()
            actors = root.get("actor_ids") or ()
            for account in accounts:
                base = next((item for item in credentials if item.account_id == account and item.actor_id == "*"), None)
                if not base:
                    continue
                # The human read identity is issued only from authenticated
                # accounts. An unrestricted assigned root also admits it.
                humans = [item.actor_id for item in credentials if item.account_id == account and item.actor_id != "*" and not item.actor_id.startswith("agent:") and item.capabilities == frozenset({"read"})]
                for actor in set(actors) | (set(humans) if not actors else set()):
                    if (str(actor).startswith('agent:') and str(root.get('root_id', '')).startswith('skill-history-')
                            and root.get('selected_child') is not None):
                        continue  # This private scope publishes its own least-privilege exact role.
                    key = (str(account), str(actor))
                    if key in active:
                        continue
                    active.add(key)
                    index = next((i for i, item in enumerate(projected) if item.account_id == account and item.actor_id == actor and item.checkpoint_root_id is None), None)
                    if index is not None:
                        existing = projected[index]
                        if not str(actor).startswith("agent:") and existing.capabilities == frozenset({"read"}):
                            projected[index] = ScopedHistoryCredential(actor_id=actor, account_id=account, capabilities=base.capabilities | existing.capabilities, owner_accounts=base.owner_accounts | existing.owner_accounts, token=existing.token)
                        continue
                    token = self._root_actor_tokens.get(key) or secrets.token_urlsafe(32)
                    self._root_actor_tokens[key] = token
                    projected.append(ScopedHistoryCredential(actor_id=actor, account_id=account, capabilities=base.capabilities, owner_accounts=base.owner_accounts, token=token))
        # Personal settings are available even before the first Files root
        # is assigned. Add only the base account's settings capabilities.
        for index, item in enumerate(projected):
            if item.actor_id == "*" or item.actor_id.startswith("agent:") or item.capabilities != frozenset({"read"}):
                continue
            base = next((row for row in credentials if row.account_id == item.account_id and row.actor_id == "*"), None)
            if base:
                settings = base.capabilities & {"settings-read", "settings-write"}
                if "admin" in base.capabilities:
                    settings = settings | {"settings-admin"}
                projected[index] = replace(item, capabilities=item.capabilities | settings)
        self._root_actor_tokens = {key: token for key, token in self._root_actor_tokens.items() if key in active}
        return tuple(projected)

    def client_for(self, actor_id: str, account_id: str) -> HistoryClient:
        return HistoryClient(str(self.socket_path), actor_id=actor_id, account_id=account_id, token=self.credential_token(actor_id, account_id))

    def credential_token(self, actor_id: str, account_id: str) -> str:
        # Prefer exact credentials. An agent:<owner> never matches the human
        # actor, so it keeps its existing wildcard assigned-root token.
        for match in (actor_id, "*"):
            for credential in self.credentials:
                if credential.account_id == account_id and credential.actor_id == match and credential.checkpoint_root_id is None:
                    return credential.token
        raise HistoryClientError("no supervisor-issued history credential for this owner")

    def _checkpoint_probe_locked(self, actor_id: str, account_id: str, root_ids: list[str], process, generation: int) -> bool:
        if process is None or process.poll() is not None or not actor_id or actor_id=="*":return False
        if not root_ids or len(root_ids)>64 or len(set(root_ids))!=len(root_ids) or any(not isinstance(root,str) or not root for root in root_ids):return False
        for root_id in root_ids:
            roots=[r for r in self.authorized_roots if str(r.get("root_id"))==root_id and account_id in (r.get("account_ids") or ()) and (not r.get("actor_ids") or actor_id in r["actor_ids"])]
            if len(roots)!=1:return False
        try:
            token=self.credential_token(actor_id,account_id)
            client=HistoryClient(str(self.socket_path),actor_id=actor_id,account_id=account_id,token=token,timeout=1.0)
            result=client._call({"CheckpointCapabilities":{"protocol_version":1,"auth":{"actor_id":actor_id,"account_id":account_id,"token":token},"action_id":"checkpoint-capabilities"}})
            capability=result.get("CheckpointCapabilities")
            return bool(isinstance(capability,dict) and capability.get("schema_version") in (4,5) and capability.get("checkpoint_capture") is True
                and capability.get("process_id")==process.pid and capability.get("authority_generation")==generation
                and self.process is process and process.poll() is None and self._authority_generation==generation)
        except Exception:return False

    def authority_barrier(self, actor_id: str, account_id: str) -> bool:
        """Drain earlier frames and verify this exact process/authority generation.

        False covers older peers, timeout and every unprovable result. Callers
        owning a temporary scope must fence the actual worker before unlocking.
        """
        if not actor_id or actor_id == "*" or actor_id.startswith("agent:"):
            return False
        with self._scope_lock:
            process = self.process
            generation = self._authority_generation
            if process is None or process.poll() is not None:
                return False
            try:
                credential = next(item for item in self.credentials if item.account_id == account_id
                    and item.actor_id == actor_id and item.checkpoint_root_id is None
                    and ("read" in item.capabilities or "admin" in item.capabilities))
                client = HistoryClient(str(self.socket_path), actor_id=actor_id, account_id=account_id,
                    token=credential.token, timeout=1.5)
                response = client._call({"AuthorityBarrier": {"envelope":client._control_envelope("authority-barrier"),
                    "expected_generation":generation, "expected_process_id":process.pid}})
                return (response.get("AuthorityBarrier") == {"authority_generation":generation,"process_id":process.pid}
                    and self.process is process and process.poll() is None and self._authority_generation == generation)
            except Exception:
                return False

    def _skill_agent_barrier(self, scope, *, phase, credential=None) -> bool:
        """Drain one exact temporary Skills Agent scope; never a general control grant."""
        from src.openclank.skill_history_scope import _AgentSkillScope, _AGENT_SKILL_SCOPE_SEAL
        if (type(scope) is not _AgentSkillScope or scope._seal is not _AGENT_SKILL_SCOPE_SEAL
                or scope.issuer.supervisor is not self or phase not in ('present', 'absent')):
            return False
        with self._scope_lock:
            try:
                scope.assert_retained()
                if phase == 'present':
                    scope.assert_current()
                    credential = scope.active_credential
                    expected_caps = frozenset({'read'}) if scope.mode == 'read' else frozenset({'read', 'capture'})
                    if (scope.issuer._scopes.get(scope.root_id) is not scope.descriptor
                            or type(credential) is not ScopedHistoryCredential or credential not in self.credentials
                            or credential.actor_id != scope.actor or credential.account_id != scope.account
                            or credential.checkpoint_root_id is not None or credential.drain_session_id is not None
                            or credential.capabilities != expected_caps or credential.owner_accounts):
                        return False
                    drain_id = None
                else:
                    if (not scope.revoking or any(root.get('root_id') == scope.root_id for root in self.authorized_roots)
                            or type(credential) is not ScopedHistoryCredential or credential not in self.credentials
                            or credential.actor_id != scope.actor or credential.account_id != scope.account
                            or credential.checkpoint_root_id != scope.root_id
                            or credential.drain_session_id != scope.drain_session_id
                            or credential.capabilities != frozenset({'skill-draft-drain'}) or credential.owner_accounts):
                        return False
                    drain_id = scope.drain_session_id
                process, generation = self.process, self._authority_generation
                if process is not scope.process or process is None or process.poll() is not None:
                    return False
                client = HistoryClient(str(self.socket_path), actor_id=scope.actor, account_id=scope.account,
                                       token=credential.token, timeout=1.5)
                response = client._call({'AgentAuthorityBarrier': {'envelope': client._control_envelope('skill-agent-barrier'),
                    'root_id': scope.root_id, 'phase': phase, 'drain_session_id': drain_id,
                    'expected_generation': generation, 'expected_process_id': process.pid}})
                valid = (response.get('AgentAuthorityBarrier') == {'authority_generation': generation, 'process_id': process.pid}
                    and self.process is process and process.poll() is None and self._authority_generation == generation)
                if phase == 'present':
                    scope.assert_current()
                else:
                    scope.assert_retained()
                return valid
            except Exception:
                return False

    async def _publish_skill_agent_scope(self, scope, roots):
        """Publish a Host-only exact Agent role with no general control privileges."""
        from src.openclank.skill_history_scope import _AgentSkillScope, _AGENT_SKILL_SCOPE_SEAL
        if (type(scope) is not _AgentSkillScope or scope._seal is not _AGENT_SKILL_SCOPE_SEAL
                or scope.issuer.supervisor is not self or scope.revoking or scope.mode not in ('create', 'read')):
            raise HistoryClientError('agent scope unavailable')
        async with self._rotation_lock:
            if self._fenced or self.process is not scope.process or scope.process.poll() is not None:
                raise HistoryClientError('agent scope unavailable')
            scope.assert_current()
            if (sum(root.get('root_id') == scope.root_id for root in roots) != 1
                    or any(root.get('root_id') == scope.root_id for root in self.authorized_roots)):
                raise HistoryClientError('agent scope changed')
            caps = frozenset({'read'}) if scope.mode == 'read' else frozenset({'read', 'capture'})
            role = ScopedHistoryCredential(actor_id=scope.actor, account_id=scope.account, capabilities=caps)
            scope.active_credential = role
            self._publish_scope((*self._base_credentials, role), tuple(roots))
            scope.published = True
            if not await asyncio.wait_for(asyncio.to_thread(self._skill_agent_barrier, scope, phase='present'), timeout=3.0):
                raise HistoryClientError('agent scope drain unavailable')
            scope.assert_current()

    async def _revoke_skill_agent_scope(self, scope, roots):
        """Publish root removal and a drain-only role atomically, then remove the role."""
        from src.openclank.skill_history_scope import _AgentSkillScope, _AGENT_SKILL_SCOPE_SEAL
        if (type(scope) is not _AgentSkillScope or scope._seal is not _AGENT_SKILL_SCOPE_SEAL
                or scope.issuer.supervisor is not self or not scope.revoking):
            raise HistoryClientError('agent scope unavailable')
        async with self._rotation_lock:
            if self._fenced or self.process is not scope.process or scope.process.poll() is not None:
                raise HistoryClientError('agent scope unavailable')
            if scope.active_credential not in self._base_credentials:
                raise HistoryClientError('agent scope credential changed')
            base = tuple(item for item in self._base_credentials if item is not scope.active_credential)
            expected = tuple(root for root in self.authorized_roots if root.get('root_id') != scope.root_id)
            if (sum(root.get('root_id') == scope.root_id for root in self.authorized_roots) != 1
                    or tuple(roots) != expected or any(item.drain_session_id is not None for item in base)):
                raise HistoryClientError('agent scope changed')
            drain = ScopedHistoryCredential(actor_id=scope.actor, account_id=scope.account,
                capabilities=frozenset({'skill-draft-drain'}), checkpoint_root_id=scope.root_id,
                drain_session_id=scope.drain_session_id)
            self._publish_scope((*base, drain), expected)
            try:
                if not await asyncio.wait_for(asyncio.to_thread(self._skill_agent_barrier, scope,
                        phase='absent', credential=drain), timeout=3.0):
                    raise HistoryClientError('agent scope drain unavailable')
            finally:
                self._publish_scope(base, expected)

    def checkpoint_capability(self, actor_id: str, account_id: str, root_id: str) -> bool:
        """Read support without minting a role or publishing authority."""
        with self._scope_lock:
            return self._checkpoint_probe_locked(actor_id,account_id,[root_id],self.process,self._authority_generation)

    def files_checkpoint_tokens(self, actor_id: str, account_id: str, root_ids: list[str]) -> dict[str, str]:
        return self._files_root_tokens(actor_id, account_id, root_ids, "checkpoint")

    def files_worktree_tokens(self, actor_id: str, account_id: str, root_ids: list[str]) -> dict[str, str]:
        return self._files_root_tokens(actor_id, account_id, root_ids, "worktree")

    def _files_root_tokens(self, actor_id: str, account_id: str, root_ids: list[str], purpose: str) -> dict[str, str]:
        """Negotiate one private Files-only purpose against the live authority."""
        with self._scope_lock:
            process = self.process
            generation = self._authority_generation
            if process is None or process.poll() is not None or not actor_id or actor_id == "*" or (purpose == "worktree" and actor_id.startswith("agent:")):
                return {}
            if not root_ids or len(root_ids)>64 or len(set(root_ids))!=len(root_ids) or any(not isinstance(root,str) or not root for root in root_ids):
                return {}
            for root_id in root_ids:
                roots = [r for r in self.authorized_roots if str(r.get("root_id")) == root_id
                         and account_id in (r.get("account_ids") or ())
                         and (not r.get("actor_ids") or actor_id in r["actor_ids"])]
                if len(roots)!=1: return {}
            if self._checkpoint_process is not process:
                self._checkpoint_credentials.clear()
                self._checkpoint_unavailable.clear()
                self._checkpoint_verified.clear()
                self.credentials = tuple(c for c in self.credentials if c.checkpoint_root_id is None)
                self._checkpoint_process = process
            keys = [(account_id,actor_id,root_id,purpose) for root_id in root_ids]
            probe_key = (process.pid,generation,account_id,actor_id,tuple(root_ids),purpose)
            if all(key in self._checkpoint_credentials for key in keys) and probe_key in self._checkpoint_verified:
                return {key[2]:self._checkpoint_credentials[key].token for key in keys}
            if probe_key in self._checkpoint_unavailable: return {}
            def unavailable():
                if len(self._checkpoint_unavailable)>=128: self._checkpoint_unavailable.clear()
                self._checkpoint_unavailable.add(probe_key)
                return {}
            if purpose == "checkpoint":
                ready = self._checkpoint_probe_locked(actor_id,account_id,root_ids,process,generation)
            else:
                try:
                    token = self.credential_token(actor_id,account_id)
                    client = HistoryClient(str(self.socket_path),actor_id=actor_id,account_id=account_id,token=token,timeout=1.0)
                    response = client._call({"FilesWorktreeCapabilities":client._control_envelope("worktree-capabilities")})
                    capability = response.get("FilesWorktreeCapabilities")
                    ready = (isinstance(capability,dict) and capability.get("worktree_capture") is True
                        and capability.get("process_id")==process.pid and capability.get("authority_generation")==generation
                        and self.process is process and process.poll() is None and self._authority_generation==generation)
                except Exception:
                    ready = False
            if not ready:
                return unavailable()
            if all(key in self._checkpoint_credentials for key in keys):
                if len(self._checkpoint_verified)>=128:self._checkpoint_verified.clear()
                self._checkpoint_verified.add(probe_key)
                return {key[2]:self._checkpoint_credentials[key].token for key in keys}
            old_credentials, old_generation = self.credentials, self._authority_generation
            added = []
            for key in keys:
                if key not in self._checkpoint_credentials:
                    role = ScopedHistoryCredential(actor_id=actor_id, account_id=account_id,
                        capabilities=frozenset({"checkpoint-capture" if purpose == "checkpoint" else "worktree-capture"}), checkpoint_root_id=key[2])
                    self._checkpoint_credentials[key] = role
                    added.append((key,role))
            self.credentials = (*self.credentials, *(role for _,role in added))
            self._authority_generation += 1
            try:
                self._write_authority()
            except Exception:
                for key,_ in added: self._checkpoint_credentials.pop(key, None)
                self.credentials, self._authority_generation = old_credentials, old_generation
                raise
            if self.process is not process or process.poll() is not None:
                return {}
            if len(self._checkpoint_verified)>=128:self._checkpoint_verified.clear()
            self._checkpoint_verified.add((process.pid,self._authority_generation,account_id,actor_id,tuple(root_ids),purpose))
            return {key[2]:self._checkpoint_credentials[key].token for key in keys}

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
        cleanup_endpoint(self.socket_path)

    async def start(self) -> None:
        if self._fenced:
            raise HistoryClientError("history supervisor is fenced")
        if self.process is not None and self.process.poll() is None:
            return
        if self._temporary_scope_count:
            raise HistoryClientError("history worker cannot restart while a temporary scope is owned")
        with self._scope_lock:
            self._checkpoint_credentials.clear()
            self._checkpoint_unavailable.clear()
            self._checkpoint_verified.clear()
            self._checkpoint_process = None
            self.credentials = tuple(item for item in self.credentials if item.checkpoint_root_id is None)
            self._authority_generation += 1
        if os.name != "nt" and self.socket_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                if probe.connect_ex(str(self.socket_path)) == 0:
                    raise HistoryClientError("history worker socket is already in use")
            finally:
                probe.close()
            cleanup_endpoint(self.socket_path)
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
            if os.name == "nt" or self.socket_path.exists():
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
            cleanup_endpoint(self.socket_path)
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
        cleanup_endpoint(self.socket_path)

    async def hold_temporary_scope(self, process) -> None:
        async with self._rotation_lock:
            if self._fenced or process is None or self.process is not process or process.poll() is not None:
                raise HistoryClientError("history worker unavailable")
            self._temporary_scope_count += 1

    async def release_temporary_scope(self) -> None:
        async with self._rotation_lock:
            if self._temporary_scope_count < 1:
                raise HistoryClientError("history scope ownership unavailable")
            self._temporary_scope_count -= 1

    def fence_now(self, original) -> None:
        """Emergency synchronous fence while the caller retains its locks."""
        self._fenced = True
        for process in (original, self.process):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if process is not None and process.poll() is None:
                raise HistoryClientError("history worker could not be fenced")

    async def fence(self) -> None:
        """Prevent restart and stop the owned worker under the rotation lock."""
        async with self._rotation_lock:
            self._fenced = True
            await self.stop()

    async def _rotate_scope(
        self,
        credentials: tuple[ScopedHistoryCredential, ...],
        authorized_roots: tuple[Mapping[str, Any], ...],
    ) -> None:
        """Publish credentials and roots while keeping a live worker available."""
        async with self._rotation_lock:
            if self._fenced: raise HistoryClientError("history supervisor is fenced")
            self._publish_scope(credentials, authorized_roots)
            if self.process is None or self.process.poll() is not None:
                await self.start()

    def _publish_scope(
        self,
        credentials: tuple[ScopedHistoryCredential, ...],
        authorized_roots: tuple[Mapping[str, Any], ...],
    ) -> None:
        with self._scope_lock:
            self._checkpoint_credentials.clear()
            self._checkpoint_unavailable.clear()
            self._checkpoint_verified.clear()
            self._checkpoint_process = None
            self._base_credentials = tuple(credentials)
            self.authorized_roots = tuple(dict(item) for item in authorized_roots)
            self.credentials = self._root_actor_bindings(self._base_credentials, self.authorized_roots)
            self._authority_generation += 1
            self._write_authority()

    async def rotate_credentials(self, credentials: list[ScopedHistoryCredential]) -> None:
        """Apply account create/revoke/rotation in the live authority snapshot."""
        await self._rotate_scope(tuple(credentials), self.authorized_roots)

    async def sync_authorized_roots(self, authorized_roots: list[Mapping[str, Any]]) -> None:
        """Refresh provider root authority when the Files registry generation changes."""
        async with self._rotation_lock:
            if self._fenced: raise HistoryClientError("history supervisor is fenced")
            projected = tuple(dict(item) for item in authorized_roots)
            if projected != self.authorized_roots:
                self._publish_scope(self._base_credentials, projected)
                if self.process is None or self.process.poll() is not None:
                    await self.start()

    async def sync_accounts(
        self,
        users: Mapping[str, Any],
        authorized_roots: list[Mapping[str, Any]] | None = None,
    ) -> None:
        async with self._rotation_lock:
            if self._fenced: raise HistoryClientError("history supervisor is fenced")
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
        previous = {(item.account_id, item.actor_id): item for item in self._base_credentials}
        refreshed: list[ScopedHistoryCredential] = []
        from core.auth import normalize_known_username
        for _username, record in users.items():
            if not normalize_known_username(users, _username) or not isinstance(record, Mapping):
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
            old = previous.get((account_id, "*"))
            refreshed.append(
                ScopedHistoryCredential(
                    actor_id="*",
                    account_id=account_id,
                    capabilities=frozenset(capabilities),
                    token=old.token if old is not None else secrets.token_urlsafe(32),
                )
            )
            actor = "service:memery-history:" + account_id
            prior = previous.get((account_id, actor))
            refreshed.append(ScopedHistoryCredential(
                actor_id=actor, account_id=account_id, capabilities=frozenset({"memery-history"}),
                token=prior.token if prior is not None else secrets.token_urlsafe(32),
            ))
            old_human = previous.get((account_id, _username))
            refreshed.append(ScopedHistoryCredential(
                actor_id=_username, account_id=account_id, capabilities=frozenset({"read"}),
                token=old_human.token if old_human is not None else secrets.token_urlsafe(32),
            ))
        if not refreshed:
            old = previous.get(("service:health", "supervisor"))
            refreshed.append(
                ScopedHistoryCredential(
                    actor_id="supervisor",
                    account_id="service:health",
                    capabilities=frozenset(),
                    token=old.token if old is not None else secrets.token_urlsafe(32),
                )
            )
        projected_roots = (
            tuple(dict(item) for item in authorized_roots)
            if authorized_roots is not None
            else self.authorized_roots
        )
        changed = (
            [item.wire() for item in refreshed] != [item.wire() for item in self._base_credentials]
            or projected_roots != self.authorized_roots
        )
        if changed:
            self._publish_scope(tuple(refreshed), projected_roots)
            if self.process is None or self.process.poll() is not None:
                await self.start()

    def _write_credentials(self) -> None:
        self.credential_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.credential_file.with_name(f".{self.credential_file.name}.{secrets.token_hex(8)}.tmp")
        payload = json.dumps([item.wire() for item in self.credentials], separators=(",", ":"))
        fd = private_file_fd(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            publish_private_file(temporary, self.credential_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _write_authorized_roots(self) -> None:
        """Atomically publish the current Files root snapshot for a live worker."""
        self.authorized_roots_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.authorized_roots_file.with_name(
            f".{self.authorized_roots_file.name}.{secrets.token_hex(8)}.tmp"
        )
        payload = json.dumps(
            list(self.authorized_roots), separators=(",", ":"), sort_keys=True
        )
        fd = private_file_fd(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            publish_private_file(temporary, self.authorized_roots_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _write_authority(self) -> None:
        """Atomically publish credentials and roots as one generation."""
        self.authority_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.authority_file.with_name(f".{self.authority_file.name}.{secrets.token_hex(8)}.tmp")
        payload = json.dumps(
            {
                "generation": self._authority_generation,
                "credentials": [item.wire() for item in self.credentials],
                "authorized_roots": list(self.authorized_roots),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        fd = private_file_fd(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            publish_private_file(temporary, self.authority_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


MAX_FRAME_BYTES = 1024 * 1024
# Keep inline control frames comfortably below the IPC ceiling. Larger
# payloads use the service-owned chunk staging protocol; this is a wire-shape
# threshold, not a file eligibility limit.
_INLINE_CONTENT_BYTES = 512 * 1024
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
        try:
            response = exchange(self.socket_path, raw, self.timeout, MAX_FRAME_BYTES)
        except OSError as exc:
            # Transport loss is best-effort Lore unavailability, distinct from
            # local preimage validation errors at the mutation owner.
            raise HistoryClientError("history worker transport is unavailable") from exc
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

    def prepare_batch(self, envelope: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> dict[str, Any]:
        return self._prepare_batch(envelope, entries, once=False)

    def prepare_batch_once(self, envelope: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> dict[str, Any]:
        result = self._prepare_batch(envelope, entries, once=True)
        value = result.get("BatchOnce")
        if type(value) is not dict or set(value) != {"created", "action"} or type(value["created"]) is not bool or type(value["action"]) is not dict:
            raise HistoryClientError("batch once receipt unavailable")
        return value

    def _prepare_batch(
        self, envelope: Mapping[str, Any], entries: list[Mapping[str, Any]], *, once: bool,
    ) -> dict[str, Any]:
        """Durably prepare every exact resource preimage in one parent action.

        Batch entries use the same bounded inline encoding as ``Prepare``. A
        provider with a larger preimage must use its staged/chunk owner path;
        silently dropping secondary bytes would violate the batch contract.
        """
        wire_entries: list[dict[str, Any]] = []
        staged: list[tuple[str, str]] = []
        action_id = str(envelope.get("action_id") or "")
        inline_bytes = 0
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
                    if len(content) > _INLINE_CONTENT_BYTES or inline_bytes + len(content) > _INLINE_CONTENT_BYTES:
                        fingerprint = self._canonical_staged_fingerprint(content, fingerprint)
                        upload_id = self._stage_content(action_id, content, fingerprint)
                        staged.append((upload_id, action_id))
                        item["staged_upload_id"] = upload_id
                        item["fingerprint"] = fingerprint
                        item["content"] = None
                    else:
                        item["content"] = self._wire_content(content)
                        inline_bytes += len(content)
                wire_entries.append(item)
            response = self._call(
                {
                    ("PrepareBatchOnce" if once else "PrepareBatch"): {
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

    def settle_recovery(self, action_id: str) -> dict[str, Any]:
        return self._call({"SettleRecovery": self._control_envelope(action_id)})

    def get_capture_action(self, action_id: str) -> dict[str, Any]:
        """Private selected capture metadata, authorized to the exact actor."""
        return self._call({"GetCaptureAction": self._control_envelope(action_id)})

    def selected_checkpoint_status(self, request_id: str) -> dict[str, Any]:
        """Read only the original actor/account checkpoint; never reissue it."""
        if not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}",request_id):
            raise HistoryClientError("checkpoint identity unavailable")
        material=f"{self.account_id}\0{self.actor_id}\0{request_id}".encode("utf-8")
        action_id="checkpoint-"+hashlib.sha256(material).hexdigest()
        response=self._call({"GetSelectedCheckpoint":{"envelope":self._control_envelope(action_id),"request_id":request_id}})
        result=response.get("CheckpointStatus")
        keys={"state","request_digest","locator","items","consistency","quiescence","started_millis","finished_millis"}
        if not isinstance(result,dict) or set(result)!=keys or result.get("state") not in {"pending","failed","complete"}:
            raise HistoryAvailabilityError("checkpoint_unavailable","Checkpoint unavailable")
        if not isinstance(result.get("items"),list) or len(result["items"])>16:
            raise HistoryClientError("checkpoint receipt unavailable")
        if result["state"]=="complete":
            if (not isinstance(result.get("locator"),str) or not result["locator"].startswith("oc-history-checkpoint:v1:")
                or len(result["locator"])>24000 or not result["items"] or result.get("consistency")!="observed_interval"
                or result.get("quiescence")!="unverified" or type(result.get("started_millis")) is not int
                or type(result.get("finished_millis")) is not int or result["finished_millis"]<result["started_millis"]):
                raise HistoryClientError("checkpoint receipt unavailable")
            for item in result["items"]:
                if (not isinstance(item,dict) or set(item)!={"resource_id","version_ref","fingerprint","size"}
                    or not isinstance(item["resource_id"],str) or not isinstance(item["version_ref"],str)
                    or not isinstance(item["fingerprint"],str) or type(item["size"]) is not int or not 0<=item["size"]<=16*1024*1024):
                    raise HistoryClientError("checkpoint receipt unavailable")
        elif result["locator"] is not None or result["items"]:
            raise HistoryClientError("checkpoint receipt unavailable")
        return result

    def after_unavailable(self, action_id: str) -> dict[str, Any]:
        """Persist recovery failure for a known committed action; never replay."""
        return self._call({"AfterUnavailable": self._control_envelope(action_id)})

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

    def complete_batch(
        self,
        action_id: str,
        entries: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist every after-state and outcome for one committed mutation batch."""
        wire_entries: list[dict[str, Any]] = []
        staged: list[str] = []
        try:
            for entry in entries:
                item = dict(entry)
                content = item.get("content")
                item["staged_upload_id"] = None
                if content is not None:
                    if not isinstance(content, (bytes, bytearray)):
                        raise HistoryClientError("batch after content must be bytes")
                    content = bytes(content)
                    self._check_content(content)
                    fingerprint = str(item.get("fingerprint") or "missing")
                    fingerprint = self._canonical_staged_fingerprint(content, fingerprint)
                    if len(content) > _INLINE_CONTENT_BYTES:
                        upload_id = self._stage_content(action_id, content, fingerprint)
                        staged.append(upload_id)
                        item["staged_upload_id"] = upload_id
                        item["content"] = None
                        item["fingerprint"] = fingerprint
                    else:
                        item["content"] = self._wire_content(content)
                wire_entries.append(item)
            return self._call({
                "CompleteBatch": {
                    "envelope": self._control_envelope(action_id),
                    "batch_version": 1,
                    "entries": wire_entries,
                }
            })
        except Exception:
            for upload_id in staged:
                self._abort_staged(action_id, upload_id)
            raise

    def abort(self, action_id: str) -> dict[str, Any]:
        return self._call({"Abort": {"envelope": self._control_envelope(action_id)}})

    def read_version(self, action_id: str, receipt: Mapping[str, Any], *, max_bytes: int | None = None) -> bytes | None:
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
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0 or content_length > max_bytes):
            raise HistoryClientError("history version exceeds the requested bound")
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
    def _raise_if_unavailable(response: Mapping[str, Any]) -> None:
        unavailable = response.get("Unavailable")
        if not isinstance(unavailable, Mapping):
            return
        code = str(unavailable.get("code") or "history_unavailable")
        message = str(unavailable.get("message") or "selected History data is unavailable")
        raise HistoryAvailabilityError(code, message)

    def list_resource_versions(
        self,
        resource_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List one bounded page from the owning Files resource's Lore history."""
        resource_id = str(resource_id or "").strip()
        if not resource_id:
            raise HistoryClientError("history resource id is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HistoryClientError("history version page size must be between 1 and 100")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
            raise HistoryClientError("history page cursor is invalid")
        response = self._call(
            {
                "ListResourceVersions": {
                    "envelope": self._control_envelope("history-resource-versions"),
                    "resource_id": resource_id,
                    "cursor": cursor,
                    "limit": limit,
                }
            }
        )
        self._raise_if_unavailable(response)
        page = response.get("Versions")
        if not isinstance(page, Mapping) or not isinstance(page.get("items"), list):
            raise HistoryClientError("history worker returned an invalid resource-version page")
        items = page["items"]
        if len(items) > limit or not all(isinstance(item, Mapping) for item in items):
            raise HistoryClientError("history worker returned an unbounded resource-version page")
        next_cursor = page.get("next_cursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 2048):
            raise HistoryClientError("history worker returned an invalid page cursor")
        return {"items": [dict(item) for item in items], "next_cursor": next_cursor}

    def list_resource_changes(self, resource_id: str, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Read action groups for one already-authorized registered resource."""
        if not isinstance(resource_id, str) or not resource_id or len(resource_id) > 256:
            raise HistoryClientError("history resource id is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HistoryClientError("history change page size is invalid")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
            raise HistoryClientError("history change cursor is invalid")
        response = self._call({"ListResourceChanges": {"envelope": self._control_envelope("history-resource-changes"), "resource_id": resource_id, "cursor": cursor, "limit": limit}})
        self._raise_if_unavailable(response)
        page = response.get("Changes")
        if not isinstance(page, Mapping) or page.get("version") != 1 or page.get("coverage") != "recorded_receipts_only" or page.get("order") != "action_id" or not isinstance(page.get("items"), list) or len(page["items"]) > limit or not all(isinstance(item, Mapping) for item in page["items"]):
            raise HistoryClientError("history worker returned an invalid change page")
        revision, next_cursor = page.get("revision"), page.get("next_cursor")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or (next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 4096)):
            raise HistoryClientError("history worker returned an invalid change cursor")
        return {"version": 1, "coverage": "recorded_receipts_only", "order": "action_id", "revision": revision, "items": [dict(item) for item in page["items"]], "next_cursor": next_cursor}

    def get_managed_action_batch(self, *, action_id: str, request_digest: str, complete_digest: str, origin: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve a completed batch under current read authority, without capture rights."""
        if (not isinstance(action_id, str) or not action_id or len(action_id) > 256
                or any(ord(c) < 32 or ord(c) == 127 for c in action_id)
                or not isinstance(request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", request_digest)
                or not isinstance(complete_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", complete_digest)
                or not isinstance(origin, Mapping)):
            raise HistoryClientError("history batch selector unavailable")
        response = self._call({"GetManagedActionBatch": {"envelope": self._control_envelope(action_id),
            "request_digest": request_digest, "complete_digest": complete_digest, "origin": dict(origin)}})
        self._raise_if_unavailable(response)
        batch = response.get("ManagedActionBatch")
        if not isinstance(batch, dict) or set(batch) != {"version", "members"} or batch["version"] != 2 or not isinstance(batch["members"], list) or not 2 <= len(batch["members"]) <= 64:
            raise HistoryClientError("history batch unavailable")
        seen = set()
        for member in batch["members"]:
            if (not isinstance(member, dict) or set(member) != {"resource_id", "before_version_id", "after_version_id"}
                    or any(not isinstance(member[k], str) or not member[k] or len(member[k]) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in member[k]) for k in member)
                    or member["resource_id"] in seen):
                raise HistoryClientError("history batch unavailable")
            seen.add(member["resource_id"])
        return {"version": 2, "members": [dict(member) for member in batch["members"]]}

    def get_managed_action_locator(self, *, resource_id: str, action_id: str, origin: Mapping[str, Any]) -> str:
        """Ask History for the exact catalog-owned locator under current resource authority."""
        response = self._call({'GetManagedActionLocator': {'envelope': self._control_envelope(action_id),
            'resource_id': resource_id, 'origin': dict(origin)}})
        self._raise_if_unavailable(response)
        item = response.get('ManagedActionLocator')
        if (not isinstance(item, Mapping) or set(item) != {'locator'} or not isinstance(item['locator'], str)
                or not item['locator'].startswith('oc-history-action:v1:') or len(item['locator']) > 4096):
            raise HistoryClientError('history action unavailable')
        return item['locator']

    def get_managed_resource_change(self, *, resource_id: str, action_id: str, before_version_id: str, after_version_id: str, origin: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve one authorized recorded action; no capture-actor impersonation."""
        response=self._call({'GetResourceChange':{'envelope':self._control_envelope(action_id),'resource_id':resource_id,
            'before_version_id':before_version_id,'after_version_id':after_version_id,'origin':dict(origin)}})
        self._raise_if_unavailable(response)
        item=response.get('ResourceChange')
        if not isinstance(item,Mapping) or item.get('action_id')!=action_id:raise HistoryClientError('history action unavailable')
        return dict(item)

    def list_ordered_managed_changes(self, resource_id: str, *, origin_action_id: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        if (type(resource_id) is not str or not resource_id or len(resource_id)>256 or type(limit) is not int or not 1<=limit<=100
            or (origin_action_id is not None and (type(origin_action_id) is not str or not origin_action_id or len(origin_action_id)>256))
            or (cursor is not None and (type(cursor) is not str or len(cursor)>16384))):raise HistoryClientError('ordered history unavailable')
        response=self._call({'ListOrderedManagedChanges':{'envelope':self._control_envelope('ordered-managed-changes'),'resource_id':resource_id,'origin_action_id':origin_action_id,'cursor':cursor,'limit':limit}})
        self._raise_if_unavailable(response);page=response.get('OrderedManagedChanges')
        coverage,order=('recorded_origin_only','action_id') if origin_action_id is not None else ('recorded_timestamps_only','recorded_timestamp_action_id')
        if (type(page) is not dict or set(page)!={'version','scope','coverage','order','revision','items','next_cursor'} or type(page['version']) is not int or page['version']!=1
            or page['scope']!='one_authorized_resource' or page['coverage']!=coverage or page['order']!=order
            or type(page['revision']) is not int or page['revision']<0 or type(page['items']) is not list or len(page['items'])>limit
            or (page['next_cursor'] is not None and (type(page['next_cursor']) is not str or len(page['next_cursor'])>16384))):raise HistoryClientError('ordered history unavailable')
        for item in page['items']:
            if (type(item) is not dict or set(item)!={'change','origin','locator'} or type(item['change']) is not dict
                or (item['origin'] is not None and type(item['origin']) is not dict)
                or (item['locator'] is not None and (type(item['locator']) is not str or not item['locator'].startswith('oc-history-action:v1:') or len(item['locator'])>4096))):raise HistoryClientError('ordered history unavailable')
        return page

    def cross_origin_fence(self, resource_id: str, origin_action_id: str) -> dict[str, Any]:
        if not isinstance(resource_id,str) or not resource_id or len(resource_id)>256 or not isinstance(origin_action_id,str) or not origin_action_id or len(origin_action_id)>512:raise HistoryClientError("origin unavailable")
        response=self._call({"CrossOriginFence":{"envelope":self._control_envelope("cross-origin-fence"),"resource_id":resource_id,"origin_action_id":origin_action_id}})
        self._raise_if_unavailable(response);value=response.get("CrossOriginFence")
        if (type(value) is not dict or set(value)!={"incarnation","origin_revision","registry_revision","authority_generation"} or not re.fullmatch(r"[0-9a-f]{64}",str(value.get("incarnation") or ""))
            or any(type(value.get(k)) is not int or not 0<=value[k]<=9_007_199_254_740_991 for k in ("origin_revision","registry_revision","authority_generation"))):raise HistoryClientError("origin unavailable")
        return value

    def cross_origin_candidates(self, resource_id: str, origin_action_id: str, *, cursor: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(resource_id,str) or not resource_id or len(resource_id)>256 or not isinstance(origin_action_id,str) or not origin_action_id or len(origin_action_id)>512:raise HistoryClientError("origin unavailable")
        if cursor is not None and (type(cursor) is not dict or set(cursor)!={"incarnation","origin_revision","registry_revision","authority_generation","after"}
            or not re.fullmatch(r"[0-9a-f]{64}",str(cursor.get("incarnation") or "")) or any(type(cursor.get(k)) is not int or not 0<=cursor[k]<=9_007_199_254_740_991 for k in ("origin_revision","registry_revision","authority_generation"))
            or not isinstance(cursor.get("after"),str) or not 1<=len(cursor["after"])<=8192):raise HistoryClientError("origin cursor unavailable")
        body={"envelope":self._control_envelope("cross-origin-candidates"),"resource_id":resource_id,"origin_action_id":origin_action_id}
        if cursor:body.update(cursor)
        response=self._call({"ListCrossOriginChanges":body});self._raise_if_unavailable(response);page=response.get("CrossOriginChanges")
        if (type(page) is not dict or set(page)!={"incarnation","origin_revision","registry_revision","authority_generation","items","next"}
            or not re.fullmatch(r"[0-9a-f]{64}",str(page.get("incarnation") or "")) or any(type(page.get(k)) is not int or not 0<=page[k]<=9_007_199_254_740_991 for k in ("origin_revision","registry_revision","authority_generation"))
            or type(page.get("items")) is not list or len(page["items"])>100 or (page.get("next") is not None and (not isinstance(page["next"],str) or len(page["next"])>8192))):raise HistoryClientError("origin unavailable")
        for item in page["items"]:
            if (type(item) is not dict or set(item)!={"position","resource","action_id","recorded_millis","state","availability","locator","registration"} or not isinstance(item["position"],str) or not 1<=len(item["position"])<=8192
                or not isinstance(item["action_id"],str) or not 1<=len(item["action_id"])<=512
                or (item["recorded_millis"] is not None and (not isinstance(item["recorded_millis"],str) or not re.fullmatch(r"0|[1-9][0-9]{0,19}",item["recorded_millis"]) or int(item["recorded_millis"])>18446744073709551615))
                or not isinstance(item["state"],str) or not re.fullmatch(r"[a-z]{1,64}",item["state"]) or item["availability"] not in {"available","expiring","expired"}
                or (item["locator"] is not None and (not isinstance(item["locator"],str) or not item["locator"].startswith("oc-history-action:v1:") or len(item["locator"])>4096)) or type(item["resource"]) is not dict
                or set(item["resource"])!={"account_id","workspace_id","provider","resource_id"} or (item["registration"] is not None and type(item["registration"]) is not dict)):
                raise HistoryClientError("origin unavailable")
        return page

    @staticmethod
    def _workspace_activity_proof(value: Any) -> bool:
        return (type(value) is dict and all(re.fullmatch(r'[0-9a-f]{64}', str(value.get(k) or '')) for k in ('incarnation','scopes_digest'))
            and all(type(value.get(k)) is int and 0 <= value[k] <= 9_007_199_254_740_991 for k in ('registry_revision','authority_generation'))
            and all(type(value.get(k)) is str and 1 <= len(value[k]) <= 512 for k in ('root_id','workspace_id')))

    def workspace_activity_fence(self, resource_id: str) -> dict[str, Any]:
        if type(resource_id) is not str or not 1 <= len(resource_id) <= 256: raise HistoryClientError('workspace unavailable')
        response=self._call({'WorkspaceActivityFence':{'envelope':self._control_envelope('workspace-activity-fence'),'resource_id':resource_id}})
        self._raise_if_unavailable(response); value=response.get('WorkspaceActivityFence')
        if not self._workspace_activity_proof(value) or set(value)!={'incarnation','scopes_digest','registry_revision','authority_generation','root_id','workspace_id'}: raise HistoryClientError('workspace unavailable')
        return value

    def workspace_activity_candidates(self, resource_id: str, *, cursor: Mapping[str, Any] | None = None, graph_activity: bool = False) -> dict[str, Any]:
        if type(resource_id) is not str or not 1 <= len(resource_id) <= 256: raise HistoryClientError('workspace unavailable')
        fields={'incarnation','scopes_digest','registry_revision','authority_generation'}
        if cursor is not None and (type(cursor) is not dict or set(cursor)!=fields|{'after'} or any(not re.fullmatch(r'[0-9a-f]{64}',str(cursor.get(k) or '')) for k in ('incarnation','scopes_digest')) or any(type(cursor.get(k)) is not int or not 0<=cursor[k]<=9_007_199_254_740_991 for k in ('registry_revision','authority_generation')) or type(cursor.get('after')) is not str or not 1<=len(cursor['after'])<=16384): raise HistoryClientError('workspace cursor unavailable')
        body={'envelope':self._control_envelope('workspace-activity'),'resource_id':resource_id}
        if graph_activity: body['graph_activity']=True
        if cursor: body.update(cursor)
        response=self._call({'ListWorkspaceActivity':body});self._raise_if_unavailable(response);page=response.get('WorkspaceActivity')
        if not self._workspace_activity_proof(page) or set(page)!=(fields|{'root_id','workspace_id','items','next'}) or type(page.get('items')) is not list or len(page['items'])>100 or (page.get('next') is not None and (type(page['next']) is not str or not 1<=len(page['next'])<=16384)): raise HistoryClientError('workspace unavailable')
        for item in page['items']:
            if (type(item) is not dict or set(item)-({'graph_witness'} if graph_activity else set())!={'position','resource','action_id','recorded_millis','state','availability','locator','registration'} or type(item['position']) is not str or not 1<=len(item['position'])<=16384 or type(item['action_id']) is not str or not 1<=len(item['action_id'])<=512 or type(item['recorded_millis']) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,19}',item['recorded_millis']) or int(item['recorded_millis'])>18446744073709551615 or type(item['state']) is not str or not re.fullmatch(r'[a-z]{1,64}',item['state']) or item['availability'] not in {'available','expiring','expired'} or (item['locator'] is not None and (type(item['locator']) is not str or not item['locator'].startswith(('oc-history-action:v1:','oc-history-action:v2:') if graph_activity else 'oc-history-action:v1:') or len(item['locator'])>4096)) or type(item['resource']) is not dict or set(item['resource'])!={'account_id','workspace_id','provider','resource_id'} or (item['registration'] is not None and type(item['registration']) is not dict)): raise HistoryClientError('workspace unavailable')
            witness=item.get('graph_witness')
            if witness is not None and (type(witness) is not dict or set(witness)!={'after_version_id','after_fingerprint'}
                or any(type(value) is not str or not 1<=len(value)<=256 or any(ord(c)<32 for c in value) for value in witness.values())):
                raise HistoryClientError('workspace activity witness unavailable')
        return page

    def list_managed_change_fragments(self, resource_id: str, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        if (not isinstance(resource_id,str) or not resource_id or len(resource_id)>256 or type(limit) is not int or not 1<=limit<=100
            or (cursor is not None and (type(cursor) is not str or len(cursor)>4096))):raise HistoryClientError('managed change page unavailable')
        response=self._call({'ListManagedChangeFragments':{'envelope':self._control_envelope('managed-change-fragments'),'resource_id':resource_id,'cursor':cursor,'limit':limit}})
        self._raise_if_unavailable(response);page=response.get('ManagedChangeFragments')
        if (type(page) is not dict or set(page)!={'version','scope','coverage','order','revision','groups','next_cursor'} or type(page['version']) is not int or page['version']!=1
            or page['scope']!='one_authorized_resource' or page['coverage']!='page_fragment' or page['order']!='action_id'
            or type(page['revision']) is not int or page['revision']<0 or type(page['groups']) is not list or len(page['groups'])>limit
            or (page['next_cursor'] is not None and (type(page['next_cursor']) is not str or len(page['next_cursor'])>4096))):raise HistoryClientError('managed change page unavailable')
        total=0
        for group in page['groups']:
            if (type(group) is not dict or set(group)!={'group_id','actor_id','actions'} or type(group['actions']) is not list or not group['actions']
                or (group['group_id'] is not None and (type(group['group_id']) is not str or not re.fullmatch(r'[0-9a-f]{64}',group['group_id'])))
                or (group['actor_id'] is not None and (type(group['actor_id']) is not str or not group['actor_id'] or len(group['actor_id'].encode())>256))):raise HistoryClientError('managed change page unavailable')
            total+=len(group['actions'])
            for item in group['actions']:
                if (type(item) is not dict or set(item)!={'change','origin','locator'} or type(item['change']) is not dict
                    or (item['origin'] is not None and type(item['origin']) is not dict)
                    or (item['locator'] is not None and (type(item['locator']) is not str or not item['locator'].startswith('oc-history-action:v1:') or len(item['locator'])>4096))):raise HistoryClientError('managed change page unavailable')
        if total>limit:raise HistoryClientError('managed change page unavailable')
        return page

    def resolve_resource_version(self, resource_id: str, version_ref: str) -> dict[str, Any]:
        """Resolve a selected opaque version to the existing RestoreHost receipt."""
        resource_id = str(resource_id or "").strip()
        version_ref = str(version_ref or "").strip()
        if not resource_id or not version_ref or len(version_ref) > 1024:
            raise HistoryClientError("history resource and version identities are required")
        response = self._call(
            {
                "ResolveResourceVersion": {
                    "envelope": self._control_envelope("history-resource-version-restore"),
                    "resource_id": resource_id,
                    "version_ref": version_ref,
                }
            }
        )
        self._raise_if_unavailable(response)
        selection = response.get("ResourceVersionSelection")
        if not isinstance(selection, Mapping):
            raise HistoryClientError("history worker returned an invalid restore selection")
        resource = selection.get("resource")
        version = selection.get("version")
        receipt = selection.get("receipt")
        if not all(isinstance(value, Mapping) for value in (resource, version, receipt)):
            raise HistoryClientError("history worker returned an incomplete restore selection")
        return {
            "resource": dict(resource),
            "version": dict(version),
            "receipt": dict(receipt),
        }

    def digest_resource_version(self, resource_id: str, version_ref: str) -> dict[str, Any]:
        """Digest one exact retained version through History's bounded read owner."""
        if not isinstance(resource_id,str) or not resource_id.strip() or not isinstance(version_ref,str) or not version_ref.strip() or len(version_ref)>1024:
            raise HistoryClientError("history resource and version identities are required")
        response=self._call({"DigestResourceVersion":{"envelope":self._control_envelope("history-resource-version-read"),"resource_id":resource_id,"version_ref":version_ref}})
        self._raise_if_unavailable(response)
        value=response.get("ResourceVersionDigest")
        if (not isinstance(value,Mapping) or set(value)!={"version","sha256","size"} or not isinstance(value['version'],Mapping)
                or type(value['size']) is not int or not 0<=value['size']<=64*1024*1024
                or not isinstance(value['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['sha256'])):
            raise HistoryClientError("history digest unavailable")
        return {"version":dict(value['version']),"sha256":value['sha256'],"size":value['size']}

    def read_resource_version_chunk(
        self,
        resource_id: str,
        version_ref: str,
        *,
        offset: int = 0,
        length: int = 64 * 1024,
    ) -> dict[str, Any]:
        """Read a bounded chunk for the exact resource/version selection."""
        resource_id = str(resource_id or "").strip()
        version_ref = str(version_ref or "").strip()
        if not resource_id or not version_ref or len(version_ref) > 1024:
            raise HistoryClientError("history resource and version identities are required")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise HistoryClientError("history read offset must be non-negative")
        if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 256 * 1024:
            raise HistoryClientError("history read chunk must be between 1 and 256 KiB")
        response = self._call(
            {
                "ReadResourceVersionChunk": {
                    "envelope": self._control_envelope("history-resource-version-read"),
                    "resource_id": resource_id,
                    "version_ref": version_ref,
                    "offset": offset,
                    "length": length,
                }
            }
        )
        self._raise_if_unavailable(response)
        chunk = response.get("ResourceVersionChunk")
        if not isinstance(chunk, Mapping):
            raise HistoryClientError("history worker returned an invalid resource-version chunk")
        version = chunk.get("version")
        returned_offset = chunk.get("offset")
        if not isinstance(version, Mapping) or returned_offset != offset:
            raise HistoryClientError("history worker returned an out-of-order resource-version chunk")
        raw = chunk.get("content")
        content = None if raw is None else self._decode_bytes(raw)
        if content is not None and len(content) > length:
            raise HistoryClientError("history worker returned an oversized resource-version chunk")
        return {
            "version": dict(version),
            "offset": offset,
            "content": content,
            "eof": chunk.get("eof") is True,
        }

    def preview_resource_version_restore(
        self,
        resource_id: str,
        version_ref: str,
    ) -> dict[str, Any]:
        """Preview a version against the current private Files destination."""
        resource_id = str(resource_id or "").strip()
        version_ref = str(version_ref or "").strip()
        if not resource_id or not version_ref or len(version_ref) > 1024:
            raise HistoryClientError("history resource and version identities are required")
        response = self._call(
            {
                "PreviewResourceVersionRestore": {
                    "envelope": self._control_envelope("history-resource-version-restore-preview"),
                    "resource_id": resource_id,
                    "version_ref": version_ref,
                }
            }
        )
        self._raise_if_unavailable(response)
        preview = response.get("ResourceVersionRestorePreview")
        if not isinstance(preview, Mapping):
            raise HistoryClientError("history worker returned an invalid restore preview")
        resource = preview.get("resource")
        source = preview.get("source")
        destination = preview.get("destination")
        if not all(isinstance(value, Mapping) for value in (resource, source, destination)):
            raise HistoryClientError("history worker returned an incomplete restore preview")
        if (
            str(resource.get("resource_id") or "") != resource_id
            or not isinstance(destination.get("expected_fingerprint"), str)
            or not destination.get("expected_fingerprint")
            or not isinstance(destination.get("exists"), bool)
            or preview.get("effect") not in {"create", "replace"}
            or preview.get("requires_confirmation") is not True
            or preview.get("captures_current_destination") is not True
        ):
            raise HistoryClientError("history worker returned an invalid destination precondition")
        return dict(preview)

    @staticmethod
    def _regular_file_source_v1(source: Any) -> bool:
        if type(source) is not dict or len(source)!=1:return False
        key=next(iter(source));value=source[key]
        return (key in {'Selected','Inverse'} and type(value) is dict and set(value)==({'version_ref'} if key=='Selected' else {'restore_id'})
            and type(next(iter(value.values()))) is str and 1<=len(next(iter(value.values())))<=(2048 if key=='Selected' else 256))

    @staticmethod
    def _regular_file_state_v1(value: Any) -> bool:
        if type(value) is not dict:return False
        if value=={'state':'Absent'}:return True
        return (set(value)=={'state','sha256','size','mode'} and value.get('state')=='Present'
            and type(value.get('sha256')) is str and bool(re.fullmatch(r'[0-9a-f]{64}',value['sha256']))
            and type(value.get('size')) is int and 0<=value['size']<=16*1024*1024
            and type(value.get('mode')) is int and 0<=value['mode']<=0o777)

    @classmethod
    def _regular_file_observation_v1(cls, value: Any) -> bool:
        if type(value) is not dict or set(value)!={'state','parent_device','parent_inode','file_device','file_inode'} or not cls._regular_file_state_v1(value['state']):return False
        if any(type(value.get(k)) is not int or not 0<=value[k]<=2**64-1 for k in ('parent_device','parent_inode')):return False
        fields=(value['file_device'],value['file_inode'])
        return fields==(None,None) if value['state']['state']=='Absent' else all(type(v) is int and 0<=v<=2**64-1 for v in fields)

    def preview_regular_file_v1(self,resource_id:str,source:Mapping[str,Any])->dict[str,Any]:
        if type(resource_id) is not str or not 1<=len(resource_id)<=256 or not self._regular_file_source_v1(source):raise HistoryClientError('regular state unavailable')
        result=self._call({'PreviewRegularFileV1':{'envelope':self._control_envelope('regular-file-preview'),'resource_id':resource_id,'source':source}});self._raise_if_unavailable(result);v=result.get('RegularFilePreviewV1')
        if (type(v) is not dict or set(v)!={'version','consistency','source_action_id','source_version_id','desired','expected'} or v['version']!=1 or type(v['version']) is not int or v['consistency']!='optimistic_path_guard' or any(type(v.get(k)) is not str or not 1<=len(v[k])<=1024 for k in ('source_action_id','source_version_id')) or not self._regular_file_state_v1(v['desired']) or not self._regular_file_observation_v1(v['expected'])):raise HistoryClientError('regular state unavailable')
        return v

    def _regular_file_call_v1(self,kind:str,restore_id:str,resource_id:str,source:Mapping[str,Any],expected:Mapping[str,Any])->dict[str,Any]:
        if kind not in {'ExecuteRegularFileV1','GetRegularFileStatusV1'} or type(restore_id) is not str or not 1<=len(restore_id)<=256 or type(resource_id) is not str or not 1<=len(resource_id)<=256 or not self._regular_file_source_v1(source) or not self._regular_file_observation_v1(expected):raise HistoryClientError('regular state unavailable')
        response=self._call({kind:{'envelope':self._control_envelope(restore_id),'restore_id':restore_id,'resource_id':resource_id,'source':source,'expected':expected}});self._raise_if_unavailable(response);v=response.get('RegularFileStatusV1')
        if type(v) is not dict or set(v)!={'state','verified','current_matches','consistency'} or v['state'] not in {'complete','pending','unknown','conflict'} or type(v['verified']) is not bool or (v['current_matches'] is not None and type(v['current_matches']) is not bool) or v['consistency']!='optimistic_path_guard' or (v['verified'] and v['state']!='complete'):raise HistoryClientError('regular state unavailable')
        return v

    def execute_regular_file_v1(self,restore_id:str,resource_id:str,source:Mapping[str,Any],expected:Mapping[str,Any])->dict[str,Any]:
        return self._regular_file_call_v1('ExecuteRegularFileV1',restore_id,resource_id,source,expected)

    def regular_file_status_v1(self,restore_id:str,resource_id:str,source:Mapping[str,Any],expected:Mapping[str,Any])->dict[str,Any]:
        return self._regular_file_call_v1('GetRegularFileStatusV1',restore_id,resource_id,source,expected)

    def get_restore_status(self, restore_id: str, resource_id: str, source_action_id: str,
                           source_version_id: str, expected_destination_fingerprint: str) -> dict[str, Any]:
        """Read the exact original journal; absence is unknown and never a retry grant."""
        values = (restore_id, resource_id, source_action_id, source_version_id, expected_destination_fingerprint)
        if (any(type(value) is not str or not value or len(value.encode('utf-8', 'strict')) > 1024 for value in values)
                or len(restore_id.encode('utf-8', 'strict')) > 256):
            raise HistoryClientError('restore status identity is unavailable')
        response = self._call({'GetRestoreStatus': {
            'envelope': self._control_envelope(restore_id), 'restore_id': restore_id,
            'resource_id': resource_id, 'source_action_id': source_action_id,
            'source_version_id': source_version_id,
            'expected_destination_fingerprint': expected_destination_fingerprint,
        }})
        self._raise_if_unavailable(response)
        status = response.get('RestoreStatus')
        if (type(status) is not dict or set(status) != {'state', 'outcome', 'verified'}
                or status['state'] not in ('unknown', 'pending', 'complete', 'conflict', 'partial')
                or status['outcome'] not in (None, 'Complete', 'Conflict', 'Partial', 'Missing', 'Corrupt',
                                              'Expired', 'MetadataExpired', 'UnknownVersion', 'Unauthorized')
                or type(status['verified']) is not bool
                or (status['state'] == 'complete' and status['outcome'] != 'Complete')):
            raise HistoryClientError('restore status is unavailable')
        return dict(status)

    def restore_resource_version(
        self,
        restore_id: str,
        resource_id: str,
        version_ref: str,
        *,
        expected_destination_fingerprint: str,
    ) -> dict[str, Any]:
        """Confirm an opaque-resource restore using the fingerprint just reviewed."""
        restore_id = str(restore_id or "").strip()
        resource_id = str(resource_id or "").strip()
        version_ref = str(version_ref or "").strip()
        expected_destination_fingerprint = str(expected_destination_fingerprint or "")
        if not restore_id or len(restore_id) > 256:
            raise HistoryClientError("history restore id is required")
        if not resource_id or not version_ref or len(version_ref) > 1024:
            raise HistoryClientError("history resource and version identities are required")
        if not expected_destination_fingerprint or len(expected_destination_fingerprint) > 256:
            raise HistoryClientError("history restore requires the reviewed destination fingerprint")
        return self._call(
            {
                "RestoreResourceVersion": {
                    "envelope": self._control_envelope(restore_id),
                    "restore_id": restore_id,
                    "resource_id": resource_id,
                    "version_ref": version_ref,
                    "expected_destination_fingerprint": expected_destination_fingerprint,
                }
            }
        )

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

    def list_registered_resources(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List private account-owned registry candidates for the Files adapter.

        This local IPC result includes paths and must stay inside trusted
        server code. HTTP routes project matches back to the public Files ID.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HistoryClientError("registered resource page size must be between 1 and 100")
        if cursor is not None and (
            not isinstance(cursor, str)
            or len(cursor) > 20
            or not cursor.isdecimal()
        ):
            raise HistoryClientError("registered resource cursor is invalid")
        response = self._call(
            {
                "ListRegisteredResources": {
                    "envelope": self._control_envelope("files-history-identity"),
                    "cursor": cursor,
                    "limit": limit,
                }
            }
        )
        self._raise_if_unavailable(response)
        page = response.get("RegisteredResources")
        if not isinstance(page, Mapping) or not isinstance(page.get("items"), list):
            raise HistoryClientError("history worker returned an invalid registered-resource page")
        items = page["items"]
        if len(items) > limit or not all(isinstance(item, Mapping) for item in items):
            raise HistoryClientError("history worker returned an unbounded registered-resource page")
        for item in items:
            for field_name in ("resource_id", "account_id", "workspace_id", "root_path", "relative_path"):
                value = item.get(field_name)
                if not isinstance(value, str) or len(value) > 4096:
                    raise HistoryClientError("history worker returned an invalid registered-resource identity")
            if not isinstance(item.get("active"), bool):
                raise HistoryClientError("history worker returned an invalid registered-resource state")
        next_cursor = page.get("next_cursor")
        if next_cursor is not None and (
            not isinstance(next_cursor, str)
            or len(next_cursor) > 20
            or not next_cursor.isdecimal()
        ):
            raise HistoryClientError("history worker returned an invalid registered-resource cursor")
        return {"items": [dict(item) for item in items], "next_cursor": next_cursor}

    def put_memery_component(self, change, digest, content, policy_revision, resource, relative_path):
        if len(content) > 64 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != digest:
            raise HistoryClientError("Memery component changed")
        action = f"memery-component-{change}-{digest}"
        stage = self._stage_content(action, content, f"sha256:{digest}:{len(content)}")
        try:
            self._call({"PutMemeryComponent": {"envelope": self._control_envelope(action),
                "change": change, "digest": digest, "size": len(content), "upload_id": stage,
                "policy_revision": policy_revision, "resource": resource, "relative_path": relative_path}})
        finally:
            self._abort_staged(action, stage)

    def memery_component_location(self, change, digest, resource):
        return self._call({"InspectMemeryComponent": {"envelope": self._control_envelope("memery-history"),
                          "change": change, "digest": digest, "resource": resource}})["MemeryComponentLocation"]

    def read_memery_component_part(self, change, digest, resource, part):
        raw = self._call({"ReadMemeryComponent": {"envelope": self._control_envelope("memery-history"),
                         "change": change, "digest": digest, "resource": resource, "part": part}})["Bytes"]
        if raw is None:
            return None
        data = self._decode_bytes(raw)
        if len(data) > 256 * 1024:
            raise HistoryClientError("Memery component part exceeds limit")
        return data

    def sweep_memery_components(self):
        self._call({"SweepMemeryComponents": self._control_envelope("memery-history")})

    def get_memery_manifest(self, *, assets=False, guidance=False) -> dict[str, Any]:
        return self._call({"GetMemeryManifest": {"envelope": self._control_envelope("memery-history"), "assets": "guidance" if guidance else assets}})["MemeryManifest"]

    def settle_memery_manifest(self, generation, position, witness, add, remove, *, assets=False, checkpoint=False, guidance=False):
        return self._call({"SettleMemeryManifest": {
            "envelope": self._control_envelope("memery-history"), "generation": generation,
            "position": position, "witness": witness, "add": add, "remove": remove,
            "assets": "guidance" if guidance else assets, "checkpoint": checkpoint,
        }})["MemeryManifest"]

    def get_policy(self) -> dict[str, Any]:
        return self._call({"GetPolicy": self._control_envelope("settings")})

    def get_capture_policy(self) -> dict[str, Any]:
        result = self._call({"GetCapturePolicy": self._control_envelope("capture-settings")})["CapturePolicy"]
        return {**result["capture"], "revision": result["revision"]}

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
