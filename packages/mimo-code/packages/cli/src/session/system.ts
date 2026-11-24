import { Log } from "@/util"
import { Context, Effect, Layer } from "effect"
import path from "node:path"
import { createHash } from "node:crypto"
import { sql } from "drizzle-orm"
import { Database } from "../storage"
import { managedSessionBinding, markerMatches } from "../memory/session-scope"
import type { ActorExecutionIdentity } from "../actor/turn"
import { isAdmittedToolTimingTurn } from "./tool-timing"
import type { AgentProvenanceHandle, EnvironmentProvenance, EnvironmentProvenanceHandle } from "./closed-source-provenance"

import { Instance } from "../project/instance"
import { InstanceRef } from "@/effect/instance-ref"

import PROMPT_DEFAULT from "./prompt/default.txt"
import PROMPT_GPT from "./prompt/gpt.txt"
import { Provider } from "@/provider"
import { ManagedProvider } from "@/acp/managed-provider"
import { sortVisionModels } from "@/provider/provider"
import { Agent } from "@/agent/agent"
import { Permission } from "@/permission"
import { Skill } from "@/skill"
import { type HarnessMode, usesCodexMode } from "@/tool/gpt"

// Every model sees the same default system prompt; only the opt-in Codex
// mode swaps in its own prompt to match the exec toolset.
export function provider(_model: Provider.Model, harness?: HarnessMode) {
  return [usesCodexMode(harness) ? PROMPT_GPT : PROMPT_DEFAULT]
}

export function agent(agent: Agent.Info, model: Provider.Model, harness?: HarnessMode) {
  return agent.prompt ? [agent.prompt] : provider(model, harness)
}

const agentWitnesses = new WeakMap<object, {strings:string[]; bytes:readonly string[]; agent:Agent.Info; model:Provider.Model; providerID:string; modelID:string; nativeModelID:string; prompt:string | undefined; mode:Agent.Info["mode"]}>()
/** A bundled default can be witnessed only at its real constructor call. */
export function agentWithProvenance(value:Agent.Info, model:Provider.Model, harness?: HarnessMode):{strings:string[];handle?:AgentProvenanceHandle} {
  const strings = agent(value,model,harness)
  if ((value.prompt !== undefined && !Agent.ownsBundledChatPrompt(value)) || value.mode === "subagent" || strings.length === 0 || strings.length > 8 ||
      strings.some(value => Buffer.byteLength(value,"utf8") > 65536)) return {strings}
  const handle = Object.freeze({}) as AgentProvenanceHandle
  agentWitnesses.set(handle,{strings,bytes:[...strings],agent:value,model,providerID:model.providerID,modelID:model.api.id,nativeModelID:model.id,prompt:value.prompt,mode:value.mode})
  return {strings,handle}
}
export function readAgentProvenance(handle:AgentProvenanceHandle):readonly string[] | undefined {
  const item=agentWitnesses.get(handle)
  if (!item || item.agent.prompt !== item.prompt || item.agent.mode !== item.mode ||
      (item.agent.prompt !== undefined && !Agent.ownsBundledChatPrompt(item.agent)) || item.agent.mode === "subagent" || item.model.providerID !== item.providerID ||
      item.model.id !== item.nativeModelID || item.model.api.id !== item.modelID || item.strings.length !== item.bytes.length ||
      item.strings.some((value,index) => value !== item.bytes[index])) return
  return item.bytes.map(hashEnvironment)
}

export type SkillInvocation = Readonly<{value:string | undefined}>
const skillWitnesses = new WeakMap<object,{value:string | undefined; agent:Agent.Info; sessionPermission:Permission.Ruleset | undefined; permission:string}>()
const currentSkillWitnesses = new WeakMap<object, {current:()=>boolean}>()
/** Catalog evidence is local to the constructor and never authorizes a skill call. */
export function readCurrentSkillProvenance(invocation:SkillInvocation):boolean {
  try { return currentSkillWitnesses.get(invocation)?.current() === true } catch { return false }
}
export function readEmptySkillProvenance(invocation:SkillInvocation):boolean {
  try { const witness=skillWitnesses.get(invocation); return !!witness && witness.value===invocation.value && JSON.stringify(Agent.runtimePermission(witness.agent,witness.sessionPermission))===witness.permission } catch { return false }
}

