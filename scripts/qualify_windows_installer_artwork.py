#!/usr/bin/env python3
"""Native sealed-installer artwork branches; private fixtures, safe receipts only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import sys

from build_windows_installer import digest, SOURCE
from emoji_asset_bundle import ROOT, expected_manifest, file_digest
from qualify_windows_installer import shortcut_comparison, windows_shortcut_folders


PARTS_MANIFEST_SHA256 = 'f58411bc3aee12c7947ef27271f86cde9e4813df230c97d6394c1a715acdb2f9'


def admit_installer(installer, target):
    provenance_path = installer.with_suffix('.provenance.json')
    provenance = json.loads(provenance_path.read_text())
    checksum = digest(installer)
    expected = installer.with_suffix('.exe.sha256').read_text(encoding='ascii').split()
    if (provenance.get('target') != target or provenance.get('unsigned_beta') is not True
            or expected != [checksum, installer.name]
            or provenance.get('installer_sha256') != checksum
            or provenance.get('installer_bootstrap_architecture') != 'x64'
            or provenance.get('payload_architecture') != target.removeprefix('windows-')
            or provenance.get('external_artwork_embedded') is not False
            or provenance.get('installer_source_sha256') != digest(SOURCE)
            or type(provenance.get('payload_bytes')) is not int or provenance['payload_bytes'] <= 0):
        raise RuntimeError('Sealed installer target, checksum or source provenance disagrees')
    with installer.open('rb') as binary:
        if binary.read(2) != b'MZ':
            raise RuntimeError('Installer is not a PE executable')
        binary.seek(0x3c)
        offset = binary.read(4)
        if len(offset) != 4:
            raise RuntimeError('Installer PE header is truncated')
        binary.seek(struct.unpack('<I', offset)[0])
        if binary.read(6) != b'PE\x00\x00\x64\x86':
            raise RuntimeError('Installer bootstrap is not the declared x64 executable')
    return provenance, checksum, digest(provenance_path)


def registry_runtime_state(winreg):
    state = {}
    for hive_name, hive in [('user', winreg.HKEY_CURRENT_USER), ('machine', winreg.HKEY_LOCAL_MACHINE)]:
        for location in ['Environment', 'SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment', 'Software\\Python']:
            try:
                with winreg.OpenKey(hive, location, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as entry:
                    names, index = [], 0
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


def qualify(installer, target, parts, output):
    if os.name != 'nt':
        raise RuntimeError('Installer artwork qualification requires actual Windows')
    import winreg

    provenance, checksum, provenance_sha = admit_installer(installer, target)
    pin = expected_manifest(ROOT / 'static/vendor/google-emoji/bundle-manifest.json')
    if digest(parts / 'emoji-assets.parts.json') != PARTS_MANIFEST_SHA256:
        raise RuntimeError('Existing parts manifest is not the pinned release manifest')
    for number in range(1, 2):
        if not (parts / f'emoji-assets.pack.part-{number:03}').is_file():
            raise RuntimeError('Existing artwork requires the single compact release part')
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
        raise RuntimeError('Existing personal beta registration must remain untouched')
    folders = windows_shortcut_folders(verify=False)
    desktop = Path(folders['Desktop']) / f'Open Clank Beta ({target}).lnk'
    if desktop.exists():
        raise RuntimeError('Existing desktop shortcut must remain untouched')
    baseline = registry_runtime_state(winreg)
    output.mkdir(parents=True)
    work.mkdir()
    # Default download temporarily retains the single part while assembling the pack.
    required = provenance['payload_bytes'] + pin['pack_bytes'] * 2 + 512 * 1024**2
    if shutil.disk_usage(work).free < required:
        raise RuntimeError('Insufficient space for isolated payload, download and assembly')
    receipt = {'schema_version': 1, 'target': target, 'qualification': 'failed',
               'installer_sha256': checksum, 'installer_source_sha256': provenance['installer_source_sha256'],
               'installer_provenance_sha256': provenance_sha,
               'pack_bytes': pin['pack_bytes'], 'pack_sha256': pin['pack_sha256'], 'modes': []}
    try:
        for mode in ['default-download', 'existing-parts']:
            if registered() or registry_runtime_state(winreg) != baseline or desktop.exists():
                raise RuntimeError('Personal registration, shortcuts or runtime settings changed between modes')
            case = work / mode
            case.mkdir()
            data = case / 'data'
            data.mkdir()
            temporary = case / 'temp'
            temporary.mkdir()
            installation = case / 'installation'
            group_name = 'Open Clank artwork qualification ' + secrets.token_hex(8)
            group = Path(folders['Programs']) / group_name
            if group.exists():
                raise RuntimeError('Existing Start menu group must remain untouched')
            sentinel = data / 'preserve-on-uninstall.txt'
            sentinel.write_bytes(b'Artwork qualification preserves user data.\n')
            sentinel_sha = digest(sentinel)
            environment = os.environ.copy()
            for name in tuple(environment):
                if name.endswith(('_TOKEN', '_API_KEY')) or name in {
                        'DATABASE_URL', 'ODYSSEUS_DATA_DIR', 'PYTHONHOME', 'PYTHONPATH',
                        'OPEN_CLANK_RUNTIME_PYTHON', 'OPEN_CLANK_PYTHON', 'OPEN_CLANK_AGENT_HOME'}:
                    environment.pop(name, None)
            system = Path(os.environ['SystemRoot'])
            environment.update(OPEN_CLANK_DATA_DIR=str(data), TEMP=str(temporary), TMP=str(temporary),
                               APPDATA=str(case / 'appdata'), PATH=os.pathsep.join(str(system / p) for p in ['System32', '']))
            command = [str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART',
                       '/DIR=' + str(installation), '/GROUP=' + group_name,
                       '/LOG=' + str(case / 'install-private.log')]
            if mode == 'existing-parts':
                command += ['/TASKS=downloadart', '/ARTWORKPARTS=' + str(parts)]
            result = {'mode': mode, 'qualification': 'failed', 'checks': []}
            receipt['modes'].append(result)
            with (case / 'commands-private.log').open('wb') as log:
                # No task/parts override in the first case: checkedonce defaults must execute.
                subprocess.run(command, env=environment, check=True, timeout=3600, stdout=log, stderr=log)
                executable = installation / 'payload/openclank.exe'
                uninstaller = installation / 'unins000.exe'
                if not registered() or not executable.is_file() or not uninstaller.is_file():
                    raise RuntimeError('Actual per-user installation is incomplete')
                # Inno has now created known folders under this exact case
                # environment. Reuse the native-proved strict resolver.
                # Desktop is deliberately unchecked and may not exist at all;
                # physical Start-menu links are independently mandatory below.
                installed_folders = windows_shortcut_folders(environment=environment, verify=False)
                group = Path(installed_folders['Programs']) / group_name
                installed_desktop = Path(installed_folders['Desktop']) / f'Open Clank Beta ({target}).lnk'
                if installed_desktop.exists():
                    raise RuntimeError('Default Start menu or unchecked desktop task differs')
                result['shortcuts'] = []
                private_shortcuts = []
                for role, name, arguments in [('start-menu-launch', 'Open Clank.lnk', 'server start --open-browser'),
                                               ('start-menu-stop', 'Stop Open Clank.lnk', 'server stop')]:
                    safe, detail = shortcut_comparison(group / name, executable, arguments, role=role, environment=environment)
                    result['shortcuts'].append(safe)
                    private_shortcuts.append({'role': role, 'actual': detail})
                (case / 'shortcuts-private.json').write_text(json.dumps(private_shortcuts, indent=2) + '\n', encoding='utf-8')
                if any(not all(item[field] for field in ('exists', 'target_equal', 'arguments_equal', 'working_directory_equal')) for item in result['shortcuts']):
                    raise RuntimeError('Artwork installer shortcut exact comparison failed')
                if registry_runtime_state(winreg) != baseline:
                    raise RuntimeError('Installation changed global Python or PATH state')
                result['checks'].append('actual-artwork-task-per-user-install-without-global-runtime-change')
                subprocess.run([str(executable), 'assets', 'verify'], env=environment, check=True,
                               timeout=600, stdout=log, stderr=log)
                pack = data / 'assets/google-emoji/emoji-assets.pack'
                if pack.is_symlink() or file_digest(pack) != (pin['pack_bytes'], pin['pack_sha256']):
                    raise RuntimeError('Installer did not install the exact pinned pack in isolated user data')
                result['checks'].append('installed-private-CLI-and-exact-pinned-user-data-pack-verified')
                if digest(sentinel) != sentinel_sha:
                    raise RuntimeError('Installer changed unrelated personal fixture data')
                subprocess.run([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                                '/LOG=' + str(case / 'uninstall-private.log')], env=environment, check=True,
                               timeout=600, stdout=log, stderr=log)
                if executable.exists() or group.exists() or desktop.exists() or installed_desktop.exists() or registered():
                    raise RuntimeError('Uninstall left owned payload, shortcuts or registration')
                if registry_runtime_state(winreg) != baseline:
                    raise RuntimeError('Uninstall changed global Python or PATH state')
                if digest(sentinel) != sentinel_sha or file_digest(pack) != (pin['pack_bytes'], pin['pack_sha256']):
                    raise RuntimeError('Uninstall changed personal data or installed artwork')
                result['checks'].append('normal-uninstall-preserved-exact-artwork-and-personal-data')
                result['qualification'] = 'passed'
            # Keep proved user data and private logs; discard no artifacts here.
        receipt['qualification'] = 'installer-default-download-and-existing-parts-preservation-passed'
    finally:
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--target', choices=('windows-x64', 'windows-arm64'), required=True)
    parser.add_argument('--parts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = qualify(args.installer.resolve(), args.target, args.parts.resolve(), args.output.resolve())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        print('Installer artwork qualification failed; inspect the private fixture locally.', file=sys.stderr)
        return 1
    print(receipt['qualification'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
