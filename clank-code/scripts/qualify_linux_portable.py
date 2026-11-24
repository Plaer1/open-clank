#!/usr/bin/env python3
"""Qualify actual Linux archive bytes with disposable home/data and no toolchain PATH."""
from __future__ import annotations
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import signal
import struct
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.linux_package import digest, verify_bundle, write_json
from scripts.qualify_windows_portable import fixture_png


def qualify(archive, output):
    if output.exists(): raise RuntimeError('Qualification output exists; preserve it')
    output.mkdir(parents=True)
    extracted = output / 'relocated app with spaces'
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.name.startswith('openclank/') or (member.name == 'openclank' and member.isdir())):
                raise RuntimeError('Unsafe archive member')
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise RuntimeError('Special archive member')
        tar.extractall(extracted, filter='data')
    bundle = extracted / 'openclank'
    verify_bundle(bundle)
    work = output / 'private fixture'; work.mkdir()
    home = work / 'home'; home.mkdir()
    fixture = home / 'Linux fixture Café.txt'
    original = 'Linux archive original\n'; saved_text = 'Linux archive saved\n'
    fixture.write_text(original)
    image = home / 'native content.png'; image.write_bytes(fixture_png())
    # Whitelist host desktop IPC/locale only. Developer binary/hash/runtime,
    # database, auth/archive and provider overrides cannot mask an incomplete
    # package or connect it to a checkout process/store.
    host_keys = {'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR', 'WAYLAND_DISPLAY',
                 'DISPLAY', 'XDG_DATA_DIRS', 'XDG_CONFIG_DIRS', 'XDG_CURRENT_DESKTOP',
                 'DESKTOP_SESSION', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ'}
    environment = {key:value for key,value in os.environ.items() if key in host_keys}
    environment.update(HOME=str(home), XDG_CONFIG_HOME=str(work / 'config'),
        XDG_DATA_HOME=str(work / 'xdg-data'), XDG_CACHE_HOME=str(work / 'cache'),
        OPEN_CLANK_DATA_DIR=str(work / 'data'), PATH='/usr/bin:/bin', BROWSER='/bin/true', APP_BIND='127.0.0.1')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    cli = bundle / 'openclank'
    checks = []
    receipt = {'archive_sha256': digest(archive), 'bundle': str(bundle), 'checks': checks, 'outcome': 'failed'}
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    def command(*args, timeout=240):
        value = subprocess.run([str(cli), *args], cwd=work, env=environment,
            capture_output=True, text=True, timeout=timeout)
        with (work / 'cli.log').open('a') as log:
            log.write('COMMAND ' + ' '.join(args) + '\n' + value.stdout + value.stderr)
        if value.returncode: raise RuntimeError('Packaged CLI command failed: ' + ' '.join(args))
        return value.stdout
    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers={'Origin': base, 'Content-Type': 'application/json'})
        with opener.open(req, timeout=60) as response: return json.load(response)
    def children(resource):
        return request('/api/files-v1/children', {'parent_ref': resource['ref'], 'limit': 200})['entries']
    try:
        command('--version'); command('engine', 'verify', '--json')
        checks.append('frozen-entry-engine-provenance-version-and-ACP')
        subprocess.run([str(bundle / '_internal/python/bin/python3'), '-I', '-c',
            'import sys;assert sys.dont_write_bytecode;import mcp,fastapi,sqlalchemy,grpc,cryptography,psycopg2;from src.runtime_paths import get_app_root'],
            cwd=work, env=environment, check=True, timeout=60)
        verify_bundle(bundle); checks.append('private-Python-imports-without-toolchain-PATH-no-payload-mutation')
        command('profile', 'add', 'archive-fixture', base, '--auto-start', '--use')
        command('server', 'start', '--open-browser')
        checks.append('public-CLI-fresh-first-run-server-start')
        password = secrets.token_urlsafe(24)
        assert request('/api/auth/setup', {'username': 'archivefixture', 'password': password})['ok']
        assert not list(cookies)
        try: request('/api/files-v1/roots')
        except urllib.error.HTTPError as error: assert error.code in {401,403}
        else: raise RuntimeError('Unauthenticated Files accepted')
        assert request('/api/auth/login', {'username': 'archivefixture', 'password': password})['ok']
        checks.append('first-run-auth-setup-login-and-unauthenticated-Files-denial')
        command('server', 'status'); checks.append('public-CLI-admits-owned-frozen-Linux-PID')
        roots = request('/api/files-v1/roots')['entries']
        host = next(item for item in roots if item.get('name') == 'Host locations')
        home_resource = next(item for item in children(host) if item.get('name') == 'Home')
        entries = children(home_resource)
        resource = next(item for item in entries if item.get('name') == fixture.name)
        opened = request('/api/files-v1/open-resource', {'resource_ref': resource['ref']})
        assert opened['payload']['text'] == original
        saved = request('/api/files-v1/save-resource', {'resource_ref': opened['resource']['ref'],
            'expected_revision': opened['payload']['resource']['revision'], 'text': saved_text})
        assert saved['outcome'] == 'applied' and fixture.read_text() == saved_text
        reopened = request('/api/files-v1/open-resource', {'resource_ref': opened['resource']['ref']})
        assert reopened['payload']['text'] == saved_text
        checks.append('native-Files-list-open-Editor-save-exact-bytes-reopen')
        fixture.write_text('external conflicting change\n')
        stale = request('/api/files-v1/save-resource', {'resource_ref': opened['resource']['ref'],
            'expected_revision': reopened['payload']['resource']['revision'], 'text': 'must not overwrite\n'})
        assert stale['outcome'] != 'applied' and fixture.read_text() == 'external conflicting change\n'
        checks.append('Editor-stale-conflict-preserves-external-change')
        picture = next(item for item in entries if item.get('name') == image.name)
        path = '/api/files-v1/thumbnail/' + urllib.parse.quote(picture['ref'], safe='') + '?width=192&height=192&scale=1&icon=false'
        with opener.open(urllib.request.Request(base + path, headers={'Origin':base}), timeout=60) as response:
            png = response.read(4 * 1024 * 1024 + 1)
            assert response.headers.get_content_type() == 'image/png' and png[:8] == b'\x89PNG\r\n\x1a\n'
        width, height = struct.unpack('>II', png[16:24])
        assert 1 <= height <= 192 and width == height * 2 and width <= 192
        (output / 'native-thumbnail.png').write_bytes(png)
        receipt['thumbnail_sha256'] = __import__('hashlib').sha256(png).hexdigest()
        checks.append('actual-packaged-native-KDE-thumbnail-RPC')
        from scripts.qualify_linux_graph import qualify_graph
        receipt['code_graph'] = qualify_graph(request, home)
        checks.append('packaged-production-Code-Graph-index-query-source-Files-Editor')
        command('server', 'stop'); checks.append('public-CLI-stop-owned-frozen-process')
        command('server', 'start', '--open-browser'); command('server', 'status')
        cookies.clear()
        assert request('/api/auth/login', {'username':'archivefixture', 'password':password})['ok']
        roots = request('/api/files-v1/roots')['entries']
        host = next(item for item in roots if item.get('name') == 'Host locations')
        restarted_home = next(item for item in children(host) if item.get('name') == 'Home')
        restarted_file = next(item for item in children(restarted_home) if item.get('name') == fixture.name)
        assert request('/api/files-v1/open-resource', {'resource_ref':restarted_file['ref']})['payload']['text'] == 'external conflicting change\n'
        command('server', 'stop')
        password = None
        checks.append('public-CLI-restart-relogin-and-persisted-source-read')
        verify_bundle(bundle); checks.append('payload-unchanged-after-runtime-and-restart')
        receipt['outcome'] = 'passed'
    finally:
        # Cleanup only the PID recorded inside this disposable configuration,
        # after verifying its executable and exact hidden entrypoint.
        try: command('server', 'stop', timeout=30)
        except Exception: pass
        state = work / 'config/openclank/runtime/server.json'
        if state.is_file():
            try:
                pid = int(json.loads(state.read_text())['pid']); proc = Path('/proc') / str(pid)
                args = (proc / 'cmdline').read_bytes().split(b'\0')
                if (proc / 'exe').resolve() == cli.resolve() and args[1] == b'__managed-server':
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError, KeyError, IndexError): pass
        write_json(output / 'qualification.json', receipt)
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();qualify(args.archive.resolve(),args.output.resolve())
