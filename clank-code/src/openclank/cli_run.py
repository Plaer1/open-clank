"""One-shot presentation over the existing authenticated host turn API."""
import uuid
from src.openclank.cli_output import bounded_chunk, clean_text, json_line
from src.openclank.tui_client import TuiClientError


def run_one_shot(client, session_id, prompt, output_format, stdout, stderr, owner=None):
    result = {'schema': 'openclank.run.v1', 'status': 'unknown', 'owner': owner,
              'session_id': session_id, 'engine_session_id': None, 'map_revision': None,
              'turn_id': None, 'run_id': None, 'cursor': None, 'output': '',
              'output_truncated': False,
              'recovery': {'kind': 'open_existing_chat', 'session_id': session_id}}
    operation_key = uuid.uuid4().hex
    result['recovery']['idempotency_key'] = operation_key
    code = 6
    chunks, output_bytes = [], 0
    events = None
    try:
        events = client.start_turn_events(session_id, prompt, idempotency_key=operation_key)
        saw_done = False
        for item in events:
            if item['event'] == 'started':
                result['run_id'] = item.get('run_id') or None
                continue
            if item.get('id') is not None:
                result['cursor'] = item['id']
            value = item.get('data')
            if isinstance(value, dict) and value.get('type') in ('permission_request', 'ask_user'):
                result['status'], code = 'human_action_required', 4
                print('Human action required; open the existing chat to continue.', file=stderr)
                break
            if item['event'] == 'error':
                result['status'], code = 'failed', 6
            if isinstance(value, dict) and isinstance(value.get('delta'), str) and not value.get('thinking'):
                delta = clean_text(value['delta'])
                accepted, output_bytes, truncated = ('', output_bytes, True) if result['output_truncated'] else bounded_chunk(delta, output_bytes)
                result['output_truncated'] |= truncated
                if accepted:
                    chunks.append(accepted)
                if accepted and output_format == 'text':
                    stdout.write(accepted); stdout.flush()
                elif accepted and output_format == 'jsonl':
                    json_line(stdout, {'schema': 'openclank.run.v1', 'event': 'delta', 'text': accepted})
            if item['event'] == 'done':
                saw_done = True
        if not saw_done and code == 6 and result['status'] == 'unknown':
            print('Stream ended without a terminal result; inspect the existing chat.', file=stderr)
        if saw_done and code != 4 and result['status'] != 'failed':
            if result['run_id']:
                state = client.active_turn(session_id)
                if state.get('run_id') == result['run_id'] and state.get('status') == 'done':
                    result['status'], code = 'completed', 0
                elif state.get('run_id') == result['run_id'] and state.get('status') == 'error':
                    result['status'], code = 'failed', 6
            if code == 6 and result['status'] != 'failed':
                print('Final run outcome is not confirmed; inspect the existing chat.', file=stderr)
    except KeyboardInterrupt:
        run_id = result['run_id']
        if run_id:
            try:
                stopped = client.stop_active_turn(session_id, run_id=run_id)
                if stopped.get('stopped') and stopped.get('run_id') == run_id:
                    result['status'], code = 'cancel_requested', 5
                    print('Cancellation requested for the observed run; its final outcome is not yet confirmed.', file=stderr)
                else:
                    print('Run changed; cancellation was not confirmed.', file=stderr)
            except TuiClientError:
                print('Cancellation outcome is unknown; inspect the existing chat.', file=stderr)
        else:
            print('No run identity was received; cancellation was not requested.', file=stderr)
    except BrokenPipeError:
        print('Output closed; detached from the existing run without cancelling it.', file=stderr)
        return 7
    except TuiClientError as exc:
        if exc.status_code in (401, 403): code = 3
        elif exc.status_code in (400, 404, 422): code = 2
        result['status'] = 'failed' if exc.status_code in (400, 401, 403, 404, 422) else 'unknown'
        print(clean_text(f'Run could not be confirmed: {exc}'), file=stderr)
    finally:
        if events is not None:
            events.close()
    if result['status'] == 'unknown':
        print(f'Original submission key: {operation_key}; use the TUI status command to reconcile.', file=stderr)
    result['output'] = ''.join(chunks)
    try:
        if output_format in ('json', 'jsonl'):
            json_line(stdout, result)
    except BrokenPipeError:
        print('Output closed; detached without cancelling the run.', file=stderr)
        return 7
    return code
