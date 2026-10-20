import { Deferred, Effect, Layer, Context } from "effect"
import { Bus } from "@/bus"
import { BusEvent } from "@/bus/bus-event"
import { InstanceState } from "@/effect"
import { Log } from "@/util"
import z from "zod"
import crypto from "crypto"

const log = Log.create({ service: "bash-interactive" })

// Bus events

export const Event = {
  Asked: BusEvent.define(
    "bash.interactive.asked",
    z.object({
      id: z.string(),
      sessionID: z.string(),
      callID: z.string(),
      command: z.string(),
      cwd: z.string(),
      workspace: z.string(),
      writableRoots: z.array(z.string()),
      shell: z.string(),
      network: z.enum(["enabled", "disabled"]),
      timeout: z.number(),
      env: z.record(z.string(), z.string()).optional(),
      description: z.string(),
    }),
  ),
  Cancelled: BusEvent.define(
    "bash.interactive.cancelled",
    z.object({
      id: z.string(),
      reason: z.string(),
    }),
  ),
  Replied: BusEvent.define(
    "bash.interactive.replied",
    z.object({
      id: z.string(),
      sessionID: z.string(),
      callID: z.string(),
      output: z.string(),
      exitCode: z.number(),
    }),
  ),
}

// Types

export interface InteractiveRequest {
  id: string
  sessionID: string
  callID: string
  command: string
  cwd: string
  workspace: string
  writableRoots: string[]
  shell: string
  network: "enabled" | "disabled"
  timeout: number
  env?: Record<string, string>
  description: string
}

export interface InteractiveResult {
  output: string
  exitCode: number
}

export class InteractiveError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "BashInteractiveError"
  }
}

// Service

interface PendingEntry {
  request: InteractiveRequest
  deferred: Deferred.Deferred<InteractiveResult, InteractiveError>
}

interface State {
  pending: Map<string, PendingEntry>
}

export interface Interface {
  readonly request: (input: {
    sessionID: string
    callID: string
    command: string
    cwd: string
    workspace: string
    writableRoots: string[]
    shell: string
    network?: "enabled" | "disabled"
    timeout: number
    env?: Record<string, string>
    description: string
  }) => Effect.Effect<InteractiveResult, InteractiveError>
  readonly reply: (input: {
    id: string
    sessionID: string
    callID: string
    output: string
    exitCode: number
  }) => Effect.Effect<void, InteractiveError>
  readonly list: () => Effect.Effect<ReadonlyArray<InteractiveRequest>>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/BashInteractive") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service
    const state = yield* InstanceState.make<State>(
      Effect.fn("BashInteractive.state")(function* () {
        const state: State = {
          pending: new Map(),
        }

        yield* Effect.addFinalizer(() =>
          Effect.gen(function* () {
            for (const [id, item] of state.pending) {
              yield* bus.publish(Event.Cancelled, {
                id,
                reason: "instance disposed",
              })
              yield* Deferred.fail(item.deferred, new InteractiveError("Instance disposed"))
            }
            state.pending.clear()
          }),
        )

        return state
      }),
    )

    const request = Effect.fn("BashInteractive.request")(function* (input: {
      sessionID: string
      callID: string
      command: string
      cwd: string
      workspace: string
      writableRoots: string[]
      shell: string
      network?: "enabled" | "disabled"
      timeout: number
      env?: Record<string, string>
      description: string
    }) {
      const pending = (yield* InstanceState.get(state)).pending
      const id = crypto.randomUUID()
      log.info("requesting interactive", { id, command: input.command })

      const deferred = yield* Deferred.make<InteractiveResult, InteractiveError>()
      const req: InteractiveRequest = {
        id,
        sessionID: input.sessionID,
        callID: input.callID,
        command: input.command,
        cwd: input.cwd,
        workspace: input.workspace,
        writableRoots: input.writableRoots,
        shell: input.shell,
        network: input.network ?? "enabled",
        timeout: input.timeout,
        env: input.env,
        description: input.description,
      }
      pending.set(id, { request: req, deferred })
      yield* bus.publish(Event.Asked, req)

      const result = Deferred.await(deferred).pipe(
        Effect.timeout(input.timeout),
        Effect.catchTag(
          "TimeoutError",
          () => Effect.fail(new InteractiveError(`Interactive command timed out after ${input.timeout} ms`)),
        ),
      )
      return yield* Effect.ensuring(
        result,
        Effect.gen(function* () {
          if (!pending.has(id)) return
          pending.delete(id)
          yield* bus.publish(Event.Cancelled, {
            id,
            reason: "request ended before the interactive process replied",
          })
        }),
      )
    })

    const reply = Effect.fn("BashInteractive.reply")(function* (input: {
      id: string
      sessionID: string
      callID: string
      output: string
      exitCode: number
    }) {
      const pending = (yield* InstanceState.get(state)).pending
      const existing = pending.get(input.id)
      if (!existing) {
        log.warn("reply for unknown request", { id: input.id })
        return yield* Effect.fail(new InteractiveError("Interactive request is no longer pending"))
      }
      if (
        existing.request.sessionID !== input.sessionID ||
        existing.request.callID !== input.callID
      ) {
        return yield* Effect.fail(new InteractiveError("Interactive reply does not match its session and tool call"))
      }
      pending.delete(input.id)
      log.info("replied", { id: input.id, exitCode: input.exitCode })
      yield* bus.publish(Event.Replied, {
        id: existing.request.id,
        sessionID: existing.request.sessionID,
        callID: existing.request.callID,
        output: input.output,
        exitCode: input.exitCode,
      })
      yield* Deferred.succeed(existing.deferred, {
        output: input.output,
        exitCode: input.exitCode,
      })
    })

    const list = Effect.fn("BashInteractive.list")(function* () {
      const pending = (yield* InstanceState.get(state)).pending
      return Array.from(pending.values(), (x) => x.request)
    })

    return Service.of({ request, reply, list })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(Bus.layer))

// Standalone functions (uses the instance-scoped runtime, same pattern as Bus module)
import { makeRuntime } from "@/effect/run-service"

const { runPromise } = makeRuntime(Service, defaultLayer)

export function request(input: {
  sessionID: string
  callID: string
  command: string
  cwd: string
  workspace: string
  writableRoots: string[]
  shell: string
  network?: "enabled" | "disabled"
  timeout: number
  env?: Record<string, string>
  description: string
}): Promise<InteractiveResult> {
  return runPromise((svc) => svc.request(input))
}

export function reply(input: {
  id: string
  sessionID: string
  callID: string
  output: string
  exitCode: number
}): Promise<void> {
  return runPromise((svc) => svc.reply(input))
}

export function list(): Promise<ReadonlyArray<InteractiveRequest>> {
  return runPromise((svc) => svc.list())
}
