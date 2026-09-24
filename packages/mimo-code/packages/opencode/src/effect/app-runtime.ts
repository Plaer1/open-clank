import { Layer, ManagedRuntime } from "effect"
import { attach } from "./run-service"
import * as Observability from "./observability"

import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Bus } from "@/bus"
import { Auth } from "@/auth"
import { Account } from "@/account/account"
import { Config } from "@/config"
import { Git } from "@/git"
import { Ripgrep } from "@/file/ripgrep"
import { File } from "@/file"
import { FileWatcher } from "@/file/watcher"
import { Storage } from "@/storage"
import { Snapshot } from "@/snapshot"
import { Plugin } from "@/plugin"
import { Provider } from "@/provider"
import { ProviderAuth } from "@/provider"
import { Agent } from "@/agent/agent"
import { Skill } from "@/skill"
import { Discovery } from "@/skill/discovery"
import { Question } from "@/question"
import { Permission } from "@/permission"
import { Todo } from "@/session/todo"
import { Session } from "@/session"
import { SessionStatus } from "@/session/status"
import { SessionRunState } from "@/session/run-state"
import { Goal } from "@/session/goal"
import { SessionProcessor } from "@/session/processor"
import { SessionCompaction } from "@/session/compaction"
import { SessionPrune } from "@/session/prune"
import { SessionRevert } from "@/session/revert"
import { SessionSummary } from "@/session/summary"
import { SessionPrompt } from "@/session/prompt"
import { defaultLayer as CronBridgeDefaultLayer } from "@/session/cron-bridge"
import { SessionCheckpoint } from "@/session/checkpoint"
import { Instruction } from "@/session/instruction"
import { LLM } from "@/session/llm"
import { LSP } from "@/lsp"
import { MCP } from "@/mcp"
import { McpAuth } from "@/mcp/auth"
import { Command } from "@/command"
import { Truncate } from "@/tool"
import { ToolRegistry } from "@/tool"
import { Format } from "@/format"
import { Project } from "@/project"
import { Vcs } from "@/project"
import { Worktree } from "@/worktree"
import { Pty } from "@/pty"
import { Installation } from "@/installation"
import { ShareNext } from "@/share"
import { SessionShare } from "@/share"
import { Npm } from "@/npm"
import { ActorRegistry } from "@/actor/registry"
import { ActorWaiter } from "@/actor/waiter"
import { Actor } from "@/actor/spawn"
import { TaskRegistry } from "@/task/registry"
import { WorkflowRuntime } from "@/workflow/runtime"
import { History } from "@/history"
import { Memory } from "@/memory"
import * as MemoryCapture from "@/memory/capture"
import * as CompactionCapture from "@/memory/compaction-capture"
import * as BashInteractive from "@/tool/bash-interactive"
import { memoMap } from "./memo-map"

