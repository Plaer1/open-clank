import type {
  Hooks,
  PluginInput,
  Plugin as PluginInstance,
  PluginModule,
  WorkspaceAdaptor as PluginWorkspaceAdaptor,
  ActorPreStopInput,
  ActorPostStopInput,
  ActorStopOutput,
  ActorMatcher,
} from "@mimo-ai/plugin"
import { z } from "zod"
import { matchesActor } from "./matcher"
import { Config } from "../config"
import { Bus } from "../bus"
import { BusEvent } from "../bus/bus-event"
import { Log } from "../util"
import { createOpencodeClient } from "@mimo-ai/sdk"
import { Flag } from "../flag/flag"
import { CodexAuthPlugin } from "./codex"
import { XaiAuthPlugin } from "./xai"
import { GrokAuthPlugin } from "./grok"
import { KimiCodeChinaAuthPlugin, KimiCodeGlobalAuthPlugin } from "./kimi-code"
import { OpenRouterBrowserAuthPlugin } from "./openrouter-browser"
import { KiloDeviceAuthPlugin } from "./kilo"
import { LLMGatewayBrowserAuthPlugin, LLMGatewayDevPassAuthPlugin } from "./llmgateway-browser"
import { MimoAuthPlugin, AnthropicProxyPlugin } from "./mimo"
import { ClaudeAuthPlugin } from "./claude"
import { Session } from "../session"
import { Snapshot } from "../snapshot"
import type { SessionID } from "../session/schema"
import { NamedError } from "@mimo-ai/shared/util/error"
import { CopilotAuthPlugin } from "./github-copilot/copilot"
import { gitlabAuthPlugin as GitlabAuthPlugin } from "opencode-gitlab-auth"
import { PoeAuthPlugin } from "opencode-poe-auth"
import { CloudflareAIGatewayAuthPlugin, CloudflareWorkersAuthPlugin } from "./cloudflare"
import { CheckpointSplitoverPlugin } from "./checkpoint-splitover"
import { SubagentProgressCheckerPlugin } from "./subagent-progress-checker"
import { Effect, Layer, Context, Stream, Option } from "effect"
import { EffectBridge } from "@/effect"
import { InstanceState } from "@/effect"
import { errorMessage } from "@/util/error"
import { PluginLoader } from "./loader"
import { parsePluginSpecifier, readPluginId, readV1Plugin, resolvePluginId } from "./shared"
import { registerAdaptor } from "@/control-plane/adaptors"
import type { WorkspaceAdaptor } from "@/control-plane/types"
import { Glob } from "@mimo-ai/shared/util/glob"
import fs from "fs"
import path from "path"
import { randomUUID } from "node:crypto"
import { pathToFileURL, fileURLToPath } from "url"

const log = Log.create({ service: "plugin" })

/** Keep one Anthropic auth hook: proxy loader plus native Claude methods. */
export function composeAnthropicAuthHooks(proxy: Hooks, claude: Hooks): Hooks {
  if (!proxy.auth || !claude.auth) return proxy
  return {
    ...proxy,
    auth: {
      ...proxy.auth,
      methods: [...proxy.auth.methods, ...claude.auth.methods],
    },
  }
}

async function AnthropicAuthPlugin(input: PluginInput): Promise<Hooks> {
  return composeAnthropicAuthHooks(await AnthropicProxyPlugin(input), await ClaudeAuthPlugin(input))
}

export const HookEvent = {
  Executed: BusEvent.define(
    "hook.executed",
    z.object({
      event: z.enum(["actor.preStop", "actor.postStop"]),
      hookID: z.string(),
      pluginName: z.string(),
      actorID: z.string(),
      agentType: z.string(),
      durationMs: z.number(),
      outcome: z.enum(["success", "error", "skipped"]),
      continueRequested: z.boolean(),
      reasonLength: z.number(),
    }),
  ),
  ReActReentered: BusEvent.define(
    "hook.react.reentered",
    z.object({
      phase: z.enum(["pre", "post"]),
      actorID: z.string(),
      agentType: z.string(),
      iteration: z.number(),
      triggeredByPlugins: z.array(z.string()),
      reasonPreview: z.string(),
    }),
  ),
  ReActMaxReached: BusEvent.define(
    "hook.react.max_reached",
    z.object({
      phase: z.enum(["pre", "post"]),
      actorID: z.string(),
      agentType: z.string(),
    }),
  ),
} as const

type HookEntry = {
  hook: Hooks
  pluginName: string
  /** Stable per-event hook ID: `${pluginName}#${eventName}` */
  hookIDFor: (eventName: string) => string
}

type State = {
  generation: string
  hooks: Hooks[]
  hooksWithMeta: HookEntry[]
}

type FileHookState = {
  generation: string
  hooks: Hooks[]
  meta: HookEntry[]
  dirs: string[]
  /** Absolute path -> mtimeMs at load time, for cheap staleness checks. */
  files: Record<string, number>
  /** Mutable box: last staleness check timestamp (throttle). */
  lastCheck: { value: number }
}

