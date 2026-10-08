#!/usr/bin/env python3
"""Diagnostic-only replay of the exact retained ARM bundle; never a release gate."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'de4b18c0372d5c55914e66762d79994058f8cfc4'
RUN = 37748289680
JOB = 113214974109
ATTEMPT = 1
ARTIFACT = 11541795873
ARTIFACT_SHA = '67ba14adb77040b8e6233a07be387bcf77dd06fc081b4a4c85ff8e8c65adf4d5'
TARGET = 'windows-arm64'
SECOND_CLICK = '$p=Start-Process -FilePath $args[0] -Wait -PassThru;exit $p.ExitCode'
ERROR_MARKERS = (
    'browser launch refuses an application without a verified owned server process',
    'recorded process identity is not verifiable',
    'running but not ready', 'public application identity unavailable',
    'unexpected readiness response', 'unexpected authentication response',
    'authentication endpoint returned HTTP', 'connection refused',
    'another Open Clank start is still in progress',
    'Open Clank start lock could not be verified', 'could not clear stale start lock',
    'could not create Open Clank start lock', 'could not persist Open Clank start lock',
    'could not start Open Clank', 'Open Clank did not become ready',
)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(endpoint, destination=None):
    request = urllib.request.Request('https://api.github.com/' + endpoint,
        headers={'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    request.add_unredirected_header('Authorization', 'Bearer ' + os.environ['GH_TOKEN'])
    with urllib.request.urlopen(request, timeout=120) as response:
        if not response.geturl().startswith('https://'):
            raise RuntimeError('https-required')
        if destination is not None:
            with destination.open('xb') as stream:
                shutil.copyfileobj(response, stream, 1024 * 1024)
            return
        body = response.read(8 * 1024**2 + 1)
        if len(body) > 8 * 1024**2:
            raise RuntimeError('metadata-bound-exceeded')
        return json.loads(body)


def safe_members(archive, *, payload=False):
    members = archive.infolist()
    names = [item.filename for item in members]
    if len(names) != len(set(n.casefold() for n in names)) or sum(i.file_size for i in members) > 8 * 1024**3:
        raise RuntimeError('archive-bound-or-duplicates')
    for item in members:
        p = PurePosixPath(item.filename)
        mode = item.external_attr >> 16
        if (p.is_absolute() or not p.parts or p.as_posix() != item.filename.rstrip('/') or
            any(part in {'', '.', '..'} or part.endswith(('.', ' ')) or
                re.search(r'[\\:*?"<>|]', part) or
                re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:[.].*)?', part) for part in p.parts) or
            (mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}) or
            (payload and p.parts[0] != 'openclank')):
            raise RuntimeError('unsafe-archive-member')
    return members


def admit(work, receipt):
    repo = os.environ['GITHUB_REPOSITORY']
    if repo != 'Plaer1/open-clank':
        raise RuntimeError('foreign-repository')
    api = 'repos/' + repo
    run = fetch(f'{api}/actions/runs/{RUN}')
    if (run['head_sha'], run['head_branch'], run['path'], run['event']) != (
        SOURCE, 'codex/beta1-release-build-20261006', '.github/workflows/beta-release-build.yml', 'push'):
        raise RuntimeError('run-identity-mismatch')
    if run['repository']['full_name'] != repo or run['head_repository']['full_name'] != repo:
        raise RuntimeError('foreign-run')
    job = fetch(f'{api}/actions/jobs/{JOB}')
    steps = {s['name']: s['conclusion'] for s in job['steps']}
    if (job['run_id'], job['run_attempt'], job['head_sha'], job['status'], job['conclusion']) != (
        RUN, ATTEMPT, SOURCE, 'completed', 'failure'):
        raise RuntimeError('original-producing-job-mismatch')
    if (steps.get('Qualify actual native packaged first run'),
        steps.get('Build per-user installer from the qualified bundle'),
        steps.get('Qualify actual installed startup and data-preserving uninstall')) != ('success', 'success', 'failure'):
        raise RuntimeError('original-failure-boundary-mismatch')
    # Never use carried-over attempt2 job IDs or latest-job discovery.
    listing = fetch(f'{api}/actions/runs/{RUN}/artifacts?per_page=100')
    if listing['total_count'] > 100:
        raise RuntimeError('artifact-count-bound')
    matches = [a for a in listing['artifacts'] if a['id'] == ARTIFACT]
    if len(matches) != 1:
        raise RuntimeError('require-original-artifact')
    artifact = matches[0]
    if (artifact['name'], artifact['expired'], artifact['digest'], artifact['workflow_run']['id'],
        artifact['workflow_run']['head_sha']) != ('Unqualified-portable-' + TARGET, False, 'sha256:' + ARTIFACT_SHA, RUN, SOURCE):
        raise RuntimeError('retained-artifact-identity-mismatch')
    receipt['original_job_failure_boundary_verified'] = True
    for name in ['scripts/windows-installer.iss', 'scripts/build_windows_installer.py',
                 'scripts/qualify_windows_installer.py', 'scripts/qualify_windows_portable.py',
                 'scripts/qualify_treehouse.py']:
        source = fetch(f'{api}/contents/{name}?ref={SOURCE}')
        if hashlib.sha256(base64.b64decode(source['content'])).hexdigest() != sha(ROOT / name):
            raise RuntimeError('replay-source-authority-differs')
    receipt['installer_and_qualification_source_unchanged'] = True
    outer = work / 'retained-actions.zip'
    fetch(f'{api}/actions/artifacts/{ARTIFACT}/zip', outer)
    if sha(outer) != ARTIFACT_SHA:
        raise RuntimeError('official-artifact-digest-mismatch')
    with zipfile.ZipFile(outer) as archive:
        names = [i.filename for i in safe_members(archive)]
        zips = [n for n in names if re.fullmatch(r'Open-Clank-[A-Za-z0-9._-]+-windows-arm64.zip', n)]
        if len(zips) != 1:
            raise RuntimeError('require-one-retained-portable')
        name = zips[0]
        selected = [name, name + '.sha256', 'portable-zip-' + TARGET + '.json', 'unqualified-portable-' + TARGET + '.json']
        if sorted(names) != sorted(selected):
            raise RuntimeError('unexpected-retained-members')
        for n in selected:
            with archive.open(n) as source, (work / n).open('xb') as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
    outer.unlink()  # Only this fresh, digest-admitted temporary download.
    portable = work / name
    portable_sha = sha(portable)
    checksum = (work / (name + '.sha256')).read_text().split()
    retained = json.loads((work / ('unqualified-portable-' + TARGET + '.json')).read_text())
    relocation = json.loads((work / ('portable-zip-' + TARGET + '.json')).read_text())
    if checksum not in ([portable_sha, name], [portable_sha, '*' + name]):
        raise RuntimeError('retained-portable-checksum-mismatch')
    expected = {'role': 'sealed-unqualified-portable-diagnostic-only', 'source_sha': SOURCE,
        'run_id': RUN, 'run_attempt': ATTEMPT, 'target': TARGET, 'repository': repo,
        'ref': 'refs/heads/codex/beta1-release-build-20261006', 'workflow': '.github/workflows/beta-release-build.yml',
        'archive_name': name, 'archive_sha256': portable_sha, 'archive_bytes': portable.stat().st_size,
        'qualification': 'runtime-qualification-not-yet-performed'}
    if any(retained.get(k) != v for k, v in expected.items()) or relocation.get('zip_sha256') != portable_sha or relocation.get('qualification') != 'exact-emitted-zip-relocation-admitted':
        raise RuntimeError('retained-identity-receipt-mismatch')
    destination = work / 'relocated'
    with zipfile.ZipFile(portable) as archive:
        members = safe_members(archive, payload=True)
        total = sum(i.file_size for i in members)
        if shutil.disk_usage(work).free < total * 2 + 2 * 1024**3:
            raise RuntimeError('insufficient-replay-disk')
        destination.mkdir()
        archive.extractall(destination)
    receipt.update(retained_artifact_sha256=ARTIFACT_SHA, portable_zip_sha256=portable_sha,
        original_producing_attempt=ATTEMPT, original_producing_job_id=JOB)
    return destination / 'openclank'


def replay(args, receipt):
    if os.name != 'nt' or platform.machine().lower() not in {'arm64', 'aarch64'}:
        raise RuntimeError('native-arm-python-required')
    if args.work.exists() or args.output.exists():
        raise RuntimeError('preserve-existing-replay-paths')
    args.work.mkdir(parents=True)
    receipt['stage'] = 'retained-artifact-admission'
    bundle = admit(args.work, receipt)
    from qualify_windows_portable import qualify as qualify_portable
    import qualify_windows_installer as installed
    from build_windows_installer import build
    portable_output = args.work / 'portable-qualification'
    receipt['stage'] = 'actual-retained-portable-qualification'
    qualify_portable(bundle, TARGET, portable_output, emoji_pack=args.emoji_pack)
    receipt['exact_retained_portable_requalified'] = True
    installers = args.work / 'installers'
    receipt['stage'] = 'same-source-installer-compilation'
    build(bundle, TARGET, installers, args.work / 'compiler', portable_output / 'receipt.json')
    setups = list(installers.glob('*-Setup.exe'))
    if len(setups) != 1:
        raise RuntimeError('require-one-replay-setup')
    receipt['replay_installer_sha256'] = sha(setups[0])
    receipt['replay_installer_source_sha256'] = sha(ROOT / 'scripts/windows-installer.iss')
    receipt['replay_installer_is_recompiled_not_original_setup'] = True
    original = installed.powershell

    def observe(script, *arguments, environment=None):
        second_click = script == SECOND_CLICK and len(arguments) == 1 and Path(arguments[0]).name == 'Open Clank.lnk'
        lock = Path(environment['APPDATA']) / 'OpenClank/runtime/server.start.lock' if second_click else None
        if second_click:
            receipt['start_lock_present_before_actual_second_click'] = lock.is_file()
        try:
            return original(script, *arguments, environment=environment)
        except subprocess.CalledProcessError as error:
            if script != SECOND_CLICK or len(arguments) != 1 or Path(arguments[0]).name != 'Open Clank.lnk':
                raise
            receipt['actual_second_shortcut_exit_code'] = error.returncode
            receipt['start_lock_present_at_second_click_failure'] = lock.is_file()
            private = args.work / 'second-click-private'
            private.mkdir()
            (private / 'powershell-stdout.txt').write_text(error.stdout or '')
            (private / 'powershell-stderr.txt').write_text(error.stderr or '')
            receipt['known_powershell_error_markers'] = [s for s in ERROR_MARKERS if s in (error.stdout or '') + (error.stderr or '')]
            details = installed.shortcut_detail(Path(arguments[0]), environment=environment)
            target = Path(details['Target'])
            if details['Arguments'] != 'server start --open-browser' or Path(details['WorkingDirectory']) != target.parent or target.name != 'openclank.exe':
                raise RuntimeError('equivalent-client-shortcut-identity-mismatch')
            state = Path(environment['APPDATA']) / 'OpenClank/runtime/server.json'
            before = json.loads(state.read_text())
            # Same installed executable/args/workdir/environment, while original owned server is live.
            # This direct diagnostic does not replace the failed real-shortcut assertion.
            try:
                direct = subprocess.run([str(target), 'server', 'start', '--open-browser'],
                    cwd=target.parent, env=environment, capture_output=True, text=True, timeout=90)
                (private / 'direct-stdout.txt').write_text(direct.stdout)
                (private / 'direct-stderr.txt').write_text(direct.stderr)
                text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', direct.stdout + direct.stderr)
                receipt['equivalent_direct_client_exit_code'] = direct.returncode
                receipt['known_client_error_markers'] = [s for s in ERROR_MARKERS if s in text]
                after = json.loads(state.read_text())
                receipt['equivalent_direct_client_kept_original_pid'] = before.get('pid') == after.get('pid')
            except subprocess.TimeoutExpired:
                receipt['equivalent_direct_client_timed_out'] = True
            receipt['start_lock_present_after_equivalent_direct_client'] = lock.is_file()
            receipt['actual_second_shortcut_failed'] = True
            raise  # Preserve the actual shortcut/reuse gate exactly.

    installed.powershell = observe
    try:
        receipt['stage'] = 'actual-installed-shortcut-replay'
        result = installed.qualify(setups[0], TARGET, args.work / 'installed-qualification', args.emoji_pack)
        receipt['installed_replay_qualification'] = result['qualification']
        receipt['diagnostic_outcome'] = 'installed-replay-passed'
    except subprocess.CalledProcessError:
        if not receipt.get('actual_second_shortcut_failed'):
            raise
        receipt['diagnostic_outcome'] = 'actual-second-shortcut-failure-captured-unqualified'
    finally:
        installed.powershell = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--emoji-pack', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    receipt = {'schema_version': 1, 'role': 'diagnostic-only-not-release-admission',
        'source_sha': SOURCE, 'native_build_run_id': RUN, 'original_producing_attempt': ATTEMPT,
        'original_producing_job_id': JOB, 'original_retained_artifact_id': ARTIFACT, 'target': TARGET,
        'diagnostic_outcome': 'not-completed'}
    code = 0
    try:
        replay(args, receipt)
    except Exception as error:
        receipt['diagnostic_outcome'] = 'unexpected-replay-failure'
        receipt['exception_class'] = type(error).__name__
        if isinstance(error, RuntimeError) and re.fullmatch(r'[a-z][a-z-]{1,79}', str(error)):
            receipt['safe_internal_error_code'] = str(error)
        code = 1
    finally:
        if not args.output.exists():
            args.output.mkdir(parents=True)
            (args.output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print('Diagnostic replay finished; only safe receipt is retained')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
