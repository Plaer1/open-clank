"""One-time, offline migration of explicit Open Clank dotenv and systemd files."""
import argparse
import os
from pathlib import Path
import re
import stat
import tempfile

SPECIAL = {
    "ODYSSEUS_MIMO_PORT": "OPEN_CLANK_AGENT_PORT",
    "ODYSSEUS_MIMO_READINESS_BUDGET": "OPEN_CLANK_AGENT_READINESS_BUDGET",
    "MIMOCODE_CONFIG_CONTENT": "OPENCLANK_CONFIG_CONTENT",
    "MIMOCODE_CONFIG_DIR": "OPENCLANK_CONFIG_DIR",
    "MIMOCODE_HOME": "OPENCLANK_HOME",
    "MIMOCODE_PROVIDER_AUTH_FD": "OPENCLANK_PROVIDER_AUTH_FD",
    "MIMO_HIDDEN_MODELS": "OPEN_CLANK_HIDDEN_MODELS",
    "OPENCLANK_MIMO_HISTORY_CONTEXT": "OPEN_CLANK_ENGINE_HISTORY_CONTEXT",
    "OPENCLANK_MIMO_ONLY": "OPENCLANK_ISOLATED_CONFIG",
    "OPENTHESIUS_DRIVE": "OPEN_CLANK_AGENT_DRIVE",
    "OPENTHESIUS_SRC": "OPEN_CLANK_AGENT_SRC",
    "THESIUS_AGENT_HOME": "OPEN_CLANK_AGENT_HOME",
    "OPENTHESIUS_DEBUG": "OPEN_CLANK_DEBUG",
    "OPENTHESIUS_GLOBAL_CWD": "OPEN_CLANK_GLOBAL_CWD",
    "OPENTHESIUS_SAFE_DIRS": "OPEN_CLANK_SAFE_DIRS",
}
ASSIGN = re.compile(r"""^(\s*(?:export\s+|Environment=["']?)?)([A-Za-z_]\w*)(\s*=)(.*)$""")


def canonical_key(key):
    if key.startswith("ODYSSEUS_"):
        return SPECIAL.get(key, "OPEN_CLANK_" + key.removeprefix("ODYSSEUS_"))
    return SPECIAL.get(key, key)


def migrate(content, unit=False):
    rows = []
    seen = {}
    changed_keys = set()
    quote = None
    for line in content.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        match = ASSIGN.match(raw)
        if quote:
            if re.search(rf"(?<!\\){quote}\s*(?:#.*)?$", raw):
                quote = None
            rows.append(line)
            continue
        if not match or (unit and not match[1].strip().startswith("Environment=")):
            rows.append(line)
            continue
        prefix, old, eq, value = match.groups()
        if not unit and value.lstrip()[:1] in {"'", '"'}:
            delimiter = value.lstrip()[0]
            if not re.search(rf"(?<!\\){delimiter}\s*(?:#.*)?$", value.lstrip()[1:]):
                quote = delimiter
        if unit and re.search(r"""\s+["']?[A-Za-z_]\w*=""",value):
            if "ODYSSEUS_" in raw or any(name in raw for name in SPECIAL):
                raise ValueError("split combined systemd Environment assignments onto separate lines")
        new = canonical_key(old)
        if new != old:
            changed_keys.add(new)
        seen.setdefault(new, {}).setdefault(old, set()).add(value)
        if new in {"OPEN_CLANK_AGENT_SUPERVISOR_BACKEND", "OPEN_CLANK_AGENT_DRIVE"} and value.strip() in {"mimo", '"mimo"', "'mimo'"}:
            value = value.replace("mimo", "acp")
        if not unit and not value.lstrip().startswith("'"):
            value = re.sub(r"(?<!\\)\$\{([A-Z_]\w*)(?=\}|:-)",
                           lambda m: "${" + canonical_key(m[1]), value)
        rows.append(prefix + new + eq + value + line[len(raw):])
    for key in changed_keys:
        groups = seen[key]
        if len(groups) > 1 and len(set.union(*groups.values())) > 1:
            raise ValueError(f"resolve conflicting assignments for {key}")
    return "".join(rows)


def replace(path, data):
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default is preview)")
    parser.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args()
    pending = {}
    for path in args.paths:
        if path.is_symlink() or not path.is_file():
            parser.error(f"expected a regular file: {path}")
        old = path.read_bytes()
        try:
            new = migrate(old.decode(), path.suffix in {".conf", ".service"}).encode()
        except (UnicodeError, ValueError) as err:
            parser.error(f"{path}: {err}")
        if old != new:
            pending[path] = new
    if args.apply:
        for path, new in pending.items():
            replace(path, new)
    print(f"{len(pending)} file(s) {'updated' if args.apply else 'would change'}")


if __name__ == "__main__":
    main()
