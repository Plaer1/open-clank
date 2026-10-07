#!/usr/bin/env python3
"""Actual per-user install, shortcuts, browser startup, payload checks and uninstall."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import time
import urllib.request

from qualify_windows_portable import qualify as qualify_payload
from build_windows_installer import digest


def powershell(script, *arguments, environment=None):
    command = '$args=@(' + ','.join("'" + str(a).replace("'", "''") + "'" for a in arguments) + ');\n' + script
    encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            env=environment, capture_output=True, text=True, check=True, timeout=90)
    return result.stdout.strip()


def qualify(installer, target, output, pack):
    if os.name != 'nt':
        raise RuntimeError('Installer qualification requires actual Windows')
    import winreg
    provenance = json.loads(installer.with_suffix('.provenance.json').read_text())
    expected = installer.with_suffix('.exe.sha256').read_text(encoding='ascii').split()
    if provenance.get('target') != target or provenance.get('unsigned_beta') is not True or expected != [digest(installer), installer.name] or provenance.get('installer_sha256') != expected[0]:
        raise RuntimeError('Installer target, unsigned provenance or SHA256 disagrees')
    if provenance.get('installer_bootstrap_architecture') != 'x64' or provenance.get('payload_architecture') != target.removeprefix('windows-'):
        raise RuntimeError('Installer bootstrap and native payload architecture are not explicit')
    with installer.open('rb') as binary:
        if binary.read(2) != b'MZ':
            raise RuntimeError('Installer is not a PE executable')
        binary.seek(0x3c)
        binary.seek(struct.unpack('<I', binary.read(4))[0])
        if binary.read(6) != b'PE\x00\x00\x64\x86':
            raise RuntimeError('Installer bootstrap is not the declared x64 executable')
    work = output.with_name(output.name + '-private-fixture')
    if output.exists() or work.exists():
        raise RuntimeError('Qualification paths already exist; preserve them')
    key = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OpenClank.Beta1.' + target + '_is1'

    def registered():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                return True
        except FileNotFoundError:
            return False

    if registered():
        raise RuntimeError('An existing personal beta installation must remain untouched')
    folders = json.loads(powershell("[ordered]@{Programs=[Environment]::GetFolderPath('Programs');Desktop=[Environment]::GetFolderPath('Desktop')}|ConvertTo-Json -Compress"))
    group_name = 'Open Clank qualification ' + secrets.token_hex(8)
    group = Path(folders['Programs']) / group_name
    desktop = Path(folders['Desktop']) / f'Open Clank Beta ({target}).lnk'
    if group.exists() or desktop.exists():
        raise RuntimeError('Existing shortcut must remain untouched')
    output.mkdir(parents=True)
    work.mkdir()
    if shutil.disk_usage(work).free < provenance['payload_bytes'] + 512 * 1024**2:
        raise RuntimeError('Insufficient actual disk space for installed payload qualification')
    installation = work / 'installation'
    data = work / 'preserved-data'
    data.mkdir()
    sentinel = data / 'personal-data-preservation.txt'
    sentinel.write_bytes(b'installer qualification personal data must survive\n')
    sentinel_hash = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    environment = os.environ.copy()
    for variable in tuple(environment):
        if variable.endswith(('_TOKEN', '_API_KEY')) or variable in {'DATABASE_URL', 'ODYSSEUS_DATA_DIR', 'PYTHONHOME', 'PYTHONPATH', 'OPEN_CLANK_RUNTIME_PYTHON', 'OPEN_CLANK_PYTHON'}:
            environment.pop(variable, None)
    home = work / 'launcher-home'
    home.mkdir()
    environment.update(OPEN_CLANK_DATA_DIR=str(data), USERPROFILE=str(home), HOME=str(home),
                       APPDATA=str(work / 'launcher-appdata'))
    checks = []
    def global_runtime_state():
        state = {}
        for hive_name, hive in [('user', winreg.HKEY_CURRENT_USER), ('machine', winreg.HKEY_LOCAL_MACHINE)]:
            for location in ['Environment', 'SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment', 'Software\\Python']:
                try:
                    with winreg.OpenKey(hive, location, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as entry:
                        names = []
                        index = 0
                        while True:
                            try:
                                names.append(winreg.EnumKey(entry, index))
                                index += 1
                            except OSError:
                                break
                        try:
                            path = winreg.QueryValueEx(entry, 'Path')[0]
                        except FileNotFoundError:
                            path = None
                        state[hive_name + location] = (sorted(names), path)
                except FileNotFoundError:
                    state[hive_name + location] = None
        return state
    runtime_state_before = global_runtime_state()
    receipt = {'target': target, 'qualification': 'failed', 'checks': checks,
               'installer_bootstrap_architecture': 'x64', 'payload_architecture': target.removeprefix('windows-')}
    executable = installation / 'payload/openclank.exe'
    uninstaller = installation / 'unins000.exe'
    launcher_started = False
    try:
        subprocess.run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART',
                        '/DIR=' + str(installation), '/GROUP=' + group_name, '/TASKS=desktopicon',
                        '/LOG=' + str(work / 'install-private.log')], env=environment, check=True, timeout=1800)
        if not registered() or not executable.is_file() or not uninstaller.is_file():
            raise RuntimeError('Per-user installation or registration is incomplete')
        if global_runtime_state() != runtime_state_before:
            raise RuntimeError('Installation changed Python registration or global/user PATH')
        checks.append('per-user-install-private-runtime-python-registration-and-path-preserved')
        for link, args in [(group / 'Open Clank.lnk', 'server start --open-browser'),
                           (group / 'Stop Open Clank.lnk', 'server stop'), (desktop, 'server start --open-browser')]:
            detail = json.loads(powershell("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]);[ordered]@{Target=$s.TargetPath;Arguments=$s.Arguments;WorkingDirectory=$s.WorkingDirectory}|ConvertTo-Json -Compress", link))
            if Path(detail['Target']).resolve() != executable.resolve() or detail['Arguments'] != args or Path(detail['WorkingDirectory']).resolve() != executable.parent.resolve():
                raise RuntimeError('Installed shortcut target, arguments or working directory is incorrect')
        checks.append('start-menu-desktop-stop-shortcut-targets')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        with (work / 'profile-private.log').open('wb') as log:
            subprocess.run([str(executable), 'profile', 'add', 'installer-fixture', url, '--use'], env=environment, check=True, stdout=log, stderr=log, timeout=60)
        powershell('Start-Process -FilePath $args[0]', group / 'Open Clank.lnk', environment=environment)
        launcher_started = True
        deadline = time.monotonic() + 90
        while True:
            try:
                with urllib.request.urlopen(url + '/api/auth/status', timeout=2) as response:
                    status = json.load(response)
                if status.get('configured') is not False:
                    raise RuntimeError('Fresh shortcut launch must require canonical first account setup')
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Actual installed browser shortcut did not start the virgin application') from None
                time.sleep(0.25)
        checks.append('actual-browser-shortcut-virgin-auth-required')
        # A second click must reuse the verified running instance.
        state_path = Path(environment['APPDATA']) / 'OpenClank/runtime/server.json'
        state = json.loads(state_path.read_text())
        server_pid = int(state['pid'])
        powershell('$p=Start-Process -FilePath $args[0] -Wait -PassThru;exit $p.ExitCode', group / 'Open Clank.lnk', environment=environment)
        if int(json.loads(state_path.read_text())['pid']) != server_pid:
            raise RuntimeError('Second browser shortcut launch did not reuse the owned server')
        checks.append('second-browser-shortcut-reuses-verified-owned-instance')
        children = json.loads(powershell(
            "$all=@(Get-CimInstance Win32_Process);$ids=@([int]$args[0]);$found=@();do{$next=@($all|Where-Object {$_.ParentProcessId -in $ids -and $_.ProcessId -notin $found.ProcessId});$found+=@($next);$ids=@($next.ProcessId)}while($ids.Count -gt 0);ConvertTo-Json -InputObject @($found|Select-Object ProcessId,CreationDate) -Compress", server_pid))
        powershell('Start-Process -FilePath $args[0] -Wait', group / 'Stop Open Clank.lnk', environment=environment)
        launcher_started = False
        try:
            urllib.request.urlopen(url + '/api/auth/status', timeout=2)
        except OSError:
            checks.append('actual-stop-shortcut-owned-server-stopped')
        else:
            raise RuntimeError('Installed Stop shortcut did not stop its owned server')
        for child in children:
            alive = powershell("$p=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$args[0]);if($null -ne $p){$p.CreationDate|ConvertTo-Json -Compress}", child['ProcessId'])
            if alive and json.loads(alive) == child['CreationDate']:
                raise RuntimeError('Installed Stop shortcut left an owned helper process running')
        checks.append('actual-stop-shortcut-owned-helper-tree-exited')
        runtime_output = work / 'runtime-receipts'
        runtime = qualify_payload(executable.parent, target, runtime_output, emoji_pack=pack)
        if runtime['qualification'] != 'packaged-startup-auth-files-editor-passed':
            raise RuntimeError('Installed payload qualification failed')
        shutil.copyfile(runtime_output / 'receipt.json', output / 'installed-runtime-receipt.json')
        checks.append('installed-auth-files-editor-native-thumbnail-private-python')
        # Keep exact private fixture database/data inventory in memory only.
        before = {p.relative_to(work).as_posix(): digest(p)
                  for folder in (data, runtime_output.with_name(runtime_output.name + '-private-fixture') / 'data')
                  for p in folder.rglob('*') if p.is_file()}
        subprocess.run([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                        '/LOG=' + str(work / 'uninstall-private.log')], env=environment, check=True, timeout=600)
        if executable.exists() or desktop.exists() or group.exists() or registered():
            raise RuntimeError('Uninstall did not remove owned payload, shortcuts and per-user registration')
        if global_runtime_state() != runtime_state_before:
            raise RuntimeError('Uninstall changed Python registration or global/user PATH')
        if any(not (work / path).is_file() or digest(work / path) != checksum for path, checksum in before.items()):
            raise RuntimeError('Uninstall changed private personal-data fixtures')
        checks.append('uninstall-payload-shortcuts-registration-removed-personal-data-preserved')
        receipt.update(qualification='per-user-installer-start-stop-auth-files-editor-uninstall-passed',
                       personal_data_preserved=True, fixture_sha256=sentinel_hash)
    finally:
        if launcher_started and executable.exists():
            subprocess.run([str(executable), 'server', 'stop'], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--target', choices=('windows-x64', 'windows-arm64'), required=True)
    parser.add_argument('--emoji-pack', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    pack = args.emoji_pack or (Path(os.environ['QUALIFICATION_EMOJI_PACK']) if os.environ.get('QUALIFICATION_EMOJI_PACK') else None)
    if pack is None:
        parser.error('external assembled artwork is required for actual installed payload qualification')
    receipt = qualify(args.installer.resolve(), args.target, args.output.resolve(), pack.resolve())
    print(receipt['qualification'])


if __name__ == '__main__':
    main()
