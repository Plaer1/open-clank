"""Launch focused qualification with disposable authority before any app imports."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--files', required=True)
    parser.add_argument('--fm')
    parser.add_argument('--node')
    parser.add_argument('--chromium')
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith('OPENCLANK_HISTORY_') or key in {'OPEN_CLANK_AGENT_HOME', 'THESIUS_AGENT_HOME', 'ODYSSEUS_FILES_SOCKET', 'ODYSSEUS_FILES_GRPC_SOCKET', 'ODYSSEUS_FILES_GRPC_SESSION'}:
            environment.pop(key)
    with tempfile.TemporaryDirectory(prefix='openclank-s03-s06-projection.') as directory:
        root = Path(directory)
        for name in ('cache', 'config', 'data', 'home'):
            (root/name).mkdir()
        environment.update(OPEN_CLANK_DATA_DIR=directory, ODYSSEUS_DATA_DIR=directory,
            OPEN_CLANK_AUTHORITY_DB_PATH=str(root/'authority.db'), OPEN_CLANK_AUTHORITY_AUTH_PATH=str(root/'auth.json'),
            FM_DB_PATH=str(root/'fm.db'), OPENCLANK_CONVERSATION_ARCHIVE_DB=str(root/'archive.db'),
            DATABASE_URL='sqlite:///:memory:', ODYSSEUS_INPROCESS_POLLERS='0', PYTHONDONTWRITEBYTECODE='1',
            TMPDIR=directory, HOME=str(root/'home'), XDG_CACHE_HOME=str(root/'cache'), XDG_CONFIG_HOME=str(root/'config'), XDG_DATA_HOME=str(root/'data'))
        for path, binary_env, digest_env in ((args.files, 'ODYSSEUS_FILES_SERVICE_BIN', 'OPEN_CLANK_TEST_FILES_SHA256'),
                                            (args.fm, 'OPEN_CLANK_TEST_FM_MCP_BIN', 'OPEN_CLANK_TEST_FM_MCP_SHA256')):
            if path:
                binary = Path(path).resolve(strict=True)
                environment[binary_env] = str(binary)
                environment[digest_env] = hashlib.sha256(binary.read_bytes()).hexdigest()
                print(f'{binary_env}={binary} sha256={environment[digest_env]}', flush=True)
        if args.fm:
            environment['FM_MCP_COMMAND'] = environment['OPEN_CLANK_TEST_FM_MCP_BIN']
            environment['OPEN_CLANK_TEST_DEFAULT_FM_SHA256'] = environment['OPEN_CLANK_TEST_FM_MCP_SHA256']
        if args.node:
            environment['PATH'] = str(Path(args.node).resolve().parent) + os.pathsep + environment['PATH']
        if args.chromium:
            environment['OPENCLANK_CHROMIUM_BIN'] = str(Path(args.chromium).resolve(strict=True))
        tests = args.pytest_args
        if tests and tests[0] == '--': tests = tests[1:]
        return subprocess.call([sys.executable, '-B', '-m', 'pytest', *tests], env=environment)


if __name__ == '__main__':
    raise SystemExit(main())
