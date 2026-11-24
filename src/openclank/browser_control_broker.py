"""Purpose-bound private broker credentials and lifetools client for Browser control."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import threading
from urllib.parse import urlparse


BROKER_URL_ENV = "OPEN_CLANK_BROWSER_CONTROL_BROKER_URL"
BROKER_TOKEN_ENV = "OPEN_CLANK_BROWSER_CONTROL_BROKER_TOKEN"
BROKER_OWNER_ENV = "OPEN_CLANK_BROWSER_CONTROL_OWNER"
BROKER_SESSION_ENV = "OPEN_CLANK_BROWSER_CONTROL_SESSION_ID"
BROKER_WORKSPACE_ENV = "OPEN_CLANK_BROWSER_CONTROL_WORKSPACE_ID"
BROKER_BINDING_ENV = "OPEN_CLANK_BROWSER_CONTROL_BINDING_REVISION"
BROKER_ENABLED_ENV = "OPEN_CLANK_BROWSER_CONTROL_ENABLED"
BROKER_MUTATIONS_ENV = "OPEN_CLANK_BROWSER_CONTROL_MUTATIONS_ENABLED"
_CONFIG_LOCK = threading.RLock()
_CONFIGURED_URL = ""
_CONFIGURED_SECRET = ""


def configure_browser_control_broker(url: str, secret: str) -> None:
    """Keep the process master secret private and configure one local broker URL."""
    global _CONFIGURED_URL, _CONFIGURED_SECRET
    normalized_url = str(url or "").strip()
    normalized_secret = str(secret or "").strip()
    if bool(normalized_url) != bool(normalized_secret):
        raise ValueError("Browser broker URL and secret must be configured together")
    if normalized_url:
        parsed = urlparse(normalized_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Browser broker must use plain HTTP on loopback")
    with _CONFIG_LOCK:
        _CONFIGURED_URL = normalized_url
        _CONFIGURED_SECRET = normalized_secret


def clear_browser_control_broker() -> None:
    configure_browser_control_broker("", "")


def browser_control_binding_token(
    secret: str,
    *,
    owner: str,
    host_session_id: str,
    authority_workspace_id: str,
    binding_revision: int,
    capabilities: tuple[str, ...] | list[str] = ("observe",),
) -> str:
    """Mint a token for one owner/session/Workspace binding and purpose."""
    if (
        not all(isinstance(value, str) and value.strip() for value in
                (secret, owner, host_session_id, authority_workspace_id))
        or type(binding_revision) is not int
        or binding_revision < 0
    ):
        raise ValueError("Incomplete Browser control binding")
    admitted = tuple(sorted(set(capabilities)))
    if not admitted or set(admitted) - {"observe", "control"} or ("control" in admitted and "observe" not in admitted):
        raise ValueError("Invalid Browser capability set")
    message = json.dumps(
        ["open-clank-browser-control-v1", owner.strip(), host_session_id.strip(),
         authority_workspace_id.strip(), binding_revision, admitted],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def browser_control_descriptor_env(
    *,
    broker_url: str,
    secret: str,
    owner: str,
    host_session_id: str,
    authority_workspace_id: str,
    binding_revision: int,
    enabled: bool = False,
    mutations_enabled: bool = False,
) -> dict[str, str]:
    """Return only scoped child environment values; never exposes master secret."""
    if not enabled:
        return {}
    url = str(broker_url or "").strip()
    owner = str(owner or "").strip()
    host_session_id = str(host_session_id or "").strip()
    workspace_id = str(authority_workspace_id or "").strip()
    if not url or not owner or not host_session_id or not workspace_id:
        return {}
    token = browser_control_binding_token(
        secret,
        owner=owner,
        host_session_id=host_session_id,
        authority_workspace_id=workspace_id,
        binding_revision=binding_revision,
        capabilities=("observe", "control") if mutations_enabled else ("observe",),
    )
    return {
        BROKER_URL_ENV: url,
        BROKER_TOKEN_ENV: token,
        BROKER_OWNER_ENV: owner,
        BROKER_SESSION_ENV: host_session_id,
        BROKER_WORKSPACE_ENV: workspace_id,
        BROKER_BINDING_ENV: str(binding_revision),
        BROKER_ENABLED_ENV: "1",
        BROKER_MUTATIONS_ENV: "1" if mutations_enabled else "0",
    }


def configured_browser_control_descriptor_env(
    *, owner: str, host_session_id: str, authority_workspace_id: str,
    binding_revision: int, enabled: bool = False, mutations_enabled: bool = False,
) -> dict[str, str]:
    with _CONFIG_LOCK:
        url, secret = _CONFIGURED_URL, _CONFIGURED_SECRET
    return browser_control_descriptor_env(
        broker_url=url, secret=secret, owner=owner,
        host_session_id=host_session_id,
        authority_workspace_id=authority_workspace_id,
        binding_revision=binding_revision, enabled=enabled,
        mutations_enabled=mutations_enabled,
    )


class BrowserControlBrokerClient:
    """Small stdlib HTTP client for the lifetools child process."""

    def __init__(self, environ=None, *, timeout: float = 12.0, response_limit: int = 16 * 1024 * 1024, transport=None):
        env = os.environ if environ is None else environ
        self.url = str(env.get(BROKER_URL_ENV) or "").strip()
        self.token = str(env.get(BROKER_TOKEN_ENV) or "").strip()
        self.owner = str(env.get(BROKER_OWNER_ENV) or "").strip()
        self.session_id = str(env.get(BROKER_SESSION_ENV) or "").strip()
        self.workspace_id = str(env.get(BROKER_WORKSPACE_ENV) or "").strip()
        self.binding_revision = str(env.get(BROKER_BINDING_ENV) or "").strip()
        self.enabled = env.get(BROKER_ENABLED_ENV) == "1"
        self.mutations_enabled = env.get(BROKER_MUTATIONS_ENV) == "1"
        self.capabilities = "observe,control" if self.mutations_enabled else "observe"
        self.timeout = float(timeout)
        self.response_limit = int(response_limit)
        self._transport = transport

    async def call(self, tool_name: str, arguments: dict, *, managed_identity: dict | None = None) -> dict:
        if not self.enabled or not all((self.url, self.token, self.owner, self.session_id, self.workspace_id, self.binding_revision)):
            return {"error": "Browser control is unavailable for this managed session", "exit_code": 1}
        if tool_name == "browser_control" and not self.mutations_enabled:
            return {"error": "Browser actions are disabled for this session", "exit_code": 1, "blocked": True}
        body = json.dumps(
            {"tool": str(tool_name), "arguments": dict(arguments or {}), "managed_identity": managed_identity},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return await self._post(body, tool_name=tool_name)

    async def _post(self, body: bytes, *, tool_name: str) -> dict:
        parsed = urlparse(self.url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            return {"error": "Browser broker is unavailable", "exit_code": 1}
        headers = {
            "Content-Type": "application/json",
            "X-Odysseus-Internal-Token": self.token,
            "X-Open-Clank-Owner": self.owner,
            "X-Open-Clank-Browser-Session": self.session_id,
            "X-Open-Clank-Browser-Workspace": self.workspace_id,
            "X-Open-Clank-Browser-Binding": self.binding_revision,
            "X-Open-Clank-Browser-Capabilities": self.capabilities,
        }
        try:
            import httpx

            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, transport=self._transport) as client:
                    async with client.stream("POST", self.url, content=body, headers=headers) as response:
                        if int(response.headers.get("content-length", "0") or 0) > self.response_limit:
                            return self._unknown_or_error(tool_name, "Browser response exceeded the size limit")
                        chunks = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self.response_limit:
                                return self._unknown_or_error(tool_name, "Browser response exceeded the size limit")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        status_code = response.status_code
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                result = None
            if status_code >= 400:
                detail = result.get("detail") if isinstance(result, dict) else None
                if isinstance(detail, dict) and type(detail.get("outcome_unknown")) is bool:
                    return {**detail, "exit_code": 1, "status": status_code}
                if status_code >= 500:
                    return self._unknown_or_error(tool_name, "Browser broker could not confirm the result")
                return {"error": str(detail or "Browser broker rejected the request")[:512], "exit_code": 1, "status": status_code}
            if (not isinstance(result, dict) or (tool_name == "browser_control"
                    and (not isinstance(result.get("actionReceipt"), dict)
                         or result["actionReceipt"].get("state") != "completed"))):
                return self._unknown_or_error(tool_name, "Browser broker did not return a confirmed result")
            return result
        except TimeoutError:
            return self._unknown_or_error(tool_name, "Browser broker timed out")
        except Exception:
            return self._unknown_or_error(tool_name, "Browser broker request failed")

    @staticmethod
    def _unknown_or_error(tool_name: str, message: str) -> dict:
        if tool_name == "browser_control":
            return {"error": message, "code": "browser_outcome_unknown", "outcome_unknown": True, "exit_code": 1}
        return {"error": message, "code": "browser_response_unavailable", "exit_code": 1}
