// Native settings discovery metadata; no configuration values or runtime writers.
export const NATIVE_SETTINGS_REFERENCE = Object.freeze([
  {
    "identity": "native-setting:disabled_providers",
    "path": "disabled_providers",
    "category": "Models and providers",
    "description": "Disabled provider names",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "disabled_providers"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:enabled_providers",
    "path": "enabled_providers",
    "category": "Models and providers",
    "description": "Enabled provider names",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "enabled_providers"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:model",
    "path": "model",
    "category": "Models and providers",
    "description": "Default model route",
    "disposition": "host-equivalent",
    "scope": "session / account",
    "destination": {
      "panel": "added-models",
      "field": "model"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:small_model",
    "path": "small_model",
    "category": "Models and providers",
    "description": "Helper model preference",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "added-models",
      "field": "small_model"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:vision_model",
    "path": "vision_model",
    "category": "Models and providers",
    "description": "Vision model preference",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "added-models",
      "field": "vision_model"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:model_groups",
    "path": "model_groups",
    "category": "Models and providers",
    "description": "Named model groups",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "added-models",
      "field": "model_groups"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:model_groups.{group}.models",
    "path": "model_groups.{group}.models",
    "category": "Models and providers",
    "description": "Configured models in the named group or provider",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "added-models",
      "field": "model_groups.{group}.models"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider",
    "path": "provider",
    "category": "Models and providers",
    "description": "Native provider definitions",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:voice",
    "path": "voice",
    "category": "Models and providers",
    "description": "Native voice model choices",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "voice"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:voice.asr_model",
    "path": "voice.asr_model",
    "category": "Models and providers",
    "description": "Speech recognition model",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "voice.asr_model"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:voice.control_model",
    "path": "voice.control_model",
    "category": "Models and providers",
    "description": "Speech control model",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "voice.control_model"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.id",
    "path": "provider.{provider}.models.{model}.id",
    "category": "Models and providers",
    "description": "Provider model identifier",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.id"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.name",
    "path": "provider.{provider}.models.{model}.name",
    "category": "Models and providers",
    "description": "Provider model display name",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.name"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.family",
    "path": "provider.{provider}.models.{model}.family",
    "category": "Models and providers",
    "description": "Provider model model family",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.family"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.release_date",
    "path": "provider.{provider}.models.{model}.release_date",
    "category": "Models and providers",
    "description": "Provider model release date",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.release_date"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.attachment",
    "path": "provider.{provider}.models.{model}.attachment",
    "category": "Models and providers",
    "description": "Provider model attachment support",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.attachment"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.reasoning",
    "path": "provider.{provider}.models.{model}.reasoning",
    "category": "Models and providers",
    "description": "Provider model reasoning support",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.reasoning"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.temperature",
    "path": "provider.{provider}.models.{model}.temperature",
    "category": "Models and providers",
    "description": "Provider model temperature support",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.temperature"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.tool_call",
    "path": "provider.{provider}.models.{model}.tool_call",
    "category": "Models and providers",
    "description": "Provider model tool call support",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.tool_call"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.interleaved",
    "path": "provider.{provider}.models.{model}.interleaved",
    "category": "Models and providers",
    "description": "Provider model interleaved reasoning",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.interleaved"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.interleaved.field",
    "path": "provider.{provider}.models.{model}.interleaved.field",
    "category": "Models and providers",
    "description": "Provider model reasoning field name",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.interleaved.field"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost",
    "path": "provider.{provider}.models.{model}.cost",
    "category": "Models and providers",
    "description": "Model token pricing metadata",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.input",
    "path": "provider.{provider}.models.{model}.cost.input",
    "category": "Models and providers",
    "description": "Model input pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.input"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.output",
    "path": "provider.{provider}.models.{model}.cost.output",
    "category": "Models and providers",
    "description": "Model output pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.output"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.cache_read",
    "path": "provider.{provider}.models.{model}.cost.cache_read",
    "category": "Models and providers",
    "description": "Model cache read pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.cache_read"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.cache_write",
    "path": "provider.{provider}.models.{model}.cost.cache_write",
    "category": "Models and providers",
    "description": "Model cache write pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.cache_write"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.context_over_200k",
    "path": "provider.{provider}.models.{model}.cost.context_over_200k",
    "category": "Models and providers",
    "description": "Model context over 200k pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.context_over_200k"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.context_over_200k.input",
    "path": "provider.{provider}.models.{model}.cost.context_over_200k.input",
    "category": "Models and providers",
    "description": "Long-context input pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.context_over_200k.input"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.context_over_200k.output",
    "path": "provider.{provider}.models.{model}.cost.context_over_200k.output",
    "category": "Models and providers",
    "description": "Long-context output pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.context_over_200k.output"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.context_over_200k.cache_read",
    "path": "provider.{provider}.models.{model}.cost.context_over_200k.cache_read",
    "category": "Models and providers",
    "description": "Long-context cache read pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.context_over_200k.cache_read"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cost.context_over_200k.cache_write",
    "path": "provider.{provider}.models.{model}.cost.context_over_200k.cache_write",
    "category": "Models and providers",
    "description": "Long-context cache write pricing",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cost.context_over_200k.cache_write"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.limit",
    "path": "provider.{provider}.models.{model}.limit",
    "category": "Models and providers",
    "description": "Provider model limit",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.limit"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.limit.context",
    "path": "provider.{provider}.models.{model}.limit.context",
    "category": "Models and providers",
    "description": "Model context window limit",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.limit.context"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.limit.input",
    "path": "provider.{provider}.models.{model}.limit.input",
    "category": "Models and providers",
    "description": "Model input limit",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.limit.input"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.limit.output",
    "path": "provider.{provider}.models.{model}.limit.output",
    "category": "Models and providers",
    "description": "Model output limit",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.limit.output"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.modalities",
    "path": "provider.{provider}.models.{model}.modalities",
    "category": "Models and providers",
    "description": "Provider model modalities",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.modalities"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.modalities.input",
    "path": "provider.{provider}.models.{model}.modalities.input",
    "category": "Models and providers",
    "description": "Model input modalities",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.modalities.input"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.modalities.output",
    "path": "provider.{provider}.models.{model}.modalities.output",
    "category": "Models and providers",
    "description": "Model output modalities",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.modalities.output"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.experimental",
    "path": "provider.{provider}.models.{model}.experimental",
    "category": "Models and providers",
    "description": "Provider model experimental",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.experimental"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.status",
    "path": "provider.{provider}.models.{model}.status",
    "category": "Models and providers",
    "description": "Provider model status",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.status"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.cachePromptTTL",
    "path": "provider.{provider}.models.{model}.cachePromptTTL",
    "category": "Models and providers",
    "description": "Provider model prompt cache lifetime",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.cachePromptTTL"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.provider",
    "path": "provider.{provider}.models.{model}.provider",
    "category": "Models and providers",
    "description": "Provider model provider",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.provider"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.options",
    "path": "provider.{provider}.models.{model}.options",
    "category": "Models and providers",
    "description": "Provider model provider or extension options",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.options"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.headers",
    "path": "provider.{provider}.models.{model}.headers",
    "category": "Models and providers",
    "description": "HTTP headers; credential values use the existing authentication owner",
    "disposition": "credential-flow",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.headers"
    },
    "reason": "Credential values remain in the existing authentication and redaction flow.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.variants",
    "path": "provider.{provider}.models.{model}.variants",
    "category": "Models and providers",
    "description": "Named model variant definitions",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.variants"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.variants.{variant}.disabled",
    "path": "provider.{provider}.models.{model}.variants.{variant}.disabled",
    "category": "Models and providers",
    "description": "Provider model disabled state",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.variants.{variant}.disabled"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.api",
    "path": "provider.{provider}.api",
    "category": "Models and providers",
    "description": "Provider provider api endpoint",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.api"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.name",
    "path": "provider.{provider}.name",
    "category": "Models and providers",
    "description": "Provider display name",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.name"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.env",
    "path": "provider.{provider}.env",
    "category": "Models and providers",
    "description": "Provider credential environment variable names",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.env"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.id",
    "path": "provider.{provider}.id",
    "category": "Models and providers",
    "description": "Provider identifier",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.id"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.npm",
    "path": "provider.{provider}.npm",
    "category": "Models and providers",
    "description": "Provider provider adapter package",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.npm"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.whitelist",
    "path": "provider.{provider}.whitelist",
    "category": "Models and providers",
    "description": "Provider allowed model names",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.whitelist"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.blacklist",
    "path": "provider.{provider}.blacklist",
    "category": "Models and providers",
    "description": "Provider excluded model names",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.blacklist"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options",
    "path": "provider.{provider}.options",
    "category": "Models and providers",
    "description": "Provider provider or extension options",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options.apiKey",
    "path": "provider.{provider}.options.apiKey",
    "category": "Models and providers",
    "description": "Provider provider api credential",
    "disposition": "credential-flow",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options.apiKey"
    },
    "reason": "Credential values remain in the existing authentication and redaction flow.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options.baseURL",
    "path": "provider.{provider}.options.baseURL",
    "category": "Models and providers",
    "description": "Provider provider base url",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options.baseURL"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options.enterpriseUrl",
    "path": "provider.{provider}.options.enterpriseUrl",
    "category": "Models and providers",
    "description": "Provider enterprise account url",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options.enterpriseUrl"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options.setCacheKey",
    "path": "provider.{provider}.options.setCacheKey",
    "category": "Models and providers",
    "description": "Provider provider prompt cache keys",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options.setCacheKey"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options.timeout",
    "path": "provider.{provider}.options.timeout",
    "category": "Models and providers",
    "description": "Provider request timeout",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options.timeout"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options.headerTimeout",
    "path": "provider.{provider}.options.headerTimeout",
    "category": "Models and providers",
    "description": "Provider response-header timeout",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options.headerTimeout"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options.chunkTimeout",
    "path": "provider.{provider}.options.chunkTimeout",
    "category": "Models and providers",
    "description": "Provider streaming-chunk timeout",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options.chunkTimeout"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry",
    "path": "provider.{provider}.retry",
    "category": "Models and providers",
    "description": "Provider retry",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models",
    "path": "provider.{provider}.models",
    "category": "Models and providers",
    "description": "Configured models in the named group or provider",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.only_configured_models",
    "path": "provider.{provider}.only_configured_models",
    "category": "Models and providers",
    "description": "Provider show only configured provider models",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.only_configured_models"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.request",
    "path": "provider.{provider}.retry.request",
    "category": "Models and providers",
    "description": "Declared retry request setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.request"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.stream",
    "path": "provider.{provider}.retry.stream",
    "category": "Models and providers",
    "description": "Declared retry stream setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.stream"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.maxCandidate",
    "path": "provider.{provider}.retry.maxCandidate",
    "category": "Models and providers",
    "description": "Declared retry maxCandidate setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.maxCandidate"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.maxJudge",
    "path": "provider.{provider}.retry.maxJudge",
    "category": "Models and providers",
    "description": "Declared retry maxJudge setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.maxJudge"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.network",
    "path": "provider.{provider}.retry.network",
    "category": "Models and providers",
    "description": "Declared retry network setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.network"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.server",
    "path": "provider.{provider}.retry.server",
    "category": "Models and providers",
    "description": "Declared retry server setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.server"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.rateLimit",
    "path": "provider.{provider}.retry.rateLimit",
    "category": "Models and providers",
    "description": "Declared retry rateLimit setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.rateLimit"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.unknown",
    "path": "provider.{provider}.retry.unknown",
    "category": "Models and providers",
    "description": "Declared retry unknown setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.unknown"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.jitterRatio",
    "path": "provider.{provider}.retry.jitterRatio",
    "category": "Models and providers",
    "description": "Declared retry jitterRatio setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.jitterRatio"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.{budget}.mode",
    "path": "provider.{provider}.retry.{budget}.mode",
    "category": "Models and providers",
    "description": "Declared retry mode setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.{budget}.mode"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.{budget}.maxRetries",
    "path": "provider.{provider}.retry.{budget}.maxRetries",
    "category": "Models and providers",
    "description": "Declared retry maxRetries setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.{budget}.maxRetries"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.{budget}.deadlineMs",
    "path": "provider.{provider}.retry.{budget}.deadlineMs",
    "category": "Models and providers",
    "description": "Declared retry deadlineMs setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.{budget}.deadlineMs"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.{budget}.noDeadline",
    "path": "provider.{provider}.retry.{budget}.noDeadline",
    "category": "Models and providers",
    "description": "Declared retry noDeadline setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.{budget}.noDeadline"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.{budget}.initialDelayMs",
    "path": "provider.{provider}.retry.{budget}.initialDelayMs",
    "category": "Models and providers",
    "description": "Declared retry initialDelayMs setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.{budget}.initialDelayMs"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.{budget}.maxDelayMs",
    "path": "provider.{provider}.retry.{budget}.maxDelayMs",
    "category": "Models and providers",
    "description": "Declared retry maxDelayMs setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.{budget}.maxDelayMs"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.retry.{budget}.jitterRatio",
    "path": "provider.{provider}.retry.{budget}.jitterRatio",
    "category": "Models and providers",
    "description": "Declared retry jitterRatio setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.retry.{budget}.jitterRatio"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.provider.npm",
    "path": "provider.{provider}.models.{model}.provider.npm",
    "category": "Models and providers",
    "description": "Provider model provider adapter package",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.provider.npm"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.provider.api",
    "path": "provider.{provider}.models.{model}.provider.api",
    "category": "Models and providers",
    "description": "Provider model provider api endpoint",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.provider.api"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.options.{option}",
    "path": "provider.{provider}.options.{option}",
    "category": "Models and providers",
    "description": "Named provider option; values follow that extension or SDK schema",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.options.{option}"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.options.{option}",
    "path": "provider.{provider}.models.{model}.options.{option}",
    "category": "Models and providers",
    "description": "Named model option; values follow that extension or SDK schema",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.options.{option}"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.headers.{header}",
    "path": "provider.{provider}.models.{model}.headers.{header}",
    "category": "Models and providers",
    "description": "HTTP headers; credential values use the existing authentication owner",
    "disposition": "credential-flow",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.headers.{header}"
    },
    "reason": "Credential values remain in the existing authentication and redaction flow.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:provider.{provider}.models.{model}.variants.{variant}.{option}",
    "path": "provider.{provider}.models.{model}.variants.{variant}.{option}",
    "category": "Models and providers",
    "description": "Named model option; values follow that extension or SDK schema",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "services",
      "field": "provider.{provider}.models.{model}.variants.{variant}.{option}"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:model_groups.{group}.default",
    "path": "model_groups.{group}.default",
    "category": "Models and providers",
    "description": "Default route for a named model group",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "added-models",
      "field": "model_groups.{group}.default"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:default_agent",
    "path": "default_agent",
    "category": "Agents and permissions",
    "description": "Native default agent (distinct from Chat, Plan and Agent interaction modes)",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "default_agent"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mode",
    "path": "mode",
    "category": "Agents and permissions",
    "description": "Legacy native agent configuration name",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "mode"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mode.build",
    "path": "mode.build",
    "category": "Agents and permissions",
    "description": "Native mode build",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "mode.build"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mode.plan",
    "path": "mode.plan",
    "category": "Agents and permissions",
    "description": "Native mode plan",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "mode.plan"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent",
    "path": "agent",
    "category": "Agents and permissions",
    "description": "Named native agent definitions",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.plan",
    "path": "agent.plan",
    "category": "Agents and permissions",
    "description": "Native agent plan",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.plan"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.build",
    "path": "agent.build",
    "category": "Agents and permissions",
    "description": "Native agent build",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.build"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.general",
    "path": "agent.general",
    "category": "Agents and permissions",
    "description": "Native agent general",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.general"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.explore",
    "path": "agent.explore",
    "category": "Agents and permissions",
    "description": "Native agent explore",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.explore"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.title",
    "path": "agent.title",
    "category": "Agents and permissions",
    "description": "Native agent title",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.title"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.summary",
    "path": "agent.summary",
    "category": "Agents and permissions",
    "description": "Native agent summary",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.summary"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.compaction",
    "path": "agent.compaction",
    "category": "Agents and permissions",
    "description": "Native agent compaction",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.compaction"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission",
    "path": "permission",
    "category": "Agents and permissions",
    "description": "Ordered native tool authorization rules",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:tools",
    "path": "tools",
    "category": "Agents and permissions",
    "description": "Legacy boolean tool authorization rules",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "tools"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:tool",
    "path": "tool",
    "category": "Agents and permissions",
    "description": "Native tool invocation formats",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "tool"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:tool.invocation_style",
    "path": "tool.invocation_style",
    "category": "Agents and permissions",
    "description": "Default JSON or shell tool invocation style",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "tool.invocation_style"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:tool.invocation_style_by_tool",
    "path": "tool.invocation_style_by_tool",
    "category": "Agents and permissions",
    "description": "Invocation style overrides by tool name",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "tool.invocation_style_by_tool"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.model",
    "path": "agent.{name}.model",
    "category": "Agents and permissions",
    "description": "Native agent model route",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.model"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.variant",
    "path": "agent.{name}.variant",
    "category": "Agents and permissions",
    "description": "Native agent model variant",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.variant"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.temperature",
    "path": "agent.{name}.temperature",
    "category": "Agents and permissions",
    "description": "Native agent sampling temperature",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.temperature"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.top_p",
    "path": "agent.{name}.top_p",
    "category": "Agents and permissions",
    "description": "Native agent top-p sampling",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.top_p"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.prompt",
    "path": "agent.{name}.prompt",
    "category": "Agents and permissions",
    "description": "Native agent agent instructions",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.prompt"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.tools",
    "path": "agent.{name}.tools",
    "category": "Agents and permissions",
    "description": "Native agent tools",
    "disposition": "owner-restricted",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai"
    },
    "reason": "Use the named profile tool allowlist. Legacy arbitrary tool flags are read-only here.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.disable",
    "path": "agent.{name}.disable",
    "category": "Agents and permissions",
    "description": "Native agent agent disabled state",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.disable"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.description",
    "path": "agent.{name}.description",
    "category": "Agents and permissions",
    "description": "Native agent description",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.description"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.mode",
    "path": "agent.{name}.mode",
    "category": "Agents and permissions",
    "description": "Native agent mode",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.mode"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.hidden",
    "path": "agent.{name}.hidden",
    "category": "Agents and permissions",
    "description": "Native agent agent visibility in suggestions",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.hidden"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.options",
    "path": "agent.{name}.options",
    "category": "Agents and permissions",
    "description": "Native agent provider or extension options",
    "disposition": "owner-restricted",
    "scope": "workspace / account",
    "destination": {
      "panel": "added-models"
    },
    "reason": "Configure model and provider options in Added Models. Named profile options are read-only here.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.color",
    "path": "agent.{name}.color",
    "category": "Agents and permissions",
    "description": "Native agent native agent display color",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.color"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.steps",
    "path": "agent.{name}.steps",
    "category": "Agents and permissions",
    "description": "Native agent agent step limit",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.steps"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.tool_allowlist",
    "path": "agent.{name}.tool_allowlist",
    "category": "Agents and permissions",
    "description": "Native agent agent tool allowlist",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.tool_allowlist"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.maxSteps",
    "path": "agent.{name}.maxSteps",
    "category": "Agents and permissions",
    "description": "Native agent legacy agent step limit",
    "disposition": "compatibility",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.maxSteps"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.permission",
    "path": "agent.{name}.permission",
    "category": "Agents and permissions",
    "description": "Native agent permission",
    "disposition": "owner-restricted",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai"
    },
    "reason": "Use the public Agent permission and File Access controls. Named profiles cannot replace those permissions.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.options.{option}",
    "path": "agent.{name}.options.{option}",
    "category": "Agents and permissions",
    "description": "Named agent option; values follow that extension or SDK schema",
    "disposition": "owner-restricted",
    "scope": "account",
    "destination": {
      "panel": "added-models"
    },
    "reason": "Configure model and provider options in Added Models. Named profile options are read-only here.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.{unknown}",
    "path": "agent.{name}.{unknown}",
    "category": "Agents and permissions",
    "description": "Named native agent entry",
    "disposition": "owner-restricted",
    "scope": "account",
    "destination": {
      "panel": "ai"
    },
    "reason": "Named profiles accept only the listed fields. Unknown extension fields are read-only here.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mode.{name}",
    "path": "mode.{name}",
    "category": "Agents and permissions",
    "description": "Named native mode entry",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "mode.{name}"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.{tool-or-pattern}",
    "path": "permission.{tool-or-pattern}",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for tool or pattern",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.{tool-or-pattern}"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.{tool-or-pattern}.{pattern}",
    "path": "permission.{tool-or-pattern}.{pattern}",
    "category": "Agents and permissions",
    "description": "Ordered allow, ask or deny rule for a matching path or command pattern",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.{tool-or-pattern}.{pattern}"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.permission.{tool-or-pattern}",
    "path": "agent.{name}.permission.{tool-or-pattern}",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for tool or pattern",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.permission.{tool-or-pattern}"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:agent.{name}.permission.{tool-or-pattern}.{pattern}",
    "path": "agent.{name}.permission.{tool-or-pattern}.{pattern}",
    "category": "Agents and permissions",
    "description": "Ordered allow, ask or deny rule for a matching path or command pattern",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "agent.{name}.permission.{tool-or-pattern}.{pattern}"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:tools.{tool}",
    "path": "tools.{tool}",
    "category": "Agents and permissions",
    "description": "Named native tools entry",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "tools.{tool}"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.__originalKeys",
    "path": "permission.__originalKeys",
    "category": "Agents and permissions",
    "description": "Original permission rule order (read-only)",
    "disposition": "derived",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "permission.__originalKeys"
    },
    "reason": "Read-only native provenance; not a saved preference.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.read",
    "path": "permission.read",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for read",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.read"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.edit",
    "path": "permission.edit",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for edit",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.edit"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.glob",
    "path": "permission.glob",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for glob",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.glob"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.grep",
    "path": "permission.grep",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for grep",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.grep"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.list",
    "path": "permission.list",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for list",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.list"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.bash",
    "path": "permission.bash",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for bash",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.bash"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.task",
    "path": "permission.task",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for task",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.task"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.actor",
    "path": "permission.actor",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for actor",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.actor"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.external_directory",
    "path": "permission.external_directory",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for external directory",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.external_directory"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.question",
    "path": "permission.question",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for question",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.question"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.webfetch",
    "path": "permission.webfetch",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for webfetch",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.webfetch"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.websearch",
    "path": "permission.websearch",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for websearch",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.websearch"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.codesearch",
    "path": "permission.codesearch",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for codesearch",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.codesearch"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.lsp",
    "path": "permission.lsp",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for lsp",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.lsp"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.doom_loop",
    "path": "permission.doom_loop",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for doom loop",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.doom_loop"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:permission.skill",
    "path": "permission.skill",
    "category": "Agents and permissions",
    "description": "Native allow, ask or deny policy for skill",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "permission.skill"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compaction",
    "path": "compaction",
    "category": "Context and memory",
    "description": "Native context compaction policy",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "compaction"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compaction.auto",
    "path": "compaction.auto",
    "category": "Context and memory",
    "description": "Automatic context compaction",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "compaction.auto"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compaction.prune",
    "path": "compaction.prune",
    "category": "Context and memory",
    "description": "Prune older tool context",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "compaction.prune"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compaction.tail_turns",
    "path": "compaction.tail_turns",
    "category": "Context and memory",
    "description": "Recent turns retained during compaction",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "compaction.tail_turns"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compaction.preserve_recent_tokens",
    "path": "compaction.preserve_recent_tokens",
    "category": "Context and memory",
    "description": "Recent context token budget to preserve",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "compaction.preserve_recent_tokens"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compaction.reserved",
    "path": "compaction.reserved",
    "category": "Context and memory",
    "description": "Token reserve for native compaction",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "compaction.reserved"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compaction.max_context",
    "path": "compaction.max_context",
    "category": "Context and memory",
    "description": "Model-aware maximum context budget",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "compaction.max_context"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint",
    "path": "checkpoint",
    "category": "Context and memory",
    "description": "Native context checkpoint policy",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.thresholds",
    "path": "checkpoint.thresholds",
    "category": "Context and memory",
    "description": "Context thresholds that trigger checkpoint writes",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.thresholds"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.reserved",
    "path": "checkpoint.reserved",
    "category": "Context and memory",
    "description": "Checkpoint token reserve",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.reserved"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.max_writer_failures",
    "path": "checkpoint.max_writer_failures",
    "category": "Context and memory",
    "description": "Maximum checkpoint writer failures",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.max_writer_failures"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.fork",
    "path": "checkpoint.fork",
    "category": "Context and memory",
    "description": "Write checkpoints in a fork when the model supports it",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.fork"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps",
    "path": "checkpoint.push_caps",
    "category": "Context and memory",
    "description": "Checkpoint section token budgets",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.tasks_ledger",
    "path": "checkpoint.push_caps.tasks_ledger",
    "category": "Context and memory",
    "description": "Checkpoint token budget for tasks ledger",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.tasks_ledger"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.focus_task",
    "path": "checkpoint.push_caps.focus_task",
    "category": "Context and memory",
    "description": "Checkpoint token budget for focus task",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.focus_task"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.actor_ledger",
    "path": "checkpoint.push_caps.actor_ledger",
    "category": "Context and memory",
    "description": "Checkpoint token budget for actor ledger",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.actor_ledger"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.memory_titles",
    "path": "checkpoint.push_caps.memory_titles",
    "category": "Context and memory",
    "description": "Checkpoint token budget for memory titles",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.memory_titles"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.global",
    "path": "checkpoint.push_caps.global",
    "category": "Context and memory",
    "description": "Checkpoint token budget for global",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.global"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.checkpoint",
    "path": "checkpoint.push_caps.checkpoint",
    "category": "Context and memory",
    "description": "Checkpoint token budget for checkpoint",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.checkpoint"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.memory",
    "path": "checkpoint.push_caps.memory",
    "category": "Context and memory",
    "description": "Checkpoint token budget for memory",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.memory"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.notes",
    "path": "checkpoint.push_caps.notes",
    "category": "Context and memory",
    "description": "Checkpoint token budget for notes",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.notes"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.design_decisions",
    "path": "checkpoint.push_caps.design_decisions",
    "category": "Context and memory",
    "description": "Checkpoint token budget for design decisions",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.design_decisions"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.open_notes",
    "path": "checkpoint.push_caps.open_notes",
    "category": "Context and memory",
    "description": "Checkpoint token budget for open notes",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.open_notes"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.recent_user",
    "path": "checkpoint.push_caps.recent_user",
    "category": "Context and memory",
    "description": "Checkpoint token budget for recent user",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.recent_user"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.push_caps.recent_user_per_msg",
    "path": "checkpoint.push_caps.recent_user_per_msg",
    "category": "Context and memory",
    "description": "Checkpoint token budget for recent user per msg",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.push_caps.recent_user_per_msg"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.task_archive_days",
    "path": "checkpoint.task_archive_days",
    "category": "Context and memory",
    "description": "Age of completed tasks shown in checkpoint context",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.task_archive_days"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.task_cleanup_days",
    "path": "checkpoint.task_cleanup_days",
    "category": "Context and memory",
    "description": "Legacy name for task archive age",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.task_cleanup_days"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.memory_reconcile_on_search",
    "path": "checkpoint.memory_reconcile_on_search",
    "category": "Context and memory",
    "description": "Reconcile native memory when searching",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.memory_reconcile_on_search"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:checkpoint.memory_search_score_floor",
    "path": "checkpoint.memory_search_score_floor",
    "category": "Context and memory",
    "description": "Minimum native memory search match score",
    "disposition": "incomplete",
    "scope": "session / account",
    "destination": {
      "panel": "ai",
      "field": "checkpoint.memory_search_score_floor"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory",
    "path": "memory",
    "category": "Context and memory",
    "description": "Native memory policy",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "memory"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory.disable_write",
    "path": "memory.disable_write",
    "category": "Context and memory",
    "description": "Disable native memory writes while preserving reads",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "memory.disable_write"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory.cc_index",
    "path": "memory.cc_index",
    "category": "Context and memory",
    "description": "Include the cross-product memory index",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "memory.cc_index"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory.provider",
    "path": "memory.provider",
    "category": "Context and memory",
    "description": "Native memory backend choice (host memory routing remains separate)",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "memory.provider"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory.graph",
    "path": "memory.graph",
    "category": "Context and memory",
    "description": "Native memory graph declarations",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "memory.graph"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory.graph.enabled",
    "path": "memory.graph.enabled",
    "category": "Context and memory",
    "description": "Declared memory graph enabled setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "memory.graph.enabled"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory.graph.model",
    "path": "memory.graph.model",
    "category": "Context and memory",
    "description": "Declared memory graph model setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "memory.graph.model"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory.graph.every_n_turns",
    "path": "memory.graph.every_n_turns",
    "category": "Context and memory",
    "description": "Declared memory graph every n turns setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "memory.graph.every_n_turns"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:memory.graph.min_interval_seconds",
    "path": "memory.graph.min_interval_seconds",
    "category": "Context and memory",
    "description": "Declared memory graph min interval seconds setting",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "memory.graph.min_interval_seconds"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:history",
    "path": "history",
    "category": "Context and memory",
    "description": "Native history search policy",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "history",
      "field": "history"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:dream",
    "path": "dream",
    "category": "Context and memory",
    "description": "Native memory consolidation policy",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "dream"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:dream.auto",
    "path": "dream.auto",
    "category": "Context and memory",
    "description": "Run consolidation when starting a native chat",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "dream.auto"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:dream.interval_days",
    "path": "dream.interval_days",
    "category": "Context and memory",
    "description": "Days between automatic consolidation runs",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "dream.interval_days"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:distill",
    "path": "distill",
    "category": "Context and memory",
    "description": "Native repeated-work distillation policy",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "distill"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:distill.auto",
    "path": "distill.auto",
    "category": "Context and memory",
    "description": "Run distillation when starting a native chat",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "distill.auto"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:distill.interval_days",
    "path": "distill.interval_days",
    "category": "Context and memory",
    "description": "Days between automatic distillation runs",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "distill.interval_days"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:history.kinds",
    "path": "history.kinds",
    "category": "Context and memory",
    "description": "Content kinds included in native history search",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "history",
      "field": "history.kinds"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:command",
    "path": "command",
    "category": "Workspace and extensions",
    "description": "Named native command definitions",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "command"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:skills",
    "path": "skills",
    "category": "Workspace and extensions",
    "description": "Native skill discovery sources",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "skills"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compose",
    "path": "compose",
    "category": "Workspace and extensions",
    "description": "Native Compose document paths",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "compose"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:watcher",
    "path": "watcher",
    "category": "Workspace and extensions",
    "description": "Workspace watcher policy",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "watcher"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:watcher.ignore",
    "path": "watcher.ignore",
    "category": "Workspace and extensions",
    "description": "Workspace patterns ignored by the watcher",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "watcher.ignore"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:snapshot",
    "path": "snapshot",
    "category": "Workspace and extensions",
    "description": "Native file snapshots used by undo and redo",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "snapshot"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:plugin",
    "path": "plugin",
    "category": "Workspace and extensions",
    "description": "Native plugin declarations",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "plugin"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp",
    "path": "mcp",
    "category": "Workspace and extensions",
    "description": "Native MCP connection definitions",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:formatter",
    "path": "formatter",
    "category": "Workspace and extensions",
    "description": "Workspace formatter definitions",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "formatter"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:lsp",
    "path": "lsp",
    "category": "Workspace and extensions",
    "description": "Workspace language server definitions",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "lsp"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:instructions",
    "path": "instructions",
    "category": "Workspace and extensions",
    "description": "Workspace instruction files",
    "disposition": "owner-restricted",
    "scope": "workspace / account",
    "destination": {
      "panel": "general"
    },
    "reason": "Native instruction paths and URLs require the Files authority before reading. Manage workspace instructions through the workspace Hexes owner.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:command.{name}.template",
    "path": "command.{name}.template",
    "category": "Workspace and extensions",
    "description": "Native command command template",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "command.{name}.template"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:command.{name}.description",
    "path": "command.{name}.description",
    "category": "Workspace and extensions",
    "description": "Native command description",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "command.{name}.description"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:command.{name}.agent",
    "path": "command.{name}.agent",
    "category": "Workspace and extensions",
    "description": "Native command agent selection",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "command.{name}.agent"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:command.{name}.model",
    "path": "command.{name}.model",
    "category": "Workspace and extensions",
    "description": "Native command model route",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "command.{name}.model"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:command.{name}.subtask",
    "path": "command.{name}.subtask",
    "category": "Workspace and extensions",
    "description": "Native command run as a delegated task",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "command.{name}.subtask"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:skills.paths",
    "path": "skills.paths",
    "category": "Workspace and extensions",
    "description": "Native skills skill search paths",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "skills.paths"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:skills.urls",
    "path": "skills.urls",
    "category": "Workspace and extensions",
    "description": "Native skills skill source urls",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "skills.urls"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compose.docs",
    "path": "compose.docs",
    "category": "Workspace and extensions",
    "description": "Native compose compose document location",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "compose.docs"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:compose.docs_absolute",
    "path": "compose.docs_absolute",
    "category": "Workspace and extensions",
    "description": "Native compose absolute compose document paths",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "compose.docs_absolute"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[local].type",
    "path": "mcp.{name}[local].type",
    "category": "Workspace and extensions",
    "description": "Local MCP connection type",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[local].type"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[local].command",
    "path": "mcp.{name}[local].command",
    "category": "Workspace and extensions",
    "description": "Local MCP launch command and arguments",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[local].command"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[local].environment",
    "path": "mcp.{name}[local].environment",
    "category": "Workspace and extensions",
    "description": "Local MCP environment variable names and values",
    "disposition": "credential-flow",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[local].environment"
    },
    "reason": "Credential values remain in the existing authentication and redaction flow.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[local].enabled",
    "path": "mcp.{name}[local].enabled",
    "category": "Workspace and extensions",
    "description": "Local MCP enabled state",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[local].enabled"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[local].timeout",
    "path": "mcp.{name}[local].timeout",
    "category": "Workspace and extensions",
    "description": "Local MCP request timeout",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[local].timeout"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].oauth.clientId",
    "path": "mcp.{name}[remote].oauth.clientId",
    "category": "Workspace and extensions",
    "description": "Remote MCP oauth client identifier",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].oauth.clientId"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].oauth.clientSecret",
    "path": "mcp.{name}[remote].oauth.clientSecret",
    "category": "Workspace and extensions",
    "description": "Remote MCP oauth client credential",
    "disposition": "credential-flow",
    "scope": "account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].oauth.clientSecret"
    },
    "reason": "Credential values remain in the existing authentication and redaction flow.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].oauth.scope",
    "path": "mcp.{name}[remote].oauth.scope",
    "category": "Workspace and extensions",
    "description": "Remote MCP oauth scopes",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].oauth.scope"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].oauth.redirectUri",
    "path": "mcp.{name}[remote].oauth.redirectUri",
    "category": "Workspace and extensions",
    "description": "Remote MCP oauth callback uri",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].oauth.redirectUri"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].type",
    "path": "mcp.{name}[remote].type",
    "category": "Workspace and extensions",
    "description": "Remote MCP connection type",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].type"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].url",
    "path": "mcp.{name}[remote].url",
    "category": "Workspace and extensions",
    "description": "Remote MCP service url",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].url"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].enabled",
    "path": "mcp.{name}[remote].enabled",
    "category": "Workspace and extensions",
    "description": "Remote MCP enabled state",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].enabled"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].headers",
    "path": "mcp.{name}[remote].headers",
    "category": "Workspace and extensions",
    "description": "HTTP headers; credential values use the existing authentication owner",
    "disposition": "credential-flow",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].headers"
    },
    "reason": "Credential values remain in the existing authentication and redaction flow.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].oauth",
    "path": "mcp.{name}[remote].oauth",
    "category": "Workspace and extensions",
    "description": "Remote MCP oauth",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].oauth"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].timeout",
    "path": "mcp.{name}[remote].timeout",
    "category": "Workspace and extensions",
    "description": "Remote MCP request timeout",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].timeout"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:formatter.{name}.disabled",
    "path": "formatter.{name}.disabled",
    "category": "Workspace and extensions",
    "description": "Workspace formatter disabled state",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "formatter.{name}.disabled"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:formatter.{name}.command",
    "path": "formatter.{name}.command",
    "category": "Workspace and extensions",
    "description": "Workspace formatter launch command and arguments",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "formatter.{name}.command"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:formatter.{name}.environment",
    "path": "formatter.{name}.environment",
    "category": "Workspace and extensions",
    "description": "Workspace formatter environment variable names and values",
    "disposition": "credential-flow",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "formatter.{name}.environment"
    },
    "reason": "Credential values remain in the existing authentication and redaction flow.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:formatter.{name}.extensions",
    "path": "formatter.{name}.extensions",
    "category": "Workspace and extensions",
    "description": "Workspace formatter file extensions",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "formatter.{name}.extensions"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:lsp.{name}.disabled",
    "path": "lsp.{name}.disabled",
    "category": "Workspace and extensions",
    "description": "Workspace language server disabled state",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "lsp.{name}.disabled"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:lsp.{name}.command",
    "path": "lsp.{name}.command",
    "category": "Workspace and extensions",
    "description": "Workspace language server launch command and arguments",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "lsp.{name}.command"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:lsp.{name}.extensions",
    "path": "lsp.{name}.extensions",
    "category": "Workspace and extensions",
    "description": "Workspace language server file extensions",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "lsp.{name}.extensions"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:lsp.{name}.env",
    "path": "lsp.{name}.env",
    "category": "Workspace and extensions",
    "description": "Workspace language server credential environment variable names",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "lsp.{name}.env"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:lsp.{name}.initialization",
    "path": "lsp.{name}.initialization",
    "category": "Workspace and extensions",
    "description": "Workspace language server language-server initialization options",
    "disposition": "incomplete",
    "scope": "workspace",
    "destination": {
      "panel": "ai",
      "field": "lsp.{name}.initialization"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:command.{name}",
    "path": "command.{name}",
    "category": "Workspace and extensions",
    "description": "Named native command entry",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "ai",
      "field": "command.{name}"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}.enabled",
    "path": "mcp.{name}.enabled",
    "category": "Workspace and extensions",
    "description": "Native mcp enabled state",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}.enabled"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:plugin_origins",
    "path": "plugin_origins",
    "category": "Workspace and extensions",
    "description": "Native plugin source provenance (read-only)",
    "disposition": "derived",
    "scope": "account",
    "destination": {
      "panel": "integrations",
      "field": "plugin_origins"
    },
    "reason": "Read-only native provenance; not a saved preference.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp_origins",
    "path": "mcp_origins",
    "category": "Workspace and extensions",
    "description": "Native MCP source provenance (read-only)",
    "disposition": "derived",
    "scope": "account",
    "destination": {
      "panel": "integrations",
      "field": "mcp_origins"
    },
    "reason": "Read-only native provenance; not a saved preference.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[local].sampling",
    "path": "mcp.{name}[local].sampling",
    "category": "Workspace and extensions",
    "description": "Local MCP mcp sampling permission (deny, ask or allow)",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[local].sampling"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:mcp.{name}[remote].sampling",
    "path": "mcp.{name}[remote].sampling",
    "category": "Workspace and extensions",
    "description": "Remote MCP mcp sampling permission (deny, ask or allow)",
    "disposition": "host-equivalent",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "mcp.{name}[remote].sampling"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": true,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:plugin[]",
    "path": "plugin[]",
    "category": "Workspace and extensions",
    "description": "Native plugin[] plugin[]",
    "disposition": "incomplete",
    "scope": "workspace / account",
    "destination": {
      "panel": "integrations",
      "field": "plugin[]"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:plugin[][1].{option}",
    "path": "plugin[][1].{option}",
    "category": "Workspace and extensions",
    "description": "Named plugin option; values follow that extension or SDK schema",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "integrations",
      "field": "plugin[][1].{option}"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": true,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:$schema",
    "path": "$schema",
    "category": "Runtime and sharing",
    "description": "Configuration schema document identifier",
    "disposition": "validation-metadata",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "$schema"
    },
    "reason": "Used by configuration validation, not a runtime preference.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:logLevel",
    "path": "logLevel",
    "category": "Runtime and sharing",
    "description": "Native diagnostic log level",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "logging",
      "field": "logLevel"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:server",
    "path": "server",
    "category": "Runtime and sharing",
    "description": "Native server listener settings",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "server"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:llmServer",
    "path": "llmServer",
    "category": "Runtime and sharing",
    "description": "Temporary model-server credential lifetimes",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "llmServer"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:share",
    "path": "share",
    "category": "Runtime and sharing",
    "description": "Native sharing policy (publishing requires its existing owner and consent)",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "history",
      "field": "share"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:autoshare",
    "path": "autoshare",
    "category": "Runtime and sharing",
    "description": "Legacy automatic sharing name",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "history",
      "field": "autoshare"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:autoupdate",
    "path": "autoupdate",
    "category": "Runtime and sharing",
    "description": "Embedded engine update policy",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "autoupdate"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:username",
    "path": "username",
    "category": "Runtime and sharing",
    "description": "Native terminal display name (separate from account identity)",
    "disposition": "host-equivalent",
    "scope": "account",
    "destination": {
      "panel": "account",
      "field": "username"
    },
    "reason": "The destination has Open Clank controls; this native preference is separate unless a supported control is explicitly shown.",
    "dynamic": false,
    "availability": "available",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:layout",
    "path": "layout",
    "category": "Runtime and sharing",
    "description": "Legacy terminal layout name",
    "disposition": "compatibility",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "layout"
    },
    "reason": "Legacy input; use its current replacement instead of adding another control.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:enterprise",
    "path": "enterprise",
    "category": "Runtime and sharing",
    "description": "Native enterprise service settings",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "enterprise"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:enterprise.url",
    "path": "enterprise.url",
    "category": "Runtime and sharing",
    "description": "Enterprise service URL",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "enterprise.url"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental",
    "path": "experimental",
    "category": "Runtime and sharing",
    "description": "Native experimental feature settings",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.disable_paste_summary",
    "path": "experimental.disable_paste_summary",
    "category": "Runtime and sharing",
    "description": "Native experimental disable paste summary",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.disable_paste_summary"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.batch_tool",
    "path": "experimental.batch_tool",
    "category": "Runtime and sharing",
    "description": "Native experimental batch tool",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.batch_tool"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.openTelemetry",
    "path": "experimental.openTelemetry",
    "category": "Runtime and sharing",
    "description": "Native experimental open telemetry",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.openTelemetry"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.primary_tools",
    "path": "experimental.primary_tools",
    "category": "Runtime and sharing",
    "description": "Declared primary-tool selection",
    "disposition": "declared-only",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.primary_tools"
    },
    "reason": "This declaration does not have a verified native effect.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.continue_loop_on_deny",
    "path": "experimental.continue_loop_on_deny",
    "category": "Runtime and sharing",
    "description": "Native experimental continue loop on deny",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.continue_loop_on_deny"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.try_best",
    "path": "experimental.try_best",
    "category": "Runtime and sharing",
    "description": "Native experimental try best",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.try_best"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.try_best.edit_window",
    "path": "experimental.try_best.edit_window",
    "category": "Runtime and sharing",
    "description": "Native experimental edit window",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.try_best.edit_window"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.try_best.edit_similarity",
    "path": "experimental.try_best.edit_similarity",
    "category": "Runtime and sharing",
    "description": "Native experimental edit similarity",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.try_best.edit_similarity"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.try_best.edit_matches",
    "path": "experimental.try_best.edit_matches",
    "category": "Runtime and sharing",
    "description": "Native experimental edit matches",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.try_best.edit_matches"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.try_best.action_streak",
    "path": "experimental.try_best.action_streak",
    "category": "Runtime and sharing",
    "description": "Native experimental action streak",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.try_best.action_streak"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.mcp_timeout",
    "path": "experimental.mcp_timeout",
    "category": "Runtime and sharing",
    "description": "Native experimental mcp timeout",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.mcp_timeout"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.predict_next_prompt",
    "path": "experimental.predict_next_prompt",
    "category": "Runtime and sharing",
    "description": "Native next-prompt prediction (separate from the browser composer)",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.predict_next_prompt"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.maxMode",
    "path": "experimental.maxMode",
    "category": "Runtime and sharing",
    "description": "Native parallel inference candidates",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.maxMode"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:experimental.maxMode.candidates",
    "path": "experimental.maxMode.candidates",
    "category": "Runtime and sharing",
    "description": "Parallel candidate count; increases inference work",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "experimental.maxMode.candidates"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:workflow",
    "path": "workflow",
    "category": "Runtime and sharing",
    "description": "Native workflow limits",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "workflow"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:workflow.maxConcurrentAgents",
    "path": "workflow.maxConcurrentAgents",
    "category": "Runtime and sharing",
    "description": "Maximum active workflow agents across the native process",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "workflow.maxConcurrentAgents"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:workflow.maxDepth",
    "path": "workflow.maxDepth",
    "category": "Runtime and sharing",
    "description": "Maximum workflow nesting depth",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "workflow.maxDepth"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:workflow.maxLifecycleAgents",
    "path": "workflow.maxLifecycleAgents",
    "category": "Runtime and sharing",
    "description": "Maximum agents created during one workflow run",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "workflow.maxLifecycleAgents"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:workflow.scriptDeadlineMs",
    "path": "workflow.scriptDeadlineMs",
    "category": "Runtime and sharing",
    "description": "Workflow script time limit in milliseconds",
    "disposition": "incomplete",
    "scope": "account",
    "destination": {
      "panel": "ai",
      "field": "workflow.scriptDeadlineMs"
    },
    "reason": "No Open Clank control is available for this native setting yet.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:server.port",
    "path": "server.port",
    "category": "Runtime and sharing",
    "description": "Native server listener port",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "server.port"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:server.hostname",
    "path": "server.hostname",
    "category": "Runtime and sharing",
    "description": "Native server listener hostname",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "server.hostname"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:server.mdns",
    "path": "server.mdns",
    "category": "Runtime and sharing",
    "description": "Native server local service discovery",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "server.mdns"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:server.mdnsDomain",
    "path": "server.mdnsDomain",
    "category": "Runtime and sharing",
    "description": "Native server local discovery domain",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "server.mdnsDomain"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:server.cors",
    "path": "server.cors",
    "category": "Runtime and sharing",
    "description": "Native server allowed browser origins",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "server.cors"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:llmServer.ttl",
    "path": "llmServer.ttl",
    "category": "Runtime and sharing",
    "description": "Native llm server sliding credential lifetime",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "llmServer.ttl"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  },
  {
    "identity": "native-setting:llmServer.maxAge",
    "path": "llmServer.maxAge",
    "category": "Runtime and sharing",
    "description": "Native llm server absolute credential lifetime",
    "disposition": "operator",
    "scope": "native process / operator",
    "destination": {
      "panel": "system",
      "field": "llmServer.maxAge"
    },
    "reason": "Managed by the native host or terminal operator, not ordinary chat preferences.",
    "dynamic": false,
    "availability": "reference",
    "writable": false,
    "origin": "native configuration"
  }
].map(Object.freeze));

