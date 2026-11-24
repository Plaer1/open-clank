#!/usr/bin/env python3
"""Sample the local Linux user service without reading command lines or environment.

Print one numeric JSON object per sample. Process roles: 0 other, 1 host,
2 engine, 3 memory, 4 history, 5 files, 6 Python helper. CPU units are cumulative
microseconds (cgroup) and clock ticks (processes); delta fields compare consecutive
observations of the same incarnation. Memory is in bytes.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def counters(path):
    result = {}
    for line in path.read_text().splitlines():
        name, value, *_ = line.split()
        result[name.rstrip(":")] = int(value)
    return result


def process(pid, main_pid):
    proc = Path("/proc") / str(pid)
    stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    if stat[0] == "Z":
        return None
    name = (proc / "comm").read_text().strip()
    role = {"openclank-engin": 2, "memery-worker": 3, "openclank-histo": 4,
            "odysseus-files-": 5, "python": 6}.get(name, 0)
    memory = {}
    for line in (proc / "smaps_rollup").read_text().splitlines()[1:]:
        field, value, *_ = line.split()
        if field in ("Pss:", "Pss_Anon:", "Pss_File:", "Private_Dirty:", "SwapPss:"):
            memory[field[:-1]] = int(value) * 1024
    start = int(stat[19])
    if (proc / "stat").read_text().rsplit(")", 1)[1].split()[19] != str(start):
        return None
    return dict(pid=pid, start_ticks=start, role=1 if pid == main_pid else role,
                cpu_ticks=int(stat[11]) + int(stat[12]), **memory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", default="openclank.service", help="systemd user service to measure")
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--interval", type=float, default=2)
    args = parser.parse_args()
    if not (1 <= args.samples <= 600 and .25 <= args.interval <= 60):
        parser.error("use 1–600 samples and an interval between 0.25 and 60 seconds")
    if not args.unit.endswith(".service") or not args.unit[0].isalnum() or "/" in args.unit:
        parser.error("use a systemd service name")
    unit = subprocess.check_output(["systemctl", "--user", "show", "-p", "ControlGroup",
                                    "-p", "MainPID", "--", args.unit], text=True)
    fields = dict(line.split("=", 1) for line in unit.splitlines())
    group = fields["ControlGroup"].strip("/")
    if not group or not int(fields["MainPID"]):
        parser.error("selected service is inactive")
    root = Path("/sys/fs/cgroup") / group
    previous = {}
    previous_cpu = None
    for index in range(args.samples):
        now = time.monotonic_ns()
        memory = counters(root / "memory.stat")
        cpu = counters(root / "cpu.stat")["usage_usec"]
        rows = []
        skipped = 0
        for group_file in root.rglob("cgroup.procs"):
            try:
                pids = group_file.read_text().split()
            except FileNotFoundError:
                continue
            for pid in map(int, pids):
                try:
                    row = process(pid, int(fields["MainPID"]))
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    row = None
                if row is None:
                    skipped += 1
                    continue
                key = (pid, row["start_ticks"])
                if key in previous:
                    row["cpu_delta_ticks"] = row["cpu_ticks"] - previous[key]
                rows.append(row)
        previous = {(r["pid"], r["start_ticks"]): r["cpu_ticks"] for r in rows}
        sample = dict(monotonic_ns=now, hz=os.sysconf("SC_CLK_TCK"),
                      interval_s=args.interval, current=int((root / "memory.current").read_text()),
                      peak=int((root / "memory.peak").read_text()), cpu_us=cpu,
                      skipped=skipped, processes=rows,
                      **{k: memory[k] for k in ("anon", "file", "slab")})
        if previous_cpu is not None:
            sample["cpu_delta_us"] = cpu - previous_cpu
        previous_cpu = cpu
        sample["collection_ns"] = time.monotonic_ns() - now
        print(json.dumps(sample, separators=(",", ":")), flush=True)
        if index + 1 < args.samples:
            time.sleep(max(0, args.interval - sample["collection_ns"] / 1e9))


if __name__ == "__main__":
    main()
