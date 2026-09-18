import json
import subprocess
import sys
from pathlib import Path


def test_transport_bakeoff_is_local_and_emits_all_candidates():
    script = Path(__file__).parents[1] / "scripts" / "odysseus_transport_bakeoff.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--iterations",
            "5",
            "--bulk-bytes",
            "32",
            "--concurrency",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert report["schema"] == "open-clank.odysseus-transport-bakeoff/v1"
    assert set(report["candidates"]) == {"unix", "tcp", "stdio"}
    assert report["candidates"]["stdio"]["concurrency"] is None
    assert report["candidates"]["unix"]["concurrency"]["p95_ms"] >= 0
    assert report["candidates"]["tcp"]["bulk"]["p99_ms"] >= 0
    assert any("Windows" in limitation for limitation in report["limitations"])
