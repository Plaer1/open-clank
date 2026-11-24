import { Effect, Deferred, Exit } from "effect"
import { encodeForkContext } from "@/actor/fork-context"
import type { ActorRegistry } from "@/actor/registry"
import { Session } from "@/session"
import { Provider } from "@/provider"
import { prefixCaptureRef } from "@/session/prefix-capture-ref"
import type { ForkContext, Interface as ActorInterface } from "@/actor/spawn"
import type { SessionID, MessageID } from "../session/schema"
import type { ProviderID, ModelID } from "../provider/schema"

// Wraps the human/agent question in a side-boundary system-reminder:
// one-shot, READ-ONLY, answer-to-caller.
// The hard read-only guarantee comes from the tool whitelist at spawn (only
// read/grep/glob); this prompt reinforces it and forbids continuing the task.
function SIDE_QUESTION_PROMPT(question: string): string {
  return [
    "<system-reminder>",
    "This is a SIDE QUESTION about the session above (a frozen snapshot of its history).",
    "Answer it in a single response from that frozen context.",
    "You MAY use read-only tools (read/grep/glob) to inspect files, but you MUST NOT",
    "modify any file, run any command, or change any state. Do NOT continue, resume, or",
    "execute the session's underlying task — just answer the question, then stop.",
    "</system-reminder>",
    "",
    question,
  ].join("\n")
}