// Symbolic source declarations support review of additions; they are not product controls.
export const NATIVE_SETTINGS_SOURCE_DECLARATIONS = {
  "config.ts": [
    "ConfigDirectoryTypoError.dir",
    "ConfigDirectoryTypoError.path",
    "ConfigDirectoryTypoError.suggestion",
    "InfoSchema.$schema",
    "InfoSchema.agent",
    "InfoSchema.agent.build",
    "InfoSchema.agent.compaction",
    "InfoSchema.agent.explore",
    "InfoSchema.agent.general",
    "InfoSchema.agent.plan",
    "InfoSchema.agent.summary",
    "InfoSchema.agent.title",
    "InfoSchema.autoshare",
    "InfoSchema.autoupdate",
    "InfoSchema.checkpoint",
    "InfoSchema.checkpoint.fork",
    "InfoSchema.checkpoint.max_writer_failures",
    "InfoSchema.checkpoint.memory_reconcile_on_search",
    "InfoSchema.checkpoint.memory_search_score_floor",
    "InfoSchema.checkpoint.push_caps",
    "InfoSchema.checkpoint.push_caps.actor_ledger",
    "InfoSchema.checkpoint.push_caps.checkpoint",
    "InfoSchema.checkpoint.push_caps.design_decisions",
    "InfoSchema.checkpoint.push_caps.focus_task",
    "InfoSchema.checkpoint.push_caps.global",
    "InfoSchema.checkpoint.push_caps.memory",
    "InfoSchema.checkpoint.push_caps.memory_titles",
    "InfoSchema.checkpoint.push_caps.notes",
    "InfoSchema.checkpoint.push_caps.open_notes",
    "InfoSchema.checkpoint.push_caps.recent_user",
    "InfoSchema.checkpoint.push_caps.recent_user_per_msg",
    "InfoSchema.checkpoint.push_caps.tasks_ledger",
    "InfoSchema.checkpoint.reserved",
    "InfoSchema.checkpoint.task_archive_days",
    "InfoSchema.checkpoint.task_cleanup_days",
    "InfoSchema.checkpoint.thresholds",
    "InfoSchema.command",
    "InfoSchema.compaction",
    "InfoSchema.compaction.auto",
    "InfoSchema.compaction.max_context",
    "InfoSchema.compaction.preserve_recent_tokens",
    "InfoSchema.compaction.prune",
    "InfoSchema.compaction.reserved",
    "InfoSchema.compaction.tail_turns",
    "InfoSchema.compose",
    "InfoSchema.default_agent",
    "InfoSchema.disabled_providers",
    "InfoSchema.distill",
    "InfoSchema.distill.auto",
    "InfoSchema.distill.interval_days",
    "InfoSchema.dream",
    "InfoSchema.dream.auto",
    "InfoSchema.dream.interval_days",
    "InfoSchema.enabled_providers",
    "InfoSchema.enterprise",
    "InfoSchema.enterprise.url",
    "InfoSchema.experimental",
    "InfoSchema.experimental.batch_tool",
    "InfoSchema.experimental.continue_loop_on_deny",
    "InfoSchema.experimental.disable_paste_summary",
    "InfoSchema.experimental.maxMode",
    "InfoSchema.experimental.maxMode.candidates",
    "InfoSchema.experimental.mcp_timeout",
    "InfoSchema.experimental.openTelemetry",
    "InfoSchema.experimental.predict_next_prompt",
    "InfoSchema.experimental.primary_tools",
    "InfoSchema.experimental.try_best",
    "InfoSchema.experimental.try_best.action_streak",
    "InfoSchema.experimental.try_best.edit_matches",
    "InfoSchema.experimental.try_best.edit_similarity",
    "InfoSchema.experimental.try_best.edit_window",
    "InfoSchema.formatter",
    "InfoSchema.history",
    "InfoSchema.instructions",
    "InfoSchema.layout",
    "InfoSchema.llmServer",
    "InfoSchema.logLevel",
    "InfoSchema.lsp",
    "InfoSchema.mcp",
    "InfoSchema.mcp.enabled",
    "InfoSchema.memory",
    "InfoSchema.memory.cc_index",
    "InfoSchema.memory.disable_write",
    "InfoSchema.memory.graph",
    "InfoSchema.memory.graph.enabled",
    "InfoSchema.memory.graph.every_n_turns",
    "InfoSchema.memory.graph.min_interval_seconds",
    "InfoSchema.memory.graph.model",
    "InfoSchema.memory.provider",
    "InfoSchema.mode",
    "InfoSchema.mode.build",
    "InfoSchema.mode.plan",
    "InfoSchema.model",
    "InfoSchema.model_groups",
    "InfoSchema.model_groups.default",
    "InfoSchema.model_groups.models",
    "InfoSchema.permission",
    "InfoSchema.plugin",
    "InfoSchema.provider",
    "InfoSchema.server",
    "InfoSchema.share",
    "InfoSchema.skills",
    "InfoSchema.small_model",
    "InfoSchema.snapshot",
    "InfoSchema.tool",
    "InfoSchema.tool.invocation_style",
    "InfoSchema.tool.invocation_style_by_tool",
    "InfoSchema.tools",
    "InfoSchema.username",
    "InfoSchema.vision_model",
    "InfoSchema.voice",
    "InfoSchema.voice.asr_model",
    "InfoSchema.voice.control_model",
    "InfoSchema.watcher",
    "InfoSchema.watcher.ignore",
    "InfoSchema.workflow",
    "InfoSchema.workflow.maxConcurrentAgents",
    "InfoSchema.workflow.maxDepth",
    "InfoSchema.workflow.maxLifecycleAgents",
    "InfoSchema.workflow.scriptDeadlineMs"
  ],
  "agent.ts": [
    "AgentSchema.color",
    "AgentSchema.description",
    "AgentSchema.disable",
    "AgentSchema.hidden",
    "AgentSchema.maxSteps",
    "AgentSchema.mode",
    "AgentSchema.model",
    "AgentSchema.options",
    "AgentSchema.permission",
    "AgentSchema.prompt",
    "AgentSchema.steps",
    "AgentSchema.temperature",
    "AgentSchema.tool_allowlist",
    "AgentSchema.tools",
    "AgentSchema.top_p",
    "AgentSchema.variant"
  ],
  "provider.ts": [
    "Info.api",
    "Info.blacklist",
    "Info.env",
    "Info.id",
    "Info.models",
    "Info.name",
    "Info.npm",
    "Info.only_configured_models",
    "Info.options",
    "Info.options.apiKey",
    "Info.options.baseURL",
    "Info.options.chunkTimeout",
    "Info.options.enterpriseUrl",
    "Info.options.headerTimeout",
    "Info.options.setCacheKey",
    "Info.options.timeout",
    "Info.retry",
    "Info.whitelist",
    "Model.attachment",
    "Model.cachePromptTTL",
    "Model.cost",
    "Model.cost.cache_read",
    "Model.cost.cache_write",
    "Model.cost.context_over_200k",
    "Model.cost.context_over_200k.cache_read",
    "Model.cost.context_over_200k.cache_write",
    "Model.cost.context_over_200k.input",
    "Model.cost.context_over_200k.output",
    "Model.cost.input",
    "Model.cost.output",
    "Model.experimental",
    "Model.family",
    "Model.headers",
    "Model.id",
    "Model.interleaved",
    "Model.interleaved.field",
    "Model.limit",
    "Model.limit.context",
    "Model.limit.input",
    "Model.limit.output",
    "Model.modalities",
    "Model.modalities.input",
    "Model.modalities.output",
    "Model.name",
    "Model.options",
    "Model.provider",
    "Model.provider.api",
    "Model.provider.npm",
    "Model.reasoning",
    "Model.release_date",
    "Model.status",
    "Model.temperature",
    "Model.tool_call",
    "Model.variants",
    "Model.variants.disabled"
  ],
  "permission.ts": [
    "ObjectShape.__originalKeys",
    "ObjectShape.actor",
    "ObjectShape.bash",
    "ObjectShape.codesearch",
    "ObjectShape.doom_loop",
    "ObjectShape.edit",
    "ObjectShape.external_directory",
    "ObjectShape.glob",
    "ObjectShape.grep",
    "ObjectShape.list",
    "ObjectShape.lsp",
    "ObjectShape.question",
    "ObjectShape.read",
    "ObjectShape.skill",
    "ObjectShape.task",
    "ObjectShape.webfetch",
    "ObjectShape.websearch"
  ],
  "retry.ts": [
    "Budget.deadlineMs",
    "Budget.initialDelayMs",
    "Budget.jitterRatio",
    "Budget.maxDelayMs",
    "Budget.maxRetries",
    "Budget.mode",
    "Budget.noDeadline",
    "Info.jitterRatio",
    "Info.maxCandidate",
    "Info.maxJudge",
    "Info.network",
    "Info.rateLimit",
    "Info.request",
    "Info.server",
    "Info.stream",
    "Info.unknown"
  ],
  "command.ts": [
    "Info.agent",
    "Info.description",
    "Info.model",
    "Info.subtask",
    "Info.template"
  ],
  "skills.ts": [
    "Info.paths",
    "Info.urls"
  ],
  "compose.ts": [
    "Info.docs",
    "Info.docs_absolute"
  ],
  "mcp.ts": [
    "Local.command",
    "Local.enabled",
    "Local.environment",
    "Local.sampling",
    "Local.timeout",
    "Local.type",
    "OAuth.clientId",
    "OAuth.clientSecret",
    "OAuth.redirectUri",
    "OAuth.scope",
    "Remote.enabled",
    "Remote.headers",
    "Remote.oauth",
    "Remote.sampling",
    "Remote.timeout",
    "Remote.type",
    "Remote.url"
  ],
  "formatter.ts": [
    "Entry.command",
    "Entry.disabled",
    "Entry.environment",
    "Entry.extensions"
  ],
  "lsp.ts": [
    "Disabled.disabled",
    "Entry.command",
    "Entry.disabled",
    "Entry.env",
    "Entry.extensions",
    "Entry.initialization"
  ],
  "plugin.ts": [],
  "history.ts": [
    "Info.kinds"
  ],
  "server.ts": [
    "Server.cors",
    "Server.hostname",
    "Server.mdns",
    "Server.mdnsDomain",
    "Server.port"
  ],
  "llm-server.ts": [
    "LLMServer.maxAge",
    "LLMServer.ttl"
  ]
};
export const NATIVE_SETTINGS_SOURCE_ROOTS = {
  "config.ts": {
    "InfoSchema": ""
  },
  "agent.ts": {
    "AgentSchema": "agent"
  },
  "provider.ts": {
    "Info": "provider",
    "Model": "provider.models"
  },
  "permission.ts": {
    "ObjectShape": "permission"
  },
  "retry.ts": {
    "Budget": "provider.retry",
    "Info": "provider.retry"
  },
  "command.ts": {
    "Info": "command"
  },
  "skills.ts": {
    "Info": "skills"
  },
  "compose.ts": {
    "Info": "compose"
  },
  "mcp.ts": {
    "Local": "mcp[local]",
    "Remote": "mcp[remote]",
    "OAuth": "mcp[remote].oauth"
  },
  "formatter.ts": {
    "Entry": "formatter"
  },
  "lsp.ts": {
    "Disabled": "lsp",
    "Entry": "lsp"
  },
  "plugin.ts": {},
  "history.ts": {
    "Info": "history"
  },
  "server.ts": {
    "Server": "server"
  },
  "llm-server.ts": {
    "LLMServer": "llmServer"
  }
};
export const NATIVE_SETTINGS_SOURCE_EXCLUSIONS = { 'config.ts:ConfigDirectoryTypoError': 'Native diagnostic error payload, outside Config.Info settings' };

