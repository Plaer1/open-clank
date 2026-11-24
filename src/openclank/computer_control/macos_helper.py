"""macOS control JSON-lines helper. Not routed until Host identity is implemented.

Pixel actions report delivery, never effect verification. AX operations currently
fail closed until the accessibility snapshot implementation is joined.
"""
from __future__ import annotations

import json
import signal
import sys
import time

from src.openclank.computer_control import macos

# X11 keysyms sent by the shared protocol, mapped to macOS virtual key codes.
KEYS = {0xff0d: 36, 0xff09: 48, 0xff1b: 53, 0xff08: 51, 0xffff: 117,
        0xff51: 123, 0xff52: 126, 0xff53: 124, 0xff54: 125,
        0xff50: 115, 0xff57: 119, 0xff55: 116, 0xff56: 121,
        0xffe3: 59, 0xffe1: 56, 0xffe9: 58}
KEYS.update(dict(zip(map(ord, 'asdfhgzxcv'), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])))
KEYS.update(dict(zip(map(ord, 'bqwertyuiop'), [11, 12, 13, 14, 15, 17, 16, 32, 34, 31, 35])))
KEYS.update(dict(zip(map(ord, 'lnmjkv'), [37, 45, 46, 38, 40, 9])))
KEYS.update(dict(zip(map(ord, '1234567890'), [18, 19, 20, 21, 23, 22, 26, 28, 25, 29])))
KEYS[32] = 49
BUTTONS = {'left': (0, 1, 2), 'right': (1, 3, 4), 'middle': (2, 25, 26)}


def out(value):
    print(json.dumps(value, separators=(',', ':')), flush=True)


def error(code):
    return {'ok': False, 'code': code}


