#!/usr/bin/env python3
"""Bounded Windows release preparation; no credential or user-data inputs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'__pycache__', '.git', '.clanker', '.clankers', '.references', 'node_modules',
            'target', 'venv', '.venv', 'cache', 'logs', '.env', '.env.local'}


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def dependencies(output):
    from packaging.requirements import Requirement
    requirements = []
    for line in (ROOT / 'requirements.txt').read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        version = metadata.version(requirement.name)
        if version not in requirement.specifier:
            raise RuntimeError('Installed dependency does not satisfy requirements: ' + requirement.name)
        requirements.append({'name': requirement.name, 'version': version})
    if metadata.version('pyinstaller') != '6.16.0':
        raise RuntimeError('The release requires PyInstaller 6.16.0')
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
    output.write_text(json.dumps({'requirements_sha256': digest(ROOT / 'requirements.txt'),
                                 'python': sys.version, 'packages': sorted(requirements, key=lambda r: r['name'])},
                                indent=2) + '\n', encoding='utf-8')


def stage_data(destination):
    if destination.exists():
        raise RuntimeError('Release data staging already exists; preserve it')
    destination.mkdir(parents=True)
    # Deliberate source/resource closure. Never traverse native compiler output
    # or copy ambient application state. Python sources support child runtimes.
    for name in ('static', 'scripts', 'mcp_servers', 'services', 'config', 'contracts', 'src', 'core', 'routes', 'licenses'):
        source = ROOT / name
        if not source.is_dir():
            raise RuntimeError('Missing release resource directory: ' + name)
        paths = []
        for directory, children, filenames in os.walk(source, followlinks=False):
            children[:] = sorted(child for child in children if child not in EXCLUDED)
            for child in children:
                if (Path(directory) / child).is_symlink():
                    raise RuntimeError('Release data directory symlink must be reviewed')
            paths.extend(Path(directory) / filename for filename in sorted(filenames))
        for path in paths:
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED for part in relative.parts):
                continue
            if path.name == 'emoji-assets.pack' or path.name.startswith('emoji-assets.pack.part-'):
                continue
            if path.is_symlink():
                raise RuntimeError('Release data symlink must be reviewed: ' + relative.as_posix())
            if not path.is_file() or path.suffix in {'.pyc', '.pyo'}:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    for name in ('LICENSE', 'requirements.txt', 'app.py'):
        shutil.copyfile(ROOT / name, destination / name)
    # Keep root license prominent as well as within the sealed runtime.


def archive(bundle, target, output):
    if output.exists() or output.with_suffix(output.suffix + '.sha256').exists():
        raise RuntimeError('Archive destination already exists; preserve it')
    for path in bundle.rglob('*'):
        if path.is_symlink():
            raise RuntimeError('Portable archive refuses symlinks')
        if path.name == 'emoji-assets.pack' or path.name.startswith('emoji-assets.pack.part-'):
            raise RuntimeError('Offline artwork must remain in separate release parts')
    provenance = json.loads((bundle / 'portable-provenance.json').read_text())
    if provenance['target'] != target:
        raise RuntimeError('Archive architecture differs from verified bundle')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zipped:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():
                zipped.write(path, 'openclank/' + path.relative_to(bundle).as_posix())
    if output.stat().st_size >= 2 * 1024**3:
        raise RuntimeError('Actual Windows archive exceeds the release asset limit; preserve it for deliberate chunking')
    output.with_suffix(output.suffix + '.sha256').write_text(digest(output) + '  ' + output.name + '\n', encoding='ascii')


def stage_python(destination, target):
    if os.name != 'nt' or destination.exists():
        raise RuntimeError('Private Python staging requires Windows and a fresh destination')
    sys.path.insert(0, str(ROOT))
    from src.openclank.engine_build import _verify_pe_target
    base = Path(sys.base_prefix).resolve()
    _verify_pe_target(base / 'python.exe', target)
    destination.mkdir(parents=True)
    # A real CPython installation supplies its stdlib, extension DLLs and
    # redistributable runtime. Never copy ambient base site-packages/tools.
    for path in sorted(base.iterdir()):
        if path.is_file() and path.suffix.lower() in {'.exe', '.dll', '.txt'}:
            shutil.copyfile(path, destination / path.name)
    for folder in ('Lib', 'DLLs', 'tcl'):
        source = base / folder
        if source.is_dir():
            shutil.copytree(source, destination / folder,
                            ignore=shutil.ignore_patterns('site-packages', '__pycache__', '*.pyc', 'test', 'tests'))
    site = destination / 'Lib/site-packages'
    for source in {Path(sysconfig.get_path('purelib')).resolve(), Path(sysconfig.get_path('platlib')).resolve()}:
        shutil.copytree(source, site, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Reject host-specific path injections instead of inheriting a runner's
    # unrelated Python install through an absolute .pth path.
    for pth in site.glob('*.pth'):
        for line in pth.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith(('#', 'import ')) and Path(line).is_absolute():
                raise RuntimeError('Private runtime refuses absolute site path injection')
    # Isolate child module search from the host registry/PATH/site settings.
    # The parent directory is the sealed first-party source/resource root.
    (destination / 'python313._pth').write_text('Lib\nDLLs\nLib/site-packages\n..\nimport site\n', encoding='ascii')
    startup = destination / 'Lib/sitecustomize.py'
    if startup.exists():
        raise RuntimeError('Private runtime already contains a sitecustomize module')
    startup.write_text('import sys\nsys.dont_write_bytecode = True\n', encoding='ascii')
    # Precompile before checksumming so first child startup cannot introduce
    # an unlisted .pyc in the sealed payload (including startup .pth imports).
    subprocess.run([str(destination / 'python.exe'), '-I', '-B', '-m', 'compileall', '-q', str(destination)],
                   check=True, timeout=300)
    files = []
    for path in sorted(destination.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Private Python runtime refuses symlinks')
        if not path.is_file():
            continue
        if path.suffix.lower() in {'.pyd', '.dll'} or path.name == 'python.exe':
            _verify_pe_target(path, target)
        files.append({'path': path.relative_to(destination).as_posix(), 'bytes': path.stat().st_size,
                      'sha256': digest(path)})
    (destination / 'runtime-inventory.json').write_text(json.dumps(
        {'schema_version': 1, 'target': target, 'python_version': sys.version, 'files': files}, indent=2) + '\n', encoding='utf-8')
    subprocess.run([str(destination / 'python.exe'), '-I', '-c',
                    'import mcp,fastapi,sqlalchemy,grpc,cryptography,psycopg2; print("private-python-imports-passed")'], check=True, timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    deps = sub.add_parser('dependencies')
    deps.add_argument('--output', type=Path, required=True)
    data = sub.add_parser('stage-data')
    data.add_argument('--output', type=Path, required=True)
    runtime = sub.add_parser('stage-python')
    runtime.add_argument('--output', type=Path, required=True)
    runtime.add_argument('--target', choices=('windows-x64', 'windows-arm64'), required=True)
    zipped = sub.add_parser('archive')
    zipped.add_argument('--bundle', type=Path, required=True)
    zipped.add_argument('--target', choices=('windows-x64', 'windows-arm64'), required=True)
    zipped.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'dependencies':
        dependencies(args.output)
    elif args.command == 'stage-data':
        stage_data(args.output)
    elif args.command == 'stage-python':
        stage_python(args.output, args.target)
    else:
        archive(args.bundle, args.target, args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
