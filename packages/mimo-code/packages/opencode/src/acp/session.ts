import { RequestError, type McpServer } from "@agentclientprotocol/sdk"
import type { ACPSessionState } from "./types"
import { Log } from "@/util"
import type { OpencodeClient } from "@mimo-ai/sdk/v2"
import path from "node:path"
import { SessionID } from "@/session/schema"
import { registerMemorySessionScope, unregisterMemorySessionScope } from "@/memory/session-scope"
import { resetManagedSessionAdmission } from "./managed-provider"

const log = Log.create({ service: "acp-session-manager" })

function isMissingSession(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false
  const value = error as Record<string, unknown>
  return value.status === 404 || (typeof value.response === "object" && value.response !== null && (value.response as Record<string, unknown>).status === 404)
}

/** Reserve a provisional engine `ses_…` without creating a session row.
 * The caller may later pass this ID to `create` so create-and-publish keeps one
 * identity across the host's projection-first candidate record. */
export function reserveProvisionalSessionID(): SessionID {
  return SessionID.descending()
}

export type DiscardReason = "create-failed" | "explicit"
export type DiscardAck = { deleted: true; sessionID: string; cwd: string; reason: DiscardReason }

/** Thrown when create-and-publish is interrupted after an engine session row
 * exists. `orphan` names that exact private candidate; callers delete only that
 * ID+cwd and never scan or guess other sessions. */
export class SessionCreateInterruption extends Error {
  readonly orphan: { sessionID: string; cwd: string }
  constructor(message: string, orphan: { sessionID: string; cwd: string }) {
    super(message)
    this.name = "SessionCreateInterruption"
    this.orphan = orphan
  }
}

export class ACPSessionManager {
  private sessions = new Map<string, ACPSessionState>()
  private sdk: OpencodeClient

  constructor(sdk: OpencodeClient) {
    this.sdk = sdk
  }

  private async disconnect(sessionId: string, cwd: string, servers: McpServer[]) {
    if (!this.sdk.mcp?.disconnect) return
    await Promise.all(
      servers.map((server) =>
        this.sdk.mcp
          .disconnect({ name: server.name, directory: cwd }, { throwOnError: true })
          .catch((error) => log.warn("mcp disconnect failed", { name: server.name, error })),
      ),
    )
  }

  /** Delete a failed private candidate; ordinary release remains non-destructive.
   * Returns an explicit deletion acknowledgement. Never claims success when the
   * SDK delete is unconfirmed. Callers pass the private session ID and canonical
   * cwd; this path never falls back to deleting chat mappings. */
  async discard(
    sessionId: string,
    cwd?: string,
    servers: McpServer[] = [],
    reason: DiscardReason = "explicit",
  ): Promise<DiscardAck> {
    const session = this.sessions.get(sessionId)
    const resolvedCwd = session?.cwd ?? cwd
    const resolvedServers = session?.mcpServers ?? servers
    if (!resolvedCwd) throw new Error("cannot discard an unregistered session without its directory")
    const canonicalCwd = path.resolve(resolvedCwd)
    try {
      await this.disconnect(sessionId, canonicalCwd, resolvedServers)
      try {
        const result = await this.sdk.session.delete({ sessionID: sessionId, directory: canonicalCwd }, { throwOnError: true })
        if (result.data !== true) throw new Error("discard session delete was not confirmed")
      } catch (error) {
        if (!isMissingSession(error)) throw error
      }
    } finally {
      unregisterMemorySessionScope(sessionId)
      this.sessions.delete(sessionId)
    }
    return { deleted: true, sessionID: sessionId, cwd: canonicalCwd, reason }
  }

  tryGet(sessionId: string): ACPSessionState | undefined {
    return this.sessions.get(sessionId)
  }