function templatePattern(template) {
  const value = template.split(/(\{[^}]+\}|\[\])/g).map(part => {
    if (part.startsWith('{') && part.endsWith('}')) return '.+?';
    if (part === '[]') return '\\[\\d+\\]';
    return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }).join('');
  return new RegExp('^' + value + '$');
}

const locked = new Set(['declared-only','derived','compatibility','validation-metadata','credential-flow','operator','owner-restricted']);

export function getNativeSettingsReference({ controls = [], translate = value => value } = {}) {
  return NATIVE_SETTINGS_REFERENCE.map(original => {
    let row = { ...original, destination: { ...original.destination } };
    const exactControl = controls.find(item => (item.path || item.nativePath || item.field) === row.path);
    const control = exactControl || controls.find(item => Array.isArray(item.referencePaths) && item.referencePaths.some(path =>
        typeof path === 'string' && path.replace(/\{[^}]+\}/g, '*') === row.path.replace(/\{[^}]+\}/g, '*')));
    if (exactControl?.supported === false) {
      row = { ...row, availability: 'unavailable', writable: false, reason: exactControl.reason || row.reason,
        destination: exactControl.destination || row.destination, scope: exactControl.scope || row.scope };
    }
    if (control && control.supported !== false && (!locked.has(row.disposition) || control.overrideDisposition === true)) {
      row = { ...row, disposition: 'configured-control', availability: control.availability || 'available', writable: control.writable === true,
        scope: control.scope || row.scope, reason: control.reason || '', destination: control.destination || { panel: 'ai', field: control.path || control.nativePath || control.field } };
    }
    const originalDescription = row.description;
    const originalCategory = row.category;
    const originalReason = row.reason;
    try { row.description = String(translate(row.description) ?? row.description); row.category = String(translate(row.category) ?? row.category); row.reason = String(translate(row.reason) ?? row.reason); } catch { /* Preserve authored text during i18n startup. */ }
    row.searchTerms = [row.path,originalDescription,originalCategory,originalReason,row.description,row.category,row.reason,row.scope];
    return row;
  });
}

export function findNativeSettingReferences(path, options = {}) {
  const exact = getNativeSettingsReference(options).filter(row => row.path === path);
  if (exact.length) return exact;
  return getNativeSettingsReference(options).filter(row => {
    if (options.transport && row.path.includes('[local]') && options.transport !== 'local') return false;
    if (options.transport && row.path.includes('[remote]') && options.transport !== 'remote') return false;
    const template = row.path.replace(/\[(?:local|remote)\]/g, '');
    return row.dynamic && templatePattern(template).test(path);
  });
}

export function findNativeSettingReference(path, options = {}) {
  const rows = findNativeSettingReferences(path, options);
  return rows.length ? { ...rows[0], ambiguous: rows.length > 1, alternatives: rows.slice(1).map(row => row.path) } : null;
}

export function searchNativeSettingsReference(query = '', options = {}) {
  const needle = String(query).toLowerCase().trim();
  return getNativeSettingsReference(options).filter(row => !needle || row.searchTerms.some(term => String(term).toLowerCase().includes(needle)));
}

export function unclassifiedNativeSettingDeclarations(module, declarations) {
  const known = new Set(NATIVE_SETTINGS_SOURCE_DECLARATIONS[module] || []);
  return declarations.filter(path => !known.has(path));
}
