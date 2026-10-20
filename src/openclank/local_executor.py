"""Capability-bound broker for registered local model executors."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from src.openclank.artifacts import ArtifactRecord, ArtifactStore


_FORBIDDEN_OPTION_KEYS = frozenset(
    {
        "command",
        "cmd",
        "path",
        "url",
        "uri",
        "credential",
        "credentials",
        "token",
        "api_key",
        "headers",
        "environment",
        "env",
    }
)


class LocalExecutorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutorInput:
    artifact_id: str
    media_type: str
    size_bytes: int
    read: Callable[[], bytes]


@dataclass(frozen=True, slots=True)
class ExecutorOutput:
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    executor_id: str
    model_id: str
    operations: frozenset[str]
    max_concurrency: int
    handler: Callable[
        [str, list[ExecutorInput], Mapping[str, Any]],
        ExecutorOutput | Awaitable[ExecutorOutput],
    ]


def _validate_options(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        raise LocalExecutorError("executor options are too deeply nested")
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_OPTION_KEYS:
                raise LocalExecutorError(f"executor option {normalized!r} is not permitted")
            _validate_options(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_options(child, depth=depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise LocalExecutorError("executor options must be JSON scalar/list/object values")


class LocalExecutorBroker:
    def __init__(self, artifact_store: ArtifactStore, *, global_concurrency: int = 2):
        self.artifacts = artifact_store
        self._specs: dict[str, ExecutorSpec] = {}
        self._limits: dict[str, asyncio.Semaphore] = {}
        self._global_limit = asyncio.Semaphore(max(1, int(global_concurrency)))

    def register(
        self,
        *,
        executor_id: str,
        model_id: str,
        operations: set[str] | frozenset[str],
        handler,
        max_concurrency: int = 1,
    ) -> None:
        identifier = str(executor_id or "").strip()
        if not identifier or identifier in self._specs:
            raise LocalExecutorError("executor ID is missing or already registered")
        allowed = frozenset(str(value) for value in operations if str(value))
        if not allowed or not callable(handler):
            raise LocalExecutorError("executor operations and handler are required")
        spec = ExecutorSpec(
            executor_id=identifier,
            model_id=str(model_id),
            operations=allowed,
            max_concurrency=max(1, int(max_concurrency)),
            handler=handler,
        )
        self._specs[identifier] = spec
        self._limits[identifier] = asyncio.Semaphore(spec.max_concurrency)

    async def invoke(
        self,
        *,
        owner: str,
        executor_id: str,
        operation: str,
        artifact_ids: list[str],
        options: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        spec = self._specs.get(str(executor_id))
        if spec is None:
            raise LocalExecutorError("local executor is not registered")
        if operation not in spec.operations:
            raise LocalExecutorError("local executor does not support this operation")
        normalized_options = dict(options or {})
        _validate_options(normalized_options)
        inputs: list[ExecutorInput] = []
        for artifact_id in artifact_ids:
            row = self.artifacts.get(owner=owner, artifact_id=artifact_id)
            inputs.append(
                ExecutorInput(
                    artifact_id=row.id,
                    media_type=row.media_type,
                    size_bytes=row.size_bytes,
                    read=lambda aid=row.id: b"".join(
                        self.artifacts.read_chunks(owner=owner, artifact_id=aid)
                    ),
                )
            )
        async with self._global_limit, self._limits[spec.executor_id]:
            if inspect.iscoroutinefunction(spec.handler):
                output = await spec.handler(operation, inputs, normalized_options)
            else:
                output = await asyncio.to_thread(
                    spec.handler,
                    operation,
                    inputs,
                    normalized_options,
                )
        if not isinstance(output, ExecutorOutput) or not isinstance(output.data, bytes):
            raise LocalExecutorError("local executor returned an invalid output")
        return self.artifacts.put(
            owner=owner,
            chunks=(output.data,),
            media_type=output.media_type,
        )


__all__ = [
    "ExecutorInput",
    "ExecutorOutput",
    "LocalExecutorBroker",
    "LocalExecutorError",
]
