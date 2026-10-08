#!/usr/bin/env python3
"""Native Inno shortcut fixture; never a runtime/package qualification receipt."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import traceback

from build_windows_installer import SOURCE, TOOL_SHA256, TOOL_VERSION, digest, install_compiler
from qualify_windows_installer import powershell, shortcut_comparison

FOLDERS = "[ordered]@{Programs=[Environment]::GetFolderPath('Programs');Desktop=[Environment]::GetFolderPath('Desktop')}|ConvertTo-Json -Compress"


def diagnose(target, output, work):
    if os.name != 'nt' or output.exists() or work.exists():
        raise RuntimeError('Native shortcut diagnosis requires Windows and fresh output/private directories')
    output.mkdir(parents=True)
    work.mkdir(parents=True)
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.endswith(('_TOKEN', '_API_KEY')):
            environment.pop(key, None)
    before = json.loads(powershell(FOLDERS))
    home = work / 'fixture-home'
    appdata = work / 'fixture-appdata'
    home.mkdir()
    appdata.mkdir()
    environment.update(USERPROFILE=str(home), HOME=str(home), APPDATA=str(appdata))
    after = json.loads(powershell(FOLDERS, environment=environment))
    group_name = 'Open Clank diagnostic ' + secrets.token_hex(8)
    desktop_name = f'Open Clank Beta ({target}).lnk'
    # Refuse every possible product-shaped desktop collision before Inno runs.
    candidates = {Path(folders['Desktop']) / desktop_name for folders in (before, after)}
    if any(path.exists() for path in candidates):
        raise RuntimeError('An existing desktop shortcut must remain untouched')
    bundle = work / 'fixture-bundle'
    bundle.mkdir()
    # Real native target bytes make WScript resolve an executable normally;
    # this fixture never runs cmd.exe as an Open Clank app.
    shutil.copyfile(Path(os.environ['SystemRoot']) / 'System32/cmd.exe', bundle / 'openclank.exe')
    source = SOURCE.read_text(encoding='utf-8')
    fixture = source.split('[Code]', 1)[0]
    fixture = fixture.replace('AppId=OpenClank.Beta1.{#Target}', 'AppId=OpenClank.ShortcutDiagnostic.' + group_name.rsplit(' ', 1)[1] + '.{#Target}')
    if fixture == source.split('[Code]', 1)[0]:
        raise RuntimeError('Diagnostic AppId isolation did not match the reviewed source')
    fixture_source = work / 'shortcut-fixture.iss'
    fixture_source.write_text(fixture, encoding='utf-8')
    installer_output = work / 'compiler-output'
    installer_output.mkdir()
    compiler = install_compiler(work / 'compiler-tools')
    with (work / 'compiler-private.log').open('wb') as log:
        subprocess.run([str(compiler), '--no-signing', '--no-ide-signtools', '/DTarget=' + target,
                        '/DBundleRoot=' + str(bundle), '/DAppVersion=1.0.2', '/DOutputRoot=' + str(installer_output),
                        str(fixture_source)], check=True, timeout=180, stdout=log, stderr=log)
    installer = installer_output / f'Open-Clank-1.0.2-{target}-Setup.exe'
    installation = work / 'installation'
    executable = installation / 'payload/openclank.exe'
    receipt = {'kind': 'native-shortcut-diagnostic-not-app-qualification', 'target': target,
               'installer_bootstrap_architecture': 'x64', 'compiler_version': TOOL_VERSION,
               'compiler_sha256': TOOL_SHA256, 'installer_source_sha256': digest(SOURCE),
               'fixture_source_sha256': digest(fixture_source), 'fixture_target_sha256': digest(bundle / 'openclank.exe'),
               'known_folders_equal_under_override': {key.lower(): before[key] == after[key] for key in before},
               'comparisons': [], 'uninstalled': False}
    raw = {'folders_before': before, 'folders_with_override': after, 'comparisons': []}
    uninstaller = installation / 'unins000.exe'
    try:
        subprocess.run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART',
                        '/DIR=' + str(installation), '/GROUP=' + group_name, '/TASKS=desktopicon',
                        '/LOG=' + str(work / 'installer-private.log')], env=environment, check=True, timeout=180)
        # Test historical and same-environment known folders, plus the exact
        # APPDATA-derived location Inno may select. No user-wide search.
        locations = [('before-override', before), ('same-environment', after),
                     ('appdata-environment', {'Programs': str(appdata / 'Microsoft/Windows/Start Menu/Programs'),
                                               'Desktop': str(home / 'Desktop')})]
        for location, folders in locations:
            for role, link, arguments in [
                ('start-menu-launch', Path(folders['Programs']) / group_name / 'Open Clank.lnk', 'server start --open-browser'),
                ('start-menu-stop', Path(folders['Programs']) / group_name / 'Stop Open Clank.lnk', 'server stop'),
                ('desktop-launch', Path(folders['Desktop']) / desktop_name, 'server start --open-browser')]:
                safe, detail = shortcut_comparison(link, executable, arguments, role=role, environment=environment)
                safe['location'] = location
                receipt['comparisons'].append(safe)
                raw['comparisons'].append({'location': location, 'role': role, 'link': str(link), 'actual': detail})
    finally:
        if uninstaller.is_file():
            subprocess.run([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                            '/LOG=' + str(work / 'uninstaller-private.log')], env=environment, check=True, timeout=180)
            receipt['uninstalled'] = not executable.exists()
        (work / 'details-private.json').write_text(json.dumps(raw, indent=2) + '\n', encoding='utf-8')
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('windows-x64', 'windows-arm64'), required=True)
    parser.add_argument('--output', type=Path, required=True, help='safe receipts only')
    parser.add_argument('--work-root', type=Path, required=True, help='fresh private fixture/compiler/log root; never upload')
    args = parser.parse_args()
    work_existed = args.work_root.exists()
    try:
        diagnose(args.target, args.output.resolve(), args.work_root.resolve())
    except Exception as exc:
        # Errors such as CalledProcessError contain private fixture paths.
        # Keep the traceback private; publish only its fixed class name.
        if not work_existed and args.work_root.is_dir():
            with (args.work_root / 'failure-private.log').open('x', encoding='utf-8') as log:
                traceback.print_exc(file=log)
        print('native-shortcut-diagnostic-failed: ' + type(exc).__name__)
        return 1
    print('native-shortcut-diagnostic-complete-not-app-qualification')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
