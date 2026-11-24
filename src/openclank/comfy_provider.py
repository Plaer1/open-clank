"""Private resolution of an existing owner-scoped ComfyUI job connection.

This does not register a provider, issue HTTP, or grant a Files destination.
The caller retains the admitted context through its guarded job operation.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

INTERFACE = 'comfyui-workflow-v1'


class ComfyProfileUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ComfyJobProfile:
    ownerAccountID: str
    connectionID: str
    connectionRevision: int
    interface: str
    credentialMode: str
    origin: str = field(repr=False)

    def job_binding(self) -> dict:
        return {'ownerAccountID':self.ownerAccountID, 'connectionID':self.connectionID,
                'connectionRevision':self.connectionRevision, 'interface':self.interface}


def _current(*, store, auth_manager, owner: str, account_id: str, connection_id: str, revision: int) -> ComfyJobProfile:
    if (not isinstance(owner, str) or not owner or not isinstance(account_id, str) or not account_id
            or not isinstance(connection_id, str) or not connection_id or type(revision) is not int or revision < 1):
        raise ComfyProfileUnavailable('profile unavailable')
    if auth_manager.account_id(owner) != account_id or auth_manager.username_for_account_id(account_id) != owner:
        raise ComfyProfileUnavailable('profile unavailable')
    try:
        row = store.get_connection(owner=owner, connection_id=connection_id)
    except Exception as exc:
        raise ComfyProfileUnavailable('profile unavailable') from exc
    if (row.owner != owner or row.id != connection_id or row.revision != revision or not row.enabled or row.deleted_at is not None
            or row.family_id != 'comfyui' or row.adapter_id != INTERFACE or row.kind != 'local' or row.billing_lane != 'local'
            or row.settings != {'interface':INTERFACE}):
        raise ComfyProfileUnavailable('profile unavailable')
    raw = row.normalized_url
    if not isinstance(raw, str) or len(raw) > 2048: raise ComfyProfileUnavailable('profile unavailable')
    try:
        parsed = urlsplit(raw)
        port = parsed.port  # Also rejects a nonnumeric or out-of-range port.
        if (parsed.scheme not in {'http','https'} or parsed.hostname not in {'127.0.0.1','::1'} or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {'','/'}
                or parsed.netloc.endswith(':') or (parsed.scheme == 'http' and port == 80) or (parsed.scheme == 'https' and port == 443)
                or raw != f'{parsed.scheme}://{parsed.netloc}' or parsed.netloc.lower() != parsed.netloc):
            raise ValueError('invalid origin')
    except ValueError as exc:
        raise ComfyProfileUnavailable('profile unavailable') from exc
    if auth_manager.account_id(owner) != account_id or auth_manager.username_for_account_id(account_id) != owner:
        raise ComfyProfileUnavailable('profile unavailable')
    return ComfyJobProfile(account_id, connection_id, revision, INTERFACE, 'none', raw)


@asynccontextmanager
async def admitted_comfy_profile(*, store, auth_manager, account_barrier, owner: str, ownerAccountID: str,
                                 connectionID: str, connectionRevision: int, interface: str):
    if interface != INTERFACE or account_barrier is None: raise ComfyProfileUnavailable('profile unavailable')
    async with account_barrier.admitted(owner):
        args = dict(store=store, auth_manager=auth_manager, owner=owner, account_id=ownerAccountID,
                    connection_id=connectionID, revision=connectionRevision)
        profile = await asyncio.to_thread(_current, **args)
        async def assert_current() -> None:
            if await asyncio.to_thread(_current, **args) != profile: raise ComfyProfileUnavailable('profile changed')
        yield profile, assert_current
