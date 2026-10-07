#!/usr/bin/env python3
"""Actual mounted/relocated Mac package auth, Files/Editor and owner restart.

AppKit GUI Quit and TCC journeys remain a separate native manual qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import plistlib
from pathlib import Path
import secrets
import shutil
import signal
import sys
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
import urllib.parse
import zlib


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fixture_png():
    """Small nonsquare content fixture; a square fallback icon cannot pass."""
    width, height = 96, 48

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    pixels = b''.join(b'\x00' + b'\xe0\x20\x20' * 48 + b'\x20\x20\xe0' * 48 for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


def qualify(bundle, target, output, parts=None, emoji_pack=None):
    if sys.platform != 'darwin':
        raise RuntimeError('Mac qualification requires actual native macOS execution')
    if output.exists():
        raise RuntimeError('Qualification directory already exists; preserve it')
    from scripts.macos_package import verify_bundle
    provenance = verify_bundle(bundle / 'Contents')
    if provenance['target'] != target:
        raise RuntimeError('Requested target differs from packaged architecture')
    work = output.with_name(output.name + '-private-fixture')
    if work.exists():
        raise RuntimeError('Private qualification fixture already exists; preserve it')
    output.mkdir(parents=True)
    work.mkdir()
    home = work / 'fixture-home'
    home.mkdir()
    fixture = home / 'macOS release fixture Café.txt'
    before = 'Open Clank macOS release fixture — original\n'
    after = 'Open Clank macOS release fixture — saved\n'
    fixture.write_bytes(before.encode('utf-8'))
    image_fixture = home / 'macOS native content fixture.png'
    image_fixture.write_bytes(fixture_png())
    environment = os.environ.copy()
    # Independent virgin data/home. Never select a developer's accounts/store.
    environment.update(OPEN_CLANK_DATA_DIR=str(work / 'data'), USERPROFILE=str(home),
                       HOME=str(home), APP_BIND='127.0.0.1')
    for key in tuple(environment):
        if key.endswith(('_TOKEN', '_API_KEY')) or key in {'DATABASE_URL', 'OPEN_CLANK_ENGINE_BIN',
              'ODYSSEUS_DATA_DIR', 'FM_DB_PATH', 'OPEN_CLANK_AUTHORITY_DB_PATH', 'OPEN_CLANK_AGENT_HOME',
              'OPEN_CLANK_RUNTIME_PYTHON', 'OPEN_CLANK_PYTHON', 'VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH'}:
            environment.pop(key, None)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 7777))
    port = 7777
    environment['APP_PORT'] = str(port)
    environment['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    base = f'http://127.0.0.1:{port}'
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    checks = []

    def request(path, body=None):
        headers = {'Origin': base, 'Content-Type': 'application/json'}
        data = json.dumps(body).encode() if body is not None else None
        with opener.open(urllib.request.Request(base + path, data=data, headers=headers), timeout=30) as response:
            return json.load(response)

    def children(resource):
        return request('/api/files-v1/children', {'parent_ref': resource['ref'], 'limit': 200})['entries']

    executable = bundle / 'Contents/Resources/runtime/openclank'
    process = None
    receipt = {'target': target, 'checks': checks, 'qualification': 'failed'}
    try:
        with (work / 'payload-verify.json').open('wb') as verification:
            subprocess.run([str(executable), 'engine', 'verify', '--json'], env=environment,
                           cwd=work, check=True, timeout=180, stdout=verification)
        checks.append('sealed-Mac-payload-and-seven-native-helper-verification')
        runtime = bundle / 'Contents/Resources/runtime/_internal/python/bin/python3'
        subprocess.run([str(runtime), '-I', '-c',
                        'import mcp,fastapi,sqlalchemy,grpc,cryptography,psycopg2;from src.openclank import lifetools_server; print("native child imports passed")'],
                       env=environment, cwd=work, check=True, timeout=60)
        subprocess.run([str(runtime), '-I', '-m', 'json.tool', '--help'], env=environment,
                       cwd=work, check=True, timeout=30, stdout=subprocess.DEVNULL)
        checks.append('standalone-private-python-I-c-and-m-without-PATH-python')
        if parts is not None:
            subprocess.run([str(executable), 'assets', 'assemble', '--parts', str(parts)],
                           env=environment, cwd=work, check=True, timeout=600)
            checks.append('complete-pinned-offline-artwork-assembled-from-external-parts')
        elif emoji_pack is not None:
            installed = work / 'data/assets/google-emoji/emoji-assets.pack'
            installed.parent.mkdir(parents=True)
            os.link(emoji_pack, installed)
            subprocess.run([str(executable), 'assets', 'verify'], env=environment, cwd=work, check=True, timeout=600)
            checks.append('complete-pinned-external-artwork-verified-by-packaged-command')
        else:
            raise RuntimeError('Complete offline qualification requires --parts or --emoji-pack')
        with (work / 'server.log').open('wb') as log:
            process = subprocess.Popen([str(executable), '__mac-app-owner'],
                                       env=environment, cwd=work, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        while True:
            if process.poll() is not None:
                raise RuntimeError('Packaged server exited before readiness; inspect server.log')
            try:
                if request('/api/health')['status'] == 'healthy':
                    break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Packaged server readiness timed out')
                time.sleep(1)
        checks.append('fresh-data-packaged-server-startup')
        password = secrets.token_urlsafe(24)
        if request('/api/auth/setup', {'username': 'releasefixture', 'password': password}).get('ok') is not True:
            raise RuntimeError('Canonical first-run setup did not create fixture account')
        checks.append('canonical-first-run-setup')
        if list(cookies):
            raise RuntimeError('First-run setup unexpectedly created an authenticated cookie')
        try:
            request('/api/files-v1/roots')
        except urllib.error.HTTPError as error:
            if error.code not in {401, 403}:
                raise
        else:
            raise RuntimeError('Files accepted an unauthenticated request')
        checks.append('unauthenticated-files-rejected')
        if request('/api/auth/login', {'username': 'releasefixture', 'password': password}).get('ok') is not True:
            raise RuntimeError('Fixture login failed')
        password = None
        checks.append('actual-password-login-with-session-cookie')
        roots = request('/api/files-v1/roots')
        host = next(item for item in roots['entries'] if item.get('name') == 'Host locations')
        home_resource = next(item for item in children(host) if item.get('name') == 'Home')
        home_entries = children(home_resource)
        image_resource = next(item for item in home_entries if item.get('name') == image_fixture.name)
        image_url = '/api/files-v1/thumbnail/' + urllib.parse.quote(image_resource['ref'], safe='')
        with opener.open(urllib.request.Request(base + image_url + '?width=192&height=192&scale=1&icon=false',
                                              headers={'Origin': base}), timeout=30) as response:
            if response.headers.get_content_type() != 'image/png':
                raise RuntimeError('Packaged native thumbnail did not return image/png')
            png = response.read(4 * 1024 * 1024 + 1)
        if not 33 <= len(png) <= 4 * 1024 * 1024 or png[:8] != b'\x89PNG\r\n\x1a\n' or png[12:16] != b'IHDR':
            raise RuntimeError('Packaged native thumbnail PNG content is missing or malformed')
        width, height = struct.unpack('>II', png[16:24])
        if not (1 <= height <= 192 and width == height * 2 and width <= 192):
            raise RuntimeError('Packaged thumbnail dimensions do not preserve the nonsquare content fixture')
        checks.append('packaged-native-PNG-content-thumbnail-without-icon-fallback')
        receipt['thumbnail_sha256'] = hashlib.sha256(png).hexdigest()
        file_resource = next(item for item in home_entries if item.get('name') == fixture.name)
        opened = request('/api/files-v1/open-resource', {'resource_ref': file_resource['ref']})
        if opened['target']['app'] != 'editor' or opened['payload']['text'] != before:
            raise RuntimeError('Files exact-open did not produce the original Editor payload')
        checks.append('native-files-list-and-editor-original-byte-open')
        revision = opened['payload']['resource']['revision']
        saved = request('/api/files-v1/save-resource', {'resource_ref': opened['resource']['ref'],
                                                       'expected_revision': revision, 'text': after})
        if saved['outcome'] != 'applied' or fixture.read_bytes() != after.encode('utf-8'):
            raise RuntimeError('Editor save or exact fixture disk bytes differ')
        reopened = request('/api/files-v1/open-resource', {'resource_ref': opened['resource']['ref']})
        if reopened['payload']['text'] != after:
            raise RuntimeError('Editor reopen differs from saved fixture')
        checks.append('editor-save-disk-bytes-and-reopen')
        receipt['fixture_sha256'] = hashlib.sha256(fixture.read_bytes()).hexdigest()
        # Quit the actual packaged owner; it must stop its managed server.
        process.terminate()
        process.wait(timeout=30)
        if process.returncode != 0:
            raise RuntimeError('Packaged Mac owner did not stop cleanly')
        try:
            request('/api/health')
        except OSError:
            pass
        else:
            raise RuntimeError('Mac owner left its server running after Quit')
        checks.append('owned-server-graceful-stop')
        with (work / 'restart.log').open('wb') as log:
            process = subprocess.Popen([str(executable), '__mac-app-owner'], env=environment,
                                       cwd=work, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        while True:
            if process.poll() is not None:
                raise RuntimeError('Packaged restart exited')
            try:
                if request('/api/health')['status'] == 'healthy':
                    break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Packaged restart timed out')
                time.sleep(1)
        if fixture.read_bytes() != after.encode('utf-8'):
            raise RuntimeError('Saved user data changed across restart')
        if reopened['payload']['text'] != after:
            raise RuntimeError('Saved Editor content changed')
        reopened_again = request('/api/files-v1/open-resource', {'resource_ref': opened['resource']['ref']})
        if reopened_again['payload']['text'] != after:
            raise RuntimeError('Packaged restart lost the Editor resource')
        checks.append('restart-preserves-account-session-and-exact-editor-bytes')
        verify_bundle(bundle / 'Contents')
        checks.append('sealed-bundle-unchanged-after-real-journeys')
        receipt['qualification'] = 'mounted-relocated-package-auth-files-editor-owner-restart-passed'
        receipt['appkit_gui_quit'] = 'pending-native-manual-qualification'
        receipt['screen_capture_TCC'] = 'not-qualified'
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
        if process is not None and process.returncode != 0:
            receipt['qualification'] = 'failed-owner-stop'
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    if receipt['qualification'].startswith('failed'):
        raise RuntimeError('Mac package qualification failed; inspect its receipt')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dmg', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parts', type=Path)
    parser.add_argument('--emoji-pack', type=Path)
    args = parser.parse_args()
    if args.parts and args.emoji_pack:
        parser.error('select one external artwork source')
    if sys.platform != 'darwin':
        parser.error('native macOS execution required')
    output = args.output.resolve()
    relocated = output.with_name(output.name + '-relocated')
    if output.exists() or relocated.exists():
        raise RuntimeError('Qualification output exists; preserve it')
    mounted = plistlib.loads(subprocess.check_output([
        '/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-plist', str(args.dmg.resolve())]))
    points = [Path(item['mount-point']) for item in mounted['system-entities'] if 'mount-point' in item]
    if len(points) != 1:
        raise RuntimeError('DMG did not mount one expected volume')
    mount = points[0]
    try:
        applications = mount / 'Applications'
        if not applications.is_symlink() or os.readlink(applications) != '/Applications':
            raise RuntimeError('DMG Applications install link is invalid')
        relocated.mkdir(parents=True)
        app = relocated / 'Application Folder With Spaces/OpenClank.app'
        shutil.copytree(mount / 'OpenClank.app', app, symlinks=True)
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
        pack = args.emoji_pack or (Path(os.environ['QUALIFICATION_EMOJI_PACK']) if os.environ.get('QUALIFICATION_EMOJI_PACK') else None)
        receipt = qualify(app, 'darwin-arm64', output,
                          args.parts.resolve() if args.parts else None, pack.resolve() if pack else None)
        from scripts.macos_package import digest
        receipt['dmg_sha256'] = digest(args.dmg)
        receipt['checks'].insert(0, 'actual-DMG-readonly-mount-and-relocation-with-spaces')
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        print(receipt['qualification'])
    finally:
        subprocess.run(['/usr/bin/hdiutil', 'detach', str(mount)], check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
