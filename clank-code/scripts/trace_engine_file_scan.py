"""Compare packaged ACP startup with a controlled ripgrep path stream on Linux.

Example: python scripts/trace_engine_file_scan.py /path/to/openclank-engine
Emits JSONL on stdout. Uses isolated temporary directories and requires Node 22+.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import time

# Observe only the disposable child's runtime. Keep its inspector off the live service.
INSPECT = """
const ws = new WebSocket(process.argv[1]);
const timer = setTimeout(() => process.exit(2), 1000);
ws.onerror = () => process.exit(1);
ws.onopen = () => ws.send(JSON.stringify({
  id: 1, method: 'Runtime.evaluate',
  params: {expression: 'JSON.stringify(process.memoryUsage())', returnByValue: true}
}));
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id !== 1) return;
  console.log(m.result.result.value);
  clearTimeout(timer);
  ws.close();
};
"""


def loopback_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sample_engine(binary, root, label, count, same_home):
    home = root / "home"
    cwd = home if same_home else root / "project"
    tools = root / "bin"
    for folder in (home, cwd, tools):
        folder.mkdir(parents=True, exist_ok=True)
    marker = root / "scan-count"
    rg = tools / "rg"
    rg.write_text(f"""#!/usr/bin/env python3
import os, sys
if '--files' in sys.argv:
    with open({str(marker)!r}, 'a') as f:
        f.write('scan\\n')
    for i in range({count}):
        print('synthetic-long-directory-name-%d/folder-%d/another/path/to/file-%d.cpp' % (i//1000, i//50, i))
else:
    os.execv('/usr/bin/rg', ['rg', *sys.argv[1:]])
""")
    rg.chmod(0o700)
    inspector = f"127.0.0.1:{loopback_port()}/{secrets.token_hex(18)}"
    env = {
        "HOME": str(home), "PATH": f"{tools}:/usr/bin",
        "OPENCLANK_HOME": str(root / "engine"), "OPENCLANK_DISABLE_MODELS_FETCH": "1",
        "OPENCLANK_DISABLE_AUTOUPDATE": "1", "BUN_INSPECT": inspector,
        "OPENCLANK_SERVER_PASSWORD": secrets.token_urlsafe(32),
    }
    with open(root / "startup.log", "w") as log:
        started = time.monotonic()
        proc = subprocess.Popen(
            [binary, "acp", "--hostname", "127.0.0.1", "--port", str(loopback_port()), "--print-logs"],
            env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=log, start_new_session=True,
        )
        try:
            for tick in (1, 3, 6, 10, 15):
                time.sleep(max(0, started + tick - time.monotonic()))
                if proc.poll() is not None:
                    raise RuntimeError(f"isolated engine exited with status {proc.returncode}")
                rollup = Path(f"/proc/{proc.pid}/smaps_rollup").read_text()
                anon = next(int(row.split()[1]) for row in rollup.splitlines() if row.startswith("Pss_Anon:"))
                peek = subprocess.run(
                    ["node", "--input-type=module", "-e", INSPECT, f"ws://{inspector}"],
                    capture_output=True, timeout=3,
                )
                mem = json.loads(peek.stdout) if peek.returncode == 0 else {}
                print(json.dumps({
                    "case": label, "elapsedSeconds": round(time.monotonic()-started, 2),
                    "anonPssBytes": anon * 1024, "processMemoryUsageBytes": mem,
                    "recursiveScans": len(marker.read_text().splitlines()) if marker.exists() else 0,
                    "inputRowsPerScan": count,
                }), flush=True)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--records", type=int, default=750000)
    args = parser.parse_args()
    if not 0 < args.records <= 1000000:
        parser.error("records must be between 1 and 1000000")
    binary = args.binary.resolve(strict=True)
    with binary.open("rb") as stream:
        print(json.dumps({"executableSHA256": hashlib.file_digest(stream, "sha256").hexdigest()}), flush=True)
    with tempfile.TemporaryDirectory(prefix="openclank-trace-") as temp:
        for label, rows, same in [
            ("project-empty", 0, False), ("project-populated", args.records, False),
            ("private-home", args.records, True),
        ]:
            sample_engine(binary, Path(temp) / label, label, rows, same)


if __name__ == "__main__":
    main()