// Wrapped in Layer.suspend so the cross-module `.defaultLayer` reads defer to
// first use instead of running at module load — same TDZ fix as Actor.appLayer.
//
// Init order (tests and routes share this):
//   1. Module graph loads. Every `defaultLayer` / `appLayer` in the Actor /
//      SessionPrompt / Command / MCP chain is `Layer.suspend`, so no
//      cross-module `.defaultLayer` read runs during init. Actor.appLayer and
//      SessionPrompt.appLayer are defined at the bottom of their modules
//      (after `layer`), which is "after bootstrap" for ESM purposes.
//   2. First AppRuntime.run* call materialises ManagedRuntime.make(AppLayer)
//      below. That is the true bootstrap point: the AppLayer thunk runs only
//      once every module export is initialized, so `Actor.appLayer` cannot be
//      in TDZ and combined test runs stop cross-polluting each other's
//      half-initialized layers.
//   3. AppLayer owns the chain once:
//        Actor.appLayer ← SessionPrompt.appLayer ← Command.appLayer ← MCP default layer
//      Each appLayer leaves its MCP-bearing dependency unmet (see
//      test/effect/app-runtime-mcp-singleton.test.ts).
export const AppLayer = Layer.suspend(() =>
  Layer.mergeAll(
    Npm.defaultLayer,
    AppFileSystem.defaultLayer,
    Bus.defaultLayer,
    Auth.defaultLayer,
    Account.defaultLayer,
    Config.defaultLayer,
    Git.defaultLayer,
    Ripgrep.defaultLayer,
    File.defaultLayer,
    FileWatcher.defaultLayer,
    Storage.defaultLayer,
    Snapshot.defaultLayer,
    Plugin.defaultLayer,
    Provider.defaultLayer,
    ProviderAuth.defaultLayer,
    Agent.defaultLayer,
    Skill.defaultLayer,
    Discovery.defaultLayer,
    Question.defaultLayer,
    Permission.defaultLayer,
    Todo.defaultLayer,
    Session.defaultLayer,
    SessionStatus.defaultLayer,
    SessionRunState.defaultLayer,
    Goal.defaultLayer,
    SessionProcessor.defaultLayer,
    SessionCompaction.defaultLayer,
    SessionPrune.defaultLayer,
    SessionRevert.defaultLayer,
    SessionSummary.defaultLayer,
    CronBridgeDefaultLayer,
    SessionCheckpoint.defaultLayer,
    Instruction.defaultLayer,
    LLM.defaultLayer,
    LSP.defaultLayer,
    McpAuth.defaultLayer,
    Truncate.defaultLayer,
    ToolRegistry.defaultLayer,
    Format.defaultLayer,
    Project.defaultLayer,
    Vcs.defaultLayer,
    Worktree.defaultLayer,
    Pty.defaultLayer,
    Installation.defaultLayer,
    ShareNext.defaultLayer,
    SessionShare.defaultLayer,
    ActorRegistry.defaultLayer,
    ActorWaiter.defaultLayer,
    TaskRegistry.defaultLayer,
    WorkflowRuntime.defaultLayer,
    Memory.defaultLayer,
    History.defaultLayer,
    // InstanceBootstrap (middleware/httpapi/worker init) depends on these two.
    // They sit on BootstrapLayer; AppLayer must own them too or every
    // AppRuntime.runPromise(InstanceBootstrap) 500s with a missing service.
    MemoryCapture.defaultLayer,
    CompactionCapture.defaultLayer,
    // MCP, Command, SessionPrompt, and Actor form one ownership chain. Their
    // standalone default layers remain convenient for focused tests, while
    // the application graph deliberately provides each stateful service once.
    Actor.appLayer.pipe(
      Layer.provideMerge(SessionPrompt.appLayer.pipe(
        Layer.provideMerge(Command.appLayer.pipe(Layer.provideMerge(MCP.defaultLayer))),
      )),
    ),
  ).pipe(Layer.provideMerge(Observability.layer), Layer.provideMerge(BashInteractive.defaultLayer)),
)

// Lazy: constructing ManagedRuntime at module load would evaluate AppLayer
// (and thus Actor.appLayer) during the import graph's init, which is exactly
// the TDZ window. First use is after bootstrap — see init-order comment above.
const makeRuntime = () => ManagedRuntime.make(AppLayer, { memoMap })
type RuntimeInstance = ReturnType<typeof makeRuntime>
type Runtime = Pick<RuntimeInstance, "runSync" | "runPromise" | "runPromiseExit" | "runFork" | "runCallback" | "dispose">
let rt: RuntimeInstance | undefined
const activeRuntime = () => (rt ??= makeRuntime())
const wrap = (effect: Parameters<RuntimeInstance["runSync"]>[0]) => attach(effect as never) as never

export const AppRuntime: Runtime = {
  runSync(effect) {
    return activeRuntime().runSync(wrap(effect))
  },
  runPromise(effect, options) {
    return activeRuntime().runPromise(wrap(effect), options)
  },
  runPromiseExit(effect, options) {
    return activeRuntime().runPromiseExit(wrap(effect), options)
  },
  runFork(effect) {
    return activeRuntime().runFork(wrap(effect))
  },
  runCallback(effect) {
    return activeRuntime().runCallback(wrap(effect))
  },
  dispose: () => {
    const active = rt
    rt = undefined
    return active ? active.dispose() : Promise.resolve()
  },
}
