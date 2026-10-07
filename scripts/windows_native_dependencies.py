#!/usr/bin/env python3
"""Native hosted Windows dependency closure with explicit, pinned source gaps.

Run in a native target MSVC environment. Source recipes derive from the accepted
Windows investigation; no VM-specific paths, secrets, or feature omissions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tarfile
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
INPUTS_SHA256 = 'f166825531360a7298a86f2c86d6c0e52d085d40c6504c9758b23dd9c2b747a5'
ADAPTER_SHA256 = '07f723d003b7b85b22cf9712399b0c52aa51a1dec92c42b97347b6e46dcf4aa0'


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def pe_bytes(data, machine):
    if len(data) < 64 or data[:2] != b'MZ':
        raise RuntimeError('Required native PE header missing')
    offset = struct.unpack_from('<I', data, 60)[0]
    if offset + 6 > len(data) or data[offset:offset + 4] != b'PE\0\0' or struct.unpack_from('<H', data, offset + 4)[0] != machine:
        raise RuntimeError('Native dependency has the wrong PE architecture')


def dependency_failure_tail(log_path, environment):
    """Bounded, redacted diagnostics for this driver's dependency-only logs."""
    with log_path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        offset = max(0, stream.tell() - 65536)
        stream.seek(offset)
        text = stream.read(65536).decode('utf-8', errors='replace')
    if offset:
        text = text.partition('\n')[2]  # Never print a truncated first line.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    values = set()
    for name, value in environment.items():
        if value and re.search(r'TOKEN|KEY|PASSWORD|PASSWD|SECRET|CREDENTIAL', name, re.I):
            values.add(value)
            values.update(piece for piece in value.splitlines() if piece.strip())
            values.add(urllib.parse.quote(value, safe=''))
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:(?:bearer|token|basic)\s+)?\S+',
                  r'\1[REDACTED]', text)
    text = re.sub(r"(?i)((?:token|password|passwd|api[_-]?key|secret|credential)\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
                  r'\1[REDACTED]', text)
    lines = text.splitlines()[-40:]
    return '\n'.join(lines)[-6000:]


