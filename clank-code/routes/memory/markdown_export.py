"""Markdown memory export (read-only) and the strict validator the upload import calls. No automatic export or sync."""
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from types import SimpleNamespace

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from src import memery_formats as formats

logger = logging.getLogger(__name__)
_HEADERS = {'Cache-Control': 'private, no-store'}
_SOURCE_READ_BOUND = 5000  # list_current_records caps its own limit here
_IMPORT_CATEGORIES = frozenset({'fact', 'contact', 'task', 'preference', 'identity', 'project', 'goal'})


def origin_label(owner):
    """Pseudonymous origin so the file never carries the account name."""
    return 'owner-' + hashlib.sha256(str(owner).encode('utf-8')).hexdigest()[:12]


def _provenance_digest(value):
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def import_suggestions(raw, *, importer_owner, max_bytes=formats.MAX_FILE_BYTES, with_provenance=False):
    """Validate one document and return model-free suggestions for the existing review (never trusted).

    Raises formats.FormatError. ``foreign`` is True when the manifest origin is not the importing owner's."""
    bundle = formats.parse_markdown(raw, max_bytes=max_bytes)
    suggestions = [{'text': i.text, 'category': i.kind if i.kind in _IMPORT_CATEGORIES else 'fact'} for i in bundle.items]
    foreign = bundle.origin != origin_label(importer_owner)
    result = {'suggestions': suggestions, 'origin': bundle.origin, 'foreign': foreign,
              'item_count': len(suggestions), 'omissions': list(bundle.omissions)}
    if not with_provenance:
        return result

    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    maximum = (1 << 53) - 1
    omitted = sum(entry["count"] for entry in bundle.omissions)
    bundle_summary = {
        "state": "unverified",
        "bundle_sha256": hashlib.sha256(data).hexdigest(),
        "origin_sha256": _provenance_digest(bundle.origin),
        "foreign": foreign,
        "omitted_items": min(omitted, maximum),
        "omissions_at_least": omitted > maximum or any(entry.get("at_least") for entry in bundle.omissions),
    }
    for item, suggestion in zip(bundle.items, suggestions):
        revision = item.source_revision
        revision_omitted = revision is not None and not 0 <= revision <= maximum
        suggestion["markdown_source"] = {
            **bundle_summary,
            "item_sha256": hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
            "declared_state": item.provenance,
            "source_sha256": item.source_content_sha256,
            "source_revision": None if revision_omitted else revision,
            "source_uri_sha256": _provenance_digest(item.source_uri) if item.source_uri else None,
            "source_messages_sha256": _provenance_digest(item.source_message_ids) if item.source_message_ids else None,
            "revision_omitted": revision_omitted,
        }
    result["markdown_bundle"] = bundle_summary
    return result


def markdown_import_result(*, ext, content, owner, max_bytes=formats.MAX_FILE_BYTES, with_provenance=False):
    """Hook for the upload import: None unless this is a ``.md`` starting with the manifest token.

    A manifest document is validated strictly (FormatError becomes HTTP 400 with its closed code) and
    returns model-free suggestions plus ``foreign``/``origin``; the caller feeds them to the normal review."""
    if ext != '.md' or not content.startswith(formats._TOKEN.encode('utf-8') + b' '):
        return None
    try:
        from src.memory_scope import memory_owner  # same owner derivation as the export route's context
        return import_suggestions(
            content, importer_owner=memory_owner(owner), max_bytes=max_bytes, with_provenance=with_provenance,
        )
    except formats.FormatError as exc:
        raise HTTPException(400, detail={'code': exc.code, 'message': 'The Markdown memory export is invalid.',
                                         'retryable': False}) from None


def _zip_files(files, omissions):
    """Standard-library zip, fixed timestamps (deterministic), in memory; one Markdown file per memory."""
    import io, zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, document in files:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, document.encode('utf-8'))
        if omissions:
            info = zipfile.ZipInfo('OMISSIONS.json', date_time=(2020, 1, 1, 0, 0, 0)); info.external_attr = 0o600 << 16
            archive.writestr(info, json.dumps(omissions, sort_keys=True))
    return out.getvalue()


def register_markdown_export(router, *, require_human, capture_context, owned, provider, as_record=lambda r: r, clock=None):
    """``provider.list_memories`` is the reader the memory panel uses, so it works on the legacy and guarded profiles.

    Guarded profile: the paired named-human bracket (``capture_context``). Legacy profile has no paired authority
    to check (``_guarded_preparation`` is None, so ``current_live`` could never be true), so it gets the same
    plain named-user scoping as ``GET /api/memory``."""
    def unavailable(status=503):
        return JSONResponse({'state': 'unavailable'}, status_code=status, headers=_HEADERS)

    def legacy_context(user):
        from src.memory_scope import memory_owner
        @asynccontextmanager
        async def admitted(_user):
            yield
        return {'owner': memory_owner(user), 'barrier': SimpleNamespace(admitted=admitted), 'current_live': lambda: True}

    async def export(request: Request, layout: str = 'document'):
        try:
            user = require_human(request)
            context = capture_context(request, user) if getattr(provider, '_guarded_v17', False) is True else legacy_context(user)
        except Exception:
            return unavailable(403)
        try:
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                rows = [as_record(r) for r in await provider.list_memories(owner=context['owner'], limit=_SOURCE_READ_BOUND)]
                # Defence in depth: the provider read is already owner scoped; never export anything else.
                records = [r for r in rows if isinstance(r, dict) and r.get('owner', context['owner']) == context['owner']]
                omissions = [{'reason': 'source_read_bound', 'count': 1, 'at_least': True}] if len(rows) >= _SOURCE_READ_BOUND else []
                now = (clock or (lambda: datetime.now(timezone.utc)))()
                stamp = now.strftime('%Y-%m-%dT%H:%M:%SZ')
                if layout == 'files':
                    files, left_out = formats.export_markdown_files(records, owner_label=origin_label(context['owner']),
                                                                    generated_at=stamp, omissions=omissions)
                    document = None
                else:
                    document = formats.export_markdown(records, owner_label=origin_label(context['owner']),
                                                       generated_at=stamp, omissions=omissions)
                if not await owned(context['current_live']): raise ValueError('authority changed')
            if layout == 'files':
                return Response(_zip_files(files, left_out), media_type='application/zip',
                                headers={**_HEADERS, 'Content-Disposition': f'attachment; filename="openclank-memory-{now.strftime("%Y%m%d")}.zip"',
                                         'X-Content-Type-Options': 'nosniff'})
            name = 'openclank-memory-' + now.strftime('%Y%m%d') + '.md'
            return Response(document.encode('utf-8'), media_type='text/markdown; charset=utf-8',
                            headers={**_HEADERS, 'Content-Disposition': f'attachment; filename="{name}"',
                                     'X-Content-Type-Options': 'nosniff'})
        except Exception as exc:
            # Class and a short message only: never memory text, owner or paths.
            logger.warning('Markdown memory export failed: %s: %s', type(exc).__name__, str(exc)[:120])
            return unavailable()

    router.add_api_route('/export/markdown', export, methods=['GET'])
