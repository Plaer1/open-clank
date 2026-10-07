#!/usr/bin/env python3
"""Compile a per-user installer from an actually qualified native portable payload."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import urllib.request

TOOL_VERSION = '7.1.0'
TOOL_SHA256 = '0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f'
TOOL_URL = 'https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe'
SOURCE = Path(__file__).with_name('windows-installer.iss')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build(bundle, target, output, tools, qualification):
    if os.name != 'nt':
        raise RuntimeError('Installer compilation requires actual Windows')
    if tools.exists() or output.exists():
        raise RuntimeError('Installer tool/output directory already exists; preserve it')
    payload = json.loads((bundle / 'portable-provenance.json').read_text())
    payload_bytes = sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file())
    receipt = json.loads(qualification.read_text())
    if payload['target'] != target or receipt.get('target') != target or receipt.get('qualification') != 'packaged-startup-auth-files-editor-passed':
        raise RuntimeError('Native portable qualification is required before installer compilation')
    for path in bundle.rglob('*'):
        if path.is_symlink() or path.name == 'emoji-assets.pack' or path.name.startswith('emoji-assets.pack.part-'):
            raise RuntimeError('Installer refuses symlinks or bundled external artwork')
    subprocess.run([str(bundle / 'openclank.exe'), 'engine', 'verify', '--json'],
                   check=True, timeout=180, stdout=subprocess.DEVNULL)
    tools.mkdir(parents=True)
    output.mkdir(parents=True)
    vendor = tools / 'innosetup-7.1.0-x64.exe'
    with urllib.request.urlopen(TOOL_URL, timeout=120) as response, vendor.open('xb') as stream:
        while data := response.read(1024 * 1024):
            stream.write(data)
    if digest(vendor) != TOOL_SHA256:
        raise RuntimeError('Official installer compiler download failed its pinned SHA256')
    script = "$s=Get-AuthenticodeSignature -LiteralPath $args[0]; if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch '(^|, )CN=Pyrsys B\\.V\\.(,|$)'){exit 1}"
    command = "$args=@('" + str(vendor).replace("'", "''") + "');\n" + script
    encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], check=True, timeout=60)
    compiler_root = tools / 'inno'
    subprocess.run([str(vendor), '/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART',
                    '/NOICONS', '/DIR=' + str(compiler_root), '/LOG=' + str(tools / 'compiler-install-private.log')],
                   check=True, timeout=180)
    compiler = compiler_root / 'ISCC.exe'
    version = subprocess.run([str(compiler), '--version'], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    if TOOL_VERSION not in version:
        raise RuntimeError('Installed compiler does not report the pinned release version')
    subprocess.run([str(compiler), '--no-signing', '--no-ide-signtools', '/DTarget=' + target,
                    '/DBundleRoot=' + str(bundle), '/DAppVersion=1.0.2', '/DOutputRoot=' + str(output), str(SOURCE)], check=True, timeout=1800)
    installer = output / f'Open-Clank-1.0.2-{target}-Setup.exe'
    if not installer.is_file() or installer.stat().st_size >= 2 * 1024**3:
        raise RuntimeError('Installer is missing or exceeds the release asset limit; preserve outputs')
    checksum = digest(installer)
    installer.with_suffix('.exe.sha256').write_text(checksum + '  ' + installer.name + '\n', encoding='ascii')
    safe = {'schema_version': 1, 'target': target, 'unsigned_beta': True, 'installer_sha256': checksum,
            'installer_bootstrap_architecture': 'x64', 'payload_architecture': target.removeprefix('windows-'),
            'payload_bytes': payload_bytes,
            'compiler_version': TOOL_VERSION, 'compiler_sha256': TOOL_SHA256, 'compiler_source': TOOL_URL,
            'installer_source_sha256': digest(SOURCE), 'payload_checksums_sha256': digest(bundle / 'SHA256SUMS'),
            'portable_qualification_sha256': digest(qualification), 'external_artwork_embedded': False}
    installer.with_suffix('.provenance.json').write_text(json.dumps(safe, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--target', choices=('windows-x64', 'windows-arm64'), required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--tool-root', type=Path, required=True)
    parser.add_argument('--qualification-receipt', type=Path, required=True)
    args = parser.parse_args()
    build(args.bundle.resolve(), args.target, args.output_root.resolve(), args.tool_root.resolve(), args.qualification_receipt.resolve())
    print('installer-compiled-from-qualified-native-payload')


if __name__ == '__main__':
    main()