def run_control():
    held = []
    geometry = None
    display_id = None
    deadline = 0.0
    used = 0
    clipboard_enabled = False

    def release():
        failed = []
        while held:
            item = held.pop()
            kind, value, x, y = item
            try:
                if kind == 'key': macos.post_key(value, False)
                elif kind == 'unicode': macos.post_unicode(value, False)
                else: macos.post_mouse(value[2], x, y, value[0])
            except Exception:
                failed.append(item)
        held.extend(reversed(failed))
        return not failed

    def stop_signal(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_signal)
    try:
        for line in sys.stdin:
            if len(line) > 8192:
                out(error('invalid_request')); continue
            try:
                req = json.loads(line)
                if not isinstance(req, dict) or req.get('v') != 1:
                    out(error('invalid_request')); continue
                op = req.get('op')
                if op == 'probe':
                    status = macos.permission_status()
                    out({'ok': True, 'state': 'probe', 'version': 1,
                         'keyboard': status['accessibility'], 'pointer': status['accessibility']})
                    continue
                if op == 'begin':
                    if geometry is not None:
                        out(error('invalid_request')); continue
                    if not macos.permission_status()['accessibility']:
                        out(error('denied')); continue
                    selected_id = macos.primary_display_id()
                    geom = macos.primary_geometry()
                    if selected_id <= 0 or macos.primary_display_id() != selected_id:
                        out(error('unsupported_target')); continue
                    if geom[2] <= 0 or geom[3] <= 0 or geom[4] <= 0 or geom[5] <= 0:
                        out(error('unavailable')); continue
                    mode = req.get('clipboard')
                    if mode not in (None, 'replace'):
                        out(error('invalid_request')); continue
                    clipboard_enabled = mode == 'replace' and macos.clipboard_available()
                    geometry = geom; display_id = selected_id; deadline = time.monotonic() + min(120, max(1, int(req.get('deadline_ms', 120000)) / 1000))
                    response = {'ok': True, 'state': 'active', 'stream': {'width': geom[4], 'height': geom[5]}}
                    if clipboard_enabled: response['clipboard_enabled'] = True
                    out(response); continue
                if op == 'stop':
                    out({'ok': True, 'state': 'stopped'} if release() else error('outcome_unknown')); return
                if geometry is None:
                    out(error('not_begun')); continue
                if time.monotonic() >= deadline or used >= 20:
                    release(); out(error('expired')); return
                if macos.primary_display_id() != display_id or macos.primary_geometry() != geometry:
                    release(); out(error('unsupported_target')); return
                if op == 'paste':
                    if set(req) != {'v', 'op', 'text', 'clipboard'} or req.get('clipboard') != 'replace' or not clipboard_enabled:
                        out(error('unsupported_clipboard')); continue
                    text = req.get('text')
                    if not isinstance(text, str) or not 1 <= len(text) <= 256 or any((ord(c)<32 and c not in '\n\t') or 127 <= ord(c) < 160 or 0xd800 <= ord(c) <= 0xdfff for c in text):
                        out(error('invalid_text')); continue
                    used += 1
                    clipboard, input_effect = 'unknown', 'not_issued'
                    try:
                        macos.replace_clipboard_text(text); clipboard = 'replaced'
                        input_effect = 'unknown'
                        for key in (55, 9):
                            held.append(('key', key, 0, 0)); macos.post_key(key, True)
                        for _ in range(2):
                            _, key, _, _ = held[-1]; macos.post_key(key, False); held.pop()
                        input_effect = 'sent'
                        out({'ok': True, 'clipboard': clipboard, 'input': input_effect, 'lifetime': 'platform'})
                    except Exception:
                        release(); out({'ok': False, 'clipboard': clipboard, 'input': input_effect, 'lifetime': 'platform'}); return
                    continue
                if op not in ('click', 'move', 'key', 'type', 'scroll', 'drag'):
                    out(error('unsupported_clipboard' if op == 'paste' else 'unsupported_action')); continue
                x = y = 0.0
                if op in ('click', 'move', 'scroll', 'drag'):
                    gx, gy, gw, gh, pw, ph = geometry
                    frame = req.get('frame')
                    px, py = (req.get('x1'), req.get('y1')) if op == 'drag' else (req.get('x'), req.get('y'))
                    if frame != {'width': pw, 'height': ph}:
                        out(error('frame_mismatch')); continue
                    if type(px) is not int or type(py) is not int or not 0 <= px < pw or not 0 <= py < ph:
                        out(error('out_of_bounds')); continue
                    x, y = gx + px * gw / pw, gy + py * gh / ph
                    if op == 'drag':
                        px2, py2 = req.get('x2'), req.get('y2')
                        if type(px2) is not int or type(py2) is not int or not 0 <= px2 < pw or not 0 <= py2 < ph:
                            out(error('out_of_bounds')); continue
                        x2, y2 = gx + px2 * gw / pw, gy + py2 * gh / ph
                if op == 'scroll' and (req.get('direction') not in ('up', 'down', 'left', 'right') or type(req.get('amount')) is not int or not 1 <= req['amount'] <= 20):
                    out(error('invalid_request')); continue
                if op == 'type' and (not isinstance(req.get('text'), str) or not 1 <= len(req['text']) <= 256 or '\x00' in req['text']):
                    out(error('invalid_text')); continue
                if op == 'click' and req.get('button') not in BUTTONS:
                    out(error('invalid_request')); continue
                if op == 'key':
                    syms = req.get('keysyms')
                    if not isinstance(syms, list) or not 1 <= len(syms) <= 4 or any(type(k) is not int or k not in KEYS for k in syms):
                        out(error('invalid_request')); continue
                used += 1
                try:
                    if op == 'move': macos.post_mouse(5, x, y, 0)
                    elif op == 'click':
                        button = BUTTONS[req['button']]
                        held.append(('mouse', button, x, y))
                        macos.post_mouse(button[1], x, y, button[0])
                        macos.post_mouse(button[2], x, y, button[0]); held.pop()
                    elif op == 'key':
                        for sym in syms:
                            key = KEYS[sym]; held.append(('key', key, 0, 0)); macos.post_key(key, True)
                        for _ in syms[::-1]:
                            _, key, _, _ = held[-1]; macos.post_key(key, False); held.pop()
                    elif op == 'type':
                        for char in req['text']:
                            held.append(('unicode', char, 0, 0)); macos.post_unicode(char, True)
                            macos.post_unicode(char, False); held.pop()
                    elif op == 'scroll':
                        macos.post_mouse(5, x, y, 0)
                        direction, amount = req['direction'], req['amount']
                        vertical = amount if direction == 'up' else -amount if direction == 'down' else 0
                        horizontal = amount if direction == 'left' else -amount if direction == 'right' else 0
                        macos.post_scroll(vertical, horizontal)
                    else:
                        button = BUTTONS['left']; held.append(('mouse', button, x, y))
                        macos.post_mouse(button[1], x, y, button[0])
                        for step in range(1, 11):
                            nx, ny = x + (x2 - x) * step / 10, y + (y2 - y) * step / 10
                            held[-1] = ('mouse', button, nx, ny)
                            macos.post_mouse(6, nx, ny, button[0])
                        macos.post_mouse(button[2], x2, y2, button[0]); held.pop()
                    out({'ok': True, 'outcome': 'sent', 'actions_left': 20 - used})
                except Exception:
                    release(); out(error('outcome_unknown')); return
            except (ValueError, TypeError, KeyError, OverflowError, OSError):
                out(error('invalid_request'))
    except KeyboardInterrupt:
        pass
    finally:
        release()