// One-shot, READ-ONLY fork-query: ask a (possibly running) target session a
// side question over a FROZEN snapshot of its history without disturbing its
// turn, and return the answer text. Mechanism mirrors tryStartCheckpointWriter
// (checkpoint.ts): capture the target's prefix at its watermark into a frozen
// ForkContext, spawn an ephemeral subagent over it with read-only tools,
// BLOCK on the outcome, return finalText. Non-interrupting: the fork runs in
// its own child session/actor over a frozen prefix; the target's own messages
// and actor are untouched.
export function forkQuery(deps: {
  sessions: Session.Interface
  actorRegistry: ActorRegistry.Interface
  provider: Provider.Interface
  actor: ActorInterface
}, targetSessionID: SessionID, question: string, selectedModel?: { providerID: ProviderID; modelID: ModelID }) {
  return Effect.gen(function* () {
    // a. Resolve the target's persisted history and the slice to snapshot.
    // A peer actor runs with actorID === its own sessionID, so SessionPrompt
    // persists its turns under agent_id = <targetSessionID> — NOT "main".
    // Read ALL slices, then pick the slice that actually holds the target's
    // conversation: "main" for a main session, else the peer's own-session slice.
    const all = yield* deps.sessions.messages({ sessionID: targetSessionID, agentID: "*" })
    const sliceOf = (agentID: string) =>
      all.filter((m) => (m.info.agentID ?? "main") === agentID)
    const mainSlice = sliceOf("main")
    // Prefer "main" when it carries real activity; otherwise fall back to the
    // peer child's own-session slice (agent_id === targetSessionID).
    const selectedMainSlice = mainSlice.some((m) => m.info.role === "user")
    const sourceActorID = selectedMainSlice ? "main" : targetSessionID
    const msgs = selectedMainSlice ? mainSlice : sliceOf(targetSessionID)
    const watermark = msgs.at(-1)?.info.id
    // Graceful: a target whose selected slice has no history (or no user
    // message) can't be snapshotted — buildPrefix needs a user message and
    // there is nothing to ask about. Answer directly instead of spawning.
    const hasUserMessage = msgs.some((m) => m.info.role === "user")
    if (!watermark || msgs.length === 0 || !hasUserMessage)
      return `(session ${targetSessionID} has no activity yet — nothing to ask about.)`

    // b. Resolve the agent from the exact selected transcript slice. A
    // synthetic default here would capture a different prompt/permission set.
    const lastAssistant = msgs.findLast((m) => m.info.role === "assistant")
    const lastUser = msgs.findLast((m) => m.info.role === "user")
    const agentName = (lastAssistant?.info as { agent?: string } | undefined)?.agent ??
      (lastUser?.info as { agent?: string } | undefined)?.agent
    if (!agentName) return "(fork-query unavailable: selected transcript actor identity is unavailable)"

    // Preserve the source actor's durable effective model. A caller may restate
    // it, but cannot switch the model for a captured full-context prefix or fall
    // back to a project default.
    const sourceActor = yield* deps.actorRegistry.get(targetSessionID, sourceActorID)
    if (!sourceActor)
      return "(fork-query unavailable: source actor membership is unavailable)"
    if (sourceActorID !== "main" && sourceActor.agent !== agentName)
      return "(fork-query unavailable: transcript slice does not match its registered actor agent)"
    let model = sourceActor.effectiveModel
    if (selectedModel) {
      const resolved = yield* Effect.exit(deps.provider.getModel(selectedModel.providerID, selectedModel.modelID))
      if (!Exit.isSuccess(resolved)) return "(fork-query unavailable: selected worker model is not authorized or available)"
      model = { providerID: resolved.value.providerID, modelID: resolved.value.id }
    }
    if (!model) return "(fork-query unavailable: source actor effective model is unavailable)"
    const providerID = model.providerID as ProviderID
    const modelID = model.modelID as ModelID

    // c. Build the frozen ForkContext via the late-bound prefix captor. If the
    // ref is unset (SessionPrompt.layer not running) we can't snapshot — degrade
    // gracefully rather than spawn a fork that would fail its runLoop.
    const buildPrefix = prefixCaptureRef.current
    if (!buildPrefix) return "(fork-query unavailable: prefix capture not initialized)"
    const prefix = yield* buildPrefix({
      sessionID: targetSessionID,
      agentName,
      providerID,
      modelID,
      msgs,
    })
    const forkCtx = {
      system: prefix.system,
      tools: prefix.tools,
      inheritedMessages: prefix.inheritedMessages,
      parentPermission: prefix.parentPermission,
      watermarkMsgID: watermark as MessageID,
      model: { providerID, modelID },
    } satisfies ForkContext
    // Validate and bound the immutable snapshot before creating a child
    // session, so capture/codec failures cannot leave partial actors.
    const encoded = yield* Effect.tryPromise({
      try: () => encodeForkContext(forkCtx, { sessionID: targetSessionID, actorID: sourceActorID }),
      catch: (error) => error instanceof Error ? error : new Error(String(error)),
    }).pipe(Effect.orElseSucceed(() => undefined))
    if (!encoded) return "(fork-query unavailable: captured prefix is invalid or exceeds its size limit)"

    // d. Ephemeral child session under the target hosts the query actor (like
    // checkpoint-writer). Parented to the target keeps it discoverable/cleanable.
    const childSession = yield* deps.sessions.create({
      parentID: targetSessionID,
      title: `ask: ${question.slice(0, 40)}`,
    })

    // e. Spawn BLOCKING + READ-ONLY. The tools whitelist (read/grep/glob) is the
    // HARD read-only guarantee: prompt.ts rejects any tool not in this list, so
    // write/edit/bash/patch are unavailable to the fork. background:false so we
    // await the answer; lifecycle:"ephemeral" so the host session is disposable.
    const result = yield* deps.actor.spawn({
      mode: "subagent",
      sessionID: childSession.id,
      parentSessionID: targetSessionID,
      parentActorID: sourceActorID,
      forkContextSource: { sessionID: targetSessionID, actorID: sourceActorID },
      agentType: agentName,
      description: "fork-query",
      task: SIDE_QUESTION_PROMPT(question),
      context: "full",
      tools: ["read", "grep", "glob"],
      model: { providerID, modelID },
      background: false,
      lifecycle: "ephemeral",
      forkContext: forkCtx,
    })
    const outcome = yield* Deferred.await(result.outcome)
    if (outcome.status === "success") return outcome.finalText ?? "(no answer)"
    const reason = outcome.status === "failure" ? outcome.error : outcome.status
    return `(fork-query failed: ${reason})`
  })
}

