#!/usr/bin/env python3
"""Build and verify the host-qualified Linux x86_64 GNU portable archive."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'linux-x64'
PYTHON_URL = 'https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.13.17%2B20261021-x86_64-unknown-linux-gnu-install_only.tar.gz'
PYTHON_SHA256 = 'cbda6f7767884fc456860b11fd184391a9320cae00a86c0f49a1c7ac6e236f7a'
PYTHON_FULL_SHA256 = '344eec7e761e7b3dccce0ce2d91a3e70d40f8ff5d3ce7d907d979c23e8451fc7'
HELPERS = ('odysseus-files-service', 'odysseus-linux-thumbnail-helper',
           'odysseus-kde-thumbnail-adapter', 'openclank-history-service', 'fm-mcp',
           'openclank-linux-host-apps')
MANIFEST = 'linux-package-manifest.json'
EXCLUDED = {MANIFEST, 'SHA256SUMS'}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def child(root, relative):
    value = PurePosixPath(relative)
    if not relative or value.is_absolute() or '..' in value.parts or '\\' in relative:
        raise RuntimeError('Unsafe package path: ' + relative)
    result = root.joinpath(*value.parts)
    if not result.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('Package path escapes root: ' + relative)
    return result


def entries(root):
    result = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative in EXCLUDED:
            continue
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or not child(root, relative).exists():
                raise RuntimeError('Absolute or broken package link: ' + relative)
            result.append({'path': relative, 'link': target, 'mode': mode})
        elif path.is_file():
            result.append({'path': relative, 'sha256': digest(path), 'bytes': path.stat().st_size, 'mode': mode})
        elif path.is_dir():
            result.append({'path': relative, 'directory': True, 'mode': mode})
        else:
            raise RuntimeError('Special file in package: ' + relative)
    return result


def verify_elf(path):
    with path.open('rb') as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', header, 18)[0] != 62:
        raise RuntimeError('Expected x86_64 little-endian ELF: ' + path.name)


def verify_bundle(root):
    root = root.resolve()
    for name in (MANIFEST, 'SHA256SUMS'):
        metadata = root / name
        if metadata.is_symlink() or not metadata.is_file() or stat.S_IMODE(metadata.stat().st_mode) != 0o644:
            raise RuntimeError('Invalid package metadata mode/type')
    manifest = json.loads((root / MANIFEST).read_text())
    if set(manifest) != {'schema_version', 'product', 'target', 'version', 'files'} or manifest['schema_version'] != 1 or manifest['product'] != 'Open Clank' or manifest['target'] != TARGET:
        raise RuntimeError('Linux package identity is invalid')
    if manifest['files'] != entries(root):
        raise RuntimeError('Linux package content, modes or symlinks changed')
    expected = digest(root / MANIFEST) + '  ' + MANIFEST + '\n'
    if (root / 'SHA256SUMS').read_text() != expected:
        raise RuntimeError('Linux package manifest checksum changed')
    internal = root / '_internal'
    critical = [root / 'openclank', internal / 'python/bin/python3', *(internal / 'bin' / name for name in HELPERS)]
    for path in critical:
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError('Package executable closure incomplete: ' + path.name)
        if path.name != 'openclank-linux-host-apps':
            verify_elf(path)
    engine_root = internal / 'libexec/openclank/engine'
    current = json.loads((engine_root / 'current.json').read_text())
    artifact = child(engine_root, current['artifact'])
    engine = json.loads((artifact / 'provenance.json').read_text())
    binary = child(artifact, engine['binary']['name'])
    if engine.get('target') != TARGET or engine.get('open_clank_version') != manifest['version'] or digest(binary) != engine['binary']['sha256']:
        raise RuntimeError('Linux package Engine identity changed')
    verify_elf(binary)
    return manifest


def elf_inventory(root):
    result = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        verify_elf(path)
        details = subprocess.check_output(['readelf', '-l', '-d', '--version-info', str(path)], text=True, stderr=subprocess.DEVNULL)
        rpaths = re.findall(r'\((?:RPATH|RUNPATH)\).*?\[(.*?)\]', details)
        if any(any(part.startswith('/') for part in value.split(':')) for value in rpaths):
            raise RuntimeError('Absolute ELF runtime library path: ' + str(path))
        result.append({'path': path.relative_to(root).as_posix(),
                       'needed': re.findall(r'\(NEEDED\).*?\[(.*?)\]', details),
                       'interpreter': re.findall(r'Requesting program interpreter: (.*?)\]', details),
                       'rpaths': rpaths,
                       'symbol_versions': sorted(set(re.findall(r'\b(?:GLIBC|GLIBCXX|CXXABI)_[\d.]+', details)))})
    return result


def relocate_python(internal):
    # These exact standalone Tcl libraries only need ordinary host system
    # libraries. Their upstream build-time RUNPATH must not escape the bundle.
    changes = []
    for name, expected in [('libtcl9.0.so', '/tools/deps/lib'),
                           ('libtcl9tk9.0.so', '/tools/deps/lib:/tools/deps/lib')]:
        path = internal / 'python/lib' / name
        current = subprocess.check_output(['patchelf', '--print-rpath', str(path)], text=True).strip()
        if current != expected:
            raise RuntimeError('Standalone Python Tcl loader path differs from reviewed asset')
        before = digest(path)
        run('patchelf', '--remove-rpath', path)
        changes.append({'path': path.relative_to(internal).as_posix(), 'before_sha256': before,
                        'after_sha256': digest(path), 'removed_runpath': current})
    return changes


def source_inputs():
    # Corresponding source includes tracked files plus newly authored public
    # implementation files. Private notes/caches are never archive inputs.
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    names += ['scripts/linux_package.py', 'build-linux-portable.sh', 'docs/linux-package.md',
              'tests/test_linux_package.py', 'src/openclank/linux_host_apps.py',
              'packages/odysseus-files/src/bin/odysseus-linux-thumbnail-helper.rs',
              'packages/odysseus-files/src/linux_thumbnail.rs',
              'native/linux/CMakeLists.txt', 'native/linux/kde-thumbnail-adapter.cpp']
    # Include other explicitly public untracked implementation paths from the
    # working tree, while requiring Git's ignore policy to exclude artifacts.
    names += subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
    result = []
    for name in sorted(set(filter(None, names))):
        path = ROOT / name
        if any(part in {'.git', '.clanker', '.clankers', 'node_modules', 'target', '__pycache__', '.venv', 'venv'} for part in Path(name).parts):
            raise RuntimeError('Private/build path in source input: ' + name)
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or not path.exists() or not path.resolve().is_relative_to(ROOT):
                raise RuntimeError('Unsafe corresponding-source link: ' + name)
            result.append({'path': name, 'link': target, 'sha256': hashlib.sha256(target.encode()).hexdigest()})
        elif path.is_file():
            result.append({'path': name, 'sha256': digest(path)})
    return result


def source_archive(output):
    if output.exists(): raise RuntimeError('Source archive exists; preserve it')
    records = source_inputs()
    included = {item['path'] for item in records}
    for item in records:
        if 'link' in item:
            target = (ROOT / item['path']).resolve().relative_to(ROOT).as_posix()
            if target not in included and not any(name.startswith(target + '/') for name in included):
                raise RuntimeError('Corresponding-source link target not included: ' + item['path'])
    with tarfile.open(output, 'w:gz', compresslevel=3, dereference=False) as tar:
        for item in records:
            path = ROOT / item['path']
            tar.add(path, arcname='openclank-source/' + item['path'], recursive=False)
    output.with_name(output.name + '.sha256').write_text(digest(output) + '  ' + output.name + '\n')
    return records


def seal(root):
    write_json(root / MANIFEST, {'schema_version': 1, 'product': 'Open Clank', 'target': TARGET,
                                'version': __import__('src.constants', fromlist=['APP_VERSION']).APP_VERSION,
                                'files': entries(root)})
    (root / 'SHA256SUMS').write_text(digest(root / MANIFEST) + '  ' + MANIFEST + '\n')
    verify_bundle(root)


def archive(root, output):
    verify_bundle(root)
    if output.exists():
        raise RuntimeError('Archive output exists; preserve it')
    with tarfile.open(output, 'w:gz', dereference=False) as tar:
        tar.add(root, arcname='openclank')
    output.with_name(output.name + '.sha256').write_text(digest(output) + '  ' + output.name + '\n')


def build(args):
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Native Linux x86_64 build host required')
    if platform.python_version() != '3.13.17':
        raise RuntimeError('Run builder with pinned standalone Python 3.13.17')
    import importlib.metadata
    if importlib.metadata.version('pyinstaller') != '6.16.0':
        raise RuntimeError('PyInstaller 6.16.0 required')
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError('Build output exists; choose a fresh directory')
    output.mkdir(parents=True)
    work = output / 'work'
    work.mkdir()
    from scripts.windows_portable_build import stage_data
    data = work / 'data'
    stage_data(data)
    run(sys.executable, '-I', '-m', 'pip', 'check')
    run(sys.executable, '-B', ROOT / 'scripts/openclank_engine.py', '--install-root', args.engine_root, 'verify', '--target', TARGET, '--json')
    frozen = work / 'frozen'
    run(sys.executable, '-I', '-m', 'PyInstaller', '--clean', '--onedir', '--console', '--noupx',
        '--name', 'openclank', '--contents-directory', '_internal', '--paths', ROOT,
        '--distpath', frozen, '--workpath', work / 'pyinstaller', '--specpath', work,
        '--hidden-import=app', '--hidden-import=scripts.openclank_bootstrap',
        '--hidden-import=scripts.openclank_engine', '--hidden-import=scripts.linux_package',
        '--hidden-import=scripts.emoji_asset_bundle', '--collect-submodules=src',
        '--collect-submodules=core', '--collect-submodules=routes', '--collect-submodules=services',
        '--collect-submodules=keyring.backends', '--copy-metadata=keyring',
        '--add-data', str(data) + ':.', ROOT / 'scripts/linux_entry.py')
    bundle = output / 'openclank'
    shutil.copytree(frozen / 'openclank', bundle, symlinks=True)
    internal = bundle / '_internal'
    shutil.copytree(args.python_root, internal / 'python', symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Build utilities are not part of the private child runtime.
    site = internal / 'python/lib/python3.13/site-packages'
    for pattern in ('PyInstaller', 'pyinstaller*', 'cmake', 'cmake-*', 'patchelf', 'patchelf-*'):
        for path in site.glob(pattern):
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
    for path in (internal / 'python/bin').iterdir():
        if path.name not in {'python', 'python3', 'python3.13'}:
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
    for pth in site.glob('*.pth'):
        if any(line.strip() and not line.startswith(('#', 'import ')) and Path(line.strip()).is_absolute() for line in pth.read_text().splitlines()):
            raise RuntimeError('Absolute private Python path injection')
    python_loader_changes = relocate_python(internal)
    from scripts.macos_package import install_python_startup
    install_python_startup(site)
    current = json.loads((args.engine_root / 'current.json').read_text())
    engine = internal / 'libexec/openclank/engine'
    engine.mkdir(parents=True)
    shutil.copy2(args.engine_root / 'current.json', engine / 'current.json')
    shutil.copytree(child(args.engine_root, current['artifact']), child(engine, current['artifact']), symlinks=True)
    bindir = internal / 'bin'
    bindir.mkdir(exist_ok=True)
    for name in HELPERS[:-1]:
        shutil.copy2(args.native_bin / name, bindir / name)
    from scripts.windows_helper_artifacts import source_inputs as helper_source_inputs
    helper_sources = {}
    for manifest, names in [('packages/odysseus-files', HELPERS[:2]),
                            ('packages/openclank-history', (HELPERS[3],)),
                            ('mcp_servers/frankenmemory', (HELPERS[4],))]:
        for name in names: helper_sources[name] = helper_source_inputs(ROOT, manifest)
    helper_sources[HELPERS[2]] = [{'path': 'native/linux/' + name, 'sha256': digest(ROOT / 'native/linux' / name)}
                                for name in ('CMakeLists.txt', 'kde-thumbnail-adapter.cpp')]
    helper_sources[HELPERS[5]] = [{'path': 'src/openclank/linux_host_apps.py', 'sha256': digest(ROOT / 'src/openclank/linux_host_apps.py')}]
    host = bindir / HELPERS[-1]
    host.write_text('#!/bin/sh\nif [ "${LD_LIBRARY_PATH_ORIG+x}" = x ]; then export LD_LIBRARY_PATH="$LD_LIBRARY_PATH_ORIG"; else unset LD_LIBRARY_PATH; fi\nHERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$HERE/../python/bin/python3" -B -m src.openclank.linux_host_apps --worker "$@"\n')
    host.chmod(0o755)
    write_json(bindir / 'helper-artifacts.json', {'schema_version': 1, 'target': TARGET, 'artifacts': [
        {'name': name, 'sha256': digest(bindir / name), 'bytes': (bindir / name).stat().st_size,
         'source_inputs': helper_sources[name]} for name in HELPERS]})
    shutil.copy2(ROOT / 'LICENSE', bundle / 'LICENSE')
    shutil.copy2(ROOT / 'docs/linux-package.md', bundle / 'README.md')
    packages = json.loads(subprocess.check_output([sys.executable, '-I', '-m', 'pip', 'list', '--format=json'], text=True))
    write_json(bundle / 'build-provenance.json', {'target': TARGET, 'host': platform.platform(),
        'glibc': platform.libc_ver(), 'python': {'version': platform.python_version(), 'url': PYTHON_URL, 'sha256': PYTHON_SHA256}, 'pyinstaller': '6.16.0',
        'rust': subprocess.check_output(['rustc', '--version'], text=True).strip(),
        'engine_cpu': 'Bun linux-x64 default AVX2 target; no older-CPU qualification',
        'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'python_loader_changes': python_loader_changes,
        'source_inputs': source_inputs(),
        'requirements_sha256': digest(ROOT / 'requirements.txt'), 'packages': packages,
        'elf': elf_inventory(bundle)})
    shutil.copytree(args.notices, internal / 'licenses/build-components', symlinks=False)
    private = internal / 'python/bin/python3'
    run(private, '-I', '-c', 'import sys;assert sys.dont_write_bytecode;import mcp,fastapi,sqlalchemy,grpc,cryptography,psycopg2;from src.runtime_paths import get_app_root;print("private-python-passed")')
    seal(bundle)
    run(bundle / 'openclank', '--version')
    run(bundle / 'openclank', 'engine', 'verify', '--json')
    verify_bundle(bundle)
    version = json.loads((bundle / MANIFEST).read_text())['version']
    archive(bundle, output / f'Open-Clank-{version}-linux-x86_64.tar.gz')
    source_archive(output / f'Open-Clank-{version}-linux-source.tar.gz')
    print(bundle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build')
    b.add_argument('--output', type=Path, required=True)
    b.add_argument('--python-root', type=Path, required=True)
    b.add_argument('--engine-root', type=Path, required=True)
    b.add_argument('--native-bin', type=Path, required=True)
    b.add_argument('--notices', type=Path, required=True)
    v = sub.add_parser('verify'); v.add_argument('--bundle', type=Path, required=True)
    a = sub.add_parser('archive'); a.add_argument('--bundle', type=Path, required=True); a.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'build': build(args)
    elif args.command == 'verify': verify_bundle(args.bundle); print('Linux package content verified')
    else: archive(args.bundle, args.output)
    return 0


if __name__ == '__main__':
    if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
