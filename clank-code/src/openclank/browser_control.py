"""Owner-bound target leases for Playwright MCP browser sessions.

This module intentionally does not start a browser or register tools globally.
The host wiring must admit a connection only through this registry and route
calls through ``dispatch_once``; generic MCP catalog exposure is a separate
integration responsibility.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit


class BrowserTargetError(RuntimeError):
    """Base class for an invalid, stale, or unauthorized Browser target."""


class BrowserTargetNotFound(BrowserTargetError):
    pass


class BrowserTargetScopeError(BrowserTargetError):
    pass


class BrowserTargetStale(BrowserTargetError):
    pass


@dataclass(frozen=True)
class BrowserTarget:
    """Internal target record; connection_id is never included in public views."""

    target_id: str
    owner_id: str
    host_session_id: str
    workspace_id: str
    binding_revision: int
    connection_id: str
    connection_generation: str
    tab_id: str
    title: str
    origin: str
    current_url: str
    navigation_generation: int
    observation_generation: int
    created_at: float
    last_observed_at: float

    def public_view(self) -> dict[str, Any]:
        return {
            "targetId": self.target_id,
            "workspaceId": self.workspace_id,
            "connectionGeneration": self.connection_generation,
            "tabId": self.tab_id,
            "title": self.title,
            "origin": self.origin,
            "navigationGeneration": self.navigation_generation,
            "observationGeneration": self.observation_generation,
            "observedAt": self.last_observed_at,
        }


class BrowserTargetRegistry:
    """In-memory owner/workspace lease registry for explicitly selected tabs."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._targets: dict[str, BrowserTarget] = {}
        self._connection_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _required(value: str, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        return normalized

    @staticmethod
    def _canonical_url(url: str) -> str:
        value = str(url or "").strip()
        if value == "about:blank":
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("target URL must be HTTP(S) without embedded credentials")
        return value

    @staticmethod
    def _origin(url: str) -> str:
        if str(url or "").strip() == "about:blank":
            return "about:blank"
        parsed = urlsplit(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("target URL must have an HTTP(S) origin")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    def register_selected_tab(
        self,
        *,
        owner_id: str,
        host_session_id: str,
        workspace_id: str,
        binding_revision: int,
        connection_id: str,
        connection_generation: str,
        tab_id: str,
        title: str,
        url: str,
    ) -> dict[str, Any]:
        """Create a lease only after the caller has explicitly selected a tab."""
        owner = self._required(owner_id, "owner_id")
        host_session = self._required(host_session_id, "host_session_id")
        workspace = self._required(workspace_id, "workspace_id")
        if type(binding_revision) is not int or binding_revision < 0:
            raise ValueError("binding_revision must be a nonnegative integer")
        connection = self._required(connection_id, "connection_id")
        generation = self._required(connection_generation, "connection_generation")
        selected_tab = self._required(tab_id, "tab_id")
        now = self._clock()
        target_id = secrets.token_urlsafe(32)
        current_url = self._canonical_url(url)
        target = BrowserTarget(
            target_id=target_id,
            owner_id=owner,
            host_session_id=host_session,
            workspace_id=workspace,
            binding_revision=binding_revision,
            connection_id=connection,
            connection_generation=generation,
            tab_id=selected_tab,
            title=str(title or "").strip(),
            origin=self._origin(url),
            current_url=current_url,
            navigation_generation=0,
            observation_generation=0,
            created_at=now,
            last_observed_at=now,
        )
        with self._lock:
            self._targets[target_id] = target
        return target.public_view()

    def resolve(
        self,
        *,
        target_id: str,
        owner_id: str,
        host_session_id: str,
        workspace_id: str,
        binding_revision: int,
        connection_generation: str,
        navigation_generation: int,
        observation_generation: Optional[int] = None,
    ) -> BrowserTarget:
        """Resolve a target only for its exact owner, scope and observed revision."""
        with self._lock:
            target = self._targets.get(str(target_id or ""))
            if target is None:
                raise BrowserTargetNotFound("Browser target is no longer active")
            if (target.owner_id != str(owner_id or "").strip()
                    or target.host_session_id != str(host_session_id or "").strip()
                    or target.workspace_id != str(workspace_id or "").strip()
                    or target.binding_revision != binding_revision):
                raise BrowserTargetScopeError("Browser target belongs to another owner or binding")
            if target.connection_generation != str(connection_generation or ""):
                raise BrowserTargetStale("Browser connection generation changed")
            if target.navigation_generation != navigation_generation:
                raise BrowserTargetStale("Browser navigation generation changed")
            if observation_generation is not None and target.observation_generation != observation_generation:
                raise BrowserTargetStale("Browser observation is stale")
            return target

    def record_observation(
        self,
        *,
        target_id: str,
        owner_id: str,
        host_session_id: str,
        workspace_id: str,
        binding_revision: int,
        connection_generation: str,
        navigation_generation: int,
        observed_tab_id: str,
        title: str,
        url: str,
    ) -> dict[str, Any]:
        """Refresh metadata without silently retargeting if the selected tab changed."""
        with self._lock:
            current = self.resolve(
                target_id=target_id,
                owner_id=owner_id,
                host_session_id=host_session_id,
                workspace_id=workspace_id,
                binding_revision=binding_revision,
                connection_generation=connection_generation,
                navigation_generation=navigation_generation,
            )
            if str(observed_tab_id or "") != current.tab_id:
                self._targets.pop(current.target_id, None)
                raise BrowserTargetStale("Selected browser tab changed; select a tab again")
            now = self._clock()
            current_url = self._canonical_url(url)
            origin = self._origin(current_url)
            changed = current_url != current.current_url
            updated = replace(
                current,
                title=str(title or "").strip(),
                origin=origin,
                current_url=current_url,
                navigation_generation=current.navigation_generation + int(changed),
                observation_generation=current.observation_generation + 1,
                last_observed_at=now,
            )
            self._targets[current.target_id] = updated
            return updated.public_view()

    async def dispatch_once(
        self,
        *,
        target_id: str,
        owner_id: str,
        host_session_id: str,
        workspace_id: str,
        binding_revision: int,
        connection_generation: str,
        navigation_generation: int,
        observation_generation: int,
        tool_name: str,
        arguments: Dict[str, Any],
        verify_selected_tab: Callable[[BrowserTarget], Any | Awaitable[Any]],
        call: Callable[[str, str, Dict[str, Any]], Any | Awaitable[Any]],
        refresh_selected_tab: Callable[[BrowserTarget], Any | Awaitable[Any]],
    ) -> dict[str, Any]:
        """Serialize, verify, and dispatch one action without replay.

        The callbacks must observe the selected tab through the transport before
        and after the action. The first observation must match the recorded tab
        index and exact current URL. The post-action observation may have a new
        URL (navigation) but must still identify the same current tab index.
        The transport exposes indices rather than durable tab IDs, so an index
        reuse detected before dispatch invalidates the lease. No retry occurs
        after exceptions/timeouts; observation generation is advanced first so
        old snapshot refs cannot be reused.
        """
        with self._lock:
            target = self.resolve(
                target_id=target_id,
                owner_id=owner_id,
                host_session_id=host_session_id,
                workspace_id=workspace_id,
                binding_revision=binding_revision,
                connection_generation=connection_generation,
                navigation_generation=navigation_generation,
                observation_generation=observation_generation,
            )
            connection_lock = self._connection_locks.setdefault(
                target.connection_id, asyncio.Lock()
            )

        async with connection_lock:
            with self._lock:
                target = self.resolve(
                    target_id=target_id,
                    owner_id=owner_id,
                    host_session_id=host_session_id,
                    workspace_id=workspace_id,
                    binding_revision=binding_revision,
                    connection_generation=connection_generation,
                    navigation_generation=navigation_generation,
                    observation_generation=observation_generation,
                )
            before = verify_selected_tab(target)
            if inspect.isawaitable(before):
                before = await before
            try:
                self._check_observation(target, before, require_same_url=True)
            except (BrowserTargetStale, ValueError) as exc:
                with self._lock:
                    self._targets.pop(target.target_id, None)
                if isinstance(exc, BrowserTargetStale):
                    raise
                raise BrowserTargetStale("Selected tab identity could not be verified") from exc

            with self._lock:
                # A concurrent release/scope retirement during tab validation
                # must win before any external action is dispatched.
                current = self.resolve(
                    target_id=target_id,
                    owner_id=owner_id,
                    host_session_id=host_session_id,
                    workspace_id=workspace_id,
                    binding_revision=binding_revision,
                    connection_generation=connection_generation,
                    navigation_generation=navigation_generation,
                    observation_generation=observation_generation,
                )
                started = replace(
                    current,
                    observation_generation=current.observation_generation + 1,
                    last_observed_at=self._clock(),
                )
                self._targets[target.target_id] = started

            result = call(target.connection_id, str(tool_name), dict(arguments or {}))
            if inspect.isawaitable(result):
                result = await result

            after = refresh_selected_tab(started)
            if inspect.isawaitable(after):
                after = await after
            try:
                self._check_observation(started, after, require_same_url=False)
            except (BrowserTargetStale, ValueError) as exc:
                with self._lock:
                    self._targets.pop(target.target_id, None)
                if isinstance(exc, BrowserTargetStale):
                    raise BrowserTargetStale(
                        "Browser action completed but selected tab could not be verified; outcome must not be replayed"
                    ) from exc
                raise BrowserTargetStale(
                    "Browser action completed but selected tab could not be verified; outcome must not be replayed"
                ) from exc

            with self._lock:
                current = self._targets.get(target.target_id)
                if current is None or current.observation_generation != started.observation_generation:
                    raise BrowserTargetStale(
                        "Browser target retired during action; outcome may be unknown and must not be replayed"
                    )
                updated_url = self._canonical_url(str(after.get("url") or ""))
                updated = replace(
                    current,
                    title=str(after.get("title") or "").strip(),
                    origin=self._origin(updated_url),
                    current_url=updated_url,
                    navigation_generation=current.navigation_generation + int(updated_url != current.current_url),
                    last_observed_at=self._clock(),
                )
                self._targets[target.target_id] = updated
            return {
                "targetId": updated.target_id,
                "connectionGeneration": updated.connection_generation,
                "navigationGeneration": updated.navigation_generation,
                "observationGeneration": updated.observation_generation,
                "origin": updated.origin,
                "title": updated.title,
                "result": result,
            }

    @staticmethod
    def _check_observation(target: BrowserTarget, observation: Any, *, require_same_url: bool) -> None:
        if not isinstance(observation, dict) or observation.get("current") is not True:
            raise BrowserTargetStale("Selected browser tab is not current")
        if str(observation.get("tab_id") or "") != target.tab_id:
            raise BrowserTargetStale("Selected browser tab changed; select a tab again")
        observed_url = BrowserTargetRegistry._canonical_url(str(observation.get("url") or ""))
        if require_same_url and observed_url != target.current_url:
            raise BrowserTargetStale("Selected tab URL changed; refresh the Browser target")

    def release(self, *, target_id: str, owner_id: str, host_session_id: str,
                workspace_id: str, binding_revision: int) -> bool:
        """Retire one target lease without closing the browser or its selected tab."""
        with self._lock:
            target = self._targets.get(str(target_id or ""))
            if target is None:
                return False
            if (target.owner_id != str(owner_id or "").strip()
                    or target.host_session_id != str(host_session_id or "").strip()
                    or target.workspace_id != str(workspace_id or "").strip()
                    or target.binding_revision != binding_revision):
                raise BrowserTargetScopeError("Browser target belongs to another owner or binding")
            del self._targets[target.target_id]
            return True

    def release_connection(self, *, connection_id: str, connection_generation: str) -> int:
        """Retire leases for a disconnected MCP client generation."""
        with self._lock:
            stale = [
                target_id
                for target_id, target in self._targets.items()
                if target.connection_id == connection_id
                and target.connection_generation == str(connection_generation)
            ]
            for target_id in stale:
                del self._targets[target_id]
            return len(stale)
