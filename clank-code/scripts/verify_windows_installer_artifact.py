#!/usr/bin/env python3
"""Read-only own-repository Actions artifact admission for installer validation."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(endpoint, *, binary=None):
    request = urllib.request.Request('https://api.github.com/' + endpoint,
                                    headers={'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    request.add_unredirected_header('Authorization', 'Bearer ' + os.environ['GH_TOKEN'])
    with urllib.request.urlopen(request, timeout=120) as response:
        if not response.geturl().startswith('https://'):
            raise RuntimeError('Artifact download requires HTTPS')
        if binary:
            with binary.open('xb') as stream:
                shutil.copyfileobj(response, stream, 1024 * 1024)
            return None
        body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise RuntimeError('API metadata exceeds bound')
        return json.loads(body)


def admit(args):
    repo = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch(r'[0-9a-f]{40}', args.source):
        raise RuntimeError('Invalid own-repository/source identity')
    if args.output.exists() or args.receipt.exists():
        raise RuntimeError('Preserve existing artifact/receipt paths')
    api = 'repos/' + repo
    run = fetch(f'{api}/actions/runs/{args.run_id}')
    if (run['status'], run['conclusion'], run['head_sha'], run['path'], run['event']) != (
            'completed', 'success', args.source,
            '.github/workflows/beta-release-build.yml', 'workflow_dispatch') or run['head_branch'] not in ('main', 'v1.0.3-beta.2'):
        raise RuntimeError('Require successful exact-source Windows build run')
    if run['repository']['full_name'] != repo or run['head_repository']['full_name'] != repo:
        raise RuntimeError('Foreign run repository refused')
    listing = fetch(f'{api}/actions/runs/{args.run_id}/artifacts?per_page=100')
    if listing['total_count'] > 100:
        raise RuntimeError('Unexpected artifact count')
    matches = [a for a in listing['artifacts'] if a['name'] == 'Open-Clank-' + args.target]
    if len(matches) != 1:
        raise RuntimeError('Require exactly one native target artifact')
    artifact = matches[0]
    if artifact['expired'] or artifact['workflow_run']['id'] != args.run_id or artifact['workflow_run']['head_sha'] != args.source:
        raise RuntimeError('Artifact run/source ownership mismatch')
    digest = artifact.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise RuntimeError('Actions artifact digest is required')
    args.output.mkdir(parents=True)
    archive = args.output / 'actions-artifact.zip'
    fetch(f"{api}/actions/artifacts/{artifact['id']}/zip", binary=archive)
    if sha(archive) != digest[7:]:
        raise RuntimeError('Actions artifact SHA256 mismatch')
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        if len(names) != len(set(names)) or sum(i.file_size for i in zipped.infolist()) > 8 * 1024**3:
            raise RuntimeError('Duplicate/oversized artifact archive')
        for info in zipped.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in info.filename or ':' in info.filename or stat.S_ISLNK(info.external_attr >> 16):
                raise RuntimeError('Unsafe artifact archive member')
        installers = [n for n in names if re.fullmatch(r'installers/Open-Clank-[A-Za-z0-9._-]+-' + re.escape(args.target) + r'-Setup.exe', n)]
        if len(installers) != 1:
            raise RuntimeError('Require one exact target installer')
        installer = installers[0]
        provenance = installer.removesuffix('.exe') + '.provenance.json'
        qualification = 'qualification-' + args.target + '/receipt.json'
        installed = 'installer-qualification-' + args.target + '/receipt.json'
        selected = [installer, installer + '.sha256', provenance, qualification, installed]
        for name in selected:
            target = args.output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(name) as source, target.open('xb') as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
    exe = args.output / installer
    prov = json.loads((args.output / provenance).read_text())
    if sha(exe) != prov['installer_sha256'] or (args.output / (installer + '.sha256')).read_text().split()[0] != sha(exe):
        raise RuntimeError('Installer checksum/provenance mismatch')
    if prov['target'] != args.target or prov['payload_architecture'] != args.target.removeprefix('windows-') or prov['installer_bootstrap_architecture'] != 'x64' or prov['external_artwork_embedded'] is not False:
        raise RuntimeError('Installer target/bootstrap/artwork identity mismatch')
    issuer = fetch(f'{api}/contents/scripts/windows-installer.iss?ref={args.source}')
    issuer_hash = hashlib.sha256(base64.b64decode(issuer['content'])).hexdigest()
    if issuer_hash != prov['installer_source_sha256'] or issuer_hash != sha(ROOT / 'scripts/windows-installer.iss'):
        raise RuntimeError('Exact build-source installer script differs from validation authority')
    qualified = args.output / qualification
    if sha(qualified) != prov['portable_qualification_sha256']:
        raise RuntimeError('Portable qualification provenance binding mismatch')
    record = json.loads(qualified.read_text())
    if record['target'] != args.target or record['qualification'] != 'packaged-startup-auth-files-editor-passed':
        raise RuntimeError('Require actual portable qualification')
    record = json.loads((args.output / installed).read_text())
    if record['target'] != args.target or record['qualification'] != 'per-user-installer-start-stop-auth-files-editor-uninstall-passed':
        raise RuntimeError('Require actual installed qualification')
    if type(prov.get('payload_bytes')) is not int or prov['payload_bytes'] <= 0 or not re.fullmatch(r'[0-9a-f]{64}', prov['payload_checksums_sha256']):
        raise RuntimeError('Installer payload provenance is malformed')
    archive.unlink()  # API exposes a full artifact ZIP; no portable ZIP member is extracted.
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'run_id': args.run_id, 'source_sha': args.source, 'target': args.target,
        'artifact_id': artifact['id'], 'artifact_sha256': digest[7:], 'installer_name': exe.name,
        'installer_sha256': sha(exe), 'installer_source_sha256': issuer_hash,
        'provenance_sha256': sha(args.output / provenance), 'payload_bytes': prov['payload_bytes'], 'admission': 'exact-successful-native-build-artifact-verified'}, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target', choices=('windows-x64', 'windows-arm64'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        admit(args)
    except Exception as error:
        # Never expose authenticated redirect URLs or credentials from exceptions.
        print(str(error) if isinstance(error, RuntimeError) else 'Artifact admission failed: ' + type(error).__name__, file=sys.stderr)
        return 1
    print('Exact native installer artifact admitted; no build performed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
