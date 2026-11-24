"""Named-human, READ-ONLY journal coverage view for one chat (BQ-8: no cursor movement on a read).

Reads one ``journal_delta.v1`` page (every main-actor row after the cursor, classified) and returns content-free class counts and
cursor positions only. The cursor is moved exclusively by ``ConversationArchive.ack_journal_delta`` (typed outcome proof + CAS),
never from here: this route calls the page read and nothing else.
"""
import json
from fastapi import Request
from fastapi.responses import JSONResponse

_HEADERS = {'Cache-Control': 'private, no-store'}


def register_journal_coverage(router, *, require_human, capture_context, owned, archive, session_factory, profile_path, load_prefs,
                              lifecycle_for, page_reader=None):
    """``archive()`` -> ConversationArchive; ``lifecycle_for(request, account)`` -> completed-turn lifecycle owner or None;
    ``page_reader(request, context, user, chat)`` (tests) replaces the default archive read."""
    def closed(status, state='unavailable'):
        return JSONResponse({'state': state}, status_code=status, headers=_HEADERS)

    async def read_body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024: raise ValueError('body too large')
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result: raise ValueError('duplicate key')
                result[key] = value
            return result
        body = json.loads(bytes(raw).decode('utf-8', 'strict'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid json')))
        chat = body.get('chat_id') if type(body) is dict and set(body) == {'chat_id'} else None
        if type(chat) is not str or not 1 <= len(chat) <= 256: raise ValueError('invalid request')
        return chat

    def default_page(request, context, user, chat):
        from sqlalchemy import text
        from src.openclank import fm_journal_coverage_reader as reader
        from src.openclank.fm_journal_delta import dedupe_for, source_proof_for
        account, owner = context['account'], context['owner']  # server-side only, never from the body
        lifecycle = lifecycle_for(request, account)
        if lifecycle is None: return None
        def authorize(candidate, chat_id):
            if candidate != account: return False
            db = session_factory()
            try:
                row = db.execute(text('SELECT owner FROM sessions WHERE id=:chat'), {'chat': chat_id}).first()
                return bool(row and context['state'].auth_manager.account_id(row[0]) == account)
            finally:
                db.close()
        path = profile_path(request)
        return archive().read_journal_delta_page(owner=account, chat_id=chat, workspace_id=context['workspace'], authorize=authorize,
            source_proof=source_proof_for(source_authority=lifecycle.source_authority, account_id=account, chat_id=chat, view_only=True),
            dedupe=dedupe_for(reader.overlap_rows_for(path, memory_owner=owner, account_id=account, chat_id=chat)))

    @router.post('/journal-coverage')
    async def journal_coverage(request: Request):
        try:
            user = require_human(request); context = capture_context(request, user)
        except Exception:
            return closed(403)
        try:
            chat = await read_body(request)
        except Exception:
            return closed(400)
        profile = getattr(request.app.state, 's05_profile', None)
        if profile is None:  # legacy store: nothing to guard, nothing to read
            return JSONResponse({'state': 'unavailable'}, headers=_HEADERS)
        try:
            from src.openclank import fm_journal_coverage_reader as reader
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                await owned(profile.assert_current)  # as the tick does; raises if host ownership lapsed
                if (await owned(load_prefs, user)).get(reader.PREF_KEY) is not True:
                    return JSONResponse({'state': 'off'}, headers=_HEADERS)  # nothing else is read
                page = await owned((page_reader or default_page), request, context, user, chat)
                if not await owned(context['current_live']): raise ValueError('authority changed')
            if page is None: return JSONResponse({'state': 'unavailable'}, headers=_HEADERS)
            return JSONResponse(reader.page_view(page), headers=_HEADERS)
        except Exception:
            return closed(503)