export interface Interface {
  readonly environment: (model: Provider.Model, now: number) => Effect.Effect<string[]>
  /** Optional, invocation-owned evidence; never a capture or dispatch grant. */
  readonly environmentWithProvenance?: (model: Provider.Model, now: number, identity: ActorExecutionIdentity) => Effect.Effect<{strings:string[]; handle?:EnvironmentProvenanceHandle}>
  readonly skills: (agent: Agent.Info, sessionPermission?: Permission.Ruleset) => Effect.Effect<string | undefined>
  readonly skillsWithProvenance?: (agent: Agent.Info, sessionPermission?: Permission.Ruleset) => Effect.Effect<SkillInvocation>
  readonly available: (agent?: Agent.Info) => Effect.Effect<Skill.Info[]>
  readonly all: () => Effect.Effect<Skill.Info[]>
  /** Trust-gated resolve for host slash/mention injection (user-explicit mode). */
  readonly getForInvocation: (
    name: string,
    mode: Skill.InvocationMode,
  ) => Effect.Effect<Skill.InvocationResult>
}

const sourceEnvironmentLog = Log.create({service:"source.environment"})
const environmentWitnesses = new WeakMap<object, { strings:string[]; bytes:readonly string[]; record:EnvironmentProvenance; current:()=>boolean }>()
/** Only a handle issued by the constructor, still current and unchanged, can be read. */
export function readEnvironmentProvenance(handle: EnvironmentProvenanceHandle, expectedStrings?:string[]): EnvironmentProvenance | undefined {
  try {
    const witness = environmentWitnesses.get(handle)
    if (!witness || (expectedStrings !== undefined && witness.strings !== expectedStrings) || !witness.current() || witness.strings.length !== witness.bytes.length ||
        witness.strings.some((value,index) => value !== witness.bytes[index])) return
    return witness.record
  } catch { return }
}
const hashEnvironment = (value:string) => createHash("sha256").update(value,"utf8").digest("hex")

