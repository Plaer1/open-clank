"""Truthful TUI route capabilities; runtime policy remains authoritative."""
from __future__ import annotations


def project_client_capabilities(scopes, *, turn_service: bool, engine_status: str = "unknown", run_id: str | None = None, run_active: bool | None = None):
    granted = set(scopes or ())
    def scoped(scope, *, reason=None):
        return {'status': 'available', 'reason': None} if scope in granted else {'status': 'unsupported_scope', 'reason': reason or f'Requires {scope}'}
    sessions = 'tui:sessions' in granted
    submit = {'status': 'unsupported_scope', 'reason': 'Requires tui:sessions'} if not sessions else (
        {'status': 'conditional', 'reason': 'Current engine and turn policy are checked when submitted'} if turn_service else
        {'status': 'unavailable', 'reason': 'Canonical turn service is unavailable'})
    def run_capability(*, stop=False):
        if not sessions: return {'status': 'unsupported_scope', 'reason': 'Requires tui:sessions'}
        if run_active is None: return {'status': 'unknown', 'reason': 'Select a session to observe its run'}
        if run_id and (run_active or not stop): return {'status': 'available', 'reason': 'An exact retained run ID is required'}
        return {'status': 'idle', 'reason': 'No active run was observed' if stop else 'No retained run was observed'}
    unsupported = {'status': 'unsupported', 'reason': 'No TUI route is available'}
    engine = engine_status if engine_status in {'unknown', 'not_running', 'running_unverified'} else 'unknown'
    return {'version': 1, 'engine': {'status': engine, 'negotiated_capabilities': 'unknown'}, 'operations': {
        'browse_sessions': scoped('tui:sessions'), 'read_messages': scoped('tui:sessions'),
        'create_session': unsupported, 'existing_chat_handoff': ({'status':'conditional','reason':'Requires a current owner-qualified native mapping'} if sessions else scoped('tui:sessions')),
        'submit_turn': submit, 'attachments': ({**submit, 'reason':'Existing upload IDs only; host admission is checked on submit'} if sessions and turn_service else submit),
        'bash_option': ({**submit, 'reason':'Explicit request only; host policy decides access'} if sessions and turn_service else submit),
        'web_search_option': ({**submit, 'reason':'Explicit request only; host policy decides access'} if sessions and turn_service else submit),
        'active_turn': scoped('tui:sessions'), 'stream_active_turn': run_capability(), 'stop_active_turn': run_capability(stop=True),
        'actors': ({'status':'available','reason':'Persisted actor history; live control is not provided'} if sessions else scoped('tui:sessions')), 'steer': unsupported,
        'questions': ({'status':'conditional','reason':'Current blocking ACP and actor-scoped optional questions'} if sessions and 'tui:questions' in granted else scoped('tui:sessions') if not sessions else scoped('tui:questions')),
        'approvals': ({'status':'conditional','reason':'Current ACP, managed shell and managed file requests; offered once or reject only'} if sessions and 'tui:approvals' in granted else scoped('tui:sessions') if not sessions else scoped('tui:approvals')),
        'process_output': (
            {'status': 'conditional', 'reason': 'Read-only output for a discovered, current background shell'}
            if sessions else scoped('tui:sessions')
        ),
        'foreground_process_output': unsupported, 'stop_process': unsupported,
        'code_evidence': unsupported, 'history': unsupported,
        'providers': scoped('tui:providers'), 'shares': scoped('tui:shares'),
        'tasks': scoped('tui:tasks'), 'diagnostics': scoped('tui:diagnostics'),
    }, 'note': 'Availability describes this client route and scope; it does not grant tools or prove future engine admission.'}
