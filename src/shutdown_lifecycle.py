"""Small, dependency-free helpers for observable bounded shutdown."""

import asyncio
import logging
import time


logger = logging.getLogger(__name__)


async def run_shutdown_phase(name, operation, *, timeout: float) -> str:
    """Run and time one bounded shutdown phase without hiding its outcome."""
    started = time.monotonic()
    result = "ok"
    logger.info("shutdown phase=%s event=start timeout_s=%.1f", name, timeout)
    try:
        await asyncio.wait_for(operation(), timeout=timeout)
    except asyncio.TimeoutError:
        result = "timeout"
        logger.warning("shutdown phase=%s timed out after %.1fs", name, timeout)
    except asyncio.CancelledError:
        result = "cancelled"
        raise
    except Exception as exc:
        result = "error"
        logger.warning("shutdown phase=%s failed: %s", name, exc)
    finally:
        logger.info(
            "shutdown phase=%s event=end duration_s=%.3f result=%s",
            name,
            time.monotonic() - started,
            result,
        )
    return result
