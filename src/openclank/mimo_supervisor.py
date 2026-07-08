"""Supervisor for the mimo ACP child process.

Handles spawn, ACP handshake, crash detection, bounded restart backoff,
and session reconciliation via session/resume.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from src.openclank.acp_client import ACPClient, TransportError
from src.openclank.acp_bridge import ACPBridge, PermissionHandler, register_client_callbacks

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIMO_BIN = REPO_ROOT / "bin" / "mimo"

# Odysseus skill root — where SkillsManager stores SKILL.md files.
# mimo's discoverSkills scans cfg.skills.paths for **/SKILL.md.
_ODYSSEUS_ROOT = REPO_ROOT
_ODYSSEUS_SKILLS_DIR = os.getenv(
    "ODYSSEUS_DATA_DIR",
    str(_ODYSSEUS_ROOT / "data"),
) + "/skills"

# Restart backoff
_RESTART_DELAY_INITIAL = 1.0
_RESTART_DELAY_MAX = 5.0
_RESTART_DELAY_MULTIPLIER = 2.0
_MAX_RESTART_ATTEMPTS = 10
_RESTART_WINDOW = 60.0  # reset attempt counter after this many seconds of stability


class MimoSupervisor:
    """Spawns and supervises a single `mimo acp` child over stdio.

    Owns the ACPClient and ACPBridge. Detects crashes, restarts with
    bounded backoff, and reconciles in-flight sessions via session/resume.
    """

    def __init__(
        self,
        owner: str = "",
        permission_handler=None,
        memory_provider=None,
        safe_dirs: list[str] | None = None,
    ) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._client: ACPClient | None = None
        self._bridge: ACPBridge | None = None
        self._health_task: asyncio.Task | None = None
        self._restart_count = 0
        self._last_restart_time = 0.0
        self._stopping = False
        self._owner = owner
        self._permission_handler = permission_handler
        self._memory_provider = memory_provider
        self._safe_dirs = safe_dirs

    async def start(self) -> None:
        """Spawn the child, perform ACP handshake, set up bridge."""
        await self._spawn_and_init()

    async def _spawn_and_init(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            logger.warning("mimo child already running (pid %d)", self._proc.pid)
            return

        logger.info("starting mimo acp child: %s acp", MIMO_BIN)

        # A1.1: inject odysseus skills path into mimo config via env.
        # MIMOCODE_CONFIG_CONTENT is loaded last in mimo's config chain
        # (config.ts L835) and merges on top of everything else.
        # Phase 5: os.environ.copy() inherits all env vars including
        # FM_DB_PATH — this is how the mimo child process converges on
        # the same frankenmemory db as the thesius parent. All spawners
        # (thesius-provider, mimo-capture, bridged-tool) share one db
        # because they all fork from this inherited env.
        env = os.environ.copy()
        # Server directory-containment check (middleware.ts:24-29): when no
        # server password is set, the server requires requested directories to
        # be within its CWD. Change the child's CWD to /home/e so all user
        # workspaces (~/sauce, ~/entities, ~/open-clank) are reachable.
        # Also set MIMOCODE_HOME if THESIUS_AGENT_HOME is configured (Phase 4).
        if os.path.isdir(_ODYSSEUS_SKILLS_DIR):
            skills_config = json.dumps({
                "skills": {"paths": [_ODYSSEUS_SKILLS_DIR]},
                "memory": {"provider": "frankenmemory"},
            })
            env["MIMOCODE_CONFIG_CONTENT"] = skills_config
            # A1.3: also expose the data dir so mimo's usage writer can find _usage.json
            env["ODYSSEUS_DATA_DIR"] = str(Path(_ODYSSEUS_SKILLS_DIR).parent)
            logger.info("injected odysseus skills path: %s", _ODYSSEUS_SKILLS_DIR)
        else:
            logger.warning("odysseus skills dir not found: %s", _ODYSSEUS_SKILLS_DIR)

        # Phase 4: if THESIUS_AGENT_HOME is set, pass MIMOCODE_HOME to the child
        # so mimo stores its data under the agent home instead of the XDG default.
        _agent_home = os.environ.get("THESIUS_AGENT_HOME")
        if _agent_home and "MIMOCODE_HOME" not in env:
            env["MIMOCODE_HOME"] = os.path.join(_agent_home, ".mimocode")

        try:
            self._proc = await asyncio.create_subprocess_exec(
                str(MIMO_BIN),
                "acp",
                "--hostname",
                "127.0.0.1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd="/home/e",
            )
        except NotImplementedError:
            logger.warning("create_subprocess_exec unavailable, falling back to sync spawn")
            import subprocess
            proc = subprocess.Popen(
                [str(MIMO_BIN), "acp", "--hostname", "127.0.0.1"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd="/home/e",
            )
            # Wrap the sync Popen into an async-compatible object
            self._proc = await asyncio.create_subprocess_exec(
                str(MIMO_BIN),
                "acp",
                "--hostname",
                "127.0.0.1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd="/home/e",
            )

        logger.info("mimo child started (pid %d)", self._proc.pid)
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Create ACP client on the child's stdio
        assert self._proc.stdin and self._proc.stdout
        self._client = ACPClient(self._proc.stdout, self._proc.stdin)

        # Build permission handler: caller-supplied handler takes precedence;
        # if safe_dirs are configured and no explicit handler was given,
        # create a PermissionHandler that auto-approves paths under safe dirs.
        perm_handler = self._permission_handler
        if perm_handler is None and self._safe_dirs:
            perm_handler = PermissionHandler(safe_dirs=self._safe_dirs)
            logger.info("auto-approve permission handler created with safe dirs: %s", self._safe_dirs)

        register_client_callbacks(self._client, permission_handler=perm_handler)
        await self._client.start_reader()

        # Perform ACP handshake
        try:
            result = await self._client.initialize()
            logger.info("ACP handshake complete: %s", result.get("agentInfo", {}))
        except (TransportError, Exception) as e:
            logger.error("ACP handshake failed: %s", e)
            await self._teardown_child()
            raise

        # Create the bridge (B2: owner flows through for lifetools MCP context)
        cwd = os.environ.get("OPENTHESIUS_GLOBAL_CWD", str(Path.home() / "open-clank"))
        self._bridge = ACPBridge(
            self._client,
            cwd,
            owner=self._owner,
            permission_handler=perm_handler,
            memory_provider=self._memory_provider,
        )

        # Start health monitor
        self._health_task = asyncio.create_task(self._health_monitor())

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            async for line in self._proc.stderr:
                logger.warning("mimo stderr: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            pass

    async def _health_monitor(self) -> None:
        """Detect child crash (EOF or proc exit) and trigger restart."""
        try:
            while not self._stopping:
                await asyncio.sleep(1.0)
                if self._proc is None:
                    break
                if self._proc.returncode is not None:
                    logger.warning("mimo child exited (code %d)", self._proc.returncode)
                    await self._handle_crash()
                    return
                if self._client and self._client.is_closed:
                    logger.warning("ACP client closed (child likely crashed)")
                    await self._handle_crash()
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_crash(self) -> None:
        """Handle child crash: fail in-flight requests, restart with backoff, reconcile."""
        if self._stopping:
            return

        # Fail all pending requests on the old client
        if self._client:
            await self._client.close()

        await self._teardown_child()

        # Restart with bounded backoff
        await self._restart_with_backoff()

        if not self._client or not self._bridge:
            logger.error("mimo restart failed — no client available")
            return

        # Reconcile in-flight sessions — DB-driven: query thesius DB for
        # recent ses_-prefixed sessions and resume each one.
        await self._reconcile_sessions()

    async def _restart_with_backoff(self) -> None:
        """Restart the child with bounded exponential backoff."""
        import time
        now = time.time()

        # Reset counter if we've been stable long enough
        if now - self._last_restart_time > _RESTART_WINDOW:
            self._restart_count = 0

        delay = _RESTART_DELAY_INITIAL
        for attempt in range(_MAX_RESTART_ATTEMPTS):
            self._restart_count += 1
            self._last_restart_time = time.time()
            logger.info("mimo restart attempt %d/%d (delay %.1fs)", attempt + 1, _MAX_RESTART_ATTEMPTS, delay)
            await asyncio.sleep(delay)

            try:
                await self._spawn_and_init()
                logger.info("mimo restarted successfully")
                return
            except Exception as e:
                logger.error("mimo restart attempt %d failed: %s", attempt + 1, e)
                delay = min(delay * _RESTART_DELAY_MULTIPLIER, _RESTART_DELAY_MAX)

        logger.error("mimo restart exhausted %d attempts — giving up", _MAX_RESTART_ATTEMPTS)

    async def _reconcile_sessions(self) -> None:
        """Re-establish mimo sessions after a crash/restart.

        Queries the thesius DB for sessions whose id starts with 'ses_' and
        were recently updated, then calls resume_session for each. The time
        cutoff avoids re-resuming thousands of old sessions.
        """
        assert self._bridge
        try:
            from core.database import Session as DbSession, SessionLocal
            from datetime import datetime, timezone, timedelta
        except ImportError:
            logger.error("cannot import database module for session reconciliation")
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        db = None
        try:
            db = SessionLocal()
            rows = (
                db.query(DbSession.id)
                .filter(DbSession.id.like("ses_%"))
                .filter(DbSession.updated_at > cutoff)
                .all()
            )
        except Exception as e:
            logger.error("DB query for session reconciliation failed: %s", e)
            return
        finally:
            if db:
                db.close()

        for (session_id,) in rows:
            try:
                await self._bridge.resume_session(session_id, session_id)
                logger.info("reconciled session %s via resume", session_id)
            except Exception as e:
                logger.error("failed to reconcile session %s: %s", session_id, e)

    async def _teardown_child(self) -> None:
        """Clean up the child process and associated tasks."""
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._proc:
            proc = self._proc
            self._proc = None
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

        self._client = None
        self._bridge = None

    async def stop(self) -> None:
        """Graceful shutdown — stop health monitor, close client, kill child."""
        self._stopping = True

        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()

        if self._proc is None:
            return

        proc = self._proc
        self._proc = None

        if proc.returncode is not None:
            logger.info("mimo child already exited (code %d)", proc.returncode)
            return

        logger.info("stopping mimo child (pid %d)", proc.pid)

        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            logger.info("mimo child exited gracefully (code %d)", proc.returncode)
        except asyncio.TimeoutError:
            logger.warning("mimo child did not exit in time, killing")
            proc.kill()
            await proc.wait()

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def client(self) -> ACPClient | None:
        return self._client

    @property
    def bridge(self) -> ACPBridge | None:
        return self._bridge
