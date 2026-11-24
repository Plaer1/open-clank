"""Authenticated client for the versioned Open Clank TUI control plane."""

from __future__ import annotations

import errno
import time
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

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
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json", **(extra_headers or {})}
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
            reason = detail.get("code") if isinstance(detail, dict) and isinstance(detail.get("code"), str) else None
            message = str(detail.get("message") or detail) if isinstance(detail, dict) else str(detail or f"Open Clank returned HTTP {response.status_code}")
            raise TuiClientError(message, status_code=response.status_code, reason=reason)
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
            f"/api/tui/v1/sessions/{quote(session_id, safe='')}/messages?limit={limit}",
        )

    def stream_turn(
        self,
        session_id: str,
        message: str,
        *,
        idempotency_key: str | None = None,
        attachments: list[str] | None = None,
        allow_bash: bool = False,
        allow_web_search: bool = False,
    ):
        if not isinstance(allow_bash, bool) or not isinstance(allow_web_search, bool):
            raise ValueError("tool choices must be explicit booleans")
        if attachments is not None and (not isinstance(attachments, list) or not all(isinstance(item, str) for item in attachments)):
            raise ValueError("attachments must be a list of upload IDs")
        if not self.token:
            raise TuiClientError("this profile is not logged in", status_code=401)
        body = {
            "message": message,
            "idempotency_key": idempotency_key or uuid.uuid4().hex,
            "attachments": list(attachments or []),
            "allow_bash": allow_bash,
            "allow_web_search": allow_web_search,
        }
        try:
            with self._http.stream(
                "POST",
                f"api/tui/v1/sessions/{quote(session_id, safe='')}/turns",
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

    def start_turn_events(self, session_id: str, message: str, *, idempotency_key: str):
        """Submit once and expose only identities actually supplied by the host."""
        if not self.token:
            raise TuiClientError("this profile is not logged in", status_code=401)
        body = {"message": message, "idempotency_key": idempotency_key,
                "attachments": [], "allow_bash": False, "allow_web_search": False}
        try:
            with self._http.stream("POST", f"api/tui/v1/sessions/{quote(session_id, safe='')}/turns",
                    json=body, timeout=httpx.Timeout(30.0, read=None), headers={"Accept": "text/event-stream", "Authorization": f"Bearer {self.token}"}) as response:
                if response.status_code >= 400:
                    response.read()
                    try: detail = response.json().get("detail")
                    except (ValueError, AttributeError): detail = None
                    raise TuiClientError(str(detail or f"turn failed ({response.status_code})"), status_code=response.status_code)
                yield {"event": "started", "run_id": response.headers.get("X-Agent-Run-ID")}
                event, sequence, data = "message", None, []
                for line in response.iter_lines():
                    if not line:
                        if data:
                            raw = "\n".join(data)
                            if raw == "[DONE]":
                                yield {"event": "done", "id": sequence, "data": None}
                            else:
                                try: value = json.loads(raw)
                                except ValueError: value = raw
                                yield {"event": event, "id": sequence, "data": value}
                        event, sequence, data = "message", None, []
                    elif line.startswith("event:"): event = line[6:].strip()
                    elif line.startswith("id:"):
                        value = line[3:].strip()
                        sequence = int(value) if value.isdecimal() else None
                    elif line.startswith("data:"):
                        value = line[5:]
                        data.append(value[1:] if value.startswith(" ") else value)
        except httpx.HTTPError as exc:
            raise TuiClientError(f"Open Clank turn stream failed: {exc}") from exc

    def original_turn_status(self, session_id: str, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/turns/status",
                             json_body={"idempotency_key": idempotency_key})

    def interactions(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/interactions")

    def reply_interaction(self, session_id: str, reply: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/interactions/reply", json_body=reply)

    def optional_questions(self, session_id: str, actor_id: str, map_revision: int) -> dict[str, Any]:
        return self._request("GET", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/actors/{quote(actor_id, safe='')}/questions?map_revision={map_revision}")

    def answer_optional_question(self, session_id: str, actor_id: str, map_revision: int, answer: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/actors/{quote(actor_id, safe='')}/question-answer?map_revision={map_revision}", json_body=answer)

    def background_shells(self, session_id: str) -> dict[str, Any]:
        return self._request('GET', f"/api/tui/v1/sessions/{quote(session_id, safe='')}/background-shells")

    def background_shell(self, session_id: str, job_id: str, actor_id: str, actor_session_id: str,
                         execution_revision: int, map_revision: int, *, cursor: int = 0) -> dict[str, Any]:
        from urllib.parse import urlencode
        params = urlencode({'actor_id': actor_id, 'actor_session_id': actor_session_id,
                            'execution_revision': execution_revision, 'map_revision': map_revision, 'cursor': cursor})
        return self._request('GET', f"/api/tui/v1/sessions/{quote(session_id, safe='')}/background-shell/{quote(job_id, safe='')}?{params}")

    def session_capabilities(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/capabilities")

    def existing_chat_handoff(self, session_id: str, *, expected_map_revision: int | None = None) -> dict[str, Any]:
        path = f"/api/tui/v1/sessions/{quote(session_id, safe='')}/handoff"
        if expected_map_revision is not None:
            if isinstance(expected_map_revision, bool) or not isinstance(expected_map_revision, int) or expected_map_revision < 0:
                raise ValueError("map revision must be a nonnegative integer")
            path += f"?map_revision={expected_map_revision}"
        return self._request("GET", path)

    def active_turn(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/turns/active")

    def actors(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/actors")

    def stop_active_turn(self, session_id: str, *, run_id: str) -> dict[str, Any]:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("an observed run ID is required")
        return self._request("POST", f"/api/tui/v1/sessions/{quote(session_id, safe='')}/turns/active/stop",
                             extra_headers={"X-Agent-Run-ID": run_id})

    def stream_active_turn(self, session_id: str, *, run_id: str, after: int = 0):
        if not self.token:
            raise TuiClientError("this profile is not logged in", status_code=401)
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("an observed run ID is required")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("stream position must be a nonnegative integer")
        try:
            with self._http.stream("GET", f"api/tui/v1/sessions/{quote(session_id, safe='')}/turns/active/stream?after={after}",
                headers={"Accept":"text/event-stream", "Authorization":f"Bearer {self.token}", "X-Agent-Run-ID":run_id}) as response:
                if response.status_code >= 400:
                    response.read()
                    try: detail = response.json().get("detail")
                    except (ValueError, AttributeError): detail = None
                    raise TuiClientError(str(detail or f"stream failed ({response.status_code})"), status_code=response.status_code)
                if response.headers.get("X-Agent-Run-ID") != run_id:
                    raise TuiClientError("The selected run changed; refresh the active turn", status_code=409)
                event, sequence, data = 'message', None, []
                for line in response.iter_lines():
                    if not line:
                        if data:
                            raw = "\n".join(data)
                            if raw == '[DONE]': yield {'event':'done', 'id':sequence, 'data':None}
                            else:
                                try: value = json.loads(raw)
                                except ValueError: value = raw
                                yield {'event':event, 'id':sequence, 'data':value}
                        event, sequence, data = 'message', None, []
                    elif line.startswith('event:'): event = line[6:].lstrip()
                    elif line.startswith('id:'):
                        value = line[3:].strip()
                        sequence = int(value) if value.isdecimal() else None
                    elif line.startswith('data:'): data.append(line[5:].lstrip())
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
