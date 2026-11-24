"""Ingestion of exact retained source revisions through the existing native owner."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from pathlib import Path

_MAX_BYTES = 256 * 1024
_MAX_STEPS = 22
_HASH = re.compile(r"[0-9a-f]{64}")
_TEXT_ASSETS = frozenset({".txt", ".md", ".markdown", ".csv", ".tsv", ".rst"})
_MAX_PDF_ASSET_BYTES = 8 * 1024 * 1024


def _current(check):
    try:
        return check() is True
    except (ValueError, OSError):
        return False


async def _advance(provider, *, owner, account, source, revision, text, page_ends, current, refresh, deadline):
    issue = getattr(provider, "_host_document_structure", None)
    if not callable(issue) or not _current(current):
        return {"state":"unavailable"}
    raw = text.encode()
    if not raw or len(raw) > _MAX_BYTES or not text.strip():
        return {"state":"unsupported"}
    request = {"action":"advance", "account_id":account, "source":source, "revision":revision,
               "content_sha256":hashlib.sha256(raw).hexdigest(), "text":text, "page_ends":list(page_ends)}
    last = {"state":"pending"}
    for _ in range(_MAX_STEPS):
        if not _current(current) or not await refresh():
            return {"state":"changed"}
        if asyncio.get_running_loop().time() >= deadline:
            return last
        try:
            result = await issue(owner=owner, request=request, deadline=deadline, current=current)
        except (OSError, RuntimeError, ValueError):
            return last
        if not _current(current):
            return {"state":"changed"}
        state = result.get("state")
        if state == "complete" and _HASH.fullmatch(result.get("generation") or ""):
            return {key:result[key] for key in ("state","generation","chunk_count")}
        if state != "active" or not _HASH.fullmatch(result.get("generation") or ""):
            return {"state":state if state in ("superseded","unavailable") else "unavailable"}
        last = {"state":"pending","generation":result["generation"], "next":result.get("next"), "total":result.get("total")}
    return last


async def personal(provider, authority, *, source_id, source_revision, deadline):
    snapshot = authority.snapshot
    async def refresh():
        return _current(authority.current_source)
    return await _advance(provider, owner=authority.owner, account=authority.account,
        source={"kind":"file","workspace":"personal","resource":source_id}, revision=str(source_revision),
        text=snapshot.text, page_ends=snapshot.page_ends, current=authority.current_source,
        refresh=refresh, deadline=deadline)


def _copal_owner(request, scope):
    from routes.copal_routes import _scope, require_user
    from src.openclank.account_request_barrier import AccountRequestBarrier
    state = request.app.state
    owner = require_user(request)
    auth = getattr(state,"auth_manager",None)
    capture = getattr(state,"capture_original_credential",None)
    barrier = getattr(state,"account_request_barrier",None)
    provider = getattr(state,"canonical_memory_provider",None) or getattr(state,"memory_provider",None)
    if (auth is None or type(barrier) is not AccountRequestBarrier or not callable(capture)
            or not callable(getattr(provider,"_host_document_structure",None))
            or getattr(getattr(provider,"_guarded_preparation",None),"document_structure",False) is not True
            or getattr(request.state,"api_token",False) or getattr(request.state,"internal_tool_authenticated",False)):
        return None
    account = auth.account_id(owner)
    if not account or auth.username_for_account_id(account) != owner:
        return None
    credential = capture(request,owner,account)
    def current():
        try:
            return (callable(credential) and credential() is True and _scope(request,scope["workspace_id"]) == scope
                and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                and getattr(state,"auth_manager",None) is auth
                and getattr(state,"account_request_barrier",None) is barrier
                and (getattr(state,"canonical_memory_provider",None) or getattr(state,"memory_provider",None)) is provider)
        except Exception:
            return False
    return provider, owner, account, barrier, current


async def _copal_operation(request, scope, run):
    from fastapi import HTTPException
    from src.openclank.account_request_barrier import _current_admission
    try:
        identity = _copal_owner(request,scope)
        if identity is None:
            return {"state":"unavailable"}
        provider, owner, account, barrier, account_current = identity
        deadline = asyncio.get_running_loop().time() + 6
        async with asyncio.timeout_at(deadline), barrier.admitted(owner):
            admission = _current_admission.get()
            def current():
                try:
                    barrier.assert_live_admission(owner,admission)
                    return account_current()
                except Exception:
                    return False
            if not current():
                return {"state":"changed"}
            return await run(provider,owner,account,current,deadline)
    except (HTTPException, OSError, RuntimeError, ValueError, TimeoutError):
        return {"state":"pending"}


async def journal(request, scope, document, *, get_current):
    from src.openclank.document_code_links import document_text
    text = document_text(document, limit=_MAX_BYTES)
    ident, revision = document.get("id"), document.get("head")
    if text is None or not isinstance(ident,str) or not isinstance(revision,str) or document.get("readOnly"):
        return {"state":"unsupported"}
    async def run(provider, owner, account, current, deadline):
        async def refresh():
            latest = await get_current(ident)
            return (current() and latest.get("id") == ident and latest.get("head") == revision
                    and document_text(latest,limit=_MAX_BYTES) == text and not latest.get("readOnly"))
        return await _advance(provider, owner=owner, account=account, current=current, refresh=refresh, deadline=deadline,
            source={"kind":"journal","workspace":scope["workspace_id"],"resource":ident},
            revision=revision,text=text,page_ends=())
    return await _copal_operation(request,scope,run)


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_attachment(path, suffix):
    limit = _MAX_PDF_ASSET_BYTES if suffix == ".pdf" else _MAX_BYTES
    fd = os.open(path,os.O_RDONLY | os.O_NOFOLLOW | getattr(os,"O_CLOEXEC",0))
    with os.fdopen(fd,"rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("Unsupported attachment")
        raw = stream.read(limit+1)
        after = os.fstat(stream.fileno())
        if (len(raw) != before.st_size or _file_identity(before) != _file_identity(after)
                or _file_identity(after) != _file_identity(os.stat(path,follow_symlinks=False))):
            raise ValueError("Attachment changed")
    if suffix == ".pdf":
        from src.openclank.personal_document_source import retained_document_text
        text, _, page_ends = retained_document_text(raw,suffix)
    else:
        text = raw.decode("utf-8","strict")
        page_ends = ()
        if "\x00" in text:
            raise ValueError("Unsupported attachment")
    return _file_identity(before), text, hashlib.sha256(raw).hexdigest(), page_ends


async def attachment(request, scope, asset_id, *, get_path, is_linked):
    async def run(provider, owner, account, current, deadline):
        path, name = await get_path(asset_id)
        suffix = Path(name).suffix.lower()
        if suffix not in _TEXT_ASSETS and suffix != ".pdf":
            return {"state":"unsupported"}
        try:
            identity, text, revision, page_ends = await asyncio.to_thread(_read_attachment,path,suffix)
        except (OSError, ValueError):
            return {"state":"unsupported"}
        def source_current():
            try:
                return current() and _file_identity(os.stat(path,follow_symlinks=False)) == identity
            except OSError:
                return False
        async def refresh():
            latest_path, latest_name = await get_path(asset_id)
            return (latest_path == path and latest_name == name and await is_linked(name) and source_current())
        return await _advance(provider, owner=owner, account=account, current=source_current, refresh=refresh, deadline=deadline,
            source={"kind":"attachment","workspace":scope["workspace_id"],"resource":asset_id},
            revision=revision,text=text,page_ends=page_ends)
    return await _copal_operation(request,scope,run)


async def revoke_copal(request, scope, ident, kind, *, absent):
    async def run(provider, owner, account, current, deadline):
        if not current() or not await absent():
            return {"state":"changed"}
        return await provider._host_document_structure(owner=owner,deadline=deadline,current=current,
            request={"action":"revoke","account_id":account,
                     "source":{"kind":kind,"workspace":scope["workspace_id"],"resource":ident}})
    return await _copal_operation(request,scope,run)


async def read_journal(request, scope, document, *, audience_current):
    text = document.get("text")
    if not isinstance(text,str) or not isinstance(document.get("head"),str) or not isinstance(document.get("id"),str):
        return None
    async def run(provider, owner, account, current, deadline):
        return await provider._host_document_structure(owner=owner,deadline=deadline,current=lambda: current() and audience_current(),
            request={"action":"read","account_id":account, "revision":document["head"],
                     "content_sha256":hashlib.sha256(text.encode()).hexdigest(),
                     "source":{"kind":"journal","workspace":scope["workspace_id"],"resource":document["id"]}})
    result = await _copal_operation(request,scope,run)
    return result.get("projection") if result.get("state") == "complete" else None
