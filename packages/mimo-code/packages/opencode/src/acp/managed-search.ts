import { createHash } from "node:crypto"
import { ManagedProvider } from "./managed-provider"
import {
  SEARCH_EXECUTOR_ID,
  type SearchArtifactResult,
  type SearchClaims,
  type SearchDispatchRequest,
  type SearchCallbackRequest,
  type SearchInput,
  type SearchResponseArtifact,
  validateSearchArtifactResult,
} from "./managed-search-contract"
import type { OpenClankManagedProtocol } from "./openclank-protocol"

const MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
const CHUNK_SIZE = 4 * 1024 * 1024

export interface ManagedSearchContext {
  readonly sessionID: string
  readonly actorID?: string
  readonly executionRevision?: number
  readonly callID?: string
  readonly abort: AbortSignal
}

function idempotencyKey(sessionID: string, rootOperationID: string, callID: string): string {
  return createHash("sha256").update(`${sessionID}\0${rootOperationID}\0${callID}`).digest("hex").slice(0, 64)
}

async function readArtifact(descriptor: OpenClankManagedProtocol.ArtifactDescriptor): Promise<Uint8Array> {
  if (!Number.isSafeInteger(descriptor.sizeBytes) || descriptor.sizeBytes <= 0 || descriptor.sizeBytes > MAX_ARTIFACT_BYTES) throw new Error("managed search artifact size is invalid")
  if (descriptor.mediaType !== "application/json" && !descriptor.mediaType.endsWith("+json")) throw new Error("managed search artifact is not JSON")
  const chunks: Uint8Array[] = []
  let offset = 0
  while (offset < descriptor.sizeBytes || (descriptor.sizeBytes === 0 && offset === 0)) {
    const result = await ManagedProvider.managedHostCall("_openclank/operations/v1/artifact/read", {
      artifactID: descriptor.artifactID, offset, limit: CHUNK_SIZE,
    })
    if (result.artifactID !== descriptor.artifactID || result.contentSHA256 !== descriptor.contentSHA256 || result.sizeBytes !== descriptor.sizeBytes || result.mediaType !== descriptor.mediaType || result.offset !== offset) throw new Error("managed search artifact descriptor mismatch")
    const data = Uint8Array.from(Buffer.from(result.dataBase64, "base64"))
    const chunkHash = createHash("sha256").update(data).digest("hex")
    if (chunkHash !== result.chunkSHA256 || data.length === 0 || data.length > CHUNK_SIZE || data.length > descriptor.sizeBytes - offset || (result.eof !== (offset + data.length === descriptor.sizeBytes))) throw new Error("managed search artifact chunk mismatch")
    chunks.push(data); offset += data.length
    if (data.length === 0 && offset < descriptor.sizeBytes) throw new Error("managed search artifact truncated")
  }
  const bytes = new Uint8Array(offset)
  let cursor = 0
  for (const chunk of chunks) { bytes.set(chunk, cursor); cursor += chunk.length }
  if (createHash("sha256").update(bytes).digest("hex") !== descriptor.contentSHA256) throw new Error("managed search artifact hash mismatch")
  return bytes
}

async function invoke(request: SearchCallbackRequest): Promise<SearchArtifactResult> {
  const descriptor = await ManagedProvider.managedHostCall("_openclank/operations/v1/executor/invoke", {
    executorID: SEARCH_EXECUTOR_ID,
    operation: "web.search",
    artifactIDs: [],
    options: request as unknown as Record<string, unknown>,
  })
  const bytes = await readArtifact(descriptor)
  return validateSearchArtifactResult(JSON.parse(new TextDecoder().decode(bytes)))
}

export async function managedSearch(
  context: ManagedSearchContext,
  input: Pick<SearchInput, "query" | "count" | "freshness">,
  timeoutMs = 30_000,
): Promise<SearchResponseArtifact> {
  if (!context.actorID || context.executionRevision === undefined) throw new Error("managed search requires actor execution identity")
  if (!context.callID) throw new Error("managed search requires a tool call ID")
  if (context.abort.aborted) throw new Error("managed search aborted")
  await ManagedProvider.ensureManagedSessionBinding(context.sessionID)
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 10 || timeoutMs > 120_000) throw new Error("managed search timeout is invalid")
  const scope = ManagedProvider.requireScope(context.sessionID, context.actorID, context.executionRevision)
  const rootOperationID = ManagedProvider.currentRootOperationID(context.sessionID, context.actorID, context.executionRevision)
  const claims: SearchClaims = {
    sessionID: context.sessionID,
    correlationID: context.callID,
    rootOperationID,
    idempotencyKey: idempotencyKey(context.sessionID, rootOperationID, context.callID),
  }
  void scope
  const dispatch: SearchDispatchRequest = {
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims,
    input: { query: input.query, ...(input.count === undefined ? {} : { count: input.count }), ...(input.freshness === undefined ? {} : { freshness: input.freshness }), intent: "web", extraction: "none", deadlineAt: Date.now() + timeoutMs },
  }
  let phase: "preparing" | "engine" | "terminal" = "preparing"
  let handoffID: string | undefined
  let currentArtifact: SearchArtifactResult | undefined
  let cancelSent = false
  let terminal = false
  const cancel = async () => {
    if (terminal || cancelSent || phase === "terminal") return
    cancelSent = true
    if (phase === "engine" && handoffID) {
      const artifact = currentArtifact
      if (artifact?.kind === "managed_request") await ManagedProvider.managedOperationDelegate().cancel({ rootOperationID: artifact.request.rootOperationID, idempotencyKey: artifact.request.idempotencyKey })
      return
    }
    await invoke({ executorID: SEARCH_EXECUTOR_ID, contractVersion: 1, action: "cancel", claims, ...(handoffID ? { handoffID } : {}), reason: "aborted" })
  }
  const onAbort = () => { void cancel().catch(() => undefined) }
  context.abort.addEventListener("abort", onAbort, { once: true })
  try {
    if (context.abort.aborted) { await cancel(); throw new Error("managed search aborted") }
    currentArtifact = await invoke(dispatch)
    if (currentArtifact.kind === "finalized") {
      phase = "terminal"
      if (!("response" in currentArtifact)) throw new Error("managed search finalized without a response")
      return currentArtifact.response
    }
    if (currentArtifact.kind === "external_result") { phase = "terminal"; return currentArtifact.response }
    if (currentArtifact.kind === "cancelled") throw new Error("managed search aborted")
    if (currentArtifact.kind !== "managed_request") throw new Error("managed search did not produce an executable request")
    const managedArtifact = currentArtifact
    handoffID = managedArtifact.handoffID
    if (context.abort.aborted) { await cancel(); throw new Error("managed search aborted") }
    phase = "engine"
    const result = await ManagedProvider.managedOperationDelegate().execute(managedArtifact.request)
    terminal = true
    phase = "terminal"
    const finalized = await invoke({ executorID: SEARCH_EXECUTOR_ID, contractVersion: 1, action: "finalize", claims, handoffID: managedArtifact.handoffID, terminal: result })
    if (finalized.kind !== "finalized" || !("response" in finalized)) throw new Error("managed search finalization was invalid")
    return finalized.response
  } finally {
    context.abort.removeEventListener("abort", onAbort)
  }
}
