"""Helpers for resolving runtime paths in source and frozen builds."""

import os
import json
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


class RuntimeResolutionError(RuntimeError):
    """A required runtime could not be admitted by the shared resolver."""

    def __init__(self, code: str, message: str, diagnostics: Sequence[dict] = ()):
        super().__init__(message)
        self.code = code
        self.diagnostics = tuple(dict(item) for item in diagnostics)


@dataclass(frozen=True)
class RuntimeIdentity:
    kind: str
    path: str
    prefix: str = ""
    version: str = ""
    required_modules: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def get_app_root() -> str:
    """Return the app root directory.

    In normal source runs, this is the repository root. In a frozen Windows
    build, it is the bundle content root (PyInstaller's internal directory)
    so bundled runtime folders like `static/`, `scripts/`, and `data/` stay
    together with the executable payload.
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_default_data_dir() -> str:
    """Return the default path to the data directory.

    In normal runs, this is a 'data' subdirectory under the app root.
    In frozen builds, it is a persistent user directory (~/.open-clank/data)
    to prevent SQLite databases and other persistent files from being
    written to the ephemeral, temporary extraction bundle directory.
    """
    if getattr(sys, "frozen", False):
        home = Path(os.path.expanduser("~"))
        current = home / ".open-clank"
        if (home / ".odysseus").is_dir() and not current.exists():
            raise RuntimeError("Legacy home requires .clanker/tools/migrations/python/secondary.py home-root")
        return str(current / "data")
    return os.path.join(get_app_root(), "data")


def _unique_paths(values: Iterable[Path | str]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        path = Path(value).expanduser()
        # Keep distinct virtual-environment entrypoints even when both point
        # at the same managed interpreter symlink.  Their site-packages are
        # selected by the entrypoint's adjacent ``pyvenv.cfg``; resolving the
        # symlink here would silently discard a usable environment.
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _python_candidate_paths(repo_root: Path | None = None) -> list[Path]:
    root = (repo_root or Path(get_app_root())).resolve()
    names = ("python.exe", "python") if os.name == "nt" else ("bin/python",)
    project = [root / ".venv" / names[0], root / "venv" / names[0]]
    if os.name == "nt":
        project = [root / ".venv" / "Scripts/python.exe", root / "venv" / "Scripts/python.exe"]
    virtual = os.environ.get("VIRTUAL_ENV", "").strip()
    virtual_path = Path(virtual) / ("Scripts/python.exe" if os.name == "nt" else "bin/python") if virtual else None
    return _unique_paths(
        [
            os.environ.get("OPEN_CLANK_RUNTIME_PYTHON", ""),
            os.environ.get("OPEN_CLANK_PYTHON", ""),
            virtual_path,
            *project,
            sys.executable,
            shutil.which("python3") or "",
            shutil.which("python") or "",
        ]
    )


def _probe_python(path: Path, required_modules: tuple[str, ...]) -> tuple[RuntimeIdentity | None, dict]:
    diagnostic = {"path": str(path), "kind": "python"}
    if not path.is_file() or (os.name != "nt" and not os.access(path, os.X_OK)):
        diagnostic["code"] = "runtime_not_found"
        return None, diagnostic
    probe = (
        "import importlib.util, json, platform, sys; "
        f"required={json.dumps(list(required_modules))}; "
        "missing=[m for m in required if importlib.util.find_spec(m) is None]; "
        "print(json.dumps({'missing':missing,'prefix':sys.prefix,'version':platform.python_version()}))"
    )
    try:
        completed = subprocess.run(
            [str(path), "-I", "-c", probe],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        diagnostic["code"] = "bootstrap_dependency_missing"
        diagnostic["error"] = type(exc).__name__
        return None, diagnostic
    if completed.returncode != 0:
        diagnostic["code"] = "child_import_failed"
        diagnostic["returncode"] = completed.returncode
        return None, diagnostic
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        diagnostic["code"] = "child_import_failed"
        return None, diagnostic
    missing = tuple(str(item) for item in result.get("missing", ()))
    diagnostic["missing"] = list(missing)
    if missing:
        diagnostic["code"] = "bootstrap_dependency_missing"
        return None, diagnostic
    return (
        RuntimeIdentity(
            kind="python",
            # Preserve the virtual-environment entrypoint rather than its
            # managed-interpreter symlink; the entrypoint selects the correct
            # site-packages at runtime.
            path=str(path.absolute()),
            prefix=str(result.get("prefix") or ""),
            version=str(result.get("version") or ""),
            required_modules=required_modules,
        ),
        diagnostic,
    )


def resolve_python(
    repo_root: Path | str | None = None,
    *,
    required_modules: Sequence[str] = ("mcp", "fastapi", "sqlalchemy"),
) -> RuntimeIdentity:
    """Admit the first interpreter that passes a dependency bootstrap probe."""
    root = Path(repo_root or get_app_root()).resolve()
    required = tuple(dict.fromkeys(str(module).strip() for module in required_modules if str(module).strip()))
    diagnostics: list[dict] = []
    for candidate in _python_candidate_paths(root):
        identity, diagnostic = _probe_python(candidate, required)
        diagnostics.append(diagnostic)
        if identity is not None:
            return identity
    raise RuntimeResolutionError(
        "bootstrap_dependency_missing",
        f"no admitted Python runtime provides {', '.join(required) or 'the protocol entrypoint'}",
        diagnostics,
    )


def _executable_candidates(repo_root: Path, env_names: Sequence[str], relative: Sequence[str], commands: Sequence[str]) -> list[Path]:
    return _unique_paths(
        [
            *(os.environ.get(name, "").strip() for name in env_names),
            *(repo_root / item for item in relative),
            *(Path(found) for command in commands if (found := shutil.which(command))),
        ]
    )


def resolve_fm_mcp(repo_root: Path | str | None = None) -> RuntimeIdentity:
    root = Path(repo_root or get_app_root()).resolve()
    candidates = _executable_candidates(
        root,
        ("FM_MCP_COMMAND", "OPEN_CLANK_FM_MCP"),
        (("bin/fm-mcp.exe",) if os.name == "nt" else ("bin/fm-mcp",)) +
        ("mcp_servers/frankenmemory/target/release/fm-mcp", "mcp_servers/frankenmemory/target/release/fm-mcp.exe"),
        ("fm-mcp",),
    )
    diagnostics = []
    for path in candidates:
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            return RuntimeIdentity(kind="fm_mcp", path=str(path.resolve()))
        diagnostics.append({"path": str(path), "code": "artifact_not_found"})
    raise RuntimeResolutionError("artifact_not_found", "fm-mcp executable is not available", diagnostics)


def resolve_lifetools(repo_root: Path | str | None = None) -> RuntimeIdentity:
    root = Path(repo_root or get_app_root()).resolve()
    path = root / "src/openclank/lifetools_server.py"
    if not path.is_file():
        raise RuntimeResolutionError("artifact_not_found", f"LifeTools entrypoint is missing: {path}")
    return RuntimeIdentity(kind="lifetools", path=str(path))


def _browser_candidates() -> list[Path]:
    system = platform.system().lower()
    if system == "darwin":
        relative = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        )
        commands = ("google-chrome", "chromium", "chromium-browser")
    elif system == "windows":
        relative = (
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        )
        commands = ("chrome", "chromium")
    else:
        relative = ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable")
        commands = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
    return _unique_paths([os.environ.get("OPEN_CLANK_BROWSER_EXECUTABLE", ""), os.environ.get("ODYSSEUS_BROWSER_EXECUTABLE", ""), *relative, *(Path(found) for command in commands if (found := shutil.which(command)))])


def resolve_browser() -> RuntimeIdentity:
    diagnostics = []
    for path in _browser_candidates():
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            try:
                version = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=3, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                diagnostics.append({"path": str(path), "code": "browser_unavailable", "error": type(exc).__name__})
                continue
            if version.returncode == 0:
                return RuntimeIdentity(kind="browser", path=str(path.resolve()), version=version.stdout.strip()[:120])
            diagnostics.append({"path": str(path), "code": "browser_unavailable", "returncode": version.returncode})
        else:
            diagnostics.append({"path": str(path), "code": "browser_unavailable"})
    raise RuntimeResolutionError("browser_unavailable", "no supported browser executable passed the bootstrap probe", diagnostics)


def resolve_runtime_bundle(repo_root: Path | str | None = None, *, include_browser: bool = False) -> dict:
    root = Path(repo_root or get_app_root()).resolve()
    python = resolve_python(root)
    fm = resolve_fm_mcp(root)
    lifetools = resolve_lifetools(root)
    result = {"python": python.as_dict(), "fm_mcp": fm.as_dict(), "lifetools": lifetools.as_dict()}
    if include_browser:
        result["browser"] = resolve_browser().as_dict()
    return result
