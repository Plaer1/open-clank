"""Same-origin theme artwork and local acquisition controls."""
import asyncio
import sqlite3
import sys
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt
from src.openclank.theme_emoji_assets import get_google_catalog, get_kitchen_catalog_text, get_packaged_asset, artwork_install_hint, packaged_artwork_ready
from src.openclank.files_principal import files_principal
from src.openclank.theme_emoji_local import ThemeEmojiLocalState, ThemeEmojiStateError
from src.openclank.theme_emoji_inventory import EmojiInventory, source_descriptor_ids
from src.openclank.theme_emoji_acquisition import ThemeEmojiAcquisition, InstallationLease, THEME_EMOJI_ACQUISITION_READY

router = APIRouter(prefix="/api/theme-emoji", tags=["theme-emoji"])
_CATALOG_HEADERS = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error)

def _artwork_ready():
    try:
        if packaged_artwork_ready():
            return True
    except _ERRORS:
        pass
    try:
        return EmojiInventory(_local_state.root / 'inventory.sqlite3').has_ready()
    except (KeyError, *_ERRORS):
        return False

_local_state = ThemeEmojiLocalState(artwork_probe=_artwork_ready)

class _Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pattern: str
    expected_account_id: str

class _Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    selection_id: str
    expected_revision: StrictInt
    accepted: StrictBool
    expected_account_id: str

