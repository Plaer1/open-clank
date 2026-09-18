#!/usr/bin/env python3
"""Measure local framing candidates without touching the product service.

This is a repeatable S01 harness, not a transport implementation. It compares
long-lived framed Unix sockets, loopback TCP, and stdio under the same echo
workload. Windows named-pipe and container CI runs must still provide native
evidence before the Rust transport is admitted.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from statistics import median
from typing import BinaryIO, Callable


FRAME = struct.Struct("!I")


SERVER = r'''
import socket, struct, sys, threading

FRAME = struct.Struct("!I")

def recv_frame(stream):
    header = stream.recv(4) if hasattr(stream, "recv") else stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise RuntimeError("short header")
    length = FRAME.unpack(header)[0]
    body = b""
    while len(body) < length:
        chunk = stream.recv(length - len(body)) if hasattr(stream, "recv") else stream.read(length - len(body))
        if not chunk:
            raise RuntimeError("short body")
        body += chunk
    return body

def send_frame(stream, body):
    packet = FRAME.pack(len(body)) + body
    if hasattr(stream, "sendall"):
        stream.sendall(packet)
    else:
        stream.write(packet)
        stream.flush()

def serve_connection(conn):
    try:
        while True:
            body = recv_frame(conn)
            if body is None:
                return
            send_frame(conn, body)
    finally:
        conn.close()

mode = sys.argv[1]
if mode == "stdio":
    while True:
        body = recv_frame(sys.stdin.buffer)
        if body is None:
            break
        send_frame(sys.stdout.buffer, body)
elif mode in ("unix", "tcp"):
    if mode == "unix":
        address = sys.argv[2]
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(address)
    else:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        print(server.getsockname()[1], flush=True)
    server.listen(64)
    if mode == "unix":
        print("ready", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve_connection, args=(conn,), daemon=True).start()
else:
    raise SystemExit("unknown mode")
'''


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


def summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(median(values), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "p99_ms": round(percentile(values, 0.99), 3),
    }


@dataclass
class Candidate:
    name: str
    process: subprocess.Popen[bytes]
    connect: Callable[[], object]
    close: Callable[[object], None]


@dataclass
class PipeDuplex:
    reader: BinaryIO
    writer: BinaryIO

    def read(self, size: int) -> bytes:
        return self.reader.read(size)

    def write(self, body: bytes) -> int:
        return self.writer.write(body)

    def flush(self) -> None:
        self.writer.flush()


def recv_exact(stream: object, size: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while received < size:
        chunk = stream.recv(size - received) if isinstance(stream, socket.socket) else stream.read(size - received)
        if not chunk:
            raise RuntimeError("transport closed before a complete frame")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def round_trip(stream: object, payload: bytes) -> float:
    packet = FRAME.pack(len(payload)) + payload
    start = time.perf_counter_ns()
    if isinstance(stream, socket.socket):
        stream.sendall(packet)
    else:
        stream.write(packet)
        stream.flush()
    header = recv_exact(stream, FRAME.size)
    echoed = recv_exact(stream, FRAME.unpack(header)[0])
    elapsed = (time.perf_counter_ns() - start) / 1_000_000
    if echoed != payload:
        raise RuntimeError("echo payload mismatch")
    return elapsed


def start_candidate(mode: str, temp_dir: str) -> Candidate:
    args = [sys.executable, "-u", "-c", SERVER, mode]
    if mode == "unix":
        path = os.path.join(temp_dir, "odysseus-transport.sock")
        args.append(path)
    started = time.perf_counter_ns()
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    if mode == "stdio":
        process.stdout
    elif mode == "unix":
        ready = process.stdout.readline()
        if ready.strip() != b"ready":
            raise RuntimeError(f"unix server failed to start: {ready!r}")
    else:
        port_line = process.stdout.readline()
        if not port_line:
            raise RuntimeError("tcp server failed to report its port")
        port = int(port_line)

    def connect() -> BinaryIO | socket.socket:
        if mode == "stdio":
            assert process.stdin is not None
            assert process.stdout is not None
            return PipeDuplex(process.stdout, process.stdin)
        sock = socket.socket(socket.AF_UNIX if mode == "unix" else socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(path if mode == "unix" else ("127.0.0.1", port))
        return sock

    def close(stream: object) -> None:
        if isinstance(stream, socket.socket):
            stream.close()

    _ = started
    return Candidate(mode, process, connect, close)


def stop_candidate(candidate: Candidate) -> None:
    candidate.process.terminate()
    try:
        candidate.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        candidate.process.kill()
        candidate.process.wait(timeout=2)


def measure(candidate: Candidate, iterations: int, bulk_bytes: int, concurrency: int) -> dict[str, object]:
    small = b"{\"op\":\"health\",\"request_id\":\"warm\"}"
    bulk = b"x" * bulk_bytes
    cold_start = time.perf_counter_ns()
    stream = candidate.connect()
    ready_ms = (time.perf_counter_ns() - cold_start) / 1_000_000
    try:
        round_trip(stream, small)
        warm = [round_trip(stream, small) for _ in range(iterations)]
        bulk_samples = [round_trip(stream, bulk) for _ in range(max(3, iterations // 5))]
    finally:
        candidate.close(stream)

    concurrent: list[float] = []
    if candidate.name != "stdio":
        barrier = threading.Barrier(concurrency)
        lock = threading.Lock()

        def one() -> None:
            local = candidate.connect()
            try:
                barrier.wait(timeout=5)
                elapsed = round_trip(local, small)
                with lock:
                    concurrent.append(elapsed)
            finally:
                candidate.close(local)

        threads = [threading.Thread(target=one) for _ in range(concurrency)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        if len(concurrent) != concurrency:
            raise RuntimeError(f"{candidate.name} concurrency run did not complete")

    return {
        "ready_ms": round(ready_ms, 3),
        "warm": summary(warm),
        "bulk": summary(bulk_samples),
        "concurrency": summary(concurrent) if concurrent else None,
        "iterations": iterations,
        "bulk_bytes": bulk_bytes,
        "concurrency_width": concurrency,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--bulk-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args()
    if args.iterations < 5 or args.bulk_bytes < 1 or args.concurrency < 1:
        parser.error("iterations must be >= 5; bulk-bytes and concurrency must be positive")

    results: dict[str, object] = {
        "schema": "open-clank.odysseus-transport-bakeoff/v1",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "candidates": {},
        "limitations": [
            "This harness measures framing and local process topology, not Rust implementation behavior.",
            "Windows named-pipe and Linux/container namespace runs remain required before S01 admission.",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="odysseus-transport-") as temp_dir:
        for mode in ("unix", "tcp", "stdio"):
            candidate = start_candidate(mode, temp_dir)
            try:
                results["candidates"][mode] = measure(candidate, args.iterations, args.bulk_bytes, args.concurrency)
            finally:
                stop_candidate(candidate)
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
