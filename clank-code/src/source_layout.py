"""Locate local state and Python environments in a source checkout."""

from __future__ import annotations

import os
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]


def checkout_roots(app: Path):
    yield app
    if app.name == "clank-code" and (app.parent / ".git").exists():
        yield app.parent


def profile_root(app: Path = APP_ROOT) -> Path:
    """Reuse the original checkout profile on upgrade, including its .env."""
    roots = [root for root in checkout_roots(app)
             if (root / "data" / "app.db").exists() or (root / ".env").is_file()]
    if len(roots) > 1:
        raise RuntimeError("Source profiles exist in both the checkout and clank-code. "
                           "Select one before starting by moving the other profile aside.")
    if roots:
        return roots[0]
    for root in checkout_roots(app):
        if (root / "data").is_dir():
            return root
    return app


def environment_path(name: str, app: Path = APP_ROOT) -> Path:
    for root in checkout_roots(app):
        if os.path.lexists(root / name):
            return root / name
    return app / name


def load_source_environment(app: Path = APP_ROOT) -> None:
    if getattr(sys, "frozen", False):
        return
    root = profile_root(app)
    env_file = root / ".env"
    if not env_file.is_file():
        return
    from dotenv import load_dotenv

    # Preserve shell precedence and anchor file-derived paths at their original
    # location when the selected dotenv predates the source layout change.
    inherited = set(os.environ)
    load_dotenv(env_file, encoding="utf-8-sig", override=False)
    if root == app:
        return
    path_settings = {
        "OPEN_CLANK_DATA_DIR", "COPAL_LOOSE_ROOT", "OPEN_CLANK_FILES_REGISTRY",
        "OPEN_CLANK_MEMERY_DB_PATH", "OPEN_CLANK_FM_DB_PATH", "FM_DB_PATH",
        "OPEN_CLANK_AGENT_HOME", "OPEN_CLANK_AUTHORITY_DB_PATH",
        "OPEN_CLANK_MAIL_ATTACHMENTS_DIR", "FASTEMBED_CACHE_PATH", "APP_DATA_DIR",
        "APP_LOGS_DIR", "OPENCLANK_HISTORY_ROOT", "OPENCLANK_HISTORY_SETTINGS_FILE",
        "OPENCLANK_HISTORY_SOCKET", "OPENCLANK_HISTORY_HOST_ROOT",
        "OPEN_CLANK_AUTHORITY_AUTH_PATH",
    }
    for key in os.environ.keys() - inherited:
        value = os.environ[key]
        if value and key in path_settings:
            os.environ[key] = str(root / Path(value).expanduser())
        elif key == "DATABASE_URL" and value.startswith("sqlite:///"):
            db = value.removeprefix("sqlite:///")
            if db and not Path(db).is_absolute() and not db.startswith((":", "file:")):
                os.environ[key] = "sqlite:///" + (root / db).as_posix()


if __name__ == "__main__":
    choice = sys.argv[1:]
    if choice == ["profile"]:
        print(profile_root())
    elif choice in (["venv"], [".venv"]):
        print(environment_path(choice[0]))
    elif choice[:1] == ["run"] and len(choice) > 1:
        # The service starts with system Python so the same unit works before
        # and after an upgrade, even when the venv remains at its original path.
        python = environment_path(".venv") / "bin" / "python"
        os.execv(python, [str(python), *choice[1:]])
    else:
        raise SystemExit("expected profile, venv, .venv or run SCRIPT [ARGS]")
