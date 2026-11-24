"""Original named-human account admission for private provider policy work."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import HTTPException
from src.auth_helpers import is_internal_tool_identity, require_user
from src.openclank.account_request_barrier import _Admission, _current_admission

_SEAL = object()
_SELECTION_SEAL = object()


@dataclass(frozen=True)
class HumanProviderPolicyAdmission:
    owner: str
    account_id: str
    _barrier: Any = field(repr=False, compare=False)
    _admission: Any = field(repr=False, compare=False)
    _store: Any = field(repr=False, compare=False)
    _current: Callable[[], bool] = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def assert_current(self):
        if self._seal is not _SEAL or self._current() is not True:
            raise ValueError('human provider policy authority changed')


class HumanPolicyCancelled(asyncio.CancelledError):
    """Owned worker drained after cancellation; preserve its actual outcome."""
    def __init__(self, *, returned, result=None, error=None):
        super().__init__('human provider policy request cancelled')
        self.returned, self.result, self.error = returned, result, error


@dataclass(frozen=True)
class CapturedProviderPolicy:
    """Private original-human selection; it grants no model or tool access."""
    owner: str
    account_id: str
    chat_id: str
    route_id: str
    route_revision: int
    connection_id: str
    connection_revision: int
    model_id: str
    catalog_revision: int
    candidate_json: str
    policy_json: str
    alternatives_json: str
    _admission: HumanProviderPolicyAdmission = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def assert_current(self):
        if self._seal is not _SELECTION_SEAL or type(self._admission) is not HumanProviderPolicyAdmission:
            raise ValueError('captured provider policy unavailable')
        self._admission.assert_current()


def _captured_provider_policy(admission, **values):
    if type(admission) is not HumanProviderPolicyAdmission:
        raise ValueError('captured provider policy unavailable')
    admission.assert_current()
    return CapturedProviderPolicy(_admission=admission, _seal=_SELECTION_SEAL, **values)


_INTENT_SEAL = object()


@dataclass(frozen=True)
class CapturedProviderPolicyIntent:
    """Original human foreground determination; not an account or model grant."""
    admission: HumanProviderPolicyAdmission = field(repr=False, compare=False)
    selection: CapturedProviderPolicy | None = field(repr=False, compare=False)
    state: str
    _seal: object = field(repr=False, compare=False)

    def assert_current(self):
        if self._seal is not _INTENT_SEAL or self.state not in ('selected','off'):
            raise ValueError('captured provider policy intent unavailable')
        self.admission.assert_current()
        if self.state == 'selected':
            if type(self.selection) is not CapturedProviderPolicy or self.selection._admission is not self.admission:
                raise ValueError('captured provider policy intent changed')
            self.selection.assert_current()
        elif self.selection is not None:
            raise ValueError('captured provider policy intent changed')


def _captured_provider_policy_intent(admission, selection):
    if type(admission) is not HumanProviderPolicyAdmission:
        raise ValueError('captured provider policy intent unavailable')
    intent=CapturedProviderPolicyIntent(admission,selection,'selected' if selection is not None else 'off',_INTENT_SEAL)
    intent.assert_current()
    return intent


def capture_human_provider_policy(request, store):
    """Freeze the original authenticated request before its first await."""
    state = request.state
    if (not getattr(state, 'authenticated', False) or getattr(state, 'api_token', False)
            or getattr(state, 'internal_tool_authenticated', False)):
        raise HTTPException(403, 'A named human browser session is required')
    owner = str(require_user(request) or '').strip().lower()
    if not owner or is_internal_tool_identity(owner):
        raise HTTPException(403, 'A named human browser session is required')
    app_state = request.app.state
    auth = getattr(app_state, 'auth_manager', None)
    barrier = getattr(app_state, 'account_request_barrier', None)
    admission = _current_admission.get()
    if auth is None or barrier is None or type(admission) is not _Admission or admission.owner != owner:
        raise HTTPException(503, 'Provider policy admission unavailable')
    try:
        account = auth.account_id(owner)
        if not account or auth.username_for_account_id(account) != owner:
            raise ValueError('account unavailable')
    except Exception:
        raise HTTPException(403, 'Provider policy account unavailable') from None
    factory = getattr(app_state, 'capture_original_credential', None)
    if not callable(factory):
        raise HTTPException(503, 'Provider policy credential unavailable')
    try:
        credential_current = factory(request, owner, account)
        if not callable(credential_current) or credential_current() is not True:
            raise ValueError('credential unavailable')
    except Exception:
        raise HTTPException(403, 'Provider policy credential unavailable') from None
    def current():
        try:
            return (getattr(app_state, 'capture_original_credential', None) is factory
                and credential_current() is True
                and getattr(app_state, 'auth_manager', None) is auth
                and getattr(app_state, 'account_request_barrier', None) is barrier
                and getattr(app_state, 'provider_store', None) is store
                and getattr(state, 'authenticated', False) is True
                and not getattr(state, 'api_token', False) and not getattr(state, 'internal_tool_authenticated', False)
                and str(getattr(state, 'current_user', '') or '').strip().lower() == owner
                and not admission.released and auth.account_id(owner) == account
                and auth.username_for_account_id(account) == owner)
        except Exception:
            return False
    result = HumanProviderPolicyAdmission(owner, account, barrier, admission, store, current, _SEAL)
    try: result.assert_current()
    except ValueError: raise HTTPException(503, 'Provider policy admission unavailable') from None
    return result


async def run_human_provider_policy(admission, work):
    if type(admission) is not HumanProviderPolicyAdmission or not callable(work):
        raise ValueError('human provider policy authority unavailable')
    if not await admission._barrier.is_live(admission.owner, admission._admission):
        raise ValueError('human provider policy admission unavailable')
    admission.assert_current()
    task = asyncio.create_task(asyncio.to_thread(work, admission))
    cancelled = False
    while not task.done():
        try: await asyncio.shield(task)
        except asyncio.CancelledError: cancelled = True
        except Exception: break
    try: result = task.result()
    except BaseException as error:
        if cancelled: raise HumanPolicyCancelled(returned=False, error=error) from error
        raise
    if cancelled: raise HumanPolicyCancelled(returned=True, result=result)
    return result
