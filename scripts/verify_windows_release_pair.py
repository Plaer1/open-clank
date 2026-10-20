#!/usr/bin/env python3
"""Admit the two fixed successful native producers and qualify their installer artwork."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import traceback
import tempfile
import subprocess
from types import SimpleNamespace
import zipfile
from verify_retained_windows_release import source_bytes, require_checks
from qualify_retained_windows_release import (
    BRANCH, WORKFLOW, PRODUCERS, ROOT, RUN, SOURCE, ORIGINAL_SNAPSHOT,
    REPAIRED_SNAPSHOT, fetch, safe_members, sha,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ADMISSION_BRANCH = 'codex/beta1-windows-pair-artwork-20261008'
ADMISSION_WORKFLOW = '.github/workflows/beta-windows-pair-artwork.yml'
STEP = 'Qualify exact retained native portable and installed release'
NATIVE_PRODUCERS = {
    'windows-x64': {'source': 'd324145e218d546a0dbef76492be580131d3ee36',
        'run': 37771592937, 'job': 113292241958, 'attempt': 1,
        'artifact': 11547384400,
        'digest': '41eda0f0e69949ffd2e0ba8f5c48e44794efc7ccef13f92d3196f329099db807',
        'run_conclusion': 'success',
        'retained_tool_sha256': 'ed2fe8b197c4131a1a0607309503ddc714e4140eeac80f155c3236bc88910520'},
    'windows-arm64': {'source': 'e2891a78ee5464f983a5bfbf763c255135c305c7',
        'run': 37769194374, 'job': 113284228738, 'attempt': 1,
        'artifact': 11548675006,
        'digest': '991aeb4b9db3c3ec16aca77dda364a04d972e561ec5c7dff622128bbd8ee763f',
        'run_conclusion': 'failure',
        'retained_tool_sha256': '3361ce94c7ce8a2859a6f87704c81481c9bc33a888db3df306deca1734e8560f'},
}


def verify_producer(args, *, export_portable):
    args.stage = "execution-source"
    if os.environ['GITHUB_REPOSITORY'] != 'Plaer1/open-clank':
        raise RuntimeError('foreign-repository')
    if args.output.exists() or args.receipt.exists():
        raise RuntimeError('preserve-existing-admission-paths')
    producer = NATIVE_PRODUCERS[args.target]
    args.qualification_source, args.run_id = producer['source'], producer['run']
    api = 'repos/Plaer1/open-clank'
    admission_source = os.environ['GITHUB_SHA']
    if (not re.fullmatch(r'[0-9a-f]{40}', admission_source) or
        os.environ['GITHUB_REF'] != 'refs/heads/' + ADMISSION_BRANCH):
        raise RuntimeError('admission-source-and-branch-required')
    own_run = fetch(f"{api}/actions/runs/{int(os.environ['GITHUB_RUN_ID'])}")
    if (own_run['head_sha'], own_run['head_branch'], own_run['path'], own_run['event'],
        own_run['repository']['full_name'], own_run['head_repository']['full_name']) != (
        admission_source, ADMISSION_BRANCH, ADMISSION_WORKFLOW, 'push', 'Plaer1/open-clank', 'Plaer1/open-clank'):
        raise RuntimeError('admission-execution-source-identity-mismatch')
    for name in ['scripts/verify_windows_release_pair.py',
                 'scripts/verify_retained_windows_release.py', 'scripts/qualify_retained_windows_release.py']:
        if source_bytes(api, name, admission_source) != (ROOT / name).read_bytes():
            raise RuntimeError('executed-admission-and-pure-check-source-mismatch')
    if sha(ROOT / 'scripts/verify_retained_windows_release.py') != '802d3341149f55356f1f2e7923d201750bda217d6d6beec72091b99734c85e23':
        raise RuntimeError('strict-same-run-verifier-must-remain-unchanged')
    args.stage = 'producer-source'
    run = fetch(f'{api}/actions/runs/{args.run_id}')
    if (run['status'], run['conclusion'], run['head_sha'], run['head_branch'], run['path'], run['event'],
        run['repository']['full_name'], run['head_repository']['full_name']) != (
        'completed', producer['run_conclusion'], args.qualification_source, BRANCH, WORKFLOW,
        'push', 'Plaer1/open-clank', 'Plaer1/open-clank'):
        raise RuntimeError('actual-target-producing-run-identity-mismatch')
    job = fetch(f"{api}/actions/jobs/{producer['job']}")
    if (job['run_id'], job['run_attempt'], job['head_sha'], job['name'], job['status'], job['conclusion']) != (
        args.run_id, producer['attempt'], args.qualification_source, 'qualify-' + args.target, 'completed', 'success'):
        raise RuntimeError('actual-successful-native-producing-job-required')
    if (not any(s['name'] == STEP and s['conclusion'] == 'success' for s in job['steps']) or
        not any(s['name'].startswith('Run actions/upload-artifact@') and s['conclusion'] == 'success' for s in job['steps'])):
        raise RuntimeError('actual-full-native-qualification-and-upload-required')
    for name in ['scripts/windows-installer.iss', 'scripts/build_windows_installer.py',
                 'scripts/qualify_windows_portable.py', 'scripts/qualify_treehouse.py',
                 'scripts/qualify_theme_emoji.py', 'scripts/emoji_runtime_schema.py', 'scripts/emoji_asset_bundle.py']:
        original_source = source_bytes(api, name, SOURCE)
        if original_source != source_bytes(api, name, args.qualification_source) or original_source != (ROOT / name).read_bytes():
            raise RuntimeError('exact-original-runtime-and-issuer-qualification-source-required')
    frozen = source_bytes(api, 'scripts/qualify_windows_installer.py', SOURCE)
    repaired = (ROOT / 'scripts/qualify_windows_installer.py').read_bytes()
    if (frozen.count(ORIGINAL_SNAPSHOT.encode()) != 1 or
        frozen.replace(ORIGINAL_SNAPSHOT.encode(), REPAIRED_SNAPSHOT.encode()) != repaired or
        source_bytes(api, 'scripts/qualify_windows_installer.py', args.qualification_source) != repaired):
        raise RuntimeError('exact-repaired-canonical-qualifier-source-required')
    if hashlib.sha256(source_bytes(api, 'scripts/qualify_retained_windows_release.py', args.qualification_source)).hexdigest() != producer['retained_tool_sha256']:
        raise RuntimeError('reviewed-successful-producer-tool-source-required')
    args.stage = 'original-provenance'
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
    matches = [a for a in artifacts['artifacts'] if a['name'] == 'Requalified-Windows-' + args.target]
    if len(matches) != 1:
        raise RuntimeError('unique-requalified-target-artifact-required')
    artifact = matches[0]
    if artifact['id'] != producer['artifact'] or artifact['digest'] != 'sha256:' + producer['digest']:
        raise RuntimeError('fixed-successful-target-artifact-mismatch')
    if (artifact['expired'] or artifact['workflow_run']['id'] != args.run_id or
        artifact['workflow_run']['head_sha'] != args.qualification_source or
        not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest', ''))):
        raise RuntimeError('qualified-artifact-identity-mismatch')
    args.stage = 'official-artifact'
    args.output.mkdir(parents=True)
    archive = args.output / 'actions-artifact.zip'
    fetch(f"{api}/actions/artifacts/{artifact['id']}/zip", archive)
    if sha(archive) != artifact['digest'][7:]:
        raise RuntimeError('qualified-actions-artifact-digest-mismatch')
    args.stage = 'proof-and-members'
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
        if chain['qualification_job_id'] != producer['job'] or chain['qualification_producing_attempt'] != producer['attempt']:
            raise RuntimeError('fixed-target-producing-job-proof-mismatch')
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
            if name == portable:
                # Verify the complete original ZIP bytes without a second extracted copy.
                with zipped.open(name) as stream:
                    if export_portable:
                        hasher = hashlib.sha256()
                        with (args.output / name).open('xb') as destination:
                            while chunk := stream.read(1024 * 1024):
                                destination.write(chunk)
                                hasher.update(chunk)
                        streamed_portable_sha = hasher.hexdigest()
                    else:
                        streamed_portable_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
                streamed_portable_bytes = info.file_size
                checksum = streamed_portable_sha
            else:
                with zipped.open(name) as stream, (args.output / name).open('xb') as destination:
                    shutil.copyfileobj(stream, destination, 1024 * 1024)
                checksum = sha(args.output / name)
            if checksum != chain['files'][name]['sha256']:
                raise RuntimeError('qualified-member-checksum-mismatch')
    args.stage = 'native-receipts'
    def read(name):
        return json.loads((args.output / name).read_text())
    retained = read('original-retention.json')
    relocation = read('original-relocation.json')
    portable_sha = streamed_portable_sha
    expected_retention = {'role': 'sealed-unqualified-portable-diagnostic-only', 'source_sha': SOURCE,
        'run_id': RUN, 'run_attempt': pins['attempt'], 'target': args.target, 'repository': 'Plaer1/open-clank',
        'ref': 'refs/heads/codex/beta1-release-build-20261006', 'workflow': '.github/workflows/beta-release-build.yml',
        'archive_name': portable, 'archive_sha256': portable_sha,
        'archive_bytes': streamed_portable_bytes, 'qualification': 'runtime-qualification-not-yet-performed'}
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
    args.stage = 'export'
    proof_path = args.output / 'proof-chain.json'
    with proof_path.open('xb') as stream:
        stream.write(proof_bytes)
    archive.unlink()  # Only this newly downloaded, digest-checked temporary ZIP.
    # These are freshly downloaded safe receipts, not caller or personal files.
    # Full native proof was checked; the original official artifact preserves them.
    keep = {setup, setup + '.sha256', provenance, 'proof-chain.json'}
    if not export_portable:
        for filename in required:
            if filename not in keep and filename != portable:
                (args.output / filename).unlink()
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
        'admission_source_sha': admission_source, 'qualification_run_conclusion': producer['run_conclusion'],
        'qualification_job_conclusion': 'success', 'payload_bytes': prov['payload_bytes'],
        'purpose': 'native-producer-within-pair', 'release_pair_verified': False,
        'admission': 'exact-successful-native-producer-artifact-verified'}, indent=2) + '\n')
    return json.loads(args.receipt.read_text())


def admit_pair(args):
    args.stage = 'execution-source'
    if args.output.exists() or args.receipt.exists():
        raise RuntimeError('preserve-existing-pair-admission-paths')
    if bool(args.parts) != bool(args.artwork_output):
        raise RuntimeError('artwork-parts-and-output-required-together')
    if args.artwork_output and args.artwork_output.exists():
        raise RuntimeError('preserve-existing-artwork-output')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    producers = {}
    # All target producer sources must preserve the exact seven original inputs
    # and the same repaired canonical installed qualifier, independently checked
    # by verify_producer. Diagnostic-only helpers are pinned separately.
    with tempfile.TemporaryDirectory(prefix='openclank-pair-', dir=args.output.parent) as private:
        private = Path(private)
        selected = None
        for target in ('windows-arm64', 'windows-x64'):
            probe = SimpleNamespace(target=target, output=private / target,
                                    receipt=private / (target + '.json'))
            try:
                producers[target] = verify_producer(probe, export_portable=target == args.target)
            finally:
                args.stage = getattr(probe, 'stage', 'execution-source')
            if target == args.target:
                selected = probe.output
        # Both real producer jobs and their full proof now passed. Move only
        # the selected target's exact eleven flat members, without extra copies.
        args.stage = 'export'
        if len(list(selected.iterdir())) != 11:
            raise RuntimeError('exact-eleven-release-members-required')
        selected.rename(args.output)
    declaration_bytes = json.dumps(NATIVE_PRODUCERS, sort_keys=True, separators=(',', ':')).encode()
    receipt = {'schema_version': 1, 'purpose': 'release-pair', 'target': args.target,
               'release_pair_verified': True,
               'admission': 'exact-two-successful-native-producers-release-pair-verified',
               'runtime_source_sha': SOURCE, 'original_run_id': RUN,
               'original_mainrun_conclusion': 'failure',
               'admission_source_sha': os.environ['GITHUB_SHA'],
               'admission_run_id': int(os.environ['GITHUB_RUN_ID']),
               'producer_declarations_sha256': hashlib.sha256(declaration_bytes).hexdigest(),
               'producers': producers,
               'installer_name': producers[args.target]['installer_name'],
               'installer_sha256': producers[args.target]['installer_sha256'],
               'payload_bytes': producers[args.target]['payload_bytes']}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.receipt.open('x') as stream:
        stream.write(json.dumps(receipt, indent=2) + '\n')
    if args.parts:
        args.stage = 'artwork'
        required_disk = receipt['payload_bytes'] + 4 * 449189888 + 1024**3
        if shutil.disk_usage(args.artwork_output.parent).free < required_disk:
            raise RuntimeError('insufficient-disk-for-both-artwork-installation-modes')
        artwork_path = 'scripts/qualify_windows_installer_artwork.py'
        if (sha(ROOT / artwork_path) != '927124121ce5d41a5ff9c973ffe9e3cab9f2fab98c0055336d57e690c364d65e' or
            source_bytes('repos/Plaer1/open-clank', artwork_path, os.environ['GITHUB_SHA']) != (ROOT / artwork_path).read_bytes()):
            raise RuntimeError('exact-unchanged-artwork-journey-source-required')
        # Call the existing unchanged actual installer journey directly so its
        # exception boundary is available to fixed-field safe diagnostics.
        from qualify_windows_installer_artwork import qualify
        qualify((args.output / receipt['installer_name']).resolve(), args.target,
                args.parts.resolve(), args.artwork_output.resolve())



# These are fixed strings from the unchanged, source-bound installer issuer.
# Only their enum labels leave the private Inno log; never publish log lines.
INNO_MARKERS = {
    'download-or-verification-error': 'Artwork download or verification failed.',
    'artwork-working-space-error': 'Downloading and assembling artwork needs about',
    'release-part-missing': 'An artwork release file is missing.',
    'release-part-hash-error': 'An artwork release file failed verification.',
    'payload-verification-error': 'The installed application files failed verification.',
    'artwork-assembly-error': 'Complete offline artwork assembly failed.',
    'existing-payload-collision': 'A payload already exists at this location.',
}


def safe_installer_evidence(args, error, stage):
    returncode = None
    kind = 'none'
    if isinstance(error, subprocess.CalledProcessError):
        if type(error.returncode) is int and -(2**31) <= error.returncode < 2**32:
            returncode = error.returncode
        kind = 'other'
        if isinstance(error.cmd, (list, tuple)) and error.cmd:
            executable = Path(error.cmd[0]).name.lower()
            if executable == ('Open-Clank-1.0.2-' + args.target + '-Setup.exe').lower():
                kind = 'setup'
            elif executable == 'openclank.exe':
                kind = 'private-cli'
            elif executable == 'unins000.exe':
                kind = 'uninstall'
    fixture = {'mode': 'unavailable', 'log_present': False, 'log_tail_readable': False,
               'markers': [], 'payload_executable_present': False,
               'uninstaller_present': False, 'installed_pack_present': False}
    if stage != 'artwork' or args.artwork_output is None:
        return returncode, kind, fixture
    work = args.artwork_output.resolve().with_name(args.artwork_output.name + '-private-fixture')
    # Read only this invocation's fixed fresh fixture. There is no profile-wide
    # discovery, arbitrary log path, recursive scan, or caller-supplied marker.
    if not work.is_dir() or work.is_symlink():
        return returncode, kind, fixture
    for mode in ('existing-parts', 'default-download'):
        case = work / mode
        if not case.is_dir() or case.is_symlink():
            continue
        fixture['mode'] = mode
        fixture['payload_executable_present'] = (case / 'installation/payload/openclank.exe').is_file()
        fixture['uninstaller_present'] = (case / 'installation/unins000.exe').is_file()
        fixture['installed_pack_present'] = (case / 'data/assets/google-emoji/emoji-assets.pack').is_file()
        log = case / 'install-private.log'
        fixture['log_present'] = log.is_file() and not log.is_symlink()
        if fixture['log_present']:
            try:
                with log.open('rb') as stream:
                    utf16 = stream.read(2) == b'\xff\xfe'
                    stream.seek(0, 2)
                    start = max(0, stream.tell() - 1024 * 1024)
                    if utf16:
                        start -= start % 2
                    stream.seek(start)
                    text = stream.read(1024 * 1024).decode('utf-16-le' if utf16 else 'utf-8', errors='replace')
                fixture['log_tail_readable'] = True
                fixture['markers'] = sorted(label for label, marker in INNO_MARKERS.items() if marker in text)
            except OSError:
                pass
        break
    return returncode, kind, fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('windows-arm64', 'windows-x64'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--parts', type=Path)
    parser.add_argument('--artwork-output', type=Path)
    args = parser.parse_args()
    try:
        admit_pair(args)
    except Exception as error:
        stages = {'execution-source', 'producer-source', 'original-provenance',
                  'official-artifact', 'proof-and-members', 'native-receipts', 'export', 'artwork'}
        stage = getattr(args, 'stage', 'execution-source')
        if stage not in stages:
            stage = 'execution-source'
        exception = type(error).__name__
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', exception):
            exception = 'Exception'
        allowed = {'verify_windows_release_pair.py', 'verify_retained_windows_release.py',
                   'qualify_retained_windows_release.py', 'qualify_treehouse.py',
                   'qualify_theme_emoji.py', 'qualify_windows_installer_artwork.py',
                   'qualify_windows_installer.py'}
        frames = []
        for frame, line in traceback.walk_tb(error.__traceback__):
            script = Path(frame.f_code.co_filename).resolve()
            if script.parent == ROOT / 'scripts' and script.name in allowed:
                frames.append({'script': script.name, 'line': line})
        returncode, kind, fixture = safe_installer_evidence(args, error, stage)
        print('SAFE_WINDOWS_PAIR_DIAGNOSTIC ' + json.dumps(
            {'stage': stage, 'exception_class': exception, 'frames': frames,
             'subprocess_returncode': returncode, 'subprocess_kind': kind,
             'artwork_fixture': fixture}, separators=(',', ':')), file=sys.stderr)
        return 1
    print('Both native producer proofs admitted; requested installer artwork journey passed' if args.parts else
          'Both native producer proofs admitted')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