class Builder:
    def __init__(self, args):
        self.root = args.work_root.resolve()
        self.target = args.target
        self.machine = 0xaa64 if args.target == 'windows-arm64' else 0x8664
        if os.name != 'nt' or sys.version_info[:2] != (3, 13):
            raise RuntimeError('Native Windows CPython3.13 required')
        pe_bytes(Path(sys.executable).read_bytes(), self.machine)
        expected = 'arm64' if self.machine == 0xaa64 else 'x64'
        if os.environ.get('VSCMD_ARG_TGT_ARCH', '').lower() != expected:
            raise RuntimeError('Initialize the matching native MSVC environment before this script')
        if not args.work_root.is_absolute() or self.root.exists() or len(str(self.root)) > 90:
            raise RuntimeError('Select a fresh, short, absolute task work root')
        if sha(HERE / 'windows-native-inputs.json') != INPUTS_SHA256:
            raise RuntimeError('Pinned dependency input manifest changed')
        self.config = json.loads((HERE / 'windows-native-inputs.json').read_text())
        self.root.mkdir(parents=True)
        self.inputs = self.root / 'inputs'; self.inputs.mkdir()
        self.wheels = self.root / 'wheels'; self.wheels.mkdir()
        self.logs = self.root / 'logs'; self.logs.mkdir()
        self.env = os.environ.copy()
        self.env.update(TEMP=str(self.root / 'tmp'), TMP=str(self.root / 'tmp'), CARGO_BUILD_JOBS='1',
                        DISTUTILS_USE_SDK='1', MSSdk='1', GRPC_PYTHON_BUILD_EXT_COMPILER_JOBS='2',
                        GRPC_PYTHON_BUILD_WITH_CYTHON='0')
        (self.root / 'tmp').mkdir()
        self.python = None
        self.built = []
        self.build_tools = []

    def run(self, argv, name, cwd=None, allow_failure=False):
        with (self.logs / (name + '.txt')).open('xb') as log:
            code = subprocess.run([str(item) for item in argv], cwd=cwd or self.root, env=self.env,
                                  stdout=log, stderr=subprocess.STDOUT).returncode
        if code:
            # Only this dependency driver's logs enter console diagnostics.
            # Server/auth/installer qualification logs are never read here.
            tail = dependency_failure_tail(self.logs / (name + '.txt'), self.env)
            print(f'Dependency step {name} failed ({code}); redacted tail (max40 lines/6000 chars):', file=sys.stderr)
            print(tail, file=sys.stderr)
        if code and not allow_failure:
            raise RuntimeError(f'{name} failed ({code}); see redacted dependency diagnostics')
        return code

    def fetch(self, item):
        path = self.inputs / (item.get('filename') or item['url'].rsplit('/', 1)[1])
        if not path.exists():
            with urllib.request.urlopen(item['url'], timeout=60) as source, path.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
        if sha(path) != item['sha256']:
            raise RuntimeError('Pinned native input digest differs: ' + path.name)
        return path

    def extract(self, archive, name):
        destination = self.root / name
        destination.mkdir()
        if archive.suffix == '.zip':
            with zipfile.ZipFile(archive) as zipped:
                for member in zipped.namelist():
                    relative = Path(member)
                    if relative.is_absolute() or '..' in relative.parts or '\\' in member or ':' in member:
                        raise RuntimeError('Unsafe native source archive member')
                zipped.extractall(destination)
        else:
            with tarfile.open(archive) as tar:
                tar.extractall(destination, filter='data')
        for path in destination.rglob('*'):
            if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                raise RuntimeError('Native source archive contains a link/reparse point')
        roots = list(destination.iterdir())
        return roots[0] if len(roots) == 1 and roots[0].is_dir() else destination

    def tools(self):
        if self.python is not None:
            return
        builder = self.root / 'builder'
        self.run([sys.executable, '-I', '-m', 'venv', builder], 'builder-create')
        self.python = builder / 'Scripts/python.exe'
        tools = self.config['tools'] + [self.config['sources'][name] for name in ('meson', 'ninja')]
        paths = [self.fetch(item) for item in tools]
        self.run([self.python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', *paths], 'pinned-builder-tools')
        self.env['PATH'] = str(builder / 'Scripts') + os.pathsep + self.env['PATH']

    def postgres_generators(self):
        item = self.config['sources']['WinFlexBison']
        archive = self.fetch(item)
        if archive.stat().st_size != item['size']:
            raise RuntimeError('Pinned PostgreSQL generator archive size differs')
        tools = self.extract(archive, 'postgres-generators')
        self.env['BISON_PKGDATADIR'] = str(tools / 'data')
        self.env.pop('M4', None)  # WinBison's packaged M4 implementation, not ambient tools.
        binaries = []
        for name in ('win_flex.exe', 'win_bison.exe'):
            executable = tools / name
            expected = item['executables'][name]
            if sha(executable) != expected['sha256']:
                raise RuntimeError('Pinned PostgreSQL generator executable differs')
            # These are emulated build generators only; shipped libraries stay native.
            pe_bytes(executable.read_bytes(), 0x14c)
            step = name.removesuffix('.exe') + '-version'
            self.run([executable, '--version'], step)
            version = (self.logs / (step + '.txt')).read_text(errors='replace')
            if not re.search(r'\b' + re.escape(expected['version']) + r'\b', version):
                raise RuntimeError('PostgreSQL generator version differs from pin')
            binaries.append({'name': name, 'version': expected['version'],
                             'architecture': 'x86-emulated-build-only', 'sha256': expected['sha256']})
        # Exercise bundled skeleton/M4 data before the expensive client build.
        smoke = self.root / 'postgres-generator-preflight'; smoke.mkdir()
        (smoke / 'parser.y').write_text('%token NUMBER\n%%\nstart: NUMBER;\n%%\n')
        (smoke / 'scanner.l').write_text('%option noyywrap\n%%\n[0-9]+ return 1;\n. ;\n%%\n')
        flex, bison = tools / 'win_flex.exe', tools / 'win_bison.exe'
        self.run([bison, '--output=parser.c', '--defines=parser.h', 'parser.y'], 'bison-generator-preflight', cwd=smoke)
        self.run([flex, '--outfile=scanner.c', 'scanner.l'], 'flex-generator-preflight', cwd=smoke)
        if any(not (smoke / name).is_file() or (smoke / name).stat().st_size == 0
               for name in ('parser.c', 'parser.h', 'scanner.c')):
            raise RuntimeError('Pinned PostgreSQL generator outputs missing')
        self.build_tools.append({'name': item['name'], 'version': item['version'],
                                 'role': item['role'], 'archive_sha256': item['sha256'],
                                 'binaries': binaries, 'generator_preflight': 'passed'})
        return flex, bison

    def openssl(self):
        prefix = self.root / 'openssl'
        if prefix.is_dir():
            return prefix
        source = self.extract(self.fetch(self.config['sources']['OpenSSL']), 'openssl-source')
        perl_root = self.extract(self.fetch(self.config['sources']['Strawberry-Perl-portable']), 'perl-driver')
        self.perl = perl_root / 'perl/bin/perl.exe'
        # This Perl is an explicitly emulated build driver. Its bundled x64
        # compilers/native libraries never enter ARM include/link/search paths.
        self.run([self.perl, 'Configure', 'VC-WIN64-ARM', 'no-shared', '--prefix=' + str(prefix),
                  '--openssldir=' + str(prefix / 'ssl')], 'openssl-configure', source)
        nmake = shutil.which('nmake.exe', path=self.env['PATH'])
        if not nmake:
            raise RuntimeError('Native MSVC nmake executable unavailable')
        pe_bytes(Path(nmake).read_bytes(), self.machine)
        self.run([nmake, 'build_sw'], 'openssl-build', source)
        self.run([nmake, 'install_sw'], 'openssl-install', source)
        pe_bytes((prefix / 'bin/openssl.exe').read_bytes(), self.machine)
        self.run([prefix / 'bin/openssl.exe', 'version', '-a'], 'openssl-version')
        return prefix

    def wheel_check(self, wheel, package, version):
        with zipfile.ZipFile(wheel) as zipped:
            files = zipped.namelist()
            metadata = zipped.read(next(name for name in files if name.endswith('.dist-info/METADATA'))).decode()
            if f'Name: {package}' not in metadata.splitlines() or f'Version: {version}' not in metadata.splitlines():
                raise RuntimeError('Built dependency name/version differs')
            tags = zipped.read(next(name for name in files if name.endswith('.dist-info/WHEEL'))).decode()
            if not any(line.startswith('Tag: ') and line.endswith('-win_arm64') for line in tags.splitlines()):
                raise RuntimeError('Built dependency does not declare native ARM64')
            native = [name for name in files if name.lower().endswith(('.pyd', '.dll'))]
            if not native:
                raise RuntimeError('Built wheel contains no native extension')
            for name in native:
                pe_bytes(zipped.read(name), self.machine)

    def build(self, package):
        if self.machine != 0xaa64 or package in self.built:
            raise RuntimeError('No repeated recipe or unreviewed target source fallback')
        if shutil.disk_usage(self.root).free < 8 * 1024**3:
            raise RuntimeError('Native source recipe requires eight GiB working reserve')
        self.tools()
        item = self.config['sources'][package]
        source = self.fetch(item)
        if package == 'grpcio':
            adapter = HERE / 'windows_grpc_build_adapter.py'
            if sha(adapter) != ADAPTER_SHA256:
                raise RuntimeError('Reviewed build-only grpc compiler adapter changed')
            temporary = self.root / 'builder/Lib/site-packages/sitecustomize.py'
            if temporary.exists():
                raise RuntimeError('Builder already has a sitecustomize module')
            shutil.copyfile(adapter, temporary)
            try:
                self.run([self.python, '-I', '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                          '--no-cache-dir', '--wheel-dir', self.wheels, source], 'grpc-wheel')
            finally:
                temporary.unlink()
        elif package == 'cryptography':
            prefix = self.openssl()
            self.env.update(OPENSSL_DIR=str(prefix), OPENSSL_STATIC='1', OPENSSL_NO_VENDOR='1')
            self.run([self.python, '-I', '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                      '--no-cache-dir', '--config-settings=build-args=--locked', '--wheel-dir', self.wheels, source], 'cryptography-wheel')
        elif package == 'py-rust-stemmers':
            self.run([self.python, '-I', '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                      '--no-cache-dir', '--config-settings=build-args=--locked', '--wheel-dir', self.wheels, source],
                     'stemmer-wheel')
        else:
            prefix = self.openssl()
            pg = self.extract(self.fetch(self.config['sources']['PostgreSQL']), 'postgresql-source')
            flex, bison = self.postgres_generators()
            self.env['PATH'] = str(self.perl.parent) + os.pathsep + self.env['PATH']
            self.env['INCLUDE'] = str(prefix / 'include') + os.pathsep + self.env.get('INCLUDE', '')
            self.env['LIB'] = str(prefix / 'lib') + os.pathsep + self.env.get('LIB', '')
            build = self.root / 'libpq-build'; client = self.root / 'libpq'
            native = self.root / 'libpq-native.ini'
            native.write_text("[built-in options]\nc_link_args = ['ws2_32.lib', 'crypt32.lib', 'advapi32.lib', 'user32.lib', 'gdi32.lib', 'bcrypt.lib']\n")
            meson = [self.python, '-I', '-m', 'mesonbuild.mesonmain']
            self.run([*meson, 'setup', build, pg, '--backend=ninja', '--buildtype=release', '--prefix=' + str(client),
                      '--native-file', native, '-Dssl=openssl', '-Db_lto=false',
                      '-DFLEX=' + json.dumps([flex.as_posix()]), '-DBISON=' + json.dumps([bison.as_posix()]),
                      '-DPERL=' + self.perl.as_posix(), '-DPYTHON=' + json.dumps([self.python.as_posix()])], 'libpq-setup')
            self.run([*meson, 'compile', '-C', build, '-j', '1', 'libpq:shared_library', 'pg_config'], 'libpq-build')
            mapping = json.loads(subprocess.check_output([str(x) for x in [*meson, 'introspect', '--installed', build]], env=self.env))
            for original, target in mapping.items():
                original = Path(original); destination = Path(target)
                if (destination.suffix == '.h' or destination.name in {'libpq.dll', 'libpq.lib', 'pg_config.exe'}) and original.is_file():
                    if not destination.is_relative_to(client):
                        raise RuntimeError('Native client install-map escapes task prefix')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, destination)
            if not any(re.search(r'^#define USE_OPENSSL 1$', path.read_text(), re.M) for path in build.rglob('pg_config.h')):
                raise RuntimeError('Client TLS support absent from actual libpq configuration')
            pg_config = client / 'bin/pg_config.exe'
            pe_bytes(pg_config.read_bytes(), self.machine)
            dlls = list(client.rglob('libpq.dll'))
            if len(dlls) != 1:
                raise RuntimeError('Native client DLL closure absent')
            pe_bytes(dlls[0].read_bytes(), self.machine)
            self.env['PATH'] = str(pg_config.parent) + os.pathsep + self.env['PATH']
            source = self.extract(source, 'psycopg-source')
            raw = self.root / 'psycopg-raw'; raw.mkdir()
            self.run([self.python, '-I', '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                      '--no-cache-dir', '--wheel-dir', raw, source], 'psycopg-wheel')
            raw_wheel, = raw.glob('*.whl')
            self.wheel_check(raw_wheel, package, item['version'])
            self.run([self.python, '-I', '-m', 'delvewheel', 'repair', '--add-path', dlls[0].parent,
                      '-w', self.wheels, raw_wheel], 'psycopg-private-dll-repair')
        pattern = package.replace('-', '_') + '-' + item['version'] + '-*.whl'
        wheel, = self.wheels.glob(pattern)
        self.wheel_check(wheel, item['name'], item['version'])
        self.run([self.python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', wheel], package + '-builder-import-install')
        probes = {
            'grpcio': 'import grpc; assert grpc.__version__=="1.83.0"; c=grpc.insecure_channel("127.0.0.1:1"); c.close()',
            'cryptography': 'from cryptography.fernet import Fernet; f=Fernet(Fernet.generate_key()); assert f.decrypt(f.encrypt(b"fixture"))==b"fixture"',
            'psycopg2-binary': 'import psycopg2; from psycopg2.extensions import libpq_version,parse_dsn; assert libpq_version()==180006; assert parse_dsn("sslmode=require")["sslmode"]=="require"',
            'py-rust-stemmers': 'from py_rust_stemmers import SnowballStemmer; s=SnowballStemmer("english"); words=["running","jumps","easily"]; expected=["run","jump","easili"]; assert s.stem_word("running")=="run"; assert s.stem_words(words)==expected; assert s.stem_words_parallel(words)==expected',
        }
        self.run([self.python, '-I', '-c', probes[package]], package + '-native-feature-probe')
        self.built.append(package)

    def close(self):
        # Each successful source wheel advances the unchanged complete native
        # wheel resolver. Only an actually observed reviewed gap selects a recipe.
        for number in range(5):
            name = 'full-native-resolution-' + str(number)
            code = self.run([sys.executable, '-I', '-m', 'pip', 'download', '--only-binary=:all:', '--no-cache-dir',
                             '--find-links', self.wheels, '--dest', self.wheels, '-r', ROOT / 'requirements.txt',
                             'pyinstaller==6.16.0'], name, allow_failure=True)
            if code == 0:
                break
            evidence = (self.logs / (name + '.txt')).read_text(encoding='utf-8', errors='strict')
            found = re.search(r'(?:No matching distribution found for|Could not find a version that satisfies the requirement)\s+(grpcio|psycopg2-binary|cryptography|py-rust-stemmers)(?:[<=>\[\s]|$)', evidence)
            package = found[1] if found else None
            if package is None and 'ResolutionImpossible' in evidence and re.search(
                    r'fastembed 0\.8\.[01] depends on py-rust-stemmers<0\.2\.0 and >=0\.1\.0', evidence):
                package = 'py-rust-stemmers'
            if package is None:
                raise RuntimeError('Full native dependency resolution failed outside reviewed source recipes')
            self.build(package)
        else:
            raise RuntimeError('Full unchanged native closure still failed')
        wheels = sorted(self.wheels.glob('*.whl'))
        for wheel in wheels:
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if name.lower().endswith(('.pyd', '.dll')):
                        pe_bytes(archive.read(name), self.machine)
        self.run([sys.executable, '-I', '-m', 'pip', 'install', '--no-index', '--only-binary=:all:',
                  '--find-links', self.wheels, '-r', ROOT / 'requirements.txt', 'pyinstaller==6.16.0'], 'full-offline-install')
        self.run([sys.executable, '-I', '-m', 'pip', 'check'], 'full-pip-check')
        self.run([sys.executable, '-I', '-c', 'import grpc,psycopg2,cryptography,mcp,fastapi,sqlalchemy,PIL,winrt,py_rust_stemmers,fastembed; print("full native imports passed")'], 'full-native-imports')
        self.run([sys.executable, '-I', '-m', 'pip', 'freeze', '--all'], 'full-native-freeze')
        if self.built:
            notices = Path(sysconfig.get_path('purelib')) / 'openclank_native_dependency_licenses'
            if notices.exists():
                raise RuntimeError('Native dependency notice destination exists; preserve it')
            notices.mkdir()
            for path in self.inputs.iterdir():
                if not tarfile.is_tarfile(path):
                    continue
                with tarfile.open(path) as archive:
                    for member in archive.getmembers():
                        name = Path(member.name)
                        if not member.isfile() or name.is_absolute() or '..' in name.parts:
                            continue
                        if not name.name.upper().startswith(('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE')):
                            continue
                        target = notices / path.name / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as source, target.open('xb') as destination:
                            shutil.copyfileobj(source, destination)
            (notices / 'source-inputs.json').write_text(json.dumps(
                {'inputs_manifest_sha256': INPUTS_SHA256, 'source_recipes': self.built, 'build_tools': self.build_tools,
                 'inputs': [{'name': path.name, 'sha256': sha(path)} for path in sorted(self.inputs.iterdir())]},
                indent=2) + '\n', encoding='utf-8')
        return {'target': self.target, 'python': sys.version, 'requirements_sha256': sha(ROOT / 'requirements.txt'),
                'inputs_manifest_sha256': INPUTS_SHA256, 'source_recipes': self.built, 'build_tools': self.build_tools,
                'wheels': [{'name': path.name, 'sha256': sha(path)} for path in wheels],
                'status': 'full-native-offline-install-imports-pip-check-passed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('windows-x64', 'windows-arm64'), required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    args = parser.parse_args()
    builder = Builder(args)
    receipt = {'status': 'failed', 'target': args.target}
    try:
        receipt = builder.close()
    finally:
        (builder.root / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(receipt['status'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
