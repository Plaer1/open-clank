"""Uniform 404 boundary for provider APIs removed by the hard cut."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


RETIRED_PROVIDER_ROUTE_PREFIXES = (
    "/api/model-endpoints",
    "/api/mimo/providers",
    "/api/copilot",
    "/api/chatgpt-subscription",
    "/api/model-shares",
    "/api/embeddings/endpoint",
    "/session/openai",
)
RETIRED_PROVIDER_ROUTE_EXACT = frozenset(
    {
        "/api/discover",
        "/api/ping",
        "/api/probe",
        "/api/probe-selected",
        "/api/providers",
    }
)


class RetiredProviderRouteMiddleware(BaseHTTPMiddleware):
    """Return 404 before authentication or any legacy handler can run."""

    async def dispatch(self, request, call_next):
        path = request.url.path.rstrip("/") or "/"
        if path in RETIRED_PROVIDER_ROUTE_EXACT or any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in RETIRED_PROVIDER_ROUTE_PREFIXES
        ):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return await call_next(request)


__all__ = [
    "RETIRED_PROVIDER_ROUTE_EXACT",
    "RETIRED_PROVIDER_ROUTE_PREFIXES",
    "RetiredProviderRouteMiddleware",
]