export class Service extends Context.Service<Service, Interface>()("@opencode/SystemPrompt") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const skill = yield* Skill.Service
    const providerService = yield* Provider.Service

    const environment = Effect.fn("SystemPrompt.environment")(function* (model: Provider.Model, now: number, observe?:{visionCurrent?:()=>boolean}) {
        const instance = (yield* InstanceRef) ?? Instance.current
        const project = instance.project
        const base = [
          [
            // Identity is never claimed here — who the agent IS comes from the
            // host's persona authority (identity ruling R14); this block carries
            // only operational role + environment facts.
            `You are an interactive agent that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.`,
            `You are powered by the model named ${model.api.id}. The exact model ID is ${model.providerID}/${model.api.id}`,
            `Here is some useful information about the environment you are running in:`,
            `<env>`,
            `  Working directory: ${instance.directory}`,
            `  Workspace root folder: ${instance.worktree}`,
            `  Is directory a git repo: ${project.vcs === "git" ? "yes" : "no"}`,
            `  Platform: ${process.platform}`,
            `</env>`,
          ].join("\n"),
          `IMPORTANT: Your response must ALWAYS strictly follow the same major language as the user.`,
        ]
        // Vision guidance follows the selected model's declared route capability.
        // Provider/model family names are not evidence that this exact adapter route
        // can carry image bytes, and must never suppress an explicit fallback.
        if (!model.capabilities.input.image) {
          // NOTE: vision models are resolved per-call (lazy). If provider list changes
          // mid-session, this block may differ between turns and break cached system prefix.
          // In practice provider config is stable within a session.
          const vision = observe && ManagedProvider.enabled() && providerService.getVisionModelWithProvenance
            ? yield* providerService.getVisionModelWithProvenance(model).pipe(Effect.orElseSucceed(() => ({model:undefined,current:()=>false}))) : undefined
          const preferred = vision ? vision.model : yield* providerService.getVisionModel(model).pipe(Effect.orElseSucceed(() => undefined))
          if (observe && vision) observe.visionCurrent = vision.current
          const visionModels = ManagedProvider.enabled()
            ? preferred ? [`${preferred.providerID}/${preferred.id}`] : []
            : yield* providerService
            .list()
            .pipe(
              Effect.map((providers) =>
                sortVisionModels(
                  Object.values(providers)
                    .flatMap((info) => Object.values(info.models))
                    .filter((m) => m.capabilities.input.image === true),
                )
                  .map((m) => `${m.providerID}/${m.id}`)
                  .slice(0, 3),
              ),
            )
            .pipe(Effect.orElseSucceed(() => [] as string[]))
          const preferredRef = preferred ? `${preferred.providerID}/${preferred.id}` : visionModels[0]
          base.push(
            [
              `<vision-capability>`,
              `You CANNOT see or interpret image content — this model has no vision support.`,
              `Never attempt to analyze an image's visual content yourself. If a task needs image understanding, dispatch a vision-capable subagent via the actor tool, passing the image file path so the subagent can \`read\` it.`,
              visionModels.length
                ? `Vision-capable models you can pass to --model: ${visionModels.join(", ")}. Run \`actor models --vision\` to see all of them. Example: actor run <type> "<desc>" "analyze the image at <path>" --model ${preferredRef}.`
                : `No vision-capable model is currently configured. Ask the user to configure a vision model, or use an OCR tool to extract text.`,
              `If instead you need a file's raw binary structure (not its visual content), use a shell tool such as \`hexdump -C <path>\`, NOT the read tool.`,
            ].join("\n"),
          )
        }
        return base
      })

    const skillsInvocation = Effect.fn("SystemPrompt.skillsInvocation")(function* (agent: Agent.Info, observe=false, sessionPermission?: Permission.Ruleset) {
        const effectivePermission=Agent.runtimePermission(agent,sessionPermission)
        const finish = (value:string | undefined, eligible:boolean, catalogCurrent?:()=>boolean):SkillInvocation => {
          const invocation=Object.freeze({value})
          if (observe && eligible) {
            try { skillWitnesses.set(invocation,{value,agent,sessionPermission,permission:JSON.stringify(effectivePermission)}) } catch { /* optional evidence unavailable */ }
          }
          if (observe && catalogCurrent) {
            const permission = JSON.stringify(effectivePermission)
            currentSkillWitnesses.set(invocation,{current:()=>invocation.value===value && catalogCurrent() &&
              JSON.stringify(Agent.runtimePermission(agent,sessionPermission))===permission})
          }
          return invocation
        }
        if (Permission.disabled(["skill"], effectivePermission).has("skill")) return finish(undefined,true)

        const selectedAgent = {...agent,permission:effectivePermission}
        const catalog = observe && skill.availableWithProvenance ? yield* skill.availableWithProvenance(selectedAgent) : undefined
        const list = catalog ? catalog.items : yield* skill.modelInvocable(selectedAgent)

        return finish([
          "Skills available in this session:",
          // the agents seem to ingest the information about skills a bit better if we present a more verbose
          // version of them here and a less verbose version in tool description, rather than vice versa.
          Skill.fmt(list, { verbose: true }),
        ].join("\n"),list.length === 0,catalog?.current)
      })


    return Service.of({
      environment,
      environmentWithProvenance: Effect.fn("SystemPrompt.environmentWithProvenance")(function* (model, now, identity) {
        // Always construct the unchanged normal result, even when evidence is unavailable.
        const instance = (yield* InstanceRef) ?? Instance.current
        const observed:{visionCurrent?:()=>boolean} = {}
        const strings = yield* environment(model, now, observed)
        try {
          const sessionID = String(identity.sessionID)
          const decline = (reason:string) => { try { sourceEnvironmentLog.debug("source_environment_declined",{reason}) } catch { /* diagnostic only */ }; return {strings} }
          if (!ManagedProvider.enabled()) return decline("managed")
          if (identity.actorID !== "main") return decline("actor")
          if (!identity.rootOperationID || !identity.innerTurnID) return decline("turn")
          if (!Number.isSafeInteger(identity.executionRevision) || identity.executionRevision < 0) return decline("execution_revision")
          if (!Number.isSafeInteger(now) || now < 0) return decline("session_time")
          if (strings.length !== (model.capabilities.input.image ? 2 : 3)) return decline("fragment_count")
          if (!model.capabilities.input.image && !observed.visionCurrent?.()) return decline("vision_current")
          if (strings.some((value) => typeof value !== "string" || Buffer.byteLength(value,"utf8") > 16384)) return decline("fragment_bounds")
          const tuple = {sessionID,actorID:identity.actorID,executionRevision:identity.executionRevision,rootOperationID:identity.rootOperationID,innerTurnID:identity.innerTurnID}
          const binding = managedSessionBinding(sessionID)
          const host = ManagedProvider.currentActorHostContext(sessionID,identity.actorID,identity.executionRevision)
          const issuer = ManagedProvider.currentActorToolTimingIssuer(sessionID,identity.actorID,identity.executionRevision,identity.rootOperationID)
          const row = Database.use(db => db.get<{directory:string; time_created:number}>(sql`SELECT directory,time_created FROM session WHERE id=${sessionID}`))
          if (!binding) return decline("binding_missing")
          if (binding.authority) return decline("binding_authority")
          if (binding.transition) return decline("binding_transition")
          if (!markerMatches(sessionID,binding)) return decline("binding_marker")
          if (!host?.ownerAccountID || !host.chatID) return decline("host_scope")
          if (!issuer?.isAlive()) return decline("issuer")
          if (!isAdmittedToolTimingTurn(identity,tuple)) return decline("turn_admission")
          if (!row) return decline("session_missing")
          if (row.time_created !== now) return decline("session_created")
          if (row.directory !== instance.directory) return decline("session_directory")
          if (instance.directory !== binding.physicalCwd) return decline("binding_directory")
          if (path.normalize(binding.physicalCwd) !== binding.physicalCwd || !path.isAbsolute(binding.physicalCwd)) return decline("binding_path")
          if (typeof instance.worktree !== "string" || path.normalize(instance.worktree) !== instance.worktree || !path.isAbsolute(instance.worktree)) return decline("worktree_path")
          if (binding.stableChatID !== host.chatID) return decline("binding_chat")
          if (!Number.isSafeInteger(binding.bindingRevision) || !Number.isSafeInteger(binding.mappingRevision) || !Number.isSafeInteger(binding.mapRevision)) return decline("binding_revisions")
          if ([host.ownerAccountID,host.chatID,model.providerID,model.api.id].some(v => typeof v !== "string" || !v || Buffer.byteLength(v,"utf8") > 256)) return decline("identity_bounds")
          if (typeof binding.authorityWorkspaceID !== "string" || Buffer.byteLength(binding.authorityWorkspaceID,"utf8") > 256) return decline("workspace_bounds")
          const capturedBinding = binding
          const capturedHost = host
          const capturedProviderID = model.providerID
          const capturedModelID = model.api.id
          const capturedWorktree = instance.worktree
          const capturedProject = instance.project
          const capturedProjectID = instance.project.id
          const capturedVCS = instance.project.vcs
          const imageInput = model.capabilities.input.image
          const legacyEnvironment = imageInput && !!binding.authorityWorkspaceID && capturedWorktree === binding.physicalCwd
          let diagnosing = true
          const failed = (reason:string) => { if (diagnosing) { try { sourceEnvironmentLog.debug("source_environment_declined",{reason}) } catch { /* diagnostic only */ } }; return false }
          const current = () => {
            const next = managedSessionBinding(sessionID)
            const nextHost = ManagedProvider.currentActorHostContext(sessionID,identity.actorID,identity.executionRevision)
            const session = Database.use(db => db.get<{directory:string; time_created:number}>(sql`SELECT directory,time_created FROM session WHERE id=${sessionID}`))
            if (Instance.current !== instance) return failed("current_instance")
            if (instance.directory !== capturedBinding.physicalCwd) return failed("current_directory")
            if (instance.worktree !== capturedWorktree) return failed("current_worktree")
            if (!legacyEnvironment && (instance.project !== capturedProject || instance.project.id !== capturedProjectID || instance.project.vcs !== capturedVCS)) return failed("current_project")
            if (model.capabilities.input.image !== imageInput) return failed("current_image")
            if (!imageInput && observed.visionCurrent?.() !== true) return failed("current_vision")
            if (model.providerID !== capturedProviderID || model.api.id !== capturedModelID) return failed("current_model")
            if (!next) return failed("current_binding_missing")
            if (!markerMatches(sessionID,next)) return failed("current_marker")
            if (next.registrationMarker.token !== capturedBinding.registrationMarker.token) return failed("current_marker_token")
            if (next.physicalCwd !== capturedBinding.physicalCwd) return failed("current_binding_directory")
            if (next.bindingRevision !== capturedBinding.bindingRevision || next.mappingRevision !== capturedBinding.mappingRevision || next.mapRevision !== capturedBinding.mapRevision) return failed("current_revisions")
            if (next.authorityWorkspaceID !== capturedBinding.authorityWorkspaceID || (!legacyEnvironment && next.authority) || next.transition) return failed("current_authority")
            if (nextHost?.ownerAccountID !== capturedHost.ownerAccountID || nextHost?.chatID !== capturedHost.chatID) return failed("current_host")
            if (session?.directory !== instance.directory || session?.time_created !== now) return failed("current_session")
            if (!issuer.isAlive()) return failed("current_issuer")
            if (ManagedProvider.currentActorToolTimingIssuer(sessionID,identity.actorID,identity.executionRevision,identity.rootOperationID!) !== issuer) return failed("current_issuer_identity")
            if (!isAdmittedToolTimingTurn(identity,tuple)) return failed("current_turn")
            return true
          }
          if (!current()) return {strings}
          diagnosing = false
          const scope = Object.freeze({ownerAccountID:host.ownerAccountID,chatID:host.chatID,authorityWorkspaceID:binding.authorityWorkspaceID,
            workspaceRevision:binding.bindingRevision!,mappingRevision:binding.mappingRevision!,mapRevision:binding.mapRevision!,
            cwdSHA256:hashEnvironment(binding.physicalCwd),sessionCreatedMs:now,providerID:model.providerID,modelID:model.api.id})
          const runtime = Object.freeze({kind:"runtime_environment" as const,constructorID:"system.environment.v1" as const,outputSHA256:hashEnvironment(strings[0]!)})
          const language = Object.freeze({kind:"bundled_static" as const,constructorID:"system.language.v1" as const,outputSHA256:hashEnvironment(strings[1]!)})
          const record:EnvironmentProvenance = legacyEnvironment
            ? Object.freeze({version:1,identity:Object.freeze(tuple),scope,fragments:Object.freeze([runtime,language] as const)})
            : Object.freeze({version:2,identity:Object.freeze(tuple),scope:Object.freeze({...scope,worktreeSHA256:hashEnvironment(capturedWorktree)}),scopeKind:binding.authorityWorkspaceID ? "workspace" : "account-chat",imageInput,
                fragments:imageInput ? Object.freeze([runtime,language] as const) : Object.freeze([runtime,language,Object.freeze({kind:"vision_guidance" as const,constructorID:"system.no-vision.v1" as const,outputSHA256:hashEnvironment(strings[2]!)})] as const)})
          const handle = Object.freeze({}) as EnvironmentProvenanceHandle
          environmentWitnesses.set(handle,{strings,bytes:[...strings],record,current})
          return {strings,handle}
        } catch { try { sourceEnvironmentLog.debug("source_environment_declined",{reason:"exception"}) } catch { /* diagnostic only */ }; return {strings} }
      }),

      skills: (agent,sessionPermission) => skillsInvocation(agent,false,sessionPermission).pipe(Effect.map(result => result.value)),
      skillsWithProvenance: (agent,sessionPermission) => skillsInvocation(agent,true,sessionPermission),

      // The user surface: authorization-filtered but NOT model-reachability
      // filtered, because it backs the mention scan that loads a skill the user
      // invoked explicitly. Do not switch this to modelInvocable.
      available: Effect.fn("SystemPrompt.available")(function* (agent?: Agent.Info) {
        return yield* skill.available(agent)
      }),

      all: Effect.fn("SystemPrompt.all")(function* () {
        return yield* skill.all()
      }),

      getForInvocation: Effect.fn("SystemPrompt.getForInvocation")(function* (
        name: string,
        mode: Skill.InvocationMode,
      ) {
        return yield* skill.getForInvocation(name, mode)
      }),
    })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Skill.defaultLayer),
  Layer.provide(Provider.defaultLayer),
)

export * as SystemPrompt from "./system"
