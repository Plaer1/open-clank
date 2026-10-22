#!/usr/bin/env python3
"""Collect declared Python/Rust/Engine notices for the local Linux bundle."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.linux_package import digest, child, write_json, PYTHON_FULL_SHA256


def collect(output, archive):
    if output.exists(): raise RuntimeError('Notice output exists; preserve it')
    output.mkdir(parents=True)
    records = []
    def copy(source, relative):
        target = child(output, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return {'path': relative, 'sha256': digest(target)}
    for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        record = {'kind': 'python', 'name': distribution.metadata['Name'], 'version': distribution.version,
                  'license': distribution.metadata.get('License-Expression') or distribution.metadata.get('License'), 'notices': []}
        for file in distribution.files or []:
            name = str(file)
            if any(part.lower().startswith(('license', 'copying', 'copyright', 'notice')) for part in Path(name).parts):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    record['notices'].append(copy(source, 'python-packages/' + record['name'] + '/' + Path(name).name + '-' + digest(source)[:12]))
        records.append(record)
    crates = ['mcp_servers/frankenmemory/Cargo.toml', 'packages/openclank-history/Cargo.toml', 'packages/odysseus-files/Cargo.toml']
    packages = {}
    for manifest in crates:
        metadata = json.loads(subprocess.check_output(['cargo', 'metadata', '--locked', '--format-version', '1', '--filter-platform', 'x86_64-unknown-linux-gnu', '--manifest-path', str(ROOT / manifest)]))
        resolved = {node['id'] for node in metadata['resolve']['nodes']}
        for package in metadata['packages']:
            if package['id'] in resolved: packages[(package['name'], package['version'])] = package
    expressions = set()
    for (name, version), package in sorted(packages.items()):
        base = Path(package['manifest_path']).parent
        notices = [p for p in base.iterdir() if p.is_file() and p.name.upper().startswith(('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE'))]
        if package.get('license_file'): notices.append(base / package['license_file'])
        if not notices and base.is_relative_to(ROOT):
            for parent in base.parents:
                if not parent.is_relative_to(ROOT): break
                notices = [p for p in parent.iterdir() if p.is_file() and p.name.upper().startswith(('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE'))]
                if notices: break
        expression = package.get('license')
        if not expression and not notices: raise RuntimeError('Missing native notice/declaration: ' + name)
        if expression: expressions.update(re.findall(r'[A-Za-z0-9.+-]+', expression))
        records.append({'kind': 'rust', 'name': name, 'version': version, 'license': expression,
                        'notices': [copy(p, 'rust/' + name + '-' + version + '/' + str(i) + '-' + p.name) for i, p in enumerate(sorted(set(notices)))]})
    # Copy source-distributed notice texts for the exact Engine dependency tree.
    engine = ROOT / 'packages/mimo-code'
    for directory, children, files in os.walk(engine):
        children[:] = [c for c in children if c not in {'.git', 'dist', 'target', '__pycache__'} and not (Path(directory) / c).is_symlink()]
        for name in files:
            source = Path(directory) / name
            if source.is_file() and not source.is_symlink() and name.upper().startswith(('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE')):
                relative = source.relative_to(engine).as_posix()
                records.append({'kind': 'engine-notice', 'source': relative, 'notice': copy(source, 'engine/' + relative)})
    spdx_commit = '31ba1a50e5397e00a304dbadc76531740e89ee48'
    for identifier in sorted(expressions - {'AND', 'OR', 'WITH'}):
        url = f'https://raw.githubusercontent.com/spdx/license-list-data/{spdx_commit}/text/{identifier}.txt'
        with urllib.request.urlopen(url, timeout=30) as response: data = response.read(1024 * 1024)
        target = output / 'spdx' / (identifier + '.txt'); target.parent.mkdir(exist_ok=True); target.write_bytes(data)
        records.append({'kind': 'spdx', 'id': identifier, 'url': url, 'sha256': digest(target)})
    if digest(archive) != PYTHON_FULL_SHA256: raise RuntimeError('Python notice archive checksum differs')
    import zstandard
    destination = output / 'python-distribution'
    with archive.open('rb') as compressed, zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
        with tarfile.open(fileobj=stream, mode='r|') as tar:
            for member in tar:
                if not member.isfile() or not member.name.startswith('python/'): continue
                relative = member.name[len('python/'):]
                if relative != 'PYTHON.json' and '/licenses/' not in member.name and not Path(relative).name.upper().startswith(('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE')): continue
                target = child(destination, relative); target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open('xb') as result: shutil.copyfileobj(source, result)
    metadata = json.loads((destination / 'PYTHON.json').read_text())
    if metadata['python_version'] != '3.13.17' or metadata['target_triple'] != 'x86_64-unknown-linux-gnu': raise RuntimeError('Python notice identity differs')
    required = set()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'license_path': required.update([item] if isinstance(item, str) else item)
                else: visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)
    visit(metadata)
    if not required or not all(child(destination, name).is_file() for name in required):
        raise RuntimeError('Python declared component license texts incomplete')
    records.append({'kind': 'python-distribution', 'archive_sha256': PYTHON_FULL_SHA256, 'metadata': 'python-distribution/PYTHON.json'})
    write_json(output / 'inventory.json', {'components': records})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True); parser.add_argument('--python-archive', type=Path, required=True)
    args = parser.parse_args(); collect(args.output, args.python_archive)
