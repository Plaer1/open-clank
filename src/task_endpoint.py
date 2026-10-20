"""Managed tool-free completion wrapper for background tasks."""

import asyncio
import uuid

from src.interactive_gate import wait_for_interactive_quiet


async def task_complete_text(
    messages,
    *,
    fallback_url=None,
    fallback_model=None,
    fallback_headers=None,
    owner=None,
    **kwargs,
):
    """Run a tool-free background completion through the managed task lane."""
    from src.openclank.modality_facade import complete_text

    # Compatibility-only inputs from pre-cutover callers. Provider transport
    # data cannot cross the managed operation boundary.
    del fallback_url, fallback_model, fallback_headers
    purpose = str(kwargs.pop("purpose", "tasks") or "tasks").strip().lower()
    model_route_id = kwargs.pop("model_route_id", None)
    grant_id = kwargs.pop("grant_id", None)
    root_operation_id = kwargs.pop("root_operation_id", None) or (
        f"task-utility:{uuid.uuid4().hex}"
    )
    temperature = kwargs.pop("temperature", None)
    max_output_tokens = kwargs.pop(
        "max_output_tokens",
        kwargs.pop("max_tokens", None),
    )
    timeout = kwargs.pop("timeout", None)
    # These legacy execution hints never granted tools and have no managed
    # completion equivalent.
    kwargs.pop("workload", None)
    kwargs.pop("session_id", None)
    kwargs.pop("cwd", None)
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError(f"Unsupported managed task completion options: {unknown}")
    await wait_for_interactive_quiet("background task LLM")
    completion = complete_text(
        owner=owner or "local-installation",
        messages=messages,
        purpose=purpose,
        model_route_id=model_route_id,
        grant_id=grant_id,
        root_operation_id=root_operation_id,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    if timeout is not None:
        return await asyncio.wait_for(completion, timeout=float(timeout))
    return await completion
