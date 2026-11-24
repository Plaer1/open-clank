#!/usr/bin/env python3
"""Fully qualify exact retained native Windows payloads without rebuilding them."""
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
import traceback
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'de4b18c0372d5c55914e66762d79994058f8cfc4'
RUN = 37748289680
BRANCH = 'codex/beta1-retained-windows-qualification-20261008'
WORKFLOW = '.github/workflows/beta-windows-retained-qualification.yml'
PRODUCERS = {
    'windows-arm64': {'job': 113214974109, 'attempt': 1, 'artifact': 11541795873,
        'digest': '67ba14adb77040b8e6233a07be387bcf77dd06fc081b4a4c85ff8e8c65adf4d5'},
    'windows-x64': {'job': 113256576281, 'attempt': 2, 'artifact': 11543277487,
        'digest': 'efce80092ae017a7ea14ebd1b7ca1d04133954b7d5b16ec4a4722f1ff7f27b0a'},
}
ORIGINAL_SNAPSHOT = '$all=@(Get-CimInstance Win32_Process);$ids=@([int]$args[0]);$found=@();do{$next=@($all|Where-Object {$_.ParentProcessId -in $ids -and $_.ProcessId -notin $found.ProcessId});$found+=@($next);$ids=@($next.ProcessId)}while($ids.Count -gt 0);ConvertTo-Json -InputObject @($found|Select-Object ProcessId,CreationDate) -Compress'
REPAIRED_SNAPSHOT = '$all=@(Get-CimInstance Win32_Process);$ids=@([int]$args[0]);$found=@();do{$next=@($all|Where-Object {$_.ParentProcessId -in $ids -and $_.ProcessId -notin $found.ProcessId});$found+=@($next);if($next.Count -eq 0){break};$ids=@($next|ForEach-Object {$_.ProcessId})}while($ids.Count -gt 0);ConvertTo-Json -InputObject @($found|Select-Object ProcessId,CreationDate) -Compress'


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


