"""Linux AppImage host. Keep detached server binaries at a persistent path."""
import contextlib
import fcntl
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").absolute()
ROOT = DATA / "open-clank-appimage"
RELEASE = ROOT / ("beta3-" + (BASE / "payload.sha256").read_text().strip()[:16])
IMAGE = ROOT / "Open-Clank.AppImage"
DESKTOP = DATA / "applications/open-clank-appimage.desktop"
MARKER = "X-OpenClank-AppImage=true"
PROFILE = "appimage"


@contextlib.contextmanager
def locked():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def atomic_copy(source, destination, mode):
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stage:
        path = Path(stage.name)
        try:
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, stage, 1024 * 1024)
            stage.flush()
            os.fsync(stage.fileno())
            path.chmod(mode)
            path.replace(destination)
        finally:
            path.unlink(missing_ok=True)


def prepare_runtime():
    with locked():
        if not RELEASE.exists():
            with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
                with tarfile.open(BASE / "payload.tar.gz") as archive:
                    archive.extractall(tmp, filter="data")
                candidate = Path(tmp) / "openclank"
                if not os.access(candidate / "openclank", os.X_OK):
                    raise ValueError("The application executable is missing.")
                candidate.rename(RELEASE)
    return RELEASE / "openclank"


def desktop_entry():
    target = str(IMAGE)
    if any(c in target for c in '\r\n'):
        raise ValueError("The applications directory contains a line break.")
    # Desktop Entry string escaping is applied after Exec quoting.
    for char in ('\\', '"', '`', '$'):
        target = target.replace(char, '\\' + char)
    target = target.replace('\\', '\\\\').replace('%', '%%')
    return (f'[Desktop Entry]\nType=Application\nName=Open Clank (AppImage)\n'
            f'Exec="{target}" --launch\nIcon={ROOT}/open-clank.png\n'
            f'Categories=Education;\nTerminal=false\n{MARKER}\n')


def integrate():
    original = os.environ.get("APPIMAGE", "")
    if not original or not Path(original).is_file():
        raise ValueError("Open the downloaded AppImage to add it to your applications.")
    with locked():
        if DESKTOP.exists() and MARKER not in DESKTOP.read_text().splitlines():
            raise ValueError("An unrelated launcher already uses this filename.")
        DESKTOP.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            entry = Path(tmp) / "launcher"
            entry.write_text(desktop_entry())
            atomic_copy(BASE / "open-clank.png", ROOT / "open-clank.png", 0o644)
            if Path(original).resolve() != IMAGE.resolve():
                atomic_copy(Path(original), IMAGE, 0o755)
            atomic_copy(entry, DESKTOP, 0o644)


def cli(args):
    binary = prepare_runtime()
    env = os.environ.copy()
    env.update(OPEN_CLANK_DATA_DIR=str(ROOT / "data"),
               XDG_CONFIG_HOME=str(ROOT / "config"), APP_PORT="17777")
    for key in ("APPIMAGE", "APPDIR", "LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    if args and args[0] == "server":
        with locked():
            result = subprocess.run([binary, "profile", "list"], env=env, text=True,
                                    capture_output=True, check=True)
            if PROFILE not in result.stdout.split():
                subprocess.run([binary, "profile", "add", PROFILE, "http://127.0.0.1:17777",
                                    "--auto-start"], env=env, check=True)
        args = ["--profile", PROFILE, *args]
    return subprocess.run([binary, *args], env=env, cwd=ROOT).returncode


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        import json
        record = json.loads((prepare_runtime().parent / "release.json").read_text())
        print("Open Clank " + record["channel"] + " (" + record["source_commit"][:12] + ")")
        sys.exit(0)
    if arguments == ["--launch"]:
        # Surface startup failures in the same themed window.
        os.execv(sys.executable, [sys.executable, str(BASE / "setup_host.py"), "--launch"])
    if arguments == ["--remove-launcher"]:
        with locked():
            if DESKTOP.exists() and MARKER in DESKTOP.read_text().splitlines():
                DESKTOP.unlink()
                IMAGE.unlink(missing_ok=True)
                (ROOT / "open-clank.png").unlink(missing_ok=True)
        sys.exit(0)
    sys.exit(cli(arguments))
