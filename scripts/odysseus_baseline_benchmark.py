#!/usr/bin/env python3
"""Measure pre-Rust Odysseus file/workspace/Copal baselines on synthetic data."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Awaitable, Callable, TypeVar

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starlette.requests import Request

from routes import workspace_routes
from src import tool_execution
from src.agent_tools.filesystem_tools import LsTool, ReadFileTool
from src.openclank.copal_loose import LooseCopalBridge


T = TypeVar("T")


def summarize(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)

    def percentile(fraction: float) -> float:
        index = min(
            len(ordered) - 1,
            max(0, int((len(ordered) - 1) * fraction + 0.999999)),
        )
        return round(ordered[index] * 1000, 3)

    return {
        "n": len(ordered),
        "min_ms": round(ordered[0] * 1000, 3),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": round(ordered[-1] * 1000, 3),
    }


async def measure_async(
    iterations: int, operation: Callable[[], Awaitable[T]]
) -> tuple[list[float], T]:
    samples: list[float] = []
    result: T | None = None
    for _ in range(iterations):
        started = time.perf_counter()
        result = await operation()
        samples.append(time.perf_counter() - started)
    assert result is not None
    return samples, result


def make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("synthetic-audit", 80),
            "client": ("synthetic-audit", 1),
            "app": SimpleNamespace(),
        }
    )


async def benchmark(iterations: int, cold_runs: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="odysseus-baseline-") as temp_dir:
        root = Path(temp_dir)
        tree = root / "tree"
        tree.mkdir()
        for index in range(500):
            (tree / f"entry-{index:04d}.txt").write_text(
                f"synthetic baseline {index}\n" * 8, encoding="utf-8"
            )
        one_mib = root / "one-mib.txt"
        line = "synthetic baseline payload 0123456789abcdef\n"
        one_mib.write_text(
            (line * ((1024 * 1024 // len(line)) + 1))[: 1024 * 1024],
            encoding="utf-8",
        )

        workspace_routes.get_current_user = lambda request: "synthetic-audit"
        workspace_routes.owner_is_admin_or_single_user = lambda owner: True
        router = workspace_routes.setup_workspace_routes()
        browse = next(
            route.endpoint
            for route in router.routes
            if getattr(route, "path", "").endswith("/browse")
        )
        request = make_request()
        browse_samples: list[float] = []
        browse_result = None
        for _ in range(iterations):
            started = time.perf_counter()
            browse_result = browse(request, path=str(tree))
            browse_samples.append(time.perf_counter() - started)

        token = tool_execution._active_workspace.set(str(root.resolve()))
        try:
            read_samples, read_result = await measure_async(
                iterations,
                lambda: ReadFileTool().execute(
                    json.dumps({"path": str(one_mib), "cursor": 0}), {}
                ),
            )
            list_samples, list_result = await measure_async(
                iterations,
                lambda: LsTool().execute(
                    json.dumps({"path": str(tree), "limit": 500}), {}
                ),
            )
        finally:
            tool_execution._active_workspace.reset(token)
        if read_result.get("exit_code") != 0 or list_result.get("exit_code") != 0:
            raise RuntimeError("native file baseline operation failed")

        copal_dir = root / "copal"
        bridge = LooseCopalBridge(copal_dir)
        await bridge.start()
        document_ids: list[str] = []
        for index in range(100):
            created = await bridge.call(
                "create",
                {
                    "owner": "synthetic-audit",
                    "workspace_id": "baseline",
                    "name": f"doc-{index:03d}.md",
                    "kind": "note",
                    "corpus": "notes",
                    "content": f"# Synthetic {index}\nneedle-{index % 7}\n",
                },
            )
            document_ids.append(created["doc"]["id"])
        copal_results: dict[str, dict[str, float | int]] = {}
        workloads = (
            (
                "copal_index_100",
                "index",
                {"owner": "synthetic-audit", "workspace_id": "baseline", "query": ""},
            ),
            (
                "copal_search_100",
                "search",
                {"owner": "synthetic-audit", "workspace_id": "baseline", "query": "needle-3"},
            ),
            (
                "copal_get_one",
                "get",
                {
                    "owner": "synthetic-audit",
                    "workspace_id": "baseline",
                    "id": document_ids[50],
                },
            ),
        )
        for label, operation, arguments in workloads:
            samples, _ = await measure_async(
                iterations, lambda op=operation, args=arguments: bridge.call(op, args)
            )
            copal_results[label] = summarize(samples)
        await bridge.stop()

        cold_samples: list[float] = []
        for _ in range(cold_runs):
            child = LooseCopalBridge(copal_dir)
            started = time.perf_counter()
            await child.start()
            cold_samples.append(time.perf_counter() - started)
            await child.stop()

        assert browse_result is not None
        return {
            "schema": "open-clank.odysseus-baseline/v1",
            "identity": {
                "os": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
            },
            "conditions": {
                "synthetic_only": True,
                "warm_iterations": iterations,
                "cold_child_runs": cold_runs,
                "concurrency": 1,
                "tree_entries": 500,
                "read_file_bytes": 1024 * 1024,
                "copal_documents": 100,
            },
            "observed": {
                "workspace_returned_dirs": len(browse_result["dirs"]),
                "read_returned_chars": len(read_result["output"]),
                "list_returned_entries": len(list_result["entries"]),
                "list_total_entries": list_result["page"]["total"],
            },
            "workloads": {
                "workspace_browse_500_files_no_dirs": summarize(browse_samples),
                "read_file_one_mib_contract": summarize(read_samples),
                "ls_500_entries": summarize(list_samples),
                **copal_results,
                "copal_child_cold_readiness": summarize(cold_samples),
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=35)
    parser.add_argument("--cold-runs", type=int, default=7)
    args = parser.parse_args()
    if args.iterations < 30:
        parser.error("--iterations must be at least 30")
    if args.cold_runs < 5:
        parser.error("--cold-runs must be at least 5")
    print(json.dumps(asyncio.run(benchmark(args.iterations, args.cold_runs)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
