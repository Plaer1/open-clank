import { Context } from "effect"

/** Optional in-process embedding port. No application policy lives in the engine. */
export interface ModelCallScope {
  sessionID: string
  userMessageID: string
  assistantMessageID?: string
  executionAttempt?: ExecutionAttempt
  directory?: string
  providerID: string
  modelID: string
  sdk: string
  agent: string
  ephemeral: boolean
  format?: string
}

export interface ExecutionAttempt { requestId: string; attemptId: string }
export interface ExecutionAdmission {
  attempt: ExecutionAttempt
  directory: string
  sessionID: string
  userMessageID: string
  target: { userMessageID?: string; assistantMessageID?: string }
}

export const ExecutionScope = Context.Reference<Omit<ExecutionAdmission, "target"> | undefined>("~opencode/HostExecutionScope", {
  defaultValue: () => undefined,
})

export interface ExecutionActivation { beforeWork: () => Promise<void> }
export interface HostTransport {
  prepareExecution?(input: ExecutionAdmission): void | Promise<void>
  admitExecution?(input: ExecutionAdmission): ExecutionActivation | void
  finishExecution?(attempt: ExecutionAttempt): void
  userMessage(input: { sessionID: string; userMessageID: string; parts: readonly unknown[] }): void
  modelCall<T>(scope: ModelCallScope, call: () => Promise<T>): Promise<T>
  request?(input: unknown, init: unknown, forward: () => Promise<Response>): Promise<Response>
}

let host: HostTransport | undefined
export function set(value: HostTransport | undefined) { host = value }
export function userMessage(input: Parameters<HostTransport["userMessage"]>[0]) { host?.userMessage(input) }
export function prepareExecution(input: ExecutionAdmission) {
  if (!host?.admitExecution) throw new Error("Execution host is unavailable")
  return host.prepareExecution?.(input)
}
export function admitExecution(input: ExecutionAdmission) {
  if (!host?.admitExecution) throw new Error("Execution host is unavailable")
  return host.admitExecution(input)
}
export function finishExecution(attempt: ExecutionAttempt) { host?.finishExecution?.(attempt) }
export function modelCall<T>(scope: ModelCallScope, call: () => Promise<T>): Promise<T> {
  return host ? host.modelCall(scope, call) : call()
}
export function request(input: unknown, init: unknown, forward: () => Promise<Response>): Promise<Response> {
  return host?.request ? host.request(input, init, forward) : forward()
}
export * as HostModelTransport from "./host-transport"