const FILE_HOOK_GLOB = "{hook,hooks}/*.{js,ts}"
const FILE_HOOK_CHECK_INTERVAL_MS = 500

export type ActorStopAggregatedDecision = ActorStopOutput & {
  contributingPluginNames: string[]
  contributingHookIDs: string[]
}

// Hook names that follow the (input, output) => Promise<void> trigger pattern
type TriggerName = {
  [K in keyof Hooks]-?: NonNullable<Hooks[K]> extends (input: any, output: any) => Promise<void> ? K : never
}[keyof Hooks]

export interface Interface {
  readonly provenanceGeneration?: () => Effect.Effect<{plugins:string; fileHooks:string}>
  readonly recoveryWitness?: () => Effect.Effect<import("../session/recovery-profile").HookWitness>
  readonly trigger: <
    Name extends TriggerName,
    Input = Parameters<Required<Hooks>[Name]>[0],
    Output = Parameters<Required<Hooks>[Name]>[1],
  >(
    name: Name,
    input: Input,
    output: Output,
  ) => Effect.Effect<Output>
  readonly requestEmptyToolDefinition?: (output:{description:string;parameters:unknown}) => void
  readonly readEmptyToolDefinition?: (output:{description:string;parameters:unknown},toolID:string) => boolean
  readonly requestEmptyTextTransform?: (output:{text:string}) => void
  readonly readEmptyTextTransform?: (output:{text:string},identity:{sessionID:string;messageID:string;partID:string}) => boolean
  readonly requestEmptySystemTransform?: (output:{system:string[]}) => void
  readonly readEmptySystemTransform?: (output:{system:string[]},sessionID:string) => boolean
  readonly list: () => Effect.Effect<Hooks[]>
  readonly init: () => Effect.Effect<void>
  readonly reloadFileHooks: () => Effect.Effect<void>
  readonly triggerActorPreStop: (
    input: ActorPreStopInput,
  ) => Effect.Effect<ActorStopAggregatedDecision>
  readonly triggerActorPostStop: (
    input: ActorPostStopInput,
  ) => Effect.Effect<ActorStopAggregatedDecision>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Plugin") {}

// Built-in plugins that are directly imported (not installed from npm)
const INTERNAL_PLUGINS: PluginInstance[] = [
  MimoAuthPlugin,
  AnthropicAuthPlugin,
  CodexAuthPlugin,
  XaiAuthPlugin,
  GrokAuthPlugin,
  KimiCodeChinaAuthPlugin,
  KimiCodeGlobalAuthPlugin,
  OpenRouterBrowserAuthPlugin,
  KiloDeviceAuthPlugin,
  LLMGatewayBrowserAuthPlugin,
  LLMGatewayDevPassAuthPlugin,
  CopilotAuthPlugin,
  // gitlab/poe auth are external npm packages typed against the published
  // upstream plugin package, which carries a duplicate (nominal) copy of the
  // SDK client; cast through unknown to the workspace Plugin type.
  GitlabAuthPlugin as unknown as PluginInstance,
  PoeAuthPlugin as unknown as PluginInstance,
  CloudflareWorkersAuthPlugin,
  CloudflareAIGatewayAuthPlugin,
  CheckpointSplitoverPlugin,
  SubagentProgressCheckerPlugin,
]

// These exact built-in functions only shape request headers/parameters. Their
// identity is private so external/file plugins cannot inherit the exemption by
// choosing the same name or event.
const SNAPSHOT_SAFE_REQUEST_PLUGINS = new Set<PluginInstance>([
  CodexAuthPlugin,
  MimoAuthPlugin,
  CopilotAuthPlugin,
  CloudflareWorkersAuthPlugin,
  CloudflareAIGatewayAuthPlugin,
])
const SNAPSHOT_SAFE_REQUEST_HANDLERS = new WeakSet<Function>()

function registerSnapshotSafeRequestHandlers(plugin: PluginInstance, hooks: Hooks) {
  if (!SNAPSHOT_SAFE_REQUEST_PLUGINS.has(plugin)) return
  for (const name of ["chat.headers", "chat.params"] as const) {
    const handler = hooks[name]
    if (typeof handler === "function") SNAPSHOT_SAFE_REQUEST_HANDLERS.add(handler)
  }
}

function isServerPlugin(value: unknown): value is PluginInstance {
  return typeof value === "function"
}

function isHookObject(value: unknown): value is Hooks {
  return value != null && typeof value === "object"
}

function registerHook(hooks: Hooks[], hooksWithMeta: HookEntry[], hook: unknown, pluginName: string) {
  if (!isHookObject(hook)) {
    log.warn("plugin did not return a hook object, skipping", { pluginName })
    return
  }
  hooks.push(hook)
  hooksWithMeta.push({
    hook,
    pluginName,
    hookIDFor: (event: string) => `${pluginName}#${event}`,
  })
}

function getServerPlugin(value: unknown) {
  if (isServerPlugin(value)) return value
  if (!value || typeof value !== "object" || !("server" in value)) return
  if (!isServerPlugin(value.server)) return
  return value.server
}

function getLegacyPlugins(mod: Record<string, unknown>) {
  const seen = new Set<unknown>()
  const result: PluginInstance[] = []

  for (const entry of Object.values(mod)) {
    if (seen.has(entry)) continue
    seen.add(entry)
    const plugin = getServerPlugin(entry)
    if (!plugin) throw new TypeError("Plugin export is not a function")
    result.push(plugin)
  }

  return result
}

async function applyPlugin(
  load: PluginLoader.Loaded,
  input: PluginInput,
  hooks: Hooks[],
  hooksWithMeta: HookEntry[],
) {
  const plugin = readV1Plugin(load.mod, load.spec, "server", "detect")
  if (plugin) {
    await resolvePluginId(load.source, load.spec, load.target, readPluginId(plugin.id, load.spec), load.pkg)
    const pluginName = readPluginId(plugin.id, load.spec) ?? load.pkg?.pkg ?? load.spec
    registerHook(hooks, hooksWithMeta, await (plugin as PluginModule).server(input, load.options), pluginName)
    return
  }

  for (const server of getLegacyPlugins(load.mod)) {
    const fnName = (server as { name?: string }).name
    const pluginName = fnName && fnName !== "default" && fnName !== ""
      ? fnName
      : (load.pkg?.pkg ?? load.spec)
    registerHook(hooks, hooksWithMeta, await server(input, load.options), pluginName)
  }
}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service
    const config = yield* Config.Service

