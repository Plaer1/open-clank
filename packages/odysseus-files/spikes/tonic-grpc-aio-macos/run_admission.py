#!/usr/bin/env python3
"""Run the bounded Tonic/grpc.aio Unix-socket transport admission profile."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib.util
import json
import math
import multiprocessing
import os
import platform
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import grpc


HERE = Path(__file__).resolve().parent
PYTHON_BINDINGS = HERE / "python"
sys.path.insert(0, str(PYTHON_BINDINGS))

import admission_pb2 as wire  # noqa: E402
import admission_pb2_grpc as wire_grpc  # noqa: E402


MIB = 1024 * 1024
CHUNK_BYTES = 256 * 1024
SESSION_HEADER = "x-open-clank-session"
PARENT_MODE = 0o700
SOCKET_MODE = 0o600
PRODUCER_QUEUE_CHUNKS = 4


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile without samples")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def process_rss_mib(pid: int) -> float:
    class ProcTaskInfo(ctypes.Structure):
        _fields_ = [
            ("virtual_size", ctypes.c_uint64),
            ("resident_size", ctypes.c_uint64),
            ("total_user", ctypes.c_uint64),
            ("total_system", ctypes.c_uint64),
            ("threads_user", ctypes.c_uint64),
            ("threads_system", ctypes.c_uint64),
            ("policy", ctypes.c_int32),
            ("faults", ctypes.c_int32),
            ("pageins", ctypes.c_int32),
            ("cow_faults", ctypes.c_int32),
            ("messages_sent", ctypes.c_int32),
            ("messages_received", ctypes.c_int32),
            ("syscalls_mach", ctypes.c_int32),
            ("syscalls_unix", ctypes.c_int32),
            ("context_switches", ctypes.c_int32),
            ("thread_count", ctypes.c_int32),
            ("running_thread_count", ctypes.c_int32),
            ("priority", ctypes.c_int32),
        ]

    # Avoid spawning `ps` while gRPC's native worker threads are live: forking a
    # multi-threaded grpcio process both perturbs latency and triggers fork guards.
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    task_info = ProcTaskInfo()
    received = libproc.proc_pidinfo(
        pid, 4, 0, ctypes.byref(task_info), ctypes.sizeof(task_info)
    )
    if received != ctypes.sizeof(task_info):
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return task_info.resident_size / MIB


def tcp_listener_probe(pid: int) -> tuple[bool, str]:
    lsof = shutil.which("lsof")
    if not lsof:
        return False, "lsof is unavailable"
    completed = subprocess.run(
        [lsof, "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    listeners = lines[1:] if lines and lines[0].startswith("COMMAND") else lines
    return not listeners, "\n".join(listeners)


async def wait_for_socket(process: subprocess.Popen[bytes], socket_path: Path) -> None:
    deadline = asyncio.get_running_loop().time() + 8.0
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise RuntimeError(
                f"Rust admission server exited with {process.returncode}: {stderr.strip()}"
            )
        try:
            metadata = socket_path.lstat()
        except FileNotFoundError:
            await asyncio.sleep(0.01)
            continue
        if not stat.S_ISSOCK(metadata.st_mode):
            raise RuntimeError("admission endpoint exists but is not a Unix socket")
        return
    raise TimeoutError("timed out waiting for the private Unix socket")


async def sample_rss(pid: int, stop: asyncio.Event, samples: list[float]) -> None:
    while not stop.is_set():
        try:
            samples.append(await asyncio.to_thread(process_rss_mib, pid))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except TimeoutError:
            pass


class Client:
    def __init__(self, channel: grpc.aio.Channel, session_binding: str) -> None:
        self.stub = wire_grpc.AdmissionStub(channel)
        self.metadata = ((SESSION_HEADER, session_binding),)

    async def health(self, *, timeout: float = 1.0) -> Any:
        return await self.stub.Health(
            wire.HealthRequest(), metadata=self.metadata, timeout=timeout
        )

    async def browse(self, *, timeout: float = 1.0) -> Any:
        return await self.stub.Browse(
            wire.BrowseRequest(page_size=200),
            metadata=self.metadata,
            timeout=timeout,
        )

    async def metrics(self) -> Any:
        return await self.stub.Metrics(
            wire.MetricsRequest(), metadata=self.metadata, timeout=1.0
        )


def new_channel(target: str) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(
        target,
        options=(
            ("grpc.max_receive_message_length", CHUNK_BYTES + 4096),
            # grpcio otherwise derives :authority from the percent-encoded UDS
            # path; Rust h2 correctly rejects that as an invalid authority.
            ("grpc.default_authority", "openclank.local"),
        ),
    )


async def timed(call: Callable[[], Awaitable[Any]]) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    result = await call()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return result, elapsed_ms


async def probe(client: Client, rounds: int, spacing_seconds: float) -> dict[str, list[float]]:
    health_samples: list[float] = []
    browse_samples: list[float] = []
    for _ in range(rounds):
        health_task = asyncio.create_task(timed(client.health))
        browse_task = asyncio.create_task(timed(client.browse))
        (health, health_ms), (browse, browse_ms) = await asyncio.gather(
            health_task, browse_task
        )
        if not health.ready or health.transport != "unix-domain-socket":
            raise AssertionError("health response did not identify the private Unix transport")
        if len(browse.entries) != 200:
            raise AssertionError("browse response did not return the requested bounded page")
        health_samples.append(health_ms)
        browse_samples.append(browse_ms)
        if spacing_seconds:
            await asyncio.sleep(spacing_seconds)
    return {"health_ms": health_samples, "browse_ms": browse_samples}


async def consume_slow_transfer(
    client: Client,
    total_bytes: int,
    slow_delay_seconds: float,
    first_chunk: Any,
) -> dict[str, float | int]:
    request = wire.TransferRequest(
        transfer_id="admission-slow-250mib",
        total_bytes=total_bytes,
        chunk_bytes=CHUNK_BYTES,
        producer_delay_micros=0,
    )
    started_ns = time.perf_counter_ns()
    call = client.stub.Transfer(request, metadata=client.metadata, timeout=60.0)
    expected_offset = 0
    expected_sequence = 0
    received = 0
    first_byte_ms: float | None = None
    final_seen = False
    async for chunk in call:
        if first_byte_ms is None:
            first_byte_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            first_chunk.set()
        if chunk.transfer_id != request.transfer_id:
            raise AssertionError("transfer ID changed in flight")
        if chunk.offset != expected_offset or chunk.sequence != expected_sequence:
            raise AssertionError("transfer sequence/offset is not contiguous")
        if not chunk.data or len(chunk.data) > CHUNK_BYTES:
            raise AssertionError("transfer chunk exceeded the 256 KiB bound")
        expected_fill = expected_sequence % 251
        if chunk.data[0] != expected_fill or chunk.data[-1] != expected_fill:
            raise AssertionError("binary chunk pattern was corrupted")
        expected_offset += len(chunk.data)
        received += len(chunk.data)
        expected_sequence += 1
        if chunk.final_chunk:
            if final_seen or received != total_bytes:
                raise AssertionError("final chunk marker was early or repeated")
            final_seen = True
        await asyncio.sleep(slow_delay_seconds)
    elapsed_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
    if received != total_bytes or not final_seen:
        raise AssertionError("stream ended before its exact binary byte count")
    return {
        "bytes": received,
        "chunks": expected_sequence,
        "first_byte_ms": first_byte_ms or 0.0,
        "elapsed_seconds": elapsed_seconds,
        "throughput_mib_s": received / MIB / elapsed_seconds,
    }


def run_slow_transfer_process(
    target: str,
    session_binding: str,
    total_bytes: int,
    slow_delay_seconds: float,
    start: Any,
    first_chunk: Any,
    result_sender: Any,
) -> None:
    """Own the slow data-plane channel in one bounded Python worker process."""

    async def worker() -> dict[str, float | int]:
        channel = new_channel(target)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=5.0)
            return await consume_slow_transfer(
                Client(channel, session_binding),
                total_bytes,
                slow_delay_seconds,
                first_chunk,
            )
        finally:
            await channel.close(grace=0.5)

    try:
        start.wait()
        result_sender.send(("ok", asyncio.run(worker())))
    except BaseException as error:
        result_sender.send(("error", f"{type(error).__name__}: {error}"))
    finally:
        result_sender.close()


async def receive_worker_result(
    process: multiprocessing.Process, receiver: Any, timeout_seconds: float
) -> dict[str, float | int]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if receiver.poll():
            status, payload = receiver.recv()
            if status != "ok":
                raise RuntimeError(f"transfer worker failed: {payload}")
            return payload
        if not process.is_alive():
            raise RuntimeError(
                f"transfer worker exited before reporting a result: {process.exitcode}"
            )
        await asyncio.sleep(0.01)
    raise TimeoutError("timed out waiting for the bounded transfer worker")


async def wait_for_no_active_transfers(client: Client, started_ns: int) -> float:
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        metrics = await client.metrics()
        if metrics.active_transfers == 0:
            return (time.perf_counter_ns() - started_ns) / 1_000_000
        await asyncio.sleep(0.005)
    raise TimeoutError("Rust producer did not release its active transfer slot")


async def cancellation_probe(client: Client) -> dict[str, Any]:
    request = wire.TransferRequest(
        transfer_id="admission-explicit-cancel",
        total_bytes=32 * MIB,
        chunk_bytes=CHUNK_BYTES,
        producer_delay_micros=1_000,
    )
    call = client.stub.Transfer(request, metadata=client.metadata, timeout=10.0)
    received_chunks = 0
    async for _chunk in call:
        received_chunks += 1
        if received_chunks == 3:
            cancel_started_ns = time.perf_counter_ns()
            accepted = call.cancel()
            break
    else:
        raise AssertionError("cancellation probe completed before cancellation")
    status = await call.code()
    release_ms = await wait_for_no_active_transfers(client, cancel_started_ns)
    return {
        "cancel_accepted": accepted,
        "status": status.name,
        "chunks_before_cancel": received_chunks,
        "producer_release_ms": release_ms,
    }


async def deadline_probe(client: Client) -> dict[str, Any]:
    request = wire.TransferRequest(
        transfer_id="admission-deadline",
        total_bytes=32 * MIB,
        chunk_bytes=CHUNK_BYTES,
        producer_delay_micros=20_000,
    )
    started_ns = time.perf_counter_ns()
    call = client.stub.Transfer(request, metadata=client.metadata, timeout=0.055)
    status = "OK"
    chunks = 0
    try:
        async for _chunk in call:
            chunks += 1
    except grpc.aio.AioRpcError as error:
        status = error.code().name
    elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
    release_ms = await wait_for_no_active_transfers(client, started_ns)
    return {
        "status": status,
        "chunks_before_deadline": chunks,
        "client_elapsed_ms": elapsed_ms,
        "producer_release_ms": release_ms,
    }


async def stream_admission_probe(client: Client) -> dict[str, Any]:
    def request(index: int) -> Any:
        return wire.TransferRequest(
            transfer_id=f"admission-slot-{index}",
            total_bytes=32 * MIB,
            chunk_bytes=CHUNK_BYTES,
            producer_delay_micros=20_000,
        )

    first = client.stub.Transfer(request(1), metadata=client.metadata, timeout=5.0)
    second = client.stub.Transfer(request(2), metadata=client.metadata, timeout=5.0)
    await asyncio.gather(first.read(), second.read())
    third = client.stub.Transfer(request(3), metadata=client.metadata, timeout=1.0)
    third_status = "OK"
    try:
        await third.read()
    except grpc.aio.AioRpcError as error:
        third_status = error.code().name
    cancel_started_ns = time.perf_counter_ns()
    first.cancel()
    second.cancel()
    release_ms = await wait_for_no_active_transfers(client, cancel_started_ns)
    return {
        "configured_stream_slots": 2,
        "third_stream_status": third_status,
        "release_ms": release_ms,
    }


async def unauthorized_probe(client: Client) -> str:
    try:
        await client.stub.Health(
            wire.HealthRequest(),
            metadata=((SESSION_HEADER, "0" * 64),),
            timeout=1.0,
        )
    except grpc.aio.AioRpcError as error:
        return error.code().name
    return "OK"


async def oversized_transfer_probe(client: Client) -> str:
    request = wire.TransferRequest(
        transfer_id="admission-oversized",
        total_bytes=250 * MIB + 1,
        chunk_bytes=CHUNK_BYTES,
    )
    call = client.stub.Transfer(request, metadata=client.metadata, timeout=1.0)
    try:
        await call.read()
    except grpc.aio.AioRpcError as error:
        return error.code().name
    return "OK"


def latency_summary(samples: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(samples),
        "p50_ms": percentile(samples, 0.50),
        "p95_ms": percentile(samples, 0.95),
        "p99_ms": percentile(samples, 0.99),
        "max_ms": max(samples),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    binary = Path(args.binary).expanduser().resolve()
    if not binary.is_file():
        raise FileNotFoundError(f"release admission binary does not exist: {binary}")
    if platform.system() != "Darwin":
        raise RuntimeError("the private peer-credential admission profile requires macOS")
    host_platform = platform.platform()
    host_machine = platform.machine()
    host_python = platform.python_version()

    session_binding = secrets.token_hex(32)
    environment = os.environ.copy()
    environment["OPENCLANK_ADMISSION_SESSION"] = session_binding
    process: subprocess.Popen[bytes] | None = None
    channel: grpc.aio.Channel | None = None
    transfer_process: multiprocessing.Process | None = None
    transfer_start: Any | None = None
    transfer_receiver: Any | None = None
    server_stderr = ""
    with tempfile.TemporaryDirectory(prefix="openclank-tonic-") as directory_name:
        private_directory = Path(directory_name)
        private_directory.chmod(PARENT_MODE)
        socket_path = private_directory / "files.sock"
        process = subprocess.Popen(
            [str(binary), "--socket", str(socket_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
        )
        try:
            await wait_for_socket(process, socket_path)
            parent_mode = mode(private_directory)
            socket_mode = mode(socket_path)
            socket_owner_uid = socket_path.stat(follow_symlinks=False).st_uid
            no_tcp_listener, tcp_detail = await asyncio.to_thread(
                tcp_listener_probe, process.pid
            )
            target = f"unix:{socket_path}"
            process_context = multiprocessing.get_context("spawn")
            transfer_start = process_context.Event()
            first_chunk = process_context.Event()
            transfer_receiver, transfer_sender = process_context.Pipe(duplex=False)
            total_bytes = args.total_mib * MIB
            transfer_process = process_context.Process(
                target=run_slow_transfer_process,
                args=(
                    target,
                    session_binding,
                    total_bytes,
                    args.slow_delay_ms / 1000.0,
                    transfer_start,
                    first_chunk,
                    transfer_sender,
                ),
                name="openclank-grpc-transfer",
            )
            transfer_process.start()
            transfer_sender.close()
            channel = new_channel(target)
            await asyncio.wait_for(channel.channel_ready(), timeout=5.0)
            client = Client(channel, session_binding)
            ready = await client.health()
            idle_rss_mib = await asyncio.to_thread(process_rss_mib, process.pid)

            await probe(client, 10, 0.0)
            # Match the active profile's polling cadence so the non-regression
            # ratio does not compare a hot tight loop with timer wake-ups.
            baseline = await probe(client, args.samples, 0.02)
            rss_samples = [idle_rss_mib]
            python_worker_rss_samples: list[float] = []
            rss_stop = asyncio.Event()
            rss_task = asyncio.create_task(sample_rss(process.pid, rss_stop, rss_samples))
            worker_rss_task = asyncio.create_task(
                sample_rss(
                    transfer_process.pid,
                    rss_stop,
                    python_worker_rss_samples,
                )
            )
            transfer_start.set()
            first_chunk_deadline = asyncio.get_running_loop().time() + 5.0
            while not first_chunk.is_set():
                if asyncio.get_running_loop().time() >= first_chunk_deadline:
                    raise TimeoutError("slow transfer did not produce its first chunk")
                await asyncio.sleep(0.005)
            active_probe = await probe(client, args.samples, 0.02)
            transfer = await receive_worker_result(
                transfer_process, transfer_receiver, timeout_seconds=60.0
            )
            transfer_process.join(timeout=2.0)
            if transfer_process.is_alive() or transfer_process.exitcode != 0:
                raise RuntimeError(
                    f"transfer worker did not exit cleanly: {transfer_process.exitcode}"
                )
            transfer_process = None
            transfer_receiver.close()
            transfer_receiver = None
            rss_stop.set()
            await asyncio.gather(rss_task, worker_rss_task)
            slow_metrics = await client.metrics()

            cancellation = await cancellation_probe(client)
            deadline = await deadline_probe(client)
            stream_admission = await stream_admission_probe(client)
            unauthorized_status = await unauthorized_probe(client)
            oversized_status = await oversized_transfer_probe(client)
            final_metrics = await client.metrics()

            baseline_health = latency_summary(baseline["health_ms"])
            baseline_browse = latency_summary(baseline["browse_ms"])
            active_health = latency_summary(active_probe["health_ms"])
            active_browse = latency_summary(active_probe["browse_ms"])
            health_ratio = active_health["p95_ms"] / baseline_health["p95_ms"]
            browse_ratio = active_browse["p95_ms"] / baseline_browse["p95_ms"]
            peak_rss_mib = max(rss_samples)
            rss_delta_mib = peak_rss_mib - idle_rss_mib

            checks = {
                "full_250_mib_profile": args.total_mib == 250,
                "private_parent_mode_0700": parent_mode == PARENT_MODE,
                "private_socket_mode_0600": socket_mode == SOCKET_MODE,
                "socket_owned_by_effective_uid": socket_owner_uid == os.geteuid(),
                "health_identifies_unix_transport": ready.transport
                == "unix-domain-socket",
                "no_tcp_listener": no_tcp_listener,
                "random_session_binding_rejects_forgery": unauthorized_status
                == "UNAUTHENTICATED",
                "exact_binary_byte_count": transfer["bytes"] == total_bytes,
                "chunk_bound_256_kib": ready.chunk_limit_bytes == CHUNK_BYTES,
                "first_byte_below_75_ms": transfer["first_byte_ms"] < 75.0,
                "baseline_health_below_25_ms": baseline_health["p95_ms"] < 25.0,
                "active_health_below_100_ms": active_health["p95_ms"] < 100.0,
                "active_health_at_most_2x_isolated": health_ratio <= 2.0,
                "active_browse_below_100_ms": active_browse["p95_ms"] < 100.0,
                "active_browse_at_most_2x_isolated": browse_ratio <= 2.0,
                "idle_rust_rss_below_64_mib": idle_rss_mib < 64.0,
                "stream_rss_delta_below_32_mib": rss_delta_mib < 32.0,
                "producer_queue_bounded_to_four_chunks": slow_metrics.max_producer_queue_chunks
                <= PRODUCER_QUEUE_CHUNKS
                and slow_metrics.producer_queue_capacity == PRODUCER_QUEUE_CHUNKS,
                "explicit_cancel_status": cancellation["status"] == "CANCELLED",
                "explicit_cancel_releases_below_250_ms": cancellation[
                    "producer_release_ms"
                ]
                < 250.0,
                "deadline_status": deadline["status"] == "DEADLINE_EXCEEDED",
                "deadline_releases_below_250_ms": deadline["producer_release_ms"]
                < 250.0,
                "third_stream_load_shed": stream_admission["third_stream_status"]
                == "RESOURCE_EXHAUSTED",
                "bounded_request_rejects_over_250_mib": oversized_status
                == "INVALID_ARGUMENT",
                "runtime_does_not_import_grpc_tools": importlib.util.find_spec("grpc_tools")
                is None,
            }
            report: dict[str, Any] = {
                "schema": "openclank.tonic-grpc-aio-admission.v1",
                "passed": all(checks.values()),
                "host": {
                    "platform": host_platform,
                    "machine": host_machine,
                    "python": host_python,
                    "grpcio": grpc.__version__,
                },
                "topology": {
                    "target_scheme": "unix",
                    "parent_mode": f"{parent_mode:04o}",
                    "socket_mode": f"{socket_mode:04o}",
                    "socket_owner_uid": socket_owner_uid,
                    "effective_uid": os.geteuid(),
                    "tcp_listener_detail": tcp_detail,
                    "session_binding_bytes": len(session_binding),
                    "control_http2_channels": 1,
                    "transfer_http2_channels": 1,
                    "bounded_python_transfer_processes": 1,
                },
                "profile": {
                    "total_mib": args.total_mib,
                    "chunk_kib": CHUNK_BYTES // 1024,
                    "slow_client_delay_ms_per_chunk": args.slow_delay_ms,
                    "latency_samples": args.samples,
                },
                "latency": {
                    "isolated_health": baseline_health,
                    "active_health": active_health,
                    "health_p95_ratio": health_ratio,
                    "isolated_browse_200": baseline_browse,
                    "active_browse_200": active_browse,
                    "browse_p95_ratio": browse_ratio,
                },
                "transfer": transfer,
                "memory": {
                    "idle_server_rss_mib": idle_rss_mib,
                    "peak_server_rss_mib": peak_rss_mib,
                    "stream_server_rss_delta_mib": rss_delta_mib,
                    "rss_samples": len(rss_samples),
                    "peak_python_transfer_worker_rss_mib": max(
                        python_worker_rss_samples, default=0.0
                    ),
                    "python_worker_rss_samples": len(python_worker_rss_samples),
                },
                "backpressure": {
                    "producer_queue_capacity_chunks": slow_metrics.producer_queue_capacity,
                    "max_observed_producer_queue_chunks": slow_metrics.max_producer_queue_chunks,
                    "server_emitted_bytes_after_slow_transfer": slow_metrics.emitted_bytes,
                },
                "cancellation": cancellation,
                "deadline": deadline,
                "stream_admission": stream_admission,
                "final_server_metrics": {
                    "active_transfers": final_metrics.active_transfers,
                    "completed_transfers": final_metrics.completed_transfers,
                    "cancelled_transfers": final_metrics.cancelled_transfers,
                    "emitted_bytes": final_metrics.emitted_bytes,
                },
                "checks": checks,
            }
            await client.stub.Shutdown(
                wire.ShutdownRequest(), metadata=client.metadata, timeout=1.0
            )
            await channel.close(grace=0.5)
            channel = None
            return_code = await asyncio.to_thread(process.wait, 3.0)
            if return_code != 0:
                raise RuntimeError(f"Rust admission server exited with {return_code}")
            report["checks"]["graceful_shutdown_removed_socket"] = not socket_path.exists()
            report["passed"] = all(report["checks"].values())
            return report
        finally:
            if transfer_start is not None:
                transfer_start.set()
            if transfer_process is not None:
                transfer_process.join(timeout=0.5)
                if transfer_process.is_alive():
                    transfer_process.terminate()
                    transfer_process.join(timeout=2.0)
            if transfer_receiver is not None:
                transfer_receiver.close()
            if channel is not None:
                await channel.close(grace=0.0)
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 2.0)
            if process.stderr:
                server_stderr = process.stderr.read().decode("utf-8", "replace").strip()
                process.stderr.close()
            if process.returncode not in (0, -15) and server_stderr:
                print(server_stderr, file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        default=str(HERE / "target" / "release" / "openclank-tonic-admission"),
        help="release Rust admission binary",
    )
    parser.add_argument("--total-mib", type=int, default=250)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--slow-delay-ms", type=float, default=3.0)
    arguments = parser.parse_args()
    if arguments.total_mib <= 0 or arguments.total_mib > 250:
        parser.error("--total-mib must be in 1..=250")
    if arguments.samples < 30:
        parser.error("--samples must be at least 30 for the frozen p95 profile")
    if arguments.slow_delay_ms <= 0 or arguments.slow_delay_ms > 100:
        parser.error("--slow-delay-ms must be in (0, 100]")
    return arguments


def main() -> int:
    try:
        report = asyncio.run(run(parse_args()))
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema": "openclank.tonic-grpc-aio-admission.v1",
                    "passed": False,
                    "error": f"{type(error).__name__}: {error}",
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
