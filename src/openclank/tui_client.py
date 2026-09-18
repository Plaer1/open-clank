"""Authenticated client for the versioned Open Clank TUI control plane."""

from __future__ import annotations

import errno
import time
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from src.openclank.client_profiles import ClientProfile


class TuiClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


def _connection_refused(exc: BaseException) -> bool:
    """Classify only an OS-level refused socket, including WinError 10061."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        if getattr(current, "errno", None) == errno.ECONNREFUSED:
            return True
        if getattr(current, "winerror", None) == 10061:
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class OpenClankTuiClient:
    """Thin API client; it has no engine SDK, provider adapter, or local vault."""

    def __init__(
        self,
        profile: ClientProfile,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.profile = profile
        self.token = token
        self._http = httpx.Client(
            base_url=profile.url.rstrip("/") + "/",
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OpenClankTuiClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            if not self.token:
                raise TuiClientError("this profile is not logged in", status_code=401)
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self._http.request(method, path.lstrip("/"), json=json_body, headers=headers)
        except httpx.ConnectError as exc:
            reason = "connection_refused" if _connection_refused(exc) else "connection_failed"
            raise TuiClientError(
                f"cannot connect to {self.profile.url}", reason=reason
            ) from exc
        except httpx.HTTPError as exc:
            raise TuiClientError(f"Open Clank request failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            message = str(detail or f"Open Clank returned HTTP {response.status_code}")
            raise TuiClientError(message, status_code=response.status_code)
        if not isinstance(payload, dict):
            raise TuiClientError("Open Clank returned a malformed response")
        return payload

    def info(self) -> dict[str, Any]:
        return self._request("GET", "/api/tui/v1/info", authenticated=False)

    def start_device_authorization(
        self,
        *,
        device_label: str,
        scopes: list[str] | None = None,
    ) -> DeviceAuthorization:
        body: dict[str, Any] = {"device_label": device_label}
        if scopes is not None:
            body["scopes"] = scopes
        data = self._request(
            "POST",
            "/api/tui/v1/device/start",
            json_body=body,
            authenticated=False,
        )
        try:
            return DeviceAuthorization(
                device_code=str(data["device_code"]),
                user_code=str(data["user_code"]),
                verification_uri=str(data["verification_uri"]),
                verification_uri_complete=str(data["verification_uri_complete"]),
                expires_in=int(data["expires_in"]),
                interval=max(1, int(data["interval"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TuiClientError("Open Clank returned a malformed device authorization") from exc

    def poll_device_authorization(
        self,
        flow: DeviceAuthorization,
        *,
        on_wait: Callable[[int], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        deadline = clock() + flow.expires_in
        polls = 0
        while clock() < deadline:
            polls += 1
            if on_wait:
                on_wait(polls)
            response = self._http.post(
                "api/tui/v1/device/token",
                json={"device_code": flow.device_code},
                headers={"Accept": "application/json"},
            )
            if response.status_code == 428:
                sleep(flow.interval)
                continue
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.status_code >= 400:
                detail = payload.get("detail") if isinstance(payload, dict) else None
                raise TuiClientError(
                    str(detail or f"device authorization failed ({response.status_code})"),
                    status_code=response.status_code,
                )
            if not isinstance(payload, dict) or not str(payload.get("access_token") or "").startswith("oct_"):
                raise TuiClientError("Open Clank returned a malformed device credential")
            self.token = str(payload["access_token"])
            return payload
        raise TuiClientError("device authorization expired", status_code=410)

    def bootstrap(self) -> dict[str, Any]:
        return self._request("GET", "/api/tui/v1/bootstrap")

    def sessions(self, *, archived: bool = False, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        marker = "true" if archived else "false"
        return self._request(
            "GET",
            f"/api/tui/v1/sessions?archived={marker}&offset={max(0, offset)}&limit={limit}",
        )

    def messages(self, session_id: str, *, limit: int = 100) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/tui/v1/sessions/{session_id}/messages?limit={limit}",
        )

    def stream_turn(
        self,
        session_id: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ):
        if not self.token:
            raise TuiClientError("this profile is not logged in", status_code=401)
        body = {
            "message": message,
            "idempotency_key": idempotency_key or uuid.uuid4().hex,
            "attachments": [],
            "allow_bash": False,
            "allow_web_search": False,
        }
        try:
            with self._http.stream(
                "POST",
                f"api/tui/v1/sessions/{session_id}/turns",
                json=body,
                headers={
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {self.token}",
                },
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    try:
                        detail = response.json().get("detail")
                    except (ValueError, AttributeError):
                        detail = None
                    raise TuiClientError(
                        str(detail or f"turn failed ({response.status_code})"),
                        status_code=response.status_code,
                    )
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if not value or value == "[DONE]":
                        continue
                    try:
                        yield json.loads(value)
                    except ValueError:
                        continue
        except httpx.HTTPError as exc:
            raise TuiClientError(f"Open Clank turn stream failed: {exc}") from exc

    def tasks(self, *, limit: int = 100) -> dict[str, Any]:
        return self._request("GET", f"/api/tui/v1/tasks?limit={limit}")

    def providers(self) -> dict[str, Any]:
        return self._request("GET", "/api/tui/v1/providers")

    def provider_bindings(self) -> dict[str, Any]:
        return self._request("GET", "/api/tui/v1/provider-bindings")

    def use_next_provider_account(
        self,
        connection_id: str,
        *,
        expected_revision: int,
        model_route_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/tui/v1/providers/{connection_id}/use-next",
            json_body={
                "expected_revision": expected_revision,
                "model_route_id": model_route_id,
                "idempotency_key": idempotency_key or uuid.uuid4().hex,
            },
        )

    def shares(self) -> dict[str, Any]:
        return self._request("GET", "/api/tui/v1/shares")

    def diagnostics(self) -> dict[str, Any]:
        return self._request("GET", "/api/tui/v1/diagnostics")

    def revoke_current_device(self) -> dict[str, Any]:
        return self._request("DELETE", "/api/tui/v1/device/current")
