#!/usr/bin/env python3
"""Reissue only Setup with the reviewed optional-artwork page repair; native payloads are unchanged."""
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
ADMISSION_BRANCH = 'codex/beta1-windows-installer-reissue-20261008'
ADMISSION_WORKFLOW = '.github/workflows/beta-windows-installer-reissue.yml'
STEP = 'Qualify exact retained native portable and installed release'

ORIGINAL_ISSUER_SHA256 = 'd9108edb0a514fe64524271723a0e3150ebdf30151668a09f16b28dc2ecfe7b2'
REPAIRED_ISSUER_SHA256 = 'c0476527f6ccabeb1a4ed3bea3c69c67177a4fe73b43775879b84be0e1793379'
ARTWORK_QUALIFIER_SHA256 = '927124121ce5d41a5ff9c973ffe9e3cab9f2fab98c0055336d57e690c364d65e'

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
    for name in ['scripts/reissue_windows_installer.py', 'scripts/verify_windows_release_pair.py',
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
    for name in ['scripts/build_windows_installer.py',
                 'scripts/qualify_windows_portable.py', 'scripts/qualify_treehouse.py',
                 'scripts/qualify_theme_emoji.py', 'scripts/emoji_runtime_schema.py', 'scripts/emoji_asset_bundle.py']:
        original_source = source_bytes(api, name, SOURCE)
        if original_source != source_bytes(api, name, args.qualification_source) or original_source != (ROOT / name).read_bytes():
            raise RuntimeError('exact-original-runtime-and-issuer-qualification-source-required')
    # This new authority admits precisely the reviewed optional-page repair.
    # Original successful producer proofs still require the original issuer.
    original_issuer = source_bytes(api, 'scripts/windows-installer.iss', SOURCE)
    if (hashlib.sha256(original_issuer).hexdigest() != ORIGINAL_ISSUER_SHA256 or
        source_bytes(api, 'scripts/windows-installer.iss', args.qualification_source) != original_issuer or
        sha(ROOT / 'scripts/windows-installer.iss') != REPAIRED_ISSUER_SHA256 or
        source_bytes(api, 'scripts/windows-installer.iss', admission_source) != (ROOT / 'scripts/windows-installer.iss').read_bytes()):
        raise RuntimeError('only-exact-reviewed-optional-artwork-issuer-repair-is-admitted')
    if sha(ROOT / 'scripts/verify_windows_release_pair.py') != 'ded17a27f11068e57907d113a5c166a1030b75ac1d5e667f6661e55e20bc24a9':
        raise RuntimeError('unchanged-original-pair-authority-required')
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
    if issuer_hash != ORIGINAL_ISSUER_SHA256:
        raise RuntimeError('original-historical-issuer-source-mismatch')
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



def reissue(args):
    args.stage = 'execution-source'
    if any(path.exists() for path in (args.work, args.output, args.receipt, args.artwork_output)):
        raise RuntimeError('preserve-existing-reissue-work-and-output')
    if os.name != 'nt':
        raise RuntimeError('native-Windows-installer-reissue-required')
    args.work.mkdir(parents=True)
    producers = {}
    for target in ('windows-arm64', 'windows-x64'):
        probe = SimpleNamespace(target=target, output=args.work / target,
                                receipt=args.work / (target + '.json'))
        try:
            producers[target] = verify_producer(probe, export_portable=target == args.target)
        finally:
            args.stage = getattr(probe, 'stage', 'execution-source')
    selected = args.work / args.target
    args.stage = 'issuer'
    api = 'repos/Plaer1/open-clank'
    tool_source = os.environ['GITHUB_SHA']
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    jobs = fetch(f'{api}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
    matching = [j for j in jobs['jobs'] if j['name'] == 'reissue-' + args.target]
    if jobs['total_count'] > 100 or len(matching) != 1:
        raise RuntimeError('exact-current-native-reissue-job-required')
    job = matching[0]
    if (job['run_id'], job['run_attempt'], job['head_sha']) != (run_id, attempt, tool_source):
        raise RuntimeError('actual-reissue-job-source-attempt-mismatch')
    record = producers[args.target]
    setup = record['installer_name']
    original_proof = selected / 'proof-chain.json'
    chain = json.loads(original_proof.read_text())
    portable_names = [n for n in chain['files'] if n.endswith('.zip')]
    if len(portable_names) != 1:
        raise RuntimeError('one-admitted-original-portable-required')
    portable = selected / portable_names[0]
    # Digest and all original native receipts have already been checked above.
    extraction = args.work / 'relocated'
    with zipfile.ZipFile(portable) as archive:
        members = safe_members(archive, payload=True)
        total = sum(item.file_size for item in members)
        if shutil.disk_usage(args.work).free < 2 * total + 2 * 1024**3:
            raise RuntimeError('insufficient-disposable-reissue-disk')
        extraction.mkdir()
        archive.extractall(extraction)
    bundle = extraction / 'openclank'
    old_provenance = json.loads((selected / (setup.removesuffix('.exe') + '.provenance.json')).read_text())
    args.stage = 'recompile'
    from build_windows_installer import build, TOOL_VERSION, TOOL_SHA256
    compiled = args.work / 'new-installer'
    build(bundle, args.target, compiled, args.work / 'compiler-tools',
          selected / 'portable-qualification.json')
    new_provenance_name = setup.removesuffix('.exe') + '.provenance.json'
    new_provenance = json.loads((compiled / new_provenance_name).read_text())
    if (new_provenance['installer_source_sha256'] != REPAIRED_ISSUER_SHA256 or
        new_provenance['payload_checksums_sha256'] != old_provenance['payload_checksums_sha256'] or
        new_provenance['payload_bytes'] != old_provenance['payload_bytes'] or
        new_provenance['portable_qualification_sha256'] != sha(selected / 'portable-qualification.json') or
        (new_provenance['compiler_version'], new_provenance['compiler_sha256']) != (TOOL_VERSION, TOOL_SHA256)):
        raise RuntimeError('new-Setup-must-preserve-original-payload-and-reviewed-issuer-compiler')
    # Preserve historical Setup bytes privately and in its immutable official
    # producer artifact; release exports contain the new Setup only.
    historical = args.work / 'historical-installer'
    historical.mkdir()
    (selected / setup).rename(historical / setup)
    (selected / (setup + '.sha256')).rename(selected / 'original-installer-checksum.sha256')
    (selected / new_provenance_name).rename(selected / 'original-installer-provenance.json')
    original_proof.rename(selected / 'original-proof-chain.json')
    for name in (setup, setup + '.sha256', new_provenance_name):
        (compiled / name).rename(selected / name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(selected), str(args.output))
    args.stage = 'artwork'
    if shutil.disk_usage(args.artwork_output.parent).free < new_provenance['payload_bytes'] + 4 * 449189888 + 1024**3:
        raise RuntimeError('insufficient-disk-for-both-artwork-installation-modes')
    artwork_path = 'scripts/qualify_windows_installer_artwork.py'
    if (sha(ROOT / artwork_path) != ARTWORK_QUALIFIER_SHA256 or
        source_bytes(api, artwork_path, tool_source) != (ROOT / artwork_path).read_bytes()):
        raise RuntimeError('exact-reviewed-artwork-journey-source-required')
    from qualify_windows_installer_artwork import qualify
    art = qualify((args.output / setup).resolve(), args.target,
                  args.parts.resolve(), args.artwork_output.resolve())
    if art['qualification'] != 'installer-default-download-and-existing-parts-preservation-passed':
        raise RuntimeError('actual-new-Setup-two-mode-artwork-proof-required')
    args.stage = 'export'
    files = {p.name: {'sha256': sha(p), 'bytes': p.stat().st_size} for p in args.output.iterdir()}
    if files[portable.name]['sha256'] != record['portable_zip_sha256']:
        raise RuntimeError('original-portable-bytes-must-remain-unchanged-after-reissue')
    if len(files) != 13:
        raise RuntimeError('exact-thirteen-payload-and-original-proof-files-required')
    proof = {'schema_version': 1, 'role': 'native-installer-reissued-with-reviewed-optional-artwork-page',
             'qualification': 'original-native-runtime-proof-and-new-installer-artwork-passed',
             'target': args.target, 'runtime_source_sha': SOURCE, 'original_run_id': RUN,
             'original_mainrun_conclusion': 'failure', 'original_producers': producers,
             'release_pair_verified': False, 'original_native_runtime_pair_verified': True,
             'runtime_rebuilt': False,
             'full_product_journey_replayed': False,
             'original_issuer_sha256': ORIGINAL_ISSUER_SHA256,
             'repaired_issuer_sha256': REPAIRED_ISSUER_SHA256,
             'issuer_change': 'optional-artwork-query-page-with-folder-browse-only',
             'historical_installer_sha256': record['installer_sha256'],
             'historical_proof_sha256': record['proof_chain_sha256'],
             'installer_name': setup, 'installer_sha256': sha(args.output / setup),
             'payload_checksums_sha256': new_provenance['payload_checksums_sha256'],
             'payload_bytes': new_provenance['payload_bytes'],
             'compiler_version': TOOL_VERSION, 'compiler_sha256': TOOL_SHA256,
             'tool_source_sha': tool_source, 'run_id': run_id, 'producing_attempt': attempt,
             'producing_job_id': job['id'], 'ref': 'refs/heads/' + ADMISSION_BRANCH,
             'workflow': ADMISSION_WORKFLOW, 'event': 'push',
             'artwork_qualifier_sha256': ARTWORK_QUALIFIER_SHA256,
             'artwork_receipt_sha256': sha(args.artwork_output / 'receipt.json'), 'files': files}
    proof_path = args.output / 'installer-reissue-proof.json'
    proof_path.write_text(json.dumps(proof, indent=2) + '\n')
    receipt = {k: proof[k] for k in ('schema_version', 'target', 'runtime_source_sha', 'original_run_id',
        'original_mainrun_conclusion', 'original_producers', 'release_pair_verified',
        'original_native_runtime_pair_verified', 'original_issuer_sha256',
        'repaired_issuer_sha256', 'installer_name', 'installer_sha256', 'payload_bytes', 'tool_source_sha',
        'run_id', 'producing_attempt', 'producing_job_id', 'ref', 'workflow', 'event', 'artwork_receipt_sha256')}
    receipt.update(admission='original-native-pair-and-reissued-installer-artwork-verified',
                   reissue_proof_sha256=sha(proof_path), reissue_proof_bytes=proof_path.stat().st_size)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.receipt.open('x') as stream:
        stream.write(json.dumps(receipt, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('windows-arm64', 'windows-x64'), required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--parts', type=Path, required=True)
    parser.add_argument('--artwork-output', type=Path, required=True)
    args = parser.parse_args()
    try:
        reissue(args)
    except Exception as error:
        stages = {'execution-source', 'producer-source', 'original-provenance', 'official-artifact',
                  'proof-and-members', 'native-receipts', 'export', 'issuer', 'recompile', 'artwork'}
        stage = getattr(args, 'stage', 'execution-source')
        if stage not in stages:
            stage = 'execution-source'
        exception = type(error).__name__
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', exception):
            exception = 'Exception'
        allowed = {'reissue_windows_installer.py', 'build_windows_installer.py',
                   'verify_windows_release_pair.py', 'verify_retained_windows_release.py',
                   'qualify_retained_windows_release.py', 'qualify_treehouse.py',
                   'qualify_theme_emoji.py', 'qualify_windows_installer_artwork.py', 'qualify_windows_installer.py'}
        frames = []
        for frame, line in traceback.walk_tb(error.__traceback__):
            script = Path(frame.f_code.co_filename).resolve()
            if script.parent == ROOT / 'scripts' and script.name in allowed:
                frames.append({'script': script.name, 'line': line})
        from verify_windows_release_pair import safe_installer_evidence
        returncode, kind, fixture = safe_installer_evidence(args, error, stage)
        print('SAFE_WINDOWS_REISSUE_DIAGNOSTIC ' + json.dumps(
            {'stage': stage, 'exception_class': exception, 'frames': frames,
             'subprocess_returncode': returncode, 'subprocess_kind': kind,
             'artwork_fixture': fixture}, separators=(',', ':')), file=sys.stderr)
        return 1
    print('Original native pair and newly reissued installer artwork journeys verified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
