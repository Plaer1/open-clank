import { RequestError, type McpServer } from "@agentclientprotocol/sdk"
import type { ACPSessionState } from "./types"
import { Log } from "@/util"
import type { OpencodeClient } from "@mimo-ai/sdk/v2"
import { registerMemorySessionScope, unregisterMemorySessionScope } from "@/memory/session-scope"
import { resetManagedSessionAdmission } from "./managed-provider"

const log = Log.create({ service: "acp-session-manager" })

function isMissingSession(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false
  const value = error as Record<string, unknown>
  return value.status === 404 || (typeof value.response === "object" && value.response !== null && (value.response as Record<string, unknown>).status === 404)
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

  /** Delete a failed private candidate; ordinary release remains non-destructive. */
  async discard(sessionId: string, cwd?: string, servers: McpServer[] = []) {
    const session = this.sessions.get(sessionId)
    const resolvedCwd = session?.cwd ?? cwd
    const resolvedServers = session?.mcpServers ?? servers
    if (!resolvedCwd) throw new Error("cannot discard an unregistered session without its directory")
    try {
      await this.disconnect(sessionId, resolvedCwd, resolvedServers)
      try {
        const result = await this.sdk.session.delete({ sessionID: sessionId, directory: resolvedCwd }, { throwOnError: true })
        if (result.data !== true) throw new Error("discard session delete was not confirmed")
      } catch (error) {
        if (!isMissingSession(error)) throw error
      }
    } finally {
      unregisterMemorySessionScope(sessionId)
      this.sessions.delete(sessionId)
    }
  }

  tryGet(sessionId: string): ACPSessionState | undefined {
    return this.sessions.get(sessionId)
  }

  async create(cwd: string, mcpServers: McpServer[], model?: ACPSessionState["model"]): Promise<ACPSessionState> {
    const session = await this.sdk.session
      .create(
        {
          directory: cwd,
        },
        { throwOnError: true },
      )
      .then((x) => x.data!)

    const sessionId = session.id
    const resolvedModel = model

    const state: ACPSessionState = {
      id: sessionId,
      cwd,
      mcpServers,
      createdAt: new Date(),
      model: resolvedModel,
    }
    log.info("creating_session", { state })

    // Register first: a throwing registration must not leave a stale entry.
    try {
      registerMemorySessionScope(sessionId, mcpServers, cwd)
    } catch (error) {
      await this.discard(sessionId, cwd, mcpServers)
      throw error
    }
    this.sessions.set(sessionId, state)
    return state
  }

  async load(
    sessionId: string,
    cwd: string,
    mcpServers: McpServer[],
    model?: ACPSessionState["model"],
  ): Promise<ACPSessionState> {
    const session = await this.sdk.session
      .get(
        {
          sessionID: sessionId,
          directory: cwd,
        },
        { throwOnError: true },
      )
      .then((x) => x.data!)

    const resolvedModel = model

    const state: ACPSessionState = {
      id: sessionId,
      cwd,
      mcpServers,
      createdAt: new Date(session.time.created),
      model: resolvedModel,
    }
    log.info("loading_session", { state })

    // Register first: a throwing registration must not leave a stale entry.
    try {
      registerMemorySessionScope(sessionId, mcpServers, cwd)
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
