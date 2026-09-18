#!/usr/bin/env python3
"""Generate disposable synthetic fixtures for the Odysseus file-engine plan.

The generator is deliberately unable to write outside the platform temporary
directory. It never removes an existing tree and never prints generated bodies or
absolute paths.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


TREE_COUNTS = (0, 500, 10_000, 100_000)
SYNTHETIC_LINE = b"synthetic odysseus fixture 0123456789abcdef\n"


def validated_output(raw: str | Path) -> Path:
    temp_root = Path(tempfile.gettempdir()).resolve()
    output = Path(raw).expanduser().resolve(strict=False)
    try:
        relative = output.relative_to(temp_root)
    except ValueError as exc:
        raise ValueError(f"destination must be below the OS temporary directory") from exc
    if not relative.parts:
        raise ValueError("destination may not be the temporary-directory root")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("destination must not exist or must be an empty directory")
    return output


def write_sized(path: Path, size: int) -> None:
    remaining = size
    with path.open("wb") as handle:
        while remaining:
            chunk = SYNTHETIC_LINE[: min(remaining, len(SYNTHETIC_LINE))]
            handle.write(chunk)
            remaining -= len(chunk)


def make_tree(root: Path, count: int) -> None:
    wide = root / "wide"
    deep = root / "deep" / "a" / "b" / "c" / "d" / "e"
    ignored = root / "generated" / "node_modules"
    hidden = root / ".hidden"
    unicode_dir = root / "unicode-κώδικας"
    for directory in (wide, deep, ignored, hidden, unicode_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        bucket = (wide, deep, ignored, hidden, unicode_dir)[index % 5]
        (bucket / f"entry-{index:06d}.txt").write_text(
            f"synthetic tree entry {index}\n", encoding="utf-8"
        )
    (root / "CaseName.txt").write_text("synthetic case A\n", encoding="utf-8")
    case_peer = root / "casename.txt"
    if not case_peer.exists():
        case_peer.write_text("synthetic case B\n", encoding="utf-8")


def make_file_matrix(root: Path) -> dict[str, int]:
    files = root / "files"
    files.mkdir(parents=True)
    sizes = {
        "text-4k.txt": 4 * 1024,
        "text-1m.txt": 1024 * 1024,
        "text-10m.txt": 10 * 1024 * 1024,
    }
    for name, size in sizes.items():
        write_sized(files / name, size)
    with (files / "sparse-250m.bin").open("wb") as handle:
        handle.truncate(250 * 1024 * 1024)
    (files / "lf.txt").write_bytes(b"alpha\nbeta\n")
    (files / "crlf.txt").write_bytes(b"alpha\r\nbeta\r\n")
    (files / "cr.txt").write_bytes(b"alpha\rbeta\r")
    (files / "utf8-bom.txt").write_bytes(b"\xef\xbb\xbfsynthetic\n")
    (files / "utf16-le.txt").write_bytes("synthetic\n".encode("utf-16"))
    (files / "invalid.bin").write_bytes(b"\xff\xfe\x00\x81synthetic")
    (files / "long-line.txt").write_text("x" * 1024 * 1024, encoding="utf-8")
    (files / "no-final-newline.txt").write_text("synthetic", encoding="utf-8")
    return {**sizes, "sparse-250m.bin": 250 * 1024 * 1024}


def make_permission_matrix(root: Path) -> dict[str, bool]:
    allowed = root / "permission-matrix" / "allowed-root"
    sibling = root / "permission-matrix" / "denied-sibling"
    allowed.mkdir(parents=True)
    sibling.mkdir(parents=True)
    exact = allowed / "exact-file.txt"
    exact.write_text("synthetic exact-file grant\n", encoding="utf-8")
    (allowed / "recursive-child.txt").write_text("synthetic recursive grant\n", encoding="utf-8")
    (sibling / "denied.txt").write_text("synthetic sibling denial\n", encoding="utf-8")
    symlink_supported = False
    try:
        (allowed / "escape-link").symlink_to(sibling, target_is_directory=True)
        symlink_supported = True
    except (NotImplementedError, OSError):
        pass
    return {"symlink_supported": symlink_supported}


def copal_seed_documents() -> list[dict[str, Any]]:
    return [
        {
            "name": "Notes/Synthetic.md",
            "kind": "note",
            "corpus": "notes",
            "content": "---\nstatus: active\ntags: [synthetic, audit]\n---\n# Synthetic\n\n[[Wiki/Synthetic]]\n",
        },
        {
            "name": "Wiki/Synthetic.md",
            "kind": "wiki",
            "corpus": "wiki",
            "content": "# Synthetic wiki\n\nGenerated audit fixture.\n",
        },
        {
            "name": "Views/Synthetic.base",
            "kind": "base",
            "corpus": "notes",
            "content": json.dumps({"version": 1, "views": [{"id": "table", "name": "Table", "type": "table"}]}),
        },
        {
            "name": "Canvas/Synthetic.canvas",
            "kind": "canvas",
            "corpus": "notes",
            "content": json.dumps({"nodes": [{"id": "n1", "type": "text", "text": "Synthetic"}], "edges": []}),
        },
        {
            "name": ".copal/events/synthetic.json",
            "kind": "planning-event",
            "corpus": "notes",
            "content": json.dumps({"id": "synthetic", "title": "Synthetic", "status": "pending"}),
        },
        {
            "name": ".copal/compatibility/synthetic.json",
            "kind": "compatibility",
            "corpus": "notes",
            "content": json.dumps({"source": "synthetic", "external_id": "fixture-1"}),
        },
    ]


def seed_copal_redb(root: Path, bridge_binary: Path) -> dict[str, int]:
    if not bridge_binary.is_file() or not os.access(bridge_binary, os.X_OK):
        raise ValueError(f"Copal bridge is not executable; build {bridge_binary}")
    data_dir = root / "copal-redb"
    data_dir.mkdir()
    environment = os.environ.copy()
    environment["COPAL_DATA_DIR"] = str(data_dir)
    environment["COPAL_WIKI_DATA_DIR"] = str(data_dir)
    process = subprocess.Popen(
        [str(bridge_binary)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    request_id = 0

    def call(operation: str, args: dict[str, Any]) -> Any:
        nonlocal request_id
        request_id += 1
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps({"id": request_id, "op": operation, "args": args}) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        if response.get("id") != request_id or not response.get("ok"):
            raise RuntimeError(f"Copal fixture operation failed: {operation}")
        return response.get("result")

    created = []
    try:
        call("status", {})
        for seed in copal_seed_documents():
            result = call("create", {"owner": "synthetic-audit", "workspace_id": "fixture", **seed})
            created.append(result["doc"])
        note = created[0]
        call(
            "write",
            {
                "owner": "synthetic-audit",
                "workspace_id": "fixture",
                "id": note["id"],
                "corpus": "notes",
                "base": note["head"],
                "content": note["text"] + "\nSynthetic second revision.\n",
            },
        )
        trashed = created[-1]
        call(
            "delete",
            {
                "owner": "synthetic-audit",
                "workspace_id": "fixture",
                "id": trashed["id"],
                "corpus": "notes",
            },
        )
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
    if process.returncode != 0:
        raise RuntimeError("Copal fixture bridge exited unsuccessfully")
    return {"documents_created": len(created), "documents_left_in_trash": 1}


def generate(output: Path, tree_count: int, bridge_binary: Path | None) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    make_tree(output / "tree", tree_count)
    sizes = make_file_matrix(output)
    permissions = make_permission_matrix(output)
    copal = (
        seed_copal_redb(output, bridge_binary)
        if bridge_binary is not None
        else {"documents_created": 0, "documents_left_in_trash": 0}
    )
    code = output / "code-workspace"
    code.mkdir()
    (code / "main.rs").write_text("fn main() { println!(\"synthetic\"); }\n", encoding="utf-8")
    (code / "index.ts").write_text("export const synthetic = true;\n", encoding="utf-8")
    (code / "README.md").write_text("# Synthetic code workspace\n", encoding="utf-8")
    manifest = {
        "schema": "open-clank.odysseus-audit-fixture/v1",
        "synthetic_only": True,
        "tree_entries": tree_count,
        "logical_large_file_bytes": sizes,
        "permission_matrix": permissions,
        "copal_redb": copal,
        "code_languages": ["rust", "typescript", "markdown"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="empty destination below the OS temporary directory")
    parser.add_argument("--tree-count", type=int, choices=TREE_COUNTS, default=500)
    parser.add_argument("--skip-copal-redb", action="store_true", help="omit Redb generation for generator-only tests")
    args = parser.parse_args()
    try:
        output = validated_output(args.output)
        bridge = None
        if not args.skip_copal_redb:
            bridge = Path(__file__).resolve().parents[1] / "packages" / "Copal" / "rust" / "copal-db" / "target" / "release" / "copal-bridge"
        manifest = generate(output, args.tree_count, bridge)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"schema": manifest["schema"], "synthetic_only": True, "tree_entries": args.tree_count, "copal_documents": manifest["copal_redb"]["documents_created"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