def run_a11y():
    import os
    import secrets
    snapshots = {}
    excluded = ('keepass', 'bitwarden', '1password', 'lastpass', 'dashlane', 'enpass', 'protonpass', 'nordpass', 'credential')
    sensitive = ('password', 'passwd', 'passphrase', 'secret', 'token', 'api key', 'apikey', 'api_key', 'private key', 'credential', 'pin code', 'secure')

    def secret_value(value):
        text = value.strip()
        return (text.startswith(('-----BEGIN', 'sk-', 'ghp_', 'xox')) or text.casefold().startswith('bearer ') or
                (len(text) >= 32 and ' ' not in text and all(c.isascii() and (c.isalnum() or c in '+/=-_') for c in text)))

    def info(element):
        role = macos.ax_text(element, 'AXRole')
        title = macos.ax_text(element, 'AXTitle') or macos.ax_text(element, 'AXDescription')
        ident = macos.ax_text(element, 'AXIdentifier')
        return role, title, ident

    def allowed(app):
        name = str(app.get('name') or '')
        exe = str(app.get('exe') or '')
        return bool(name and exe) and not any(word in (name + ' ' + exe).lower().replace(' ', '') for word in excluded)

    def app_for(selector):
        apps = [p for p in macos.processes() if allowed(p)]
        if not isinstance(selector, dict) or len(selector) != 1:
            return None
        if type(selector.get('pid')) is int:
            matches = [p for p in apps if p['pid'] == selector['pid']]
        elif isinstance(selector.get('name'), str) and selector['name']:
            matches = [p for p in apps if selector['name'].casefold() in p['name'].casefold()]
        else:
            return None
        return matches[0] if len(matches) == 1 else None

    def drop(objects):
        for obj in objects: macos.ax_release(obj)

    def discard(snap):
        drop(snap['refs'].values())

    def request(req):
        if not macos.permission_status()['accessibility']:
            return error('accessibility_disabled')
        op = req.get('op')
        if op == 'handshake':
            return {'ok': True, 'backend': 'macos-ax/1', 'accessibility': 'ready',
                    'actions': ['click', 'secondary', 'set_value', 'type', 'select_text'], 'limits': {'nodes': 1200, 'depth': 64, 'value_chars': 0}}
        if op == 'list_apps':
            return {'ok': True, 'apps': [{'name': p['name'], 'pid': p['pid'], 'toolkit': 'AX'}
                                         for p in macos.processes() if allowed(p)]}
        if op in ('list_windows', 'app_state'):
            app = app_for(req.get('app'))
            if app is None: return error('no_such_app')
            root = macos.ax_application(app['pid']); wins = []
            try:
                wins = macos.ax_array(root, 'AXWindows', 128)
                if op == 'list_windows':
                    return {'ok': True, 'windows': [{'index': i, 'title': info(w)[1], 'role': info(w)[0],
                            'active': False, 'showing': True, 'modal': False} for i, w in enumerate(wins)]}
                index = req.get('window_index', 0)
                if type(index) is not int or not 0 <= index < len(wins): return error('no_such_window')
                signature = info(wins[index]); token = secrets.token_hex(8); nodes = {}; refs = {}; count = 0; cut = False
                def walk(obj, path, depth):
                    nonlocal count, cut
                    if count >= 1200 or depth > 64:
                        cut = True; return None
                    sig = info(obj); count += 1; nodes[path] = sig; refs[path] = macos.ax_retain(obj)
                    role, title, ident = sig
                    hidden = any(word in (role + ' ' + title + ' ' + ident).casefold() for word in sensitive)
                    node = {'id': path, 'role': role, 'name': '[hidden]' if hidden else title}
                    if hidden:
                        node['hidden'] = True
                    else:
                        value = macos.ax_text(obj, 'AXValue')
                        if value:
                            if secret_value(value): node['value'], node['hidden'], hidden = '[hidden]', True, True
                            else: node['value'] = value[:500]
                    if not hidden:
                        actions = macos.ax_action_names(obj)
                        if actions: node['actions'] = actions[:128]
                    children = macos.ax_array(obj, 'AXChildren', max(0, 1200 - count)) if depth < 64 and not hidden else []
                    try:
                        rendered = [child for i, item in enumerate(children)
                                    if (child := walk(item, str(i) if path == 'root' else path + '.' + str(i), depth + 1)) is not None]
                        if rendered: node['children'] = rendered
                    finally: drop(children)
                    return node
                try: tree = walk(wins[index], 'root', 0)
                except Exception:
                    drop(refs.values())
                    raise
                if macos.process_info(app['pid']) != app:
                    drop(refs.values()); return error('target_gone')
                if len(snapshots) >= 8: discard(snapshots.pop(next(iter(snapshots))))
                target = {'version': 1, 'backend': 'macos-ax/1', 'targetKind': 'window',
                          'stableTargetHandle': f"ax:{app['pid']}:{app['start']}:{index}",
                          'processStartIdentity': f"{app['pid']}:{app['start']}", 'targetGeneration': token,
                          'focusState': 'unknown', 'permission': 'accessibility', 'actionCapabilities': ['click', 'secondary', 'set_value', 'type', 'select_text']}
                snapshots[token] = {'app': app, 'index': index, 'window': signature, 'nodes': nodes,
                                    'time': time.monotonic(), 'target': target, 'refs': refs}
                return {'ok': True, 'snapshot_id': token, 'target': target,
                        'app': {'name': app['name'], 'pid': app['pid']},
                        'window': {'index': index, 'title': signature[1], 'occlusion': 'unknown'},
                        'tree': tree, 'truncated': {'nodes': cut, 'depth': cut}}
            finally:
                drop(wins); macos.ax_release(root)
        if op == 'act':
            snap = snapshots.get(req.get('snapshot_id'))
            if snap is None: return error('snapshot_expired')
            if time.monotonic() - snap['time'] > 120:
                discard(snapshots.pop(req.get('snapshot_id'))); return error('snapshot_expired')
            if req.get('action') not in ('click', 'secondary', 'set_value', 'type', 'select_text'): return error('unsupported_action')
            path = req.get('element')
            if not isinstance(path, str) or path not in snap['nodes']: return error('no_such_element')
            old = snap['app']; current = macos.process_info(old['pid'])
            if current is None or current['uid'] != os.getuid() or current['start'] != old['start'] or current['name'] != old['name'] or current['exe'] != old['exe']:
                return error('target_gone')
            if not allowed(current): return error('app_excluded')
            root = macos.ax_application(old['pid']); wins = []; owned = []
            try:
                wins = macos.ax_array(root, 'AXWindows', 128)
                if (snap['index'] >= len(wins) or info(wins[snap['index']]) != snap['window'] or
                    not macos.ax_equal(wins[snap['index']], snap['refs']['root'])): return error('window_changed')
                obj = wins[snap['index']]
                if path != 'root':
                    for number in path.split('.'):
                        children = macos.ax_array(obj, 'AXChildren', 1200); owned.extend(children)
                        index = int(number)
                        if index >= len(children): return error('stale_element')
                        obj = children[index]
                sig = info(obj)
                if sig != snap['nodes'][path] or not macos.ax_equal(obj, snap['refs'][path]): return error('stale_element')
                if any(word in ' '.join(sig).casefold() for word in sensitive) or secret_value(macos.ax_text(obj, 'AXValue')): return error('not_actionable')
                action = req['action']
                if action == 'click':
                    done = macos.ax_perform(obj, 'AXPress')
                elif action == 'secondary':
                    name = req.get('name')
                    if not isinstance(name, str) or not name or len(name) > 64: return error('invalid_request')
                    if name not in macos.ax_action_names(obj): return error('unsupported_action')
                    done = macos.ax_perform(obj, name)
                elif action == 'select_text':
                    start, end = req.get('start'), req.get('end')
                    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= 1000000:
                        return error('invalid_request')
                    done = macos.ax_select_text(obj, start, end)
                else:
                    text = req.get('text')
                    if not isinstance(text, str) or not text or len(text) > 500 or '\x00' in text: return error('invalid_request')
                    done = macos.ax_set_text(obj, text, 'AXSelectedText' if action == 'type' else 'AXValue')
                if not done: return error('not_actionable')
                return {'ok': True, 'outcome': 'performed', 'verified': 'unknown', 'window_changed': False,
                        'target_before': snap['target'], 'target_after': snap['target']}
            finally:
                drop(owned); drop(wins); macos.ax_release(root)
        return error('invalid_request')

    for line in sys.stdin:
        try:
            req = json.loads(line)
            if not isinstance(req, dict) or req.get('v') != 1 or len(line) > 65536:
                out(error('invalid_request')); continue
            out(request(req))
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, AttributeError):
            out(error('bus_unavailable'))


def launch_proof():
    import re
    import socket
    try:
        line = sys.stdin.readline(2049)
        if not line.endswith('\n') or len(line) > 2048: return False
        req = json.loads(line)
        path, secret = req.get('pipe'), req.get('secret')
        if (req.get('v') != 1 or req.get('op') != 'launch_proof' or
            not isinstance(path, str) or not re.fullmatch(r'/tmp/oc-[A-Za-z0-9_]+/s', path) or
            not isinstance(secret, str) or not re.fullmatch(r'[0-9a-f]{64}', secret)):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(8)
            sock.connect(path)
            sock.sendall((secret + '\n').encode('ascii'))
            if sock.recv(1) != b'1': return False
        out({'ok': True, 'state': 'launch_proof_sent'})
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


if __name__ == '__main__':
    if sys.platform != 'darwin':
        raise SystemExit(1)
    if len(sys.argv) != 2 or sys.argv[1] not in ('control', 'a11y'):
        raise SystemExit(1)
    if not launch_proof(): raise SystemExit(1)
    (run_control if sys.argv[1] == 'control' else run_a11y)()