    const beforeHook = Effect.gen(function* () {
      const barrier = Option.getOrUndefined(yield* Effect.serviceOption(Snapshot.CaptureBarrier))
      if (barrier) yield* barrier().pipe(Effect.orDie)
    })

    const state = yield* InstanceState.make<State>(
      Effect.fn("Plugin.state")(function* (ctx) {
        const hooks: Hooks[] = []
        const hooksWithMeta: HookEntry[] = []
        const bridge = yield* EffectBridge.make()

        function publishPluginError(message: string) {
          bridge.fork(bus.publish(Session.Event.Error, { error: new NamedError.Unknown({ message }).toObject() }))
        }

        const { Server } = yield* Effect.promise(() => import("../server/server"))

        const client = createOpencodeClient({
          baseUrl: "http://mimocode.internal",
          directory: ctx.directory,
          headers: Flag.MIMOCODE_SERVER_PASSWORD
            ? {
                Authorization: `Basic ${Buffer.from(`${Flag.MIMOCODE_SERVER_USERNAME ?? "mimocode"}:${Flag.MIMOCODE_SERVER_PASSWORD}`).toString("base64")}`,
              }
            : undefined,
          fetch: async (...args) => (await Server.Default()).app.fetch(...args),
        })
        const cfg = yield* config.get()
        const input: PluginInput = {
          client,
          project: ctx.project,
          worktree: ctx.worktree,
          directory: ctx.directory,
          experimental_workspace: {
            register(type: string, adaptor: PluginWorkspaceAdaptor) {
              registerAdaptor(ctx.project.id, type, adaptor as WorkspaceAdaptor)
            },
          },
          get serverUrl(): URL {
            // Real URL after listen; placeholder origin has no conventional port hardcode.
            return Server.url ?? new URL("http://mimocode.internal")
          },
          // @ts-expect-error
          $: typeof Bun === "undefined" ? undefined : Bun.$,
        }

        for (const plugin of INTERNAL_PLUGINS) {
          if (typeof plugin !== "function") {
            log.warn("skipping invalid internal plugin")
            continue
          }
          log.info("loading internal plugin", { name: plugin.name })
          const init = yield* Effect.tryPromise({
            try: () => plugin(input),
            catch: (err) => {
              log.error("failed to load internal plugin", { name: plugin.name, error: err })
            },
          }).pipe(Effect.option)
          if (init._tag === "Some") {
            registerSnapshotSafeRequestHandlers(plugin, init.value)
            registerHook(hooks, hooksWithMeta, init.value, plugin.name || "internal")
          }
        }

        // Load optional local extensions under src/ext/. Prefers the generated
        // _manifest.ts (a fixed import specifier resolves inside Bun single-file
        // executables, where filesystem scans do not); falls back to a directory
        // scan for unbundled runs. Each *Plugin-named export is registered.
        const extModules: Record<string, Record<string, unknown>> = {}
        // @ts-ignore generated manifest; may not exist at type-check time
        const manifest = yield* Effect.tryPromise(() => import("../ext/_manifest")).pipe(Effect.option)
        if (manifest._tag === "Some") {
          Object.assign(
            extModules,
            (manifest.value as { modules?: Record<string, Record<string, unknown>> }).modules ?? {},
          )
        } else {
          const extDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "ext")
          const extFiles = fs.existsSync(extDir)
            ? fs.readdirSync(extDir).filter((f) => f.endsWith(".ts") && !f.endsWith(".d.ts") && f !== "_manifest.ts")
            : []
          for (const entry of extFiles) {
            const mod = yield* Effect.tryPromise({
              try: () => import(/* @vite-ignore */ pathToFileURL(path.join(extDir, entry)).href),
              catch: (err) => log.error("failed to import extension", { name: entry, error: err }),
            }).pipe(Effect.option)
            if (mod._tag === "Some") extModules[entry.replace(/\.ts$/, "")] = mod.value as Record<string, unknown>
          }
        }
        for (const [name, value] of Object.entries(extModules)) {
          // Only treat *Plugin-named function exports as plugins. Other modules
          // (e.g. a CLI helper export) are not plugins and must not be invoked
          // as plugin factories.
          const overlay = Object.entries(value).find(
            ([exportName, v]) => typeof v === "function" && exportName.endsWith("Plugin"),
          )?.[1] as PluginInstance | undefined
          if (!overlay) continue
          // log.info("loading extension", { name })
          const init = yield* Effect.tryPromise({
            try: () => overlay(input),
            catch: (err) => log.error("failed to load extension", { name, error: err }),
          }).pipe(Effect.option)
          if (init._tag === "Some") registerHook(hooks, hooksWithMeta, init.value, name)
        }

        const plugins = Flag.MIMOCODE_PURE ? [] : (cfg.plugin_origins ?? [])
        if (Flag.MIMOCODE_PURE && cfg.plugin_origins?.length) {
          log.info("skipping external plugins in pure mode", { count: cfg.plugin_origins.length })
        }
        if (plugins.length) yield* config.waitForDependencies()

        const loaded = yield* Effect.promise(() =>
          PluginLoader.loadExternal({
            items: plugins,
            kind: "server",
            report: {
              start(candidate) {
                log.info("loading plugin", { path: candidate.plan.spec })
              },
              missing(candidate, _retry, message) {
                log.warn("plugin has no server entrypoint", { path: candidate.plan.spec, message })
              },
              error(candidate, _retry, stage, error, resolved) {
                const spec = candidate.plan.spec
                const cause = error instanceof Error ? (error.cause ?? error) : error
                const message = stage === "load" ? errorMessage(error) : errorMessage(cause)

                if (stage === "install") {
                  const parsed = parsePluginSpecifier(spec)
                  log.error("failed to install plugin", { pkg: parsed.pkg, version: parsed.version, error: message })
                  publishPluginError(`Failed to install plugin ${parsed.pkg}@${parsed.version}: ${message}`)
                  return
                }

                if (stage === "compatibility") {
                  log.warn("plugin incompatible", { path: spec, error: message })
                  publishPluginError(`Plugin ${spec} skipped: ${message}`)
                  return
                }

                if (stage === "entry") {
                  log.error("failed to resolve plugin server entry", { path: spec, error: message })
                  publishPluginError(`Failed to load plugin ${spec}: ${message}`)
                  return
                }

                log.error("failed to load plugin", { path: spec, target: resolved?.entry, error: message })
                publishPluginError(`Failed to load plugin ${spec}: ${message}`)
              },
            },
          }),
        )
        for (const load of loaded) {
          if (!load) continue

          // Keep plugin execution sequential so hook registration and execution
          // order remains deterministic across plugin runs.
          yield* Effect.tryPromise({
            try: () => applyPlugin(load, input, hooks, hooksWithMeta),
            catch: (err) => {
              const message = errorMessage(err)
              log.error("failed to load plugin", { path: load.spec, error: message })
              return message
            },
          }).pipe(
            Effect.catch(() => {
              // TODO: make proper events for this
              // bus.publish(Session.Event.Error, {
              //   error: new NamedError.Unknown({
              //     message: `Failed to load plugin ${load.spec}: ${message}`,
              //   }).toObject(),
              // })
              return Effect.void
            }),
          )
        }

        // Notify plugins of current config
        for (const hook of hooks) {
          if (!isHookObject(hook)) continue
          yield* Effect.tryPromise({
            try: () => Promise.resolve((hook as any).config?.(cfg)),
            catch: (err) => {
              log.error("plugin config hook failed", { error: err })
            },
          }).pipe(Effect.ignore)
        }

        // Subscribe to bus events, fiber interrupted when scope closes
        if (hooks.some((hook) => isHookObject(hook) && typeof hook.event === "function")) {
          yield* bus.subscribeAll().pipe(
            Stream.runForEach((input) =>
              Effect.sync(() => {
                for (const hook of hooks) {
                  if (!isHookObject(hook)) continue
                  void hook["event"]?.({ event: input as any })
                }
              }),
            ),
            Effect.forkScoped,
          )
        }

        return { generation:randomUUID(), hooks, hooksWithMeta }
      }),
    )

    const fileHookState = yield* InstanceState.make<FileHookState>(
      Effect.fn("Plugin.fileHooks")(function* () {
        const hooks: Hooks[] = []
        const meta: HookEntry[] = []
        const files: Record<string, number> = {}
        yield* config.get()
        const dirs = yield* config.directories()

        for (const dir of dirs) {
          const matches = Glob.scanSync(FILE_HOOK_GLOB, { cwd: dir, absolute: true, dot: true, symlink: true })
          for (const match of matches) {
            const stat = yield* Effect.tryPromise({
              try: () => fs.promises.stat(match),
              catch: (err) => err,
            }).pipe(Effect.catch(() => Effect.succeed(undefined)))
            files[match] = stat?.mtimeMs ?? 0
            // Transpile and load the hook file. We use Bun.build to produce a
            // temporary .js artifact, then dynamic-import that artifact. This
            // avoids two pitfalls: (1) Bun's import() ignores query-string cache
            // busters so re-imports return stale modules, (2) require() transpiles
            // .ts in some contexts but not others (CI Linux edge case).
            const mod = yield* Effect.tryPromise({
              try: async () => {
                const result = await Bun.build({
                  entrypoints: [match],
                  target: "bun",
                  format: "esm",
                })
                if (!result.success) throw new Error(result.logs.map(String).join("\n"))
                const blob = result.outputs[0]
                const tmpFile = `${match}.${Date.now()}.mjs`
                await Bun.write(tmpFile, blob)
                try {
                  return await import(tmpFile) as Record<string, unknown>
                } finally {
                  fs.promises.unlink(tmpFile).catch(() => {})
                }
              },
              catch: (err) => err,
            }).pipe(Effect.catch((err) => {
              log.error("failed to load file hook", { path: match, error: errorMessage(err) })
              return Effect.succeed(undefined)
            }))
            if (!mod) continue
            const hookObj = mod.default ?? mod
            if (isHookObject(hookObj)) {
              const name = path.basename(match, path.extname(match))
              hooks.push(hookObj)
              meta.push({ hook: hookObj, pluginName: `file:${name}`, hookIDFor: (event: string) => `file:${name}#${event}` })
              log.info("loaded file hook", { path: match, name })
            }
          }
        }

        // Dispatch bus events to file hooks' `event` handlers. Scoped to this
        // cache entry: invalidation interrupts the fiber, and the rebuild
        // re-subscribes with the fresh hook set.
        if (hooks.some((hook) => typeof hook.event === "function")) {
          yield* bus.subscribeAll().pipe(
            Stream.runForEach((input) =>
              Effect.sync(() => {
                for (const entry of meta) {
                  const fn = entry?.hook?.event
                  if (!fn) continue
                  try {
                    void Promise.resolve(fn({ event: input as any })).catch((err) => {
                      log.error("file hook event handler failed", { hook: entry.pluginName, error: errorMessage(err) })
                    })
                  } catch (err) {
                    log.error("file hook event handler failed", { hook: entry.pluginName, error: errorMessage(err) })
                  }
                }
              }),
            ),
            Effect.forkScoped,
          )
        }

        return { generation:randomUUID(), hooks, meta, dirs, files, lastCheck: { value: Date.now() } }
      }),
    )

    // Staleness check: re-stat known hook files and re-glob hook dirs. Any
    // mtime change, added, or removed file invalidates the cache so the next
    // InstanceState.get rebuilds it. Covers ALL writers (editors, git, other
    // processes) — not just this process's write/edit tools. Throttled to
    // avoid stat storms on hot trigger paths.
    const freshFileHooks = Effect.gen(function* () {
      const fh = yield* InstanceState.get(fileHookState)
      const now = Date.now()
      if (now - fh.lastCheck.value < FILE_HOOK_CHECK_INTERVAL_MS) return fh
      fh.lastCheck.value = now

      const stale = yield* Effect.promise(async () => {
        const known = Object.keys(fh.files)
        const seen = new Set<string>()
        for (const dir of fh.dirs) {
          for (const match of Glob.scanSync(FILE_HOOK_GLOB, { cwd: dir, absolute: true, dot: true, symlink: true })) {
            seen.add(match)
            if (!(match in fh.files)) return true
          }
        }
        for (const file of known) {
          if (!seen.has(file)) return true
          const stat = await fs.promises.stat(file).catch(() => undefined)
          if ((stat?.mtimeMs ?? 0) !== fh.files[file]) return true
        }
        return false
      })

      if (!stale) return fh
      log.info("file hooks changed on disk, reloading")
      yield* InstanceState.invalidate(fileHookState)
      return yield* InstanceState.get(fileHookState)
    })

    const aggregateDecision = (
      input: ActorPreStopInput | ActorPostStopInput,
      eventName: "actor.preStop" | "actor.postStop",
    ) =>
      Effect.gen(function* () {
        const s = yield* InstanceState.get(state)
        const fh = yield* freshFileHooks
        const reasons: string[] = []
        const pluginNames: string[] = []
        const hookIDs: string[] = []
        let anyContinue = false

        for (const entry of [...(s.hooksWithMeta ?? []), ...(fh.meta ?? [])]) {
          const hook = entry?.hook
          if (!isHookObject(hook)) continue
          const reg = hook[eventName]
          if (!reg) continue

          const fn = typeof reg === "function" ? reg : reg.run
          const matcher: ActorMatcher | undefined =
            typeof reg === "function" ? undefined : reg.matcher

          if (!matchesActor(matcher, input)) {
            yield* bus.publish(HookEvent.Executed, {
              event: eventName,
              hookID: entry.hookIDFor(eventName),
              pluginName: entry.pluginName,
              actorID: input.actorID,
              agentType: input.agentType,
              durationMs: 0,
              outcome: "skipped",
              continueRequested: false,
              reasonLength: 0,
            })
            continue
          }

          yield* beforeHook

          const startedAt = Date.now()
          const o: ActorStopOutput = { continue: false }
          let hookOutcome: "success" | "error" = "success"
          // TODO: pass an AbortSignal to fn so plugin authors can wire cooperative
          // cancellation into their fetch / DB calls. Effect interrupt only stops
          // the awaiting fiber — the underlying Promise keeps running and may
          // bus.publish events after the actor has been cleaned up. See spec
          // Future work for full discussion. Strict in-process cancellation
          // (子进程隔离) is out of scope; AbortSignal is the in-process ceiling.
          yield* Effect.tryPromise({
            try: () => fn(input as never, o),
            catch: (err) => err,
          }).pipe(
            Effect.tapError((err) =>
              Effect.gen(function* () {
                hookOutcome = "error"
                log.error(`${eventName} hook failed`, { pluginName: entry.pluginName, hookID: entry.hookIDFor(eventName), error: err })
                yield* bus.publish(Session.Event.Error, {
                  sessionID: input.sessionID as SessionID,
                  error: new NamedError.Unknown({
                    message: `${eventName} hook (${entry.pluginName}) failed: ${errorMessage(err)}`,
                  }).toObject(),
                })
              }),
            ),
            Effect.ignore,
          )

          const durationMs = Date.now() - startedAt
          yield* bus.publish(HookEvent.Executed, {
            event: eventName,
            hookID: entry.hookIDFor(eventName),
            pluginName: entry.pluginName,
            actorID: input.actorID,
            agentType: input.agentType,
            durationMs,
            outcome: hookOutcome,
            continueRequested: o.continue === true,
            reasonLength: o.reason?.length ?? 0,
          })

          if (o.continue === true && o.reason && o.reason.length > 0) {
            anyContinue = true
            reasons.push(o.reason)
            pluginNames.push(entry.pluginName)
            hookIDs.push(entry.hookIDFor(eventName))
          } else if (o.continue === true) {
            log.warn(`${eventName} hook returned continue=true without reason; ignored`, {
              pluginName: entry.pluginName,
            })
          }
        }

        const aggregated: ActorStopAggregatedDecision = {
          continue: anyContinue,
          reason: reasons.length > 0 ? reasons.join("\n\n") : undefined,
          contributingPluginNames: pluginNames,
          contributingHookIDs: hookIDs,
        }
        return aggregated
      })

    const triggerActorPreStop = Effect.fn("Plugin.triggerActorPreStop")(function* (
      input: ActorPreStopInput,
    ) {
      return yield* aggregateDecision(input, "actor.preStop")
    })

    const triggerActorPostStop = Effect.fn("Plugin.triggerActorPostStop")(function* (
      input: ActorPostStopInput,
    ) {
      return yield* aggregateDecision(input, "actor.postStop")
    })

    const requestedToolDefinitions = new WeakSet<object>()
    const emptyToolDefinitions = new WeakMap<object,{toolID:string;description:string;parameters:unknown}>()
    const requestEmptyToolDefinition = (output:{description:string;parameters:unknown}) => { requestedToolDefinitions.add(output) }
    const readEmptyToolDefinition = (output:{description:string;parameters:unknown},toolID:string):boolean => {
      const item=emptyToolDefinitions.get(output)
      return !!item && item.toolID===toolID && item.description===output.description && item.parameters===output.parameters
    }
    const requestedTextTransforms = new WeakSet<object>()
    const emptyTextTransforms = new WeakMap<object,{sessionID:string;messageID:string;partID:string;text:string}>()
    const requestEmptyTextTransform = (output:{text:string}) => { requestedTextTransforms.add(output) }
    const readEmptyTextTransform = (output:{text:string},identity:{sessionID:string;messageID:string;partID:string}):boolean => {
      const item=emptyTextTransforms.get(output)
      return !!item && item.sessionID===identity.sessionID && item.messageID===identity.messageID && item.partID===identity.partID && item.text===output.text
    }
    const requestedSystemTransforms = new WeakSet<object>()
    const requestEmptySystemTransform = (output:{system:string[]}) => { requestedSystemTransforms.add(output) }
    const emptySystemTransforms = new WeakMap<object,{sessionID:string;system:string[];bytes:readonly string[]}>()
    const readEmptySystemTransform = (output:{system:string[]},sessionID:string):boolean => {
      const item=emptySystemTransforms.get(output)
      return !!item && item.sessionID===sessionID && output.system===item.system && item.system.length===item.bytes.length && item.system.every((x,i)=>x===item.bytes[i])
    }

    const HOOK_TIMEOUT_MS = 5000
    const CIRCUIT_BREAKER_THRESHOLD = 3
    const hookFailures = new Map<string, number>()

    const trigger = Effect.fn("Plugin.trigger")(function* <
      Name extends TriggerName,
      Input = Parameters<Required<Hooks>[Name]>[0],
      Output = Parameters<Required<Hooks>[Name]>[1],
    >(name: Name, input: Input, output: Output) {
      if (!name) return output
      const s = yield* InstanceState.get(state)
      const fh = yield* freshFileHooks
      const systemOutput = name === "experimental.chat.system.transform" && output && typeof output === "object" && requestedSystemTransforms.has(output) ? output as {system?:unknown} : undefined
      const textOutput = name === "experimental.text.complete" && output && typeof output === "object" && requestedTextTransforms.has(output) ? output as {text?:unknown} : undefined
      const toolOutput = name === "tool.definition" && output && typeof output === "object" && requestedToolDefinitions.has(output) ? output as {description?:unknown;parameters?:unknown} : undefined
      const systemInput = input as {sessionID?:unknown;messageID?:unknown;partID?:unknown;toolID?:unknown}
      const originalToolDescription = toolOutput && typeof toolOutput.description === "string" ? toolOutput.description : undefined
      const originalToolParameters = toolOutput?.parameters
      if (toolOutput) emptyToolDefinitions.delete(toolOutput)
      const originalText = textOutput && typeof textOutput.text === "string" ? textOutput.text : undefined
      if (textOutput) emptyTextTransforms.delete(textOutput)
      const original = systemOutput && Array.isArray(systemOutput.system) && systemOutput.system.every(x=>typeof x==="string") ? systemOutput.system as string[] : undefined
      const originalBytes = original ? [...original] : undefined
      let noHooks = (typeof systemInput?.sessionID === "string" && (!!original ||
        (originalText !== undefined && typeof systemInput.messageID === "string" && typeof systemInput.partID === "string"))) ||
        (!!toolOutput && typeof systemInput?.toolID === "string" && originalToolDescription !== undefined && originalToolParameters !== undefined)
      if (systemOutput) emptySystemTransforms.delete(systemOutput)

      for (const entry of s.hooksWithMeta ?? []) {
        if (systemOutput || textOutput || toolOutput) {
          try {
            const entryDescriptor=entry && Object.getOwnPropertyDescriptor(entry,"hook")
            const hook=entryDescriptor && "value" in entryDescriptor ? entryDescriptor.value : undefined
            const prototype=hook && Object.getPrototypeOf(hook)
            const descriptor=hook && Object.getOwnPropertyDescriptor(hook,name)
            if (!hook || (prototype !== Object.prototype && prototype !== null) ||
                (prototype === Object.prototype && Object.getOwnPropertyDescriptor(Object.prototype,name)) ||
                (descriptor && !("value" in descriptor))) noHooks=false
          } catch { noHooks=false }
        }
        const fn = entry?.hook?.[name] as ((input: Input, output: Output) => unknown) | undefined
        if ((systemOutput || textOutput || toolOutput) && typeof fn === "function") noHooks=false
        if (typeof fn !== "function") continue
        if (!SNAPSHOT_SAFE_REQUEST_HANDLERS.has(fn)) yield* beforeHook
        yield* Effect.promise(async () => fn(input, output))
      }

      for (const entry of fh.meta ?? []) {
        if (systemOutput || textOutput || toolOutput) {
          try {
            const entryDescriptor=entry && Object.getOwnPropertyDescriptor(entry,"hook")
            const hook=entryDescriptor && "value" in entryDescriptor ? entryDescriptor.value : undefined
            const prototype=hook && Object.getPrototypeOf(hook)
            const descriptor=hook && Object.getOwnPropertyDescriptor(hook,name)
            if (!hook || (prototype !== Object.prototype && prototype !== null) ||
                (prototype === Object.prototype && Object.getOwnPropertyDescriptor(Object.prototype,name)) ||
                (descriptor && !("value" in descriptor))) noHooks=false
          } catch { noHooks=false }
        }
        const fn = entry?.hook?.[name] as ((input: Input, output: Output) => unknown) | undefined
        if ((systemOutput || textOutput || toolOutput) && typeof fn === "function") noHooks=false
        if (typeof fn !== "function") continue
        const hookID = entry.hookIDFor(name)

        if ((hookFailures.get(hookID) ?? 0) >= CIRCUIT_BREAKER_THRESHOLD) {
          log.warn("hook circuit-breaker open, skipping", { hook: hookID })
          continue
        }

        yield* beforeHook

        const snapshot = structuredClone(output)
        const failed = yield* Effect.tryPromise({
          try: async () => {
            await Promise.race([
              Promise.resolve(fn(input, output)),
              new Promise<never>((_, reject) =>
                setTimeout(() => reject(new Error(`hook timed out after ${HOOK_TIMEOUT_MS}ms`)), HOOK_TIMEOUT_MS),
              ),
            ])
          },
          catch: (err) => err,
        }).pipe(
          Effect.map(() => false),
          Effect.catch((err) => {
            Object.assign(output as any, snapshot)
            const count = (hookFailures.get(hookID) ?? 0) + 1
            hookFailures.set(hookID, count)
            log.error("file hook failed, output rolled back", {
              hook: hookID,
              event: name,
              error: errorMessage(err),
              consecutiveFailures: count,
              circuitOpen: count >= CIRCUIT_BREAKER_THRESHOLD,
            })
            return Effect.succeed(true)
          }),
        )
        if (!failed) hookFailures.delete(hookID)
      }
      if (noHooks && systemOutput && original && originalBytes && systemOutput.system === original &&
          original.length === originalBytes.length && original.every((x,i)=>x===originalBytes[i]))
        emptySystemTransforms.set(systemOutput,{sessionID:systemInput.sessionID as string,system:original,bytes:originalBytes})
      if (noHooks && textOutput && originalText !== undefined && textOutput.text === originalText &&
          typeof systemInput.sessionID === "string" && typeof systemInput.messageID === "string" && typeof systemInput.partID === "string")
        emptyTextTransforms.set(textOutput,{sessionID:systemInput.sessionID,messageID:systemInput.messageID,partID:systemInput.partID,text:originalText})
      if (noHooks && toolOutput && typeof systemInput.toolID === "string" && originalToolDescription !== undefined &&
          toolOutput.description === originalToolDescription && toolOutput.parameters === originalToolParameters)
        emptyToolDefinitions.set(toolOutput,{toolID:systemInput.toolID,description:originalToolDescription,parameters:originalToolParameters})
      return output
    })

    const list = Effect.fn("Plugin.list")(function* () {
      const s = yield* InstanceState.get(state)
      return s.hooks
    })

    const init = Effect.fn("Plugin.init")(function* () {
      yield* InstanceState.get(state)
      yield* InstanceState.get(fileHookState)
    })

    const reloadFileHooks: Interface["reloadFileHooks"] = Effect.fn("Plugin.reloadFileHooks")(function* () {
      yield* InstanceState.invalidate(fileHookState)
    })

    const provenanceGeneration: NonNullable<Interface["provenanceGeneration"]> = Effect.fn("Plugin.provenanceGeneration")(function* () {
      const loaded = yield* InstanceState.get(state)
      const hooks = yield* InstanceState.get(fileHookState)
      return {plugins:loaded.generation, fileHooks:hooks.generation}
    })
    const recoveryWitness: NonNullable<Interface["recoveryWitness"]> = Effect.fn("Plugin.recoveryWitness")(function* () {
      const loaded = yield* InstanceState.get(state)
      const fileHooks = yield* freshFileHooks
      const collect = (entries: readonly HookEntry[]) => {
        const handlers: Function[] = []
        let safe = true
        for (const entry of entries) {
          const prototype = Object.getPrototypeOf(entry.hook)
          if ((prototype !== Object.prototype && prototype !== null) || Object.getOwnPropertySymbols(entry.hook).length) safe = false
          for (const [name, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(entry.hook))) {
            if (!("value" in descriptor)) { safe = false; continue }
            const value = descriptor.value
            const nested = value && typeof value === "object" ? Object.getOwnPropertyDescriptor(value, "run") : undefined
            if (value && typeof value === "object" && (!nested || !("value" in nested))) safe = false
            const fn = typeof value === "function" ? value : typeof nested?.value === "function" ? nested.value : undefined
            if (!fn) continue
            handlers.push(fn)
            if (!((name === "chat.headers" || name === "chat.params") && SNAPSHOT_SAFE_REQUEST_HANDLERS.has(fn))) safe = false
          }
        }
        return { handlers, safe: safe && handlers.length <= 512 }
      }
      const captured = collect([...(loaded.hooksWithMeta ?? []), ...(fileHooks.meta ?? [])])
      const current = () => Effect.gen(function* () {
        const now = yield* InstanceState.get(state)
        const files = yield* freshFileHooks
        const observed = collect([...(now.hooksWithMeta ?? []), ...(files.meta ?? [])])
        return now.generation === loaded.generation && files.generation === fileHooks.generation
          && observed.handlers.length === captured.handlers.length
          && observed.handlers.every((fn, index) => fn === captured.handlers[index]) && observed.safe === captured.safe
      })
      return Object.freeze({ plugins: loaded.generation, fileHooks: fileHooks.generation,
        handlers: Object.freeze(captured.handlers), noUnclassifiedHandlers: captured.safe, current })
    })
    return Service.of({ trigger, requestEmptyToolDefinition, readEmptyToolDefinition, requestEmptyTextTransform, readEmptyTextTransform, requestEmptySystemTransform, readEmptySystemTransform, list, init, reloadFileHooks, provenanceGeneration, recoveryWitness, triggerActorPreStop, triggerActorPostStop })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(Bus.layer), Layer.provide(Config.defaultLayer))

export * as Plugin from "."
