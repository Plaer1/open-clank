#!/usr/bin/env python3
"""Admit an independently qualified retained Windows installer proof chain."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import zipfile

from qualify_retained_windows_release import (
    BRANCH, WORKFLOW, PRODUCERS, ROOT, RUN, SOURCE, ORIGINAL_SNAPSHOT,
    REPAIRED_SNAPSHOT, fetch, safe_members, sha,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STEP = 'Qualify exact retained native portable and installed release'


def source_bytes(api, name, revision):
    record = fetch(f'{api}/contents/{name}?ref={revision}')
    return base64.b64decode(record['content'])


def require_checks(record, target, qualification, required):
    if record.get('target') != target or record.get('qualification') != qualification:
        raise RuntimeError('full-native-receipt-qualification-required')
    if not set(required).issubset(record.get('checks', [])):
        raise RuntimeError('required-native-journey-check-missing')


def admit(args):
    if os.environ['GITHUB_REPOSITORY'] != 'Plaer1/open-clank':
        raise RuntimeError('foreign-repository')
    if not re.fullmatch(r'[0-9a-f]{40}', args.qualification_source):
        raise RuntimeError('qualification-source-required')
    if args.output.exists() or args.receipt.exists():
        raise RuntimeError('preserve-existing-admission-paths')
    api = 'repos/Plaer1/open-clank'
    run = fetch(f'{api}/actions/runs/{args.run_id}')
    if (run['status'], run['conclusion'], run['head_sha'], run['head_branch'],
        run['path'], run['event'], run['repository']['full_name'],
        run['head_repository']['full_name']) != (
        'completed', 'success', args.qualification_source, BRANCH, WORKFLOW,
        'push', 'Plaer1/open-clank', 'Plaer1/open-clank'):
        raise RuntimeError('successful-exact-tool-qualification-run-required')
    for name in ['scripts/qualify_retained_windows_release.py',
                 'scripts/verify_retained_windows_release.py',
                 'scripts/qualify_windows_installer.py']:
        if hashlib.sha256(source_bytes(api, name, args.qualification_source)).hexdigest() != sha(ROOT / name):
            raise RuntimeError('executed-admission-tool-source-mismatch')
    for name in ['scripts/build_windows_installer.py', 'scripts/qualify_windows_portable.py',
                 'scripts/qualify_treehouse.py', 'scripts/qualify_theme_emoji.py',
                 'scripts/emoji_runtime_schema.py', 'scripts/emoji_asset_bundle.py']:
        if source_bytes(api, name, SOURCE) != (ROOT / name).read_bytes():
            raise RuntimeError('original-runtime-qualification-authority-mismatch')
    frozen = source_bytes(api, 'scripts/qualify_windows_installer.py', SOURCE)
    if frozen.count(ORIGINAL_SNAPSHOT.encode()) != 1 or frozen.replace(
        ORIGINAL_SNAPSHOT.encode(), REPAIRED_SNAPSHOT.encode()) != (ROOT / 'scripts/qualify_windows_installer.py').read_bytes():
        raise RuntimeError('canonical-tool-fix-is-not-exact-bounded-repair')
    original = fetch(f'{api}/actions/runs/{RUN}')
    if (original['head_sha'], original['status'], original['conclusion'],
        original['head_branch'], original['path'], original['event'],
        original['repository']['full_name'], original['head_repository']['full_name']) != (
        SOURCE, 'completed', 'failure', 'codex/beta1-release-build-20261006',
        '.github/workflows/beta-release-build.yml', 'push', 'Plaer1/open-clank', 'Plaer1/open-clank'):
        raise RuntimeError('original-failed-runtime-build-identity-mismatch')
    original_artifacts = fetch(f'{api}/actions/runs/{RUN}/artifacts?per_page=100')
    artifacts = fetch(f'{api}/actions/runs/{args.run_id}/artifacts?per_page=100')
    if max(original_artifacts['total_count'], artifacts['total_count']) > 100:
        raise RuntimeError('artifact-count-bound')
    # Both actual native target jobs must have succeeded in this exact tool run.
    jobs = fetch(f'{api}/actions/runs/{args.run_id}/jobs?filter=all&per_page=100')
    if jobs['total_count'] > 100:
        raise RuntimeError('qualification-job-count-bound')
    for target in PRODUCERS:
        matches = [j for j in jobs['jobs'] if j['name'] == 'qualify-' + target and j['conclusion'] == 'success']
        if not matches or any(j['head_sha'] != args.qualification_source for j in matches):
            raise RuntimeError('both-native-targets-must-pass')
        if not any(any(s['name'] == STEP and s['conclusion'] == 'success' for s in j['steps']) for j in matches):
            raise RuntimeError('full-native-tool-step-must-pass')
    matches = [a for a in artifacts['artifacts'] if a['name'] == 'Requalified-Windows-' + args.target]
    if len(matches) != 1:
        raise RuntimeError('unique-requalified-target-artifact-required')
    artifact = matches[0]
    if (artifact['expired'] or artifact['workflow_run']['id'] != args.run_id or
        artifact['workflow_run']['head_sha'] != args.qualification_source or
        not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest', ''))):
        raise RuntimeError('qualified-artifact-identity-mismatch')
    args.output.mkdir(parents=True)
    archive = args.output / 'actions-artifact.zip'
    fetch(f"{api}/actions/artifacts/{artifact['id']}/zip", archive)
    if sha(archive) != artifact['digest'][7:]:
        raise RuntimeError('qualified-actions-artifact-digest-mismatch')
    with zipfile.ZipFile(archive) as zipped:
        names = [i.filename for i in safe_members(zipped)]
        if any('/' in n for n in names) or 'proof-chain.json' not in names:
            raise RuntimeError('flat-qualified-artifact-required')
        proof_info = zipped.getinfo('proof-chain.json')
        if proof_info.file_size > 128 * 1024:
            raise RuntimeError('proof-chain-size-bound')
        proof_bytes = zipped.read(proof_info)
        chain = json.loads(proof_bytes)
        pins = PRODUCERS[args.target]
        expected = {'schema_version': 1, 'role': 'independently-requalified-retained-native-payload',
            'qualification': 'full-native-retained-portable-and-installed-passed',
            'runtime_source_sha': SOURCE, 'target': args.target, 'original_run_id': RUN,
            'original_mainrun_conclusion': 'failure', 'original_producing_job_id': pins['job'],
            'original_producing_attempt': pins['attempt'], 'original_artifact_id': pins['artifact'],
            'retained_artifact_sha256': pins['digest'], 'qualification_source_sha': args.qualification_source,
            'qualification_run_id': args.run_id, 'qualification_ref': 'refs/heads/' + BRANCH,
            'qualification_workflow': WORKFLOW, 'qualification_event': 'push',
            'original_job_failure_boundary_verified': True, 'runtime_and_issuer_sources_exact_original': True,
            'qualifier_only_empty_frontier_repair': True, 'installer_is_recompiled_not_original_setup': True,
            'canonical_qualifier_sha256': sha(ROOT / 'scripts/qualify_windows_installer.py')}
        if any(chain.get(k) != v for k, v in expected.items()):
            raise RuntimeError('fixed-qualified-proof-chain-identity-mismatch')
        job = fetch(f"{api}/actions/jobs/{chain['qualification_job_id']}")
        if (job['run_id'], job['run_attempt'], job['head_sha'], job['name'], job['status'], job['conclusion']) != (
            args.run_id, chain['qualification_producing_attempt'], args.qualification_source,
            'qualify-' + args.target, 'completed', 'success') or not any(
                s['name'] == STEP and s['conclusion'] == 'success' for s in job['steps']):
            raise RuntimeError('actual-qualification-producing-job-mismatch')
        old_job = fetch(f"{api}/actions/jobs/{pins['job']}")
        steps = {s['name']: s['conclusion'] for s in old_job['steps']}
        if (old_job['run_id'], old_job['run_attempt'], old_job['head_sha'], old_job['status'], old_job['conclusion']) != (
            RUN, pins['attempt'], SOURCE, 'completed', 'failure') or (
            steps.get('Qualify actual native packaged first run'), steps.get('Build per-user installer from the qualified bundle'),
            steps.get('Qualify actual installed startup and data-preserving uninstall')) != ('success', 'success', 'failure'):
            raise RuntimeError('original-producing-failure-boundary-mismatch')
        old_matches = [a for a in original_artifacts['artifacts'] if a['id'] == pins['artifact']]
        if len(old_matches) != 1 or (old_matches[0]['name'], old_matches[0]['expired'], old_matches[0]['digest'],
            old_matches[0]['workflow_run']['id'], old_matches[0]['workflow_run']['head_sha']) != (
            'Unqualified-portable-' + args.target, False, 'sha256:' + pins['digest'], RUN, SOURCE):
            raise RuntimeError('original-retained-artifact-authority-mismatch')
        setup = chain['installer_name']
        if not re.fullmatch(r'Open-Clank-1\.0\.2-' + re.escape(args.target) + r'-Setup\.exe', setup):
            raise RuntimeError('exact-recompiled-setup-name-required')
        portable_names = [n for n in names if re.fullmatch(r'Open-Clank-[A-Za-z0-9._-]+-' + re.escape(args.target) + r'\.zip', n)]
        if len(portable_names) != 1:
            raise RuntimeError('exact-original-emitted-zip-required')
        portable = portable_names[0]
        provenance = setup.removesuffix('.exe') + '.provenance.json'
        required = [portable, portable + '.sha256', setup, setup + '.sha256', provenance,
            'original-retention.json', 'original-relocation.json', 'portable-qualification.json',
            'installed-qualification.json', 'installed-runtime-qualification.json']
        if sorted(names) != sorted(required + ['proof-chain.json']) or set(chain['files']) != set(required):
            raise RuntimeError('exact-safe-qualified-artifact-members-required')
        for name in required:
            info = zipped.getinfo(name)
            if chain['files'][name]['bytes'] != info.file_size:
                raise RuntimeError('qualified-member-byte-count-mismatch')
            with zipped.open(name) as stream, (args.output / name).open('xb') as destination:
                shutil.copyfileobj(stream, destination, 1024 * 1024)
            if sha(args.output / name) != chain['files'][name]['sha256']:
                raise RuntimeError('qualified-member-checksum-mismatch')
    def read(name):
        return json.loads((args.output / name).read_text())
    retained = read('original-retention.json')
    relocation = read('original-relocation.json')
    portable_sha = sha(args.output / portable)
    expected_retention = {'role': 'sealed-unqualified-portable-diagnostic-only', 'source_sha': SOURCE,
        'run_id': RUN, 'run_attempt': pins['attempt'], 'target': args.target, 'repository': 'Plaer1/open-clank',
        'ref': 'refs/heads/codex/beta1-release-build-20261006', 'workflow': '.github/workflows/beta-release-build.yml',
        'archive_name': portable, 'archive_sha256': portable_sha,
        'archive_bytes': (args.output / portable).stat().st_size, 'qualification': 'runtime-qualification-not-yet-performed'}
    if any(retained.get(k) != v for k, v in expected_retention.items()) or (
        chain['portable_zip_sha256'] != portable_sha or relocation.get('zip_sha256') != portable_sha or
        relocation.get('qualification') != 'exact-emitted-zip-relocation-admitted'):
        raise RuntimeError('original-emitted-zip-proof-binding-mismatch')
    for filename, wanted in [(portable, portable_sha), (setup, sha(args.output / setup))]:
        if (args.output / (filename + '.sha256')).read_text().split() not in ([wanted, filename], [wanted, '*' + filename]):
            raise RuntimeError('published-file-checksum-line-mismatch')
    prov = read(provenance)
    issuer_hash = hashlib.sha256(source_bytes(api, 'scripts/windows-installer.iss', SOURCE)).hexdigest()
    if issuer_hash != sha(ROOT / 'scripts/windows-installer.iss'):
        raise RuntimeError('original-issuer-source-mismatch')
    from build_windows_installer import TOOL_VERSION, TOOL_SHA256
    if (prov.get('target'), prov.get('installer_sha256'), prov.get('installer_source_sha256'),
        prov.get('installer_bootstrap_architecture'), prov.get('payload_architecture'),
        prov.get('external_artwork_embedded'), prov.get('unsigned_beta'), prov.get('compiler_version'),
        prov.get('compiler_sha256')) != (args.target, sha(args.output / setup), issuer_hash, 'x64',
        args.target.removeprefix('windows-'), False, True, TOOL_VERSION, TOOL_SHA256):
        raise RuntimeError('recompiled-setup-provenance-mismatch')
    if type(prov.get('payload_bytes')) is not int or prov['payload_bytes'] <= 0 or not re.fullmatch(r'[0-9a-f]{64}', prov.get('payload_checksums_sha256', '')):
        raise RuntimeError('complete-native-payload-provenance-required')
    for filename, field in [('portable-qualification.json', 'portable_qualification_sha256'),
                            ('installed-qualification.json', 'installed_qualification_sha256'),
                            ('installed-runtime-qualification.json', 'installed_runtime_qualification_sha256')]:
        if sha(args.output / filename) != chain[field]:
            raise RuntimeError('full-qualification-receipt-binding-mismatch')
    if prov['portable_qualification_sha256'] != chain['portable_qualification_sha256'] or chain['installer_sha256'] != sha(args.output / setup):
        raise RuntimeError('setup-to-portable-qualification-binding-mismatch')
    portable_checks = ['standalone-private-python-I-c-and-m-without-PATH-python',
        'sealed-payload-unchanged-after-private-python-probes',
        'complete-pinned-external-artwork-verified-by-packaged-command', 'fresh-data-packaged-server-startup',
        'canonical-first-run-setup', 'unauthenticated-files-rejected', 'actual-password-login-with-session-cookie',
        'packaged-native-PNG-content-thumbnail-without-icon-fallback', 'native-files-list-and-editor-original-byte-open',
        'editor-save-disk-bytes-and-reopen', 'sealed-payload-unchanged-after-journey-and-owned-server-stop']
    from qualify_treehouse import CHECK as TREEHOUSE_CHECK, RESTART_CHECK
    from qualify_theme_emoji import CHECK as ARTWORK_CHECK
    portable_checks += [TREEHOUSE_CHECK, RESTART_CHECK, ARTWORK_CHECK]
    for filename in ['portable-qualification.json', 'installed-runtime-qualification.json']:
        require_checks(read(filename), args.target, 'packaged-startup-auth-files-editor-passed', portable_checks)
    require_checks(read('installed-qualification.json'), args.target,
        'per-user-installer-start-stop-auth-files-editor-uninstall-passed', [
        'per-user-install-private-runtime-python-registration-and-path-preserved',
        'start-menu-desktop-stop-shortcut-targets', 'actual-browser-shortcut-virgin-auth-required', 'second-browser-shortcut-reuses-verified-owned-instance',
        'actual-stop-shortcut-owned-server-stopped', 'actual-stop-shortcut-owned-helper-tree-exited',
        'installed-auth-files-editor-native-thumbnail-private-python',
        'uninstall-payload-shortcuts-registration-removed-personal-data-preserved'])
    installed = read('installed-qualification.json')
    shortcuts = installed.get('shortcuts', [])
    if (len(shortcuts) != 3 or {s.get('role') for s in shortcuts} !=
        {'start-menu-launch', 'start-menu-stop', 'desktop-launch'} or
        any(any(s.get(field) is not True for field in ['exists', 'target_equal', 'arguments_equal', 'working_directory_equal']) for s in shortcuts)):
        raise RuntimeError('exact-three-installed-shortcuts-required')
    if installed.get('personal_data_preserved') is not True:
        raise RuntimeError('actual-uninstall-data-preservation-required')
    # Retain the exact artifact proof only after all source/byte/journey gates pass.
    proof_path = args.output / 'proof-chain.json'
    with proof_path.open('xb') as stream:
        stream.write(proof_bytes)
    archive.unlink()  # Only this newly downloaded, digest-checked temporary ZIP.
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'schema_version': 1, 'target': args.target,
        'runtime_source_sha': SOURCE, 'original_run_id': RUN, 'original_mainrun_conclusion': 'failure',
        'original_producing_attempt': pins['attempt'], 'original_producing_job_id': pins['job'],
        'original_artifact_id': pins['artifact'], 'qualification_source_sha': args.qualification_source,
        'qualification_run_id': args.run_id, 'qualification_job_id': chain['qualification_job_id'],
        'qualification_producing_attempt': chain['qualification_producing_attempt'],
        'artifact_id': artifact['id'], 'artifact_sha256': artifact['digest'][7:],
        'installer_name': setup, 'installer_sha256': sha(args.output / setup),
        'installer_source_sha256': issuer_hash, 'portable_zip_sha256': portable_sha,
        'provenance_sha256': sha(args.output / provenance),
        'proof_chain_sha256': sha(proof_path), 'proof_chain_bytes': len(proof_bytes),
        'admission': 'exact-retained-native-proof-chain-verified'}, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--qualification-source', required=True)
    parser.add_argument('--target', choices=tuple(PRODUCERS), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        admit(args)
    except Exception as error:
        print('Retained proof-chain admission failed: ' + type(error).__name__, file=sys.stderr)
        return 1
    print('Exact retained native release proof chain admitted')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