  /** Create-and-publish under one owner lifecycle lease. A provisional ID from
   * `reserveProvisionalSessionID` is accepted so the host can persist its
   * projection-first candidate before this call. Interruptions before the SDK
   * create returns delete nothing and name no orphan; interruptions after an
   * engine row exists name that exact private session ID + canonical cwd. */
  async create(
    cwd: string,
    mcpServers: McpServer[],
    model?: ACPSessionState["model"],
    options?: { provisionalID?: SessionID },
  ): Promise<ACPSessionState> {
    const canonicalCwd = path.resolve(cwd)
    let createdSessionID: string | undefined
    let session: { id: string; time?: { created?: number | string | Date } }
    try {
      session = await this.sdk.session
        .create(
          {
            directory: canonicalCwd,
            // Reserved identity is create-and-publish only; the public SDK
            // types have not been regenerated for `id` yet (S01/S09).
            ...(options?.provisionalID ? { id: options.provisionalID } : {}),
          } as { directory: string; id?: string },
          { throwOnError: true },
        )
        .then((x) => x.data!)
      createdSessionID = session.id
    } catch (error) {
      // Nothing was created: delete nothing and name no orphan. No DB scan.
      throw error
    }

    const sessionId = createdSessionID!
    const resolvedModel = model

    const state: ACPSessionState = {
      id: sessionId,
      cwd: canonicalCwd,
      mcpServers,
      createdAt: new Date(),
      model: resolvedModel,
    }
    log.info("creating_session", { state })

    // Register first: a throwing registration is create-failed and may
    // destructive-discard this exact private candidate.
    try {
      registerMemorySessionScope(sessionId, mcpServers, canonicalCwd)
    } catch (error) {
      try {
        await this.discard(sessionId, canonicalCwd, mcpServers, "create-failed")
      } catch (discardError) {
        // Deletion was not confirmed: name the exact orphan, never guess.
        throw new SessionCreateInterruption(
          `create-and-publish interrupted and discard failed for ${sessionId} at ${canonicalCwd}: ${String(discardError)}`,
          { sessionID: sessionId, cwd: canonicalCwd },
        )
      }
      throw error
    }
    this.sessions.set(sessionId, state)
    return state
  }

  /** Resume an existing durable session. A bad descriptor is
   * resume-bad-descriptor: local scope is cleared and the durable row is left
   * intact. This path never destructive-discards. */
  async load(
    sessionId: string,
    cwd: string,
    mcpServers: McpServer[],
    model?: ACPSessionState["model"],
  ): Promise<ACPSessionState> {
    const canonicalCwd = path.resolve(cwd)
    const session = await this.sdk.session
      .get(
        {
          sessionID: sessionId,
          directory: canonicalCwd,
        },
        { throwOnError: true },
      )
      .then((x) => x.data!)

    const resolvedModel = model

    const state: ACPSessionState = {
      id: sessionId,
      cwd: canonicalCwd,
      mcpServers,
      createdAt: new Date(session.time.created),
      model: resolvedModel,
    }
    log.info("loading_session", { state })

    // resume-bad-descriptor: clear local scope only; never delete durable history.
    try {
      registerMemorySessionScope(sessionId, mcpServers, canonicalCwd)
    } catch (error) {
      unregisterMemorySessionScope(sessionId)
      resetManagedSessionAdmission(sessionId)
      throw error
    }
    this.sessions.set(sessionId, state)
    return state
  }

  get(sessionId: string): ACPSessionState {
    const session = this.sessions.get(sessionId)
    if (!session) {
      log.error("session not found", { sessionId })
      throw new RequestError(-32602, "Open Clank managed session is missing", {
        code: "OPENCLANK_SESSION_MISSING",
        sessionId,
      })
    }
    return session
  }

  async release(sessionId: string) {
    const session = this.sessions.get(sessionId)
    if (!session) {
      unregisterMemorySessionScope(sessionId)
      resetManagedSessionAdmission(sessionId)
      return
    }
    await this.disconnect(sessionId, session.cwd, session.mcpServers)
    unregisterMemorySessionScope(sessionId)
    resetManagedSessionAdmission(sessionId)
    this.sessions.delete(sessionId)
  }

  getModel(sessionId: string) {
    const session = this.get(sessionId)
    return session.model
  }

  setModel(sessionId: string, model: ACPSessionState["model"]) {
    const session = this.get(sessionId)
    session.model = model
    this.sessions.set(sessionId, session)
    return session
  }

  getVariant(sessionId: string) {
    const session = this.get(sessionId)
    return session.variant
  }

  setVariant(sessionId: string, variant?: string) {
    const session = this.get(sessionId)
    session.variant = variant
    this.sessions.set(sessionId, session)
    return session
  }

  setMode(sessionId: string, modeId: string) {
    const session = this.get(sessionId)
    session.modeId = modeId
    this.sessions.set(sessionId, session)
    return session
  }

  /** Update one ACP chat's physical working directory without changing the
   * session identity. The next prompt and every callback resolve through this
   * state; no process-wide chdir is ever performed. */
  setCwd(sessionId: string, cwd: string) {
    const session = this.get(sessionId)
    session.cwd = cwd
    this.sessions.set(sessionId, session)
    return session
  }
}