class _Updates(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt
    expected_account_id: str
    enabled: StrictBool

class _Revoke(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt
    expected_account_id: str

def _owner(request: Request, expected_account_id: str) -> str:
    owner = files_principal(request)[0]
    if not expected_account_id or len(expected_account_id) > 256 or owner != expected_account_id:
        raise HTTPException(status_code=409, detail='Account changed')
    return owner

def _status(account: str) -> dict:
    value = _local_state.status(account)
    value['acquisition_available'] = THEME_EMOJI_ACQUISITION_READY
    value['catalogue_generation'] = None
    if value['consent'] == 'accepted':
        try:
            value['catalogue_generation'] = EmojiInventory(_local_state.root / 'inventory.sqlite3').catalogue_generation()
        except _ERRORS:
            pass
    return value

def _inventory_statistics(account: str, consent: str) -> dict:
    empty = {'catalogueGeneration':None, 'expected':0, 'readyCount':0,
             'pendingCount':0, 'failedCount':0, 'bytes':0,
             'bySource':{name:{'expected':0,'readyCount':0,'pendingCount':0,'failedCount':0,'bytes':0}
                         for name in source_descriptor_ids()}}
    if consent != 'accepted':
        return empty
    try:
        value = EmojiInventory(_local_state.root / 'inventory.sqlite3').statistics()
        if value['expected'] == 0:
            value['catalogueGeneration'] = None
        return value
    except _ERRORS:
        return empty

@router.get('/local/status')
async def local_status(request: Request, expected_account_id: str = Query(min_length=1, max_length=256),
                       include_inventory: bool = True):
    try:
        account = _owner(request, expected_account_id)
        _, username, _ = files_principal(request)
        value = await asyncio.to_thread(_status, account)
        if include_inventory:
            value.update(await asyncio.to_thread(_inventory_statistics, account, value['consent']))
        scheduler = getattr(request.app.state, 'task_scheduler', None)
        if scheduler is not None:
            value.update(await asyncio.to_thread(scheduler.theme_emoji_obligation_status,
                username=username, account_id=account, local_state=_local_state))
        else:
            value.update({'next_due':None, 'updates_enabled':False, 'last_check':None, 'last_error':False, 'last_warning':False, 'runID':None, 'cancellable':False, 'acquisition_active':False})
        return JSONResponse(value, headers={'Cache-Control':'no-store'})
    except (ThemeEmojiStateError, ValueError) as exc:
        raise HTTPException(status_code=503, detail='Local theme state is unavailable') from exc

@router.post('/local/selection')
async def local_selection(body: _Selection, request: Request):
    try:
        account = _owner(request, body.expected_account_id)
        value = await asyncio.to_thread(_local_state.begin_selection, account, body.pattern)
        value['acquisition_available'] = THEME_EMOJI_ACQUISITION_READY
        if value['consent'] == 'accepted':
            value.update(await _reconcile(request, account, start_revision=value['consent_revision']))
        return JSONResponse(value, headers={'Cache-Control':'no-store'})
    except ThemeEmojiStateError as exc:
        raise HTTPException(status_code=409, detail='Local theme selection is unavailable') from exc

async def _reconcile(request: Request, account_id: str, *, start_revision: int | None = None):
    actual, username, _ = files_principal(request)
    if actual != account_id:
        raise HTTPException(status_code=409, detail='Account changed')
    scheduler = getattr(request.app.state, 'task_scheduler', None)
    if scheduler is None:
        return {'acquisition_available': False, 'next_due': None, 'acquisition_active': False}
    barrier = getattr(request.app.state, 'account_request_barrier', None)
    auth = getattr(request.app.state, 'auth_manager', None)
    if barrier is None or auth is None:
        raise HTTPException(status_code=503, detail='Emoji updates unavailable')
    # Explicit reconciliation is safe
    # after a lost response and must never run as a status-read side effect.
    async with barrier.admitted(username):
        if auth.account_id(username) != account_id or auth.username_for_account_id(account_id) != username:
            raise HTTPException(status_code=409, detail='Account changed')
        return await scheduler.reconcile_theme_emoji_obligation(
            username=username, account_id=account_id, local_state=_local_state, start_revision=start_revision)

@router.post('/local/decision')
async def local_decision(body: _Decision, request: Request):
    try:
        account = _owner(request, body.expected_account_id)
        result = await asyncio.to_thread(_local_state.decide, account, body.selection_id, body.expected_revision, body.accepted)
        start_revision = result['consent_revision'] if body.accepted else None
        linked = await _reconcile(request, account, start_revision=start_revision)
        return JSONResponse({**(await asyncio.to_thread(_status, account)), **linked}, headers={'Cache-Control':'no-store'})
    except ThemeEmojiStateError as exc:
        raise HTTPException(status_code=409, detail='Local theme consent changed') from exc

@router.post('/local/revoke')
async def local_revoke(body: _Revoke, request: Request):
    try:
        account = _owner(request, body.expected_account_id)
        _local_state.revoke(account, body.expected_revision)
        result = _status(account)
        return JSONResponse({**result, **(await _reconcile(request, account))}, headers={'Cache-Control':'no-store'})
    except ThemeEmojiStateError as exc:
        raise HTTPException(status_code=409, detail='Local theme consent changed') from exc

@router.post('/local/delete')
async def local_delete(body: _Revoke, request: Request):
    account = _owner(request, body.expected_account_id)
    _, username, _ = files_principal(request)
    barrier = getattr(request.app.state, 'account_request_barrier', None)
    auth = getattr(request.app.state, 'auth_manager', None)
    if barrier is None or auth is None:
        raise HTTPException(status_code=503, detail='Emoji artwork deletion unavailable')
    try:
        async with barrier.admitted(username):
            if auth.account_id(username) != account or auth.username_for_account_id(account) != username:
                raise HTTPException(status_code=409, detail='Account changed')
            state = _local_state.obligation(account)
            if state['consent_revision'] != body.expected_revision or state['consent'] not in {'revoked','declined'}:
                raise HTTPException(status_code=409, detail='Disable emoji artwork consent first')
            scheduler = getattr(request.app.state, 'task_scheduler', None)
            if scheduler is None:
                raise HTTPException(status_code=503, detail='Emoji update state unavailable')
            await scheduler.reconcile_theme_emoji_obligation(
                username=username, account_id=account, local_state=_local_state)
            async with InstallationLease(_local_state):
                task = asyncio.create_task(asyncio.to_thread(_local_state.delete_overlay, account))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try: await asyncio.shield(task)
                        except asyncio.CancelledError: continue
                        except Exception: break
                    if not task.cancelled(): task.exception()
                    raise
                return JSONResponse(result, headers={'Cache-Control':'no-store'})
    except (ThemeEmojiStateError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=409, detail='Shared artwork is in use or deletion requires review') from exc

@router.post('/local/updates')
async def local_updates(body: _Updates, request: Request):
    account = _owner(request, body.expected_account_id)
    _, username, _ = files_principal(request)
    scheduler = getattr(request.app.state, 'task_scheduler', None)
    barrier = getattr(request.app.state, 'account_request_barrier', None)
    if scheduler is None or barrier is None:
        raise HTTPException(status_code=503, detail='Emoji updates unavailable')
    async with barrier.admitted(username):
        try:
            linked = await scheduler.set_theme_emoji_updates(username=username, account_id=account,
                local_state=_local_state, expected_revision=body.expected_revision, enabled=body.enabled)
            value = _status(account)
            value.update(linked)
            value.update(await asyncio.to_thread(_inventory_statistics, account, value['consent']))
            return JSONResponse(value, headers={'Cache-Control':'no-store'})
        except ValueError as exc:
            raise HTTPException(status_code=409, detail='Emoji update change unavailable') from exc

@router.post('/local/cancel')
async def local_cancel(body: _Revoke, request: Request):
    account = _owner(request, body.expected_account_id)
    _, username, _ = files_principal(request)
    scheduler = getattr(request.app.state, 'task_scheduler', None)
    barrier = getattr(request.app.state, 'account_request_barrier', None)
    if scheduler is None or barrier is None:
        raise HTTPException(status_code=503, detail='Emoji updates unavailable')
    async with barrier.admitted(username):
        state = _local_state.obligation(account)
        if state['consent'] != 'accepted' or state['consent_revision'] != body.expected_revision:
            raise HTTPException(status_code=409, detail='Local theme consent changed')
        try:
            task_id = await asyncio.to_thread(scheduler.theme_emoji_link, username=username, account_id=account, local_state=_local_state)
            await scheduler.stop_task(task_id)
            value = _status(account)
            value.update(await asyncio.to_thread(scheduler.theme_emoji_obligation_status,
                username=username, account_id=account, local_state=_local_state))
            value.update(await asyncio.to_thread(_inventory_statistics, account, value['consent']))
            return JSONResponse(value, headers={'Cache-Control':'no-store'})
        except ValueError as exc:
            raise HTTPException(status_code=409, detail='Emoji update unavailable') from exc

@router.post('/local/reconcile')
async def local_reconcile(body: _Revoke, request: Request):
    account = _owner(request, body.expected_account_id)
    state = _status(account)
    if state['consent_revision'] != body.expected_revision:
        raise HTTPException(status_code=409, detail='Local theme consent changed')
    return JSONResponse({**state, **(await _reconcile(request, account))}, headers={'Cache-Control':'no-store'})


async def managed_theme_emoji_call(*, owner: str, account_id: str, operation: str, auth_manager, account_barrier, scheduler=None, reservation=None) -> dict:
    """App-owned C2 adapter; model arguments never choose owner or consent."""
    if operation not in {'status', 'discover', 'download', 'cancel'}:
        raise ValueError('Invalid emoji operation')
    if not owner or not account_id or auth_manager is None or account_barrier is None:
        raise PermissionError('Emoji authority unavailable')
    async with account_barrier.admitted(owner):
        if (auth_manager.account_id(owner) != account_id
                or auth_manager.username_for_account_id(account_id) != owner):
            raise PermissionError('Emoji authority changed')
        status = _local_state.status(account_id)
        metrics = await asyncio.to_thread(_inventory_statistics, account_id, status['consent'])
        linked = await asyncio.to_thread(scheduler.theme_emoji_obligation_status,
            username=owner, account_id=account_id, local_state=_local_state) if scheduler is not None else {}
        result = {'operation':operation, 'available':THEME_EMOJI_ACQUISITION_READY if operation == 'status' else False,
                  'consent':status['consent'], 'ready':status['ready'], **metrics,
                  'runID':linked.get('runID'), 'cancellable':bool(linked.get('cancellable')),
                  'nextDue':linked.get('next_due')}
        if operation == 'status':
            if linked.get('last_error'):
                result['message'] = 'The last artwork update failed.'
            elif linked.get('last_warning'):
                result['message'] = 'Some source metadata could not be refreshed.'
            return result
        if not THEME_EMOJI_ACQUISITION_READY:
            result['message'] = 'Emoji acquisition is unavailable'
            return result
        if scheduler is None or reservation is None:
            raise PermissionError('Emoji operation authority unavailable')
        reservation.assert_committed(owner, account_id, operation)
        task_id = await asyncio.to_thread(scheduler.theme_emoji_link, username=owner, account_id=account_id, local_state=_local_state)
        # The scheduler checks owner lifecycle and deduplicates the task. The
        # task itself rechecks account, consent and installation lease again.
        if operation == 'cancel':
            changed = await scheduler.stop_task(task_id)
            result['message'] = 'Cancellation requested' if changed else 'No artwork update is running'
        else:
            changed = await scheduler.run_task_now(task_id)
            result['message'] = 'Artwork update started' if changed else 'Artwork update is already running or unavailable'
        result['available'] = True
        return result



def _unavailable_detail() -> str:
    message = "Packaged emoji artwork is unavailable"
    if getattr(sys, "frozen", False):
        message += ". " + artwork_install_hint()
    return message


async def _overlay_read(request: Request, kind: str, asset_id: str | None = None):
    """Overlay is visible only to a currently authorized consenting owner."""
    try:
        account, owner, _ = files_principal(request)
        state = getattr(request.app, 'state', None)
        auth = getattr(state, 'auth_manager', None)
        barrier = getattr(state, 'account_request_barrier', None)
        if auth is None or barrier is None:
            return None
        expected = _local_state.obligation(account)
        if expected['consent'] != 'accepted':
            return None
        service = ThemeEmojiAcquisition(inventory=EmojiInventory(_local_state.root / 'inventory.sqlite3'),
            local_state=_local_state, auth_manager=auth, account_barrier=barrier)
        return await service.read_overlay(owner=owner, account_id=account, expected=expected,
                                          kind=kind, render_id=asset_id)
    except (HTTPException, PermissionError, KeyError, *_ERRORS):
        return None

@router.get("/kitchen/catalog", response_class=PlainTextResponse)
async def kitchen_catalog(request: Request):
    try:
        packaged = (await asyncio.to_thread(get_kitchen_catalog_text)).splitlines()
    except _ERRORS:
        packaged = []
    overlay_result = await _overlay_read(request, 'k')
    overlay = overlay_result or []
    values = sorted(set(packaged) | set(overlay))
    if not values and overlay_result is None:
        raise HTTPException(status_code=503, detail=_unavailable_detail())
    return PlainTextResponse(('\n'.join(values) + '\n') if values else '', headers={'Cache-Control':'private, no-cache', 'X-Content-Type-Options':'nosniff'})

@router.get("/google/catalog")
async def google_catalog(request: Request):
    try:
        packaged = await asyncio.to_thread(get_google_catalog)
    except _ERRORS:
        packaged = None
    overlay_result = await _overlay_read(request, 'g')
    overlay = overlay_result or []
    entries = {item['id']:item for item in (packaged or {}).get('entries', [])}
    entries.update({identity:{'id':identity} for identity in overlay if identity not in entries})
    if not entries and not packaged and overlay_result is None:
        raise HTTPException(status_code=503, detail=_unavailable_detail())
    result = dict(packaged or {'local':True, 'complete':False, 'availableKitchen':0, 'expectedKitchen':147000, 'unavailableKitchen':147000})
    result['entries'] = list(entries.values())
    return JSONResponse(result, headers={'Cache-Control':'private, no-cache', 'X-Content-Type-Options':'nosniff'})

async def _image(asset_id: str, kind: str, request: Request):
    overlay = await _overlay_read(request, 'k' if kind == 'kitchen' else 'g', asset_id)
    if overlay is not None:
        data, media_type, digest = overlay
        cache = 'private, no-cache'
    else:
        try:
            data, media_type, digest = await asyncio.to_thread(get_packaged_asset, asset_id, kind)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail='Emoji image not found') from exc
        except _ERRORS as exc:
            raise HTTPException(status_code=503, detail=_unavailable_detail()) from exc
        cache = 'private, max-age=86400, immutable'
    headers = {'Cache-Control':cache, 'ETag':'"' + digest + '"', 'X-Content-Type-Options':'nosniff'}
    if request.headers.get('if-none-match') == headers['ETag']:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type=media_type, headers=headers)

@router.get("/kitchen/{asset_id}")
async def kitchen_asset(asset_id: str, request: Request):
    return await _image(asset_id, 'kitchen', request)

@router.get("/google/{asset_id}")
async def google_asset(asset_id: str, request: Request):
    return await _image(asset_id, 'google', request)
