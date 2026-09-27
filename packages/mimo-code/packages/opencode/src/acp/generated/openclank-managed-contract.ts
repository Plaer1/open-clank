// Generated managed-provider contract constants; edit the JSON schema instead.
export const PROTOCOL_VERSION = 1 as const
export const PROVIDER_STORE_VERSION = 1 as const
export const OPERATION_ROUTER_VERSION = 1 as const
export const SCHEMA_VERSION = 2 as const
export const SCHEMA_ID = "https://openclank.dev/contracts/managed-provider/v2" as const
export const SCHEMA_HASH = "611c378cc61719f6290cd28e9c383b5a9f398fc174785c7c84b5269f366073ca" as const

export const PROVIDER_STORE_METHODS = [
  "_openclank/provider-store/v1/account/bind",
  "_openclank/provider-store/v1/account/commit",
  "_openclank/provider-store/v1/account/attempt",
  "_openclank/provider-store/v1/credential/lease",
  "_openclank/provider-store/v1/credential/replace",
  "_openclank/provider-store/v1/refresh/acquire",
  "_openclank/provider-store/v1/refresh/renew",
  "_openclank/provider-store/v1/refresh/commit",
  "_openclank/provider-store/v1/refresh/abort",
] as const
export const PROVIDER_CONTROL_METHODS = [
  "_openclank/provider-control/v1/catalog",
  "_openclank/provider-control/v1/connection/validate",
  "_openclank/provider-control/v1/account/validate",
  "_openclank/provider-control/v1/oauth/start",
  "_openclank/provider-control/v1/oauth/poll",
  "_openclank/provider-control/v1/oauth/callback",
  "_openclank/provider-control/v1/oauth/cancel",
] as const
export const OPERATION_METHODS = [
  "_openclank/operations/v1/journal/cas",
  "_openclank/operations/v1/artifact/read",
  "_openclank/operations/v1/artifact/write",
  "_openclank/operations/v1/executor/invoke",
  "_openclank/operations/v1/execute",
  "_openclank/operations/v1/cancel",
] as const
export const SESSION_METHODS = [
  "_openclank/session/v1/cwd/change",
  "_openclank/session/v1/binding/read",
  "_openclank/session/v1/settings/effective",
] as const
export const OPERATIONS = [
  "chat.stream",
  "chat.complete",
  "web.search",
  "vision.describe",
  "image.generate",
  "image.edit",
  "image.inpaint",
  "image.img2img",
  "image.upscale",
  "image.denoise",
  "image.segment",
  "image.remove_background",
  "image.restore_face",
  "audio.synthesize",
  "audio.transcribe",
  "embeddings.create",
] as const
export const METHOD_DIRECTIONS = {
  "_openclank/provider-store/v1/account/bind": "engine_to_host",
  "_openclank/provider-store/v1/account/commit": "engine_to_host",
  "_openclank/provider-store/v1/account/attempt": "engine_to_host",
  "_openclank/provider-store/v1/credential/lease": "engine_to_host",
  "_openclank/provider-store/v1/credential/replace": "engine_to_host",
  "_openclank/provider-store/v1/refresh/acquire": "engine_to_host",
  "_openclank/provider-store/v1/refresh/renew": "engine_to_host",
  "_openclank/provider-store/v1/refresh/commit": "engine_to_host",
  "_openclank/provider-store/v1/refresh/abort": "engine_to_host",
  "_openclank/provider-control/v1/catalog": "host_to_engine",
  "_openclank/provider-control/v1/connection/validate": "host_to_engine",
  "_openclank/provider-control/v1/account/validate": "host_to_engine",
  "_openclank/provider-control/v1/oauth/start": "host_to_engine",
  "_openclank/provider-control/v1/oauth/poll": "host_to_engine",
  "_openclank/provider-control/v1/oauth/callback": "host_to_engine",
  "_openclank/provider-control/v1/oauth/cancel": "host_to_engine",
  "_openclank/operations/v1/journal/cas": "engine_to_host",
  "_openclank/operations/v1/artifact/read": "engine_to_host",
  "_openclank/operations/v1/artifact/write": "engine_to_host",
  "_openclank/operations/v1/executor/invoke": "engine_to_host",
  "_openclank/operations/v1/execute": "host_to_engine",
  "_openclank/operations/v1/cancel": "host_to_engine",
  "_openclank/session/v1/cwd/change": "engine_to_host",
  "_openclank/session/v1/binding/read": "engine_to_host",
  "_openclank/session/v1/settings/effective": "host_to_engine",
} as const

export type ProviderStoreMethod = (typeof PROVIDER_STORE_METHODS)[number]
export type ProviderControlMethod = (typeof PROVIDER_CONTROL_METHODS)[number]
export type OperationMethod = (typeof OPERATION_METHODS)[number]
export type SessionMethod = (typeof SESSION_METHODS)[number]
export type ManagedMethod = ProviderStoreMethod | ProviderControlMethod | OperationMethod | SessionMethod
export type Operation = (typeof OPERATIONS)[number]
