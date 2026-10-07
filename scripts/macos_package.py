#!/usr/bin/env python3
"""Native Apple Silicon app/DMG builder and sealed bundle verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TARGET = "darwin-arm64"
PYTHON_VERSION = "3.13.16"
PYTHON_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.13.16%2B20261003-aarch64-apple-darwin-install_only.tar.gz"
PYTHON_SHA256 = "d8975d7df4f08f7b1c7aafcdfacbddcec3d366415f2c1a72b2466b6850815933"
PYTHON_FULL_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.13.16%2B20261003-aarch64-apple-darwin-pgo%2Blto-full.tar.zst"
PYTHON_FULL_SHA256 = "ca3eb5bf8110eaed1c3e516be4bd4d52bcf636f8e888359353f274288406bae7"
HELPERS = ("odysseus-files-service", "odysseus-quicklook-helper", "openclank-history-service", "fm-mcp",
           "desktop-metadata", "desktop-ocr", "openclank-macos-host-apps")
MACHO = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
SPDX_COMMIT = "31ba1a50e5397e00a304dbadc76531740e89ee48"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def child(root: Path, relative: str) -> Path:
    path = root / relative
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Bundle path escapes its root")
    return path


def entries(contents: Path):
    result = []
    for path in sorted(contents.rglob("*")):
        relative = path.relative_to(contents).as_posix()
        # The outer owner executable's signature seals this manifest. Hashing
        # that signature here would create a circular resource dependency.
        if relative in {"Resources/package-manifest.json", "MacOS/OpenClank"} or relative.startswith("_CodeSignature/"):
            continue
        if path.is_symlink():
            child(contents, relative)
            target = os.readlink(path)
            if Path(target).is_absolute():
                raise RuntimeError("Bundle symlink is absolute")
            result.append({"path": relative, "link": target})
        elif path.is_file():
            result.append({"path": relative, "sha256": digest(path), "bytes": path.stat().st_size,
                           "mode": stat.S_IMODE(path.stat().st_mode)})
    return result


def install_python_startup(site: Path):
    """Disable caches before sitecustomize or any other packaged .pth import."""
    bootstrap = site / "000_openclank_no_bytecode.pth"
    startup = site / "sitecustomize.py"
    if bootstrap.exists() or startup.exists():
        raise RuntimeError("Private Python already has an Open Clank startup hook")
    if any(path.name <= bootstrap.name for path in site.glob("*.pth")):
        raise RuntimeError("Private Python has a .pth hook before immutable runtime setup")
    # .pth statements run before sitecustomize is compiled. Import only built-in
    # sys here: setting the flag inside sitecustomize is too late for its cache.
    bootstrap.write_text("import sys; sys.dont_write_bytecode = True\n", encoding="utf-8")
    startup.write_text("import sys\nsys.dont_write_bytecode = True\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[4]))\n", encoding="utf-8")


def verify_bundle(contents: Path) -> dict:
    contents = contents.resolve()
    receipt = json.loads((contents / "Resources/package-manifest.json").read_text())
    if receipt.get("schema_version") != 1 or receipt.get("product") != "Open Clank" or receipt.get("target") != TARGET:
        raise RuntimeError("Mac package identity is invalid")
    if sys.platform != "darwin":
        raise RuntimeError("Native macOS signature verification required")
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", contents.parent,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if receipt.get("files") != entries(contents):
        raise RuntimeError("Mac package content, modes or symlinks changed")
    internal = contents / "Resources/runtime/_internal"
    critical = [contents / "MacOS/OpenClank", contents / "Resources/runtime/openclank",
                internal / "python/bin/python3", *(internal / "bin" / name for name in HELPERS)]
    if not all(path.is_file() and os.access(path, os.X_OK) for path in critical):
        raise RuntimeError("Mac package executable closure is incomplete")
    engine_root = internal / "libexec/openclank/engine"
    current = json.loads((engine_root / "current.json").read_text())
    artifact = child(engine_root, current["artifact"])
    engine = json.loads((artifact / "provenance.json").read_text())
    if engine.get("target") != TARGET or engine.get("open_clank_version") != receipt.get("version"):
        raise RuntimeError("Mac package Engine identity disagrees")
    if digest(artifact / engine["binary"]["name"]) != engine["binary"]["sha256"]:
        raise RuntimeError("Mac package Engine binary identity changed")
    return receipt


def macho_files(root):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in MACHO:
                    yield path


def macho_inventory(contents):
    """Reject foreign architectures and non-system libraries outside the app."""
    result = []
    for path in macho_files(contents):
        runtime = contents / "Resources/runtime"
        private = runtime / "_internal/python"
        executable_dir = private / "bin" if path.is_relative_to(private) else (path.parent if path.parent.name in {"bin", "MacOS"} else runtime)
        arches = subprocess.check_output(["/usr/bin/lipo", "-archs", str(path)], text=True).split()
        if "arm64" not in arches or any(a not in {"arm64", "x86_64"} for a in arches):
            raise RuntimeError("Mac binary lacks native arm64: " + path.name)
        linked = subprocess.check_output(["/usr/bin/otool", "-arch", "arm64", "-L", str(path)], text=True).splitlines()[1:]
        identifiers = subprocess.check_output(["/usr/bin/otool", "-arch", "arm64", "-D", str(path)], text=True).splitlines()[1:]
        loads = subprocess.check_output(["/usr/bin/otool", "-arch", "arm64", "-l", str(path)], text=True).splitlines()
        rpaths = []
        for index, line in enumerate(loads):
            if line.strip() == "cmd LC_RPATH":
                rpaths.append(loads[index + 2].strip().split(" (offset", 1)[0].removeprefix("path "))
        def expand(value):
            return Path(value.replace("@loader_path", str(path.parent)).replace("@executable_path", str(executable_dir)))
        libraries = []
        for line in linked:
            library = line.strip().split(" (compatibility", 1)[0]
            if library in identifiers:
                continue
            libraries.append(library)
            if library.startswith(("/usr/lib/", "/System/Library/")):
                continue
            candidates = [expand(library)]
            if library.startswith("@rpath/"):
                candidates = [expand(rpath) / library[len("@rpath/"):] for rpath in rpaths]
            if not any(candidate.is_file() and candidate.resolve().is_relative_to(contents.resolve()) for candidate in candidates):
                raise RuntimeError("Non-system Mach-O dependency is unresolved inside app: " + path.name + ": " + library)
        result.append({"path": path.relative_to(contents).as_posix(), "architectures": arches, "libraries": libraries})
    return result


def private_python(work: Path) -> Path:
    archive = work / "python.tar.gz"
    with urllib.request.urlopen(PYTHON_URL, timeout=120) as source, archive.open("xb") as target:
        shutil.copyfileobj(source, target)
    if digest(archive) != PYTHON_SHA256:
        raise RuntimeError("Pinned standalone Python checksum mismatch")
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            if Path(member.name).is_absolute() or ".." in Path(member.name).parts or not member.name.startswith("python/"):
                raise RuntimeError("Standalone Python archive path is unsafe")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise RuntimeError("Standalone Python archive contains a special file")
        source.extractall(work, filter="data")
    python = work / "python/bin/python3"
    version = subprocess.check_output([str(python), "-I", "-c", "import platform;print(platform.python_version())"], text=True).strip()
    if version != PYTHON_VERSION:
        raise RuntimeError("Standalone Python version disagrees")
    run(python, "-I", "-m", "pip", "install", "--disable-pip-version-check", "-r", ROOT / "requirements.txt",
        "pyinstaller==6.16.0", "zstandard==0.25.0")
    run(python, "-I", "-m", "pip", "check")
    return python


def extract_python_notices(archive: Path, destination: Path):
    """Stream only distribution metadata/notices; never unpack build objects."""
    import zstandard
    if destination.exists():
        raise RuntimeError("Python notice destination exists; preserve it")
    destination.mkdir(parents=True)
    with archive.open("rb") as compressed, zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                if not member.name.startswith("python/") or not member.isfile():
                    continue
                relative = member.name[len("python/"):]
                name = Path(relative).name.upper()
                if relative != "PYTHON.json" and "/licenses/" not in member.name and not name.startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE")):
                    continue
                target = child(destination, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    metadata = json.loads((destination / "PYTHON.json").read_text())
    if metadata.get("python_version") != PYTHON_VERSION or metadata.get("target_triple") != "aarch64-apple-darwin":
        raise RuntimeError("Supplemental Python notice archive identity disagrees")
    required = set()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "license_path":
                    required.update([item] if isinstance(item, str) else item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(metadata)
    if not required or not all(child(destination, relative).is_file() for relative in required):
        raise RuntimeError("Python distribution declared license texts are incomplete")


def native_notices(crates, destination):
    destination.mkdir(parents=True)
    packages = {}
    for manifest, _ in crates:
        metadata = json.loads(subprocess.check_output([
            "cargo", "+1.99.0", "metadata", "--locked", "--format-version", "1",
            "--filter-platform", "aarch64-apple-darwin", "--manifest-path", str(ROOT / manifest)], text=True))
        resolved = {item["id"] for item in metadata["resolve"]["nodes"]}
        for package in metadata["packages"]:
            if package["id"] in resolved:
                packages[(package["name"], package["version"])] = package
    records = []
    texts = set()
    for (name, version), package in sorted(packages.items()):
        base = Path(package["manifest_path"]).parent
        candidates = [path for path in base.iterdir() if path.is_file() and
                      path.name.upper().startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE"))]
        if package.get("license_file"):
            candidates.append(base / package["license_file"])
        # Workspace packages may deliberately inherit a repository notice.
        if not candidates and base.is_relative_to(ROOT):
            for parent in (base, *base.parents):
                if not parent.is_relative_to(ROOT):
                    break
                candidates = [path for path in parent.iterdir() if path.is_file() and
                              path.name.upper().startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE"))]
                if candidates:
                    break
        expression = package.get("license")
        if not expression and not candidates:
            raise RuntimeError("Native dependency lacks a license declaration/notice: " + name)
        if expression:
            texts.update(token for token in re.findall(r"[A-Za-z0-9.+-]+", expression)
                         if token not in {"AND", "OR", "WITH"})
        record = {"name": name, "version": version, "license": expression, "notices": []}
        folder = destination / (name + "-" + version)
        folder.mkdir()
        for index, source in enumerate(sorted(set(candidates))):
            target = folder / (str(index) + "-" + source.name)
            shutil.copyfile(source, target)
            record["notices"].append({"path": target.relative_to(destination).as_posix(), "sha256": digest(target)})
        records.append(record)
    spdx = destination / "spdx"
    spdx.mkdir()
    standard = []
    for identifier in sorted(texts):
        url = f"https://raw.githubusercontent.com/spdx/license-list-data/{SPDX_COMMIT}/text/{identifier}.txt"
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read(1024 * 1024)
        target = spdx / (identifier + ".txt")
        target.write_bytes(content)
        standard.append({"id": identifier, "url": url, "sha256": digest(target)})
    write_json(destination / "inventory.json", {"packages": records, "standard_license_texts": standard})


def build(output: Path, use_existing_engine: bool):
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("Native arm64 macOS build host required")
    output = output.resolve()
    if output.exists():
        raise RuntimeError("Build output exists; preserve it and choose a fresh output")
    output.mkdir(parents=True)
    work = output / "work"
    work.mkdir()
    python = private_python(work)
    sys.path.insert(0, str(ROOT))
    from src.constants import APP_VERSION
    engine_root = output / "engine"
    if use_existing_engine:
        engine_root = ROOT / "libexec/openclank/engine"
    run(python, "-B", ROOT / "scripts/openclank_engine.py", "--install-root", engine_root,
        "verify" if use_existing_engine else "build", "--target", TARGET, "--json")
    data = work / "data"
    # Shared source/resource allowlist; no ambient data or compiler outputs.
    from scripts.windows_portable_build import stage_data
    stage_data(data)
    (data / "native").mkdir()
    for name in ("DesktopMetadata.swift", "DesktopOCR.swift", "macos_host_apps.swift", "OpenClankApp.swift"):
        shutil.copyfile(ROOT / "native" / name, data / "native" / name)
    frozen = work / "frozen"
    run(python, "-I", "-m", "PyInstaller", "--clean", "--onedir", "--console", "--noupx", "--target-arch", "arm64",
        "--name", "openclank", "--contents-directory", "_internal", "--paths", ROOT,
        "--distpath", frozen, "--workpath", work / "pyinstaller", "--specpath", work,
        "--hidden-import=app", "--hidden-import=scripts.openclank_bootstrap", "--hidden-import=scripts.openclank_engine",
        "--hidden-import=scripts.emoji_asset_bundle", "--hidden-import=scripts.macos_package",
        "--collect-submodules=src", "--collect-submodules=core", "--collect-submodules=routes",
        "--collect-submodules=services", "--collect-submodules=keyring.backends", "--copy-metadata=keyring",
        "--add-data", str(data) + ":.", ROOT / "scripts/macos_entry.py")
    app = output / "OpenClank.app"
    contents = app / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True)
    shutil.copytree(frozen / "openclank", resources / "runtime", symlinks=True)
    internal = resources / "runtime/_internal"
    shutil.copytree(work / "python", internal / "python", symlinks=True)
    # Build-time console scripts have absolute interpreter shebangs. The
    # supported private interpreter is retained; these package admin commands
    # remain available through python -m without shipping host-bound scripts.
    python_bin = internal / "python/bin"
    for path in python_bin.iterdir():
        if path.name not in {"python", "python3", "python" + ".".join(PYTHON_VERSION.split(".")[:2])}:
            if path.is_dir():
                raise RuntimeError("Unexpected private Python bin directory")
            path.unlink()
    site = internal / f"python/lib/python{'.'.join(PYTHON_VERSION.split('.')[:2])}/site-packages"
    for pth in site.glob("*.pth"):
        if any(line.strip() and not line.startswith(("#", "import ")) and Path(line.strip()).is_absolute()
               for line in pth.read_text().splitlines()):
            raise RuntimeError("Private Python absolute site injection refused")
    install_python_startup(site)
    current = json.loads((engine_root / "current.json").read_text())
    artifact = child(engine_root, current["artifact"])
    bundled_engine = internal / "libexec/openclank/engine"
    bundled_engine.mkdir(parents=True)
    shutil.copyfile(engine_root / "current.json", bundled_engine / "current.json")
    shutil.copytree(artifact, bundled_engine / current["artifact"])
    bindir = internal / "bin"
    bindir.mkdir(exist_ok=True)
    crates = [("packages/odysseus-files/Cargo.toml", HELPERS[:2]),
              ("packages/openclank-history/Cargo.toml", (HELPERS[2],)),
              ("mcp_servers/frankenmemory/Cargo.toml", (HELPERS[3],))]
    cargo_output = work / "cargo"
    for manifest, names in crates:
        command = ["cargo", "+1.99.0", "build", "--locked", "--release", "--jobs", "2", "--target", "aarch64-apple-darwin",
                   "--target-dir", cargo_output, "--manifest-path", ROOT / manifest]
        for name in names:
            command += ["--bin", name]
        run(*command)
        for name in names:
            shutil.copy2(cargo_output / "aarch64-apple-darwin/release" / name, bindir / name)
    native_notices(crates, internal / "licenses/native")
    archive = work / "python-full.tar.zst"
    with urllib.request.urlopen(PYTHON_FULL_URL, timeout=120) as source, archive.open("xb") as target:
        shutil.copyfileobj(source, target)
    if digest(archive) != PYTHON_FULL_SHA256:
        raise RuntimeError("Supplemental Python notice archive checksum mismatch")
    run(python, "-I", "-B", ROOT / "scripts/macos_package.py", "python-notices", "--archive", archive,
        "--output", internal / "licenses/python-distribution")
    swift = {"desktop-metadata": "DesktopMetadata.swift", "desktop-ocr": "DesktopOCR.swift",
             "openclank-macos-host-apps": "macos_host_apps.swift"}
    for name, source in swift.items():
        run("/usr/bin/swiftc", "-O", "-target", "arm64-apple-macos15.0", ROOT / "native" / source, "-o", bindir / name)
    (contents / "MacOS").mkdir()
    run("/usr/bin/swiftc", "-O", "-target", "arm64-apple-macos15.0", ROOT / "native/OpenClankApp.swift", "-o", contents / "MacOS/OpenClank")
    icons = work / "OpenClank.iconset"
    icons.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
            run("/usr/bin/sips", "-z", size * scale, size * scale, ROOT / "static/icons/icon-512.png", "--out", icons / name,
                stdout=subprocess.DEVNULL)
    run("/usr/bin/iconutil", "-c", "icns", icons, "-o", resources / "OpenClank.icns")
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleName": "Open Clank", "CFBundleDisplayName": "Open Clank", "CFBundleIdentifier": "org.openclank.app",
                      "CFBundleExecutable": "OpenClank", "CFBundleIconFile": "OpenClank.icns", "CFBundlePackageType": "APPL",
                      "CFBundleVersion": "1", "CFBundleShortVersionString": str(APP_VERSION).split("-", 1)[0],
                      "OpenClankVersion": str(APP_VERSION), "LSMinimumSystemVersion": "15.0",
                      "NSHighResolutionCapable": True, "NSScreenCaptureUsageDescription": "Capture a screen only for a user-authorized task."}, stream)
    libraries = macho_inventory(contents)
    # Sign leaves before hashing; outer signing must not re-sign inner bytes.
    for path in macho_files(contents):
        signature = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(path)],
                                   capture_output=True, text=True, timeout=60, check=False)
        if signature.returncode != 0:
            if path.is_relative_to(bundled_engine):
                detail = (signature.stderr or signature.stdout or "no codesign diagnostic").strip()
                detail = detail.replace(str(path), path.name)[:1000]
                raise RuntimeError("Canonical Engine signature is invalid before packaging: " + detail)
            run("/usr/bin/codesign", "--force", "--sign", "-", path)
    packages = json.loads(subprocess.check_output([str(python), "-I", "-m", "pip", "list", "--format=json"], text=True))
    write_json(resources / "build-provenance.json", {
        "target": TARGET, "python": {"version": PYTHON_VERSION, "url": PYTHON_URL, "sha256": PYTHON_SHA256,
                                    "notice_archive_url": PYTHON_FULL_URL, "notice_archive_sha256": PYTHON_FULL_SHA256},
        "pyinstaller": "6.16.0", "rust": "1.99.0", "bun": "1.4.0", "requirements_sha256": digest(ROOT / "requirements.txt"),
        "packages": packages, "mach_o": libraries, "signing": "ad-hoc", "notarized": False,
        "source_inputs": [{"path": name, "sha256": digest(ROOT / name)} for name in
                          ("build-macos-app.sh", "scripts/macos_package.py", "scripts/macos_entry.py", "native/OpenClankApp.swift",
                           "native/DesktopMetadata.swift", "native/DesktopOCR.swift", "native/macos_host_apps.swift")]})
    from scripts.windows_helper_artifacts import source_inputs
    helper_sources = {name: source_inputs(ROOT, manifest.rsplit("/", 1)[0])
                      for manifest, names in crates for name in names}
    for name, source in swift.items():
        helper_sources[name] = [{"path": "native/" + source, "sha256": digest(ROOT / "native" / source)}]
    write_json(bindir / "helper-artifacts.json", {"schema_version": 1, "target": TARGET, "artifacts": [
        {"name": name, "sha256": digest(bindir / name), "bytes": (bindir / name).stat().st_size,
         "source_inputs": helper_sources[name]} for name in HELPERS]})
    write_json(resources / "package-manifest.json", {"schema_version": 1, "product": "Open Clank", "target": TARGET,
                                                     "version": str(APP_VERSION), "files": entries(contents)})
    run("/usr/bin/codesign", "--force", "--sign", "-", app)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
    verify_bundle(contents)
    private = internal / "python/bin/python3"
    # Prove ordinary interpreter startup is immutable without a caller's -B.
    run(private, "-c", "import sys;assert sys.dont_write_bytecode;print('private-runtime-default-startup-passed')")
    run(private, "-I", "-c", "import sys;assert sys.dont_write_bytecode;import mcp,fastapi,sqlalchemy,grpc,cryptography,psycopg2;from src.runtime_paths import get_app_root;print('private-runtime-imports-passed')")
    verify_bundle(contents)
    cli = resources / "runtime/openclank"
    run(cli, "--version")
    run(cli, "engine", "verify", "--json")
    stage = work / "dmg"
    stage.mkdir()
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    dmg = output / f"Open-Clank-{APP_VERSION}-macos-arm64.dmg"
    run("/usr/bin/hdiutil", "create", "-volname", "Open Clank", "-srcfolder", stage, "-format", "UDZO", dmg)
    if dmg.stat().st_size >= 2 * 1024**3:
        raise RuntimeError("DMG exceeds the release asset limit; preserve it for deliberate distribution review")
    dmg.with_suffix(".dmg.sha256").write_text(digest(dmg) + "  " + dmg.name + "\n", encoding="ascii")
    print("Built ad-hoc signed native arm64 app and DMG; packaged journey qualification remains required.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--output", type=Path, default=ROOT / "dist/macos-arm64")
    build_parser.add_argument("--use-existing-verified-engine", action="store_true")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--app", type=Path, required=True)
    notice_parser = sub.add_parser("python-notices", help="extract pinned standalone distribution notices")
    notice_parser.add_argument("--archive", type=Path, required=True)
    notice_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        build(args.output, args.use_existing_verified_engine)
    elif args.command == "verify":
        verify_bundle(args.app / "Contents")
        print("Mac bundle content verified")
    else:
        if digest(args.archive) != PYTHON_FULL_SHA256:
            raise RuntimeError("Supplemental Python notice archive checksum mismatch")
        extract_python_notices(args.archive, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