def admit(work, receipt, target):
    pins = PRODUCERS[target]
    job_id, attempt, artifact_id, artifact_sha = (pins[k] for k in ("job", "attempt", "artifact", "digest"))
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
    if run['status'] != 'completed' or run['conclusion'] != 'failure':
        raise RuntimeError('original-mainrun-must-remain-failed')
    job = fetch(f'{api}/actions/jobs/{job_id}')
    steps = {s['name']: s['conclusion'] for s in job['steps']}
    if (job['run_id'], job['run_attempt'], job['head_sha'], job['status'], job['conclusion']) != (
        RUN, attempt, SOURCE, 'completed', 'failure'):
        raise RuntimeError('original-producing-job-mismatch')
    if (steps.get('Qualify actual native packaged first run'),
        steps.get('Build per-user installer from the qualified bundle'),
        steps.get('Qualify actual installed startup and data-preserving uninstall')) != ('success', 'success', 'failure'):
        raise RuntimeError('original-failure-boundary-mismatch')
    # Never use carried-over attempt2 job IDs or latest-job discovery.
    listing = fetch(f'{api}/actions/runs/{RUN}/artifacts?per_page=100')
    if listing['total_count'] > 100:
        raise RuntimeError('artifact-count-bound')
    matches = [a for a in listing['artifacts'] if a['id'] == artifact_id]
    if len(matches) != 1:
        raise RuntimeError('require-original-artifact')
    artifact = matches[0]
    if (artifact['name'], artifact['expired'], artifact['digest'], artifact['workflow_run']['id'],
        artifact['workflow_run']['head_sha']) != ('Unqualified-portable-' + target, False, 'sha256:' + artifact_sha, RUN, SOURCE):
        raise RuntimeError('retained-artifact-identity-mismatch')
    receipt['original_job_failure_boundary_verified'] = True
    for name in ['scripts/windows-installer.iss', 'scripts/build_windows_installer.py',
                 'scripts/qualify_windows_installer.py', 'scripts/qualify_windows_portable.py',
                 'scripts/qualify_treehouse.py', 'scripts/qualify_theme_emoji.py',
                 'scripts/emoji_runtime_schema.py', 'scripts/emoji_asset_bundle.py']:
        source = fetch(f'{api}/contents/{name}?ref={SOURCE}')
        content = base64.b64decode(source['content'])
        if name == 'scripts/qualify_windows_installer.py':
            if content.count(ORIGINAL_SNAPSHOT.encode()) != 1:
                raise RuntimeError('original-qualifier-repair-authority-differs')
            content = content.replace(ORIGINAL_SNAPSHOT.encode(), REPAIRED_SNAPSHOT.encode())
        if hashlib.sha256(content).hexdigest() != sha(ROOT / name):
            raise RuntimeError('runtime-or-issuer-source-authority-differs')
    receipt['runtime_and_issuer_sources_exact_original'] = True
    receipt['qualifier_only_empty_frontier_repair'] = True
    outer = work / 'retained-actions.zip'
    fetch(f'{api}/actions/artifacts/{artifact_id}/zip', outer)
    if sha(outer) != artifact_sha:
        raise RuntimeError('official-artifact-digest-mismatch')
    with zipfile.ZipFile(outer) as archive:
        names = [i.filename for i in safe_members(archive)]
        zips = [n for n in names if re.fullmatch(r'Open-Clank-[A-Za-z0-9._-]+-' + re.escape(target) + r'\.zip', n)]
        if len(zips) != 1:
            raise RuntimeError('require-one-retained-portable')
        name = zips[0]
        selected = [name, name + '.sha256', 'portable-zip-' + target + '.json', 'unqualified-portable-' + target + '.json']
        if sorted(names) != sorted(selected):
            raise RuntimeError('unexpected-retained-members')
        for n in selected:
            with archive.open(n) as source, (work / n).open('xb') as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
    outer.unlink()  # Only this fresh, digest-admitted temporary download.
    portable = work / name
    portable_sha = sha(portable)
    checksum = (work / (name + '.sha256')).read_text().split()
    retained = json.loads((work / ('unqualified-portable-' + target + '.json')).read_text())
    relocation = json.loads((work / ('portable-zip-' + target + '.json')).read_text())
    if checksum not in ([portable_sha, name], [portable_sha, '*' + name]):
        raise RuntimeError('retained-portable-checksum-mismatch')
    expected = {'role': 'sealed-unqualified-portable-diagnostic-only', 'source_sha': SOURCE,
        'run_id': RUN, 'run_attempt': attempt, 'target': target, 'repository': repo,
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
    receipt.update(retained_artifact_sha256=artifact_sha, portable_zip_sha256=portable_sha,
        original_producing_attempt=attempt, original_producing_job_id=job_id)
    return destination / 'openclank'


def tool_identity(target):
    repo = os.environ['GITHUB_REPOSITORY']
    tool_source = os.environ['GITHUB_SHA']
    run_id = int(os.environ['GITHUB_RUN_ID'])
    attempt = int(os.environ['GITHUB_RUN_ATTEMPT'])
    if (repo != 'Plaer1/open-clank' or
        os.environ['GITHUB_REF'] != 'refs/heads/' + BRANCH or
        not re.fullmatch(r'[0-9a-f]{40}', tool_source)):
        raise RuntimeError('qualification-tool-environment-mismatch')
    api = 'repos/' + repo
    run = fetch(f'{api}/actions/runs/{run_id}')
    if (run['head_sha'], run['head_branch'], run['path'], run['event'],
        run['repository']['full_name'], run['head_repository']['full_name']) != (
        tool_source, BRANCH, WORKFLOW, 'push', repo, repo):
        raise RuntimeError('qualification-tool-run-identity-mismatch')
    jobs = fetch(f'{api}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
    if jobs['total_count'] > 100:
        raise RuntimeError('qualification-tool-job-count-bound')
    matches = [j for j in jobs['jobs'] if j['name'] == 'qualify-' + target]
    if len(matches) != 1 or matches[0]['run_attempt'] != attempt or matches[0]['head_sha'] != tool_source:
        raise RuntimeError('qualification-tool-producing-job-mismatch')
    for name in ['scripts/qualify_retained_windows_release.py',
                 'scripts/qualify_windows_installer.py']:
        source = fetch(f'{api}/contents/{name}?ref={tool_source}')
        if hashlib.sha256(base64.b64decode(source['content'])).hexdigest() != sha(ROOT / name):
            raise RuntimeError('executed-qualification-tool-source-mismatch')
    return {'qualification_source_sha': tool_source, 'qualification_run_id': run_id,
        'qualification_producing_attempt': attempt, 'qualification_job_id': matches[0]['id'],
        'qualification_ref': 'refs/heads/' + BRANCH, 'qualification_workflow': WORKFLOW,
        'qualification_event': 'push'}


def qualify(args, checkpoint):
    checkpoint["stage"] = "tool-identity"
    architecture = platform.machine().lower()
    expected_architecture = {'windows-arm64': {'arm64', 'aarch64'},
                             'windows-x64': {'amd64', 'x86_64'}}[args.target]
    if os.name != 'nt' or architecture not in expected_architecture:
        raise RuntimeError('matching-native-windows-python-required')
    if args.work.exists() or args.output.exists():
        raise RuntimeError('preserve-existing-qualification-paths')
    identity = tool_identity(args.target)
    checkpoint["tool_identity_verified"] = True
    args.work.mkdir(parents=True)
    chain = {'schema_version': 1, 'role': 'independently-requalified-retained-native-payload',
        'runtime_source_sha': SOURCE, 'target': args.target, 'original_run_id': RUN,
        'original_mainrun_conclusion': 'failure', **identity}
    checkpoint["stage"] = "retained-admission"
    bundle = admit(args.work, chain, args.target)
    from qualify_windows_portable import qualify as qualify_portable
    from qualify_windows_installer import qualify as qualify_installed
    from build_windows_installer import build
    portable_output = args.work / 'portable-qualification'
    checkpoint["stage"] = "full-portable"
    portable = qualify_portable(bundle, args.target, portable_output, emoji_pack=args.emoji_pack)
    if portable['qualification'] != 'packaged-startup-auth-files-editor-passed':
        raise RuntimeError('full-retained-portable-qualification-required')
    installer_output = args.work / 'installers'
    checkpoint["stage"] = "compiler"
    build(bundle, args.target, installer_output, args.work / 'compiler', portable_output / 'receipt.json')
    setups = list(installer_output.glob('*-Setup.exe'))
    if len(setups) != 1:
        raise RuntimeError('require-one-recompiled-native-payload-setup')
    setup = setups[0]
    installed_output = args.work / 'installed-qualification'
    checkpoint["stage"] = "full-installed"
    installed = qualify_installed(setup, args.target, installed_output, args.emoji_pack)
    if installed['qualification'] != 'per-user-installer-start-stop-auth-files-editor-uninstall-passed':
        raise RuntimeError('full-retained-installed-qualification-required')
    # Export only named payloads and safe receipts after EVERY real gate passed.
    # The private account/data fixtures and command logs remain under --work.
    checkpoint["stage"] = "export"
    args.output.mkdir(parents=True)
    retained = args.work / ('unqualified-portable-' + args.target + '.json')
    record = json.loads(retained.read_text())
    zip_name = record['archive_name']
    selected = {
        zip_name: args.work / zip_name,
        zip_name + '.sha256': args.work / (zip_name + '.sha256'),
        'original-retention.json': retained,
        'original-relocation.json': args.work / ('portable-zip-' + args.target + '.json'),
        setup.name: setup,
        setup.name + '.sha256': setup.with_suffix('.exe.sha256'),
        setup.with_suffix('.provenance.json').name: setup.with_suffix('.provenance.json'),
        'portable-qualification.json': portable_output / 'receipt.json',
        'installed-qualification.json': installed_output / 'receipt.json',
        'installed-runtime-qualification.json': installed_output / 'installed-runtime-receipt.json',
    }
    chain['files'] = {}
    for name, path in selected.items():
        shutil.copyfile(path, args.output / name)
        chain['files'][name] = {'sha256': sha(path), 'bytes': path.stat().st_size}
    chain.update(qualification='full-native-retained-portable-and-installed-passed',
        installer_name=setup.name, installer_sha256=sha(setup),
        installer_is_recompiled_not_original_setup=True,
        original_artifact_id=PRODUCERS[args.target]['artifact'],
        canonical_qualifier_sha256=sha(ROOT / 'scripts/qualify_windows_installer.py'),
        portable_qualification_sha256=sha(portable_output / 'receipt.json'),
        installed_qualification_sha256=sha(installed_output / 'receipt.json'),
        installed_runtime_qualification_sha256=sha(installed_output / 'installed-runtime-receipt.json'))
    (args.output / 'proof-chain.json').write_text(json.dumps(chain, indent=2) + '\n')



def positive_environment_integer(name):
    value = os.environ.get(name, '')
    return int(value) if re.fullmatch(r'[1-9][0-9]{0,19}', value) else None


def safe_failure(error, checkpoint):
    allowed = {'qualify_retained_windows_release.py', 'qualify_windows_installer.py',
        'qualify_windows_portable.py', 'qualify_treehouse.py', 'qualify_theme_emoji.py',
        'emoji_runtime_schema.py', 'emoji_asset_bundle.py', 'build_windows_installer.py'}
    frames = []
    for frame, line in traceback.walk_tb(error.__traceback__):
        script = Path(frame.f_code.co_filename).resolve()
        function = frame.f_code.co_name
        if (script.parent == ROOT / 'scripts' and script.name in allowed and
            re.fullmatch(r'[A-Za-z_<>][A-Za-z0-9_<>]{0,127}', function)):
            frames.append({'script': script.name, 'line': line, 'function': function})
    exception_class = type(error).__name__
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', exception_class):
        exception_class = 'Exception'
    record = {**checkpoint, 'outcome': 'failed-not-release-admission',
        'exception_class': exception_class, 'traceback_frames': frames}
    if isinstance(error, subprocess.CalledProcessError) and type(error.returncode) is int:
        record['subprocess_returncode'] = error.returncode
    return record

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=tuple(PRODUCERS), required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--emoji-pack', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--failure-receipt', type=Path, required=True)
    args = parser.parse_args()
    supplied_source = os.environ.get('GITHUB_SHA', '')
    checkpoint = {'schema_version': 1, 'role': 'safe-retained-native-failure-evidence',
        'repository': 'Plaer1/open-clank',
        'repository_identity_matched': os.environ.get('GITHUB_REPOSITORY') == 'Plaer1/open-clank',
        'runtime_source_sha': SOURCE, 'original_run_id': RUN, 'target': args.target,
        'tool_source_sha': supplied_source if re.fullmatch(r'[0-9a-f]{40}', supplied_source) else None,
        'tool_run_id': positive_environment_integer('GITHUB_RUN_ID'),
        'tool_run_attempt': positive_environment_integer('GITHUB_RUN_ATTEMPT'),
        'tool_identity_verified': False, 'stage': 'tool-identity'}
    failure_path = args.failure_receipt.resolve()
    if (failure_path.exists() or failure_path == args.output.resolve() or
        failure_path.is_relative_to(args.output.resolve()) or
        failure_path.is_relative_to(args.work.resolve())):
        print('A fresh independent safe failure receipt path is required')
        return 1
    try:
        qualify(args, checkpoint)
    except Exception as error:
        # The release workflow redirects this entire process to a private log.
        # Preserve the failed gate there; only fixed workflow status is public.
        traceback.print_exc()
        failure_path.parent.mkdir(parents=True, exist_ok=True)
        with failure_path.open('x', encoding='utf-8') as stream:
            json.dump(safe_failure(error, checkpoint), stream, indent=2)
            stream.write('\n')
        return 1
    print('Full retained native portable and installed qualification passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
