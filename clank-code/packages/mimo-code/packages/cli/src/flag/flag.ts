import { Config } from "effect"
import { closeSync, readFileSync } from "node:fs"
import { consumeWindowsServerPassword } from "./worker-auth-windows"

export function consumeInheritedServerPassword(fdValue?: string): string | undefined {
  const envName = "OPEN_CLANK_WORKER_AUTH_FD"
  const configured = fdValue !== undefined || Object.prototype.hasOwnProperty.call(process.env, envName)
  const rawFD = fdValue ?? process.env[envName]
  delete process.env["OPEN_CLANK_WORKER_AUTH_FD"]
  if (!configured) return undefined
  if (!rawFD || !/^\d+$/.test(rawFD)) {
    throw new Error("Invalid Open Clank worker-auth descriptor")
  }
  const fd = Number(rawFD)
  if (!Number.isSafeInteger(fd) || fd < 3) {
    throw new Error("Invalid Open Clank worker-auth descriptor")
  }
  let password: string
  try {
    password = readFileSync(fd, "utf8")
  } catch {
    throw new Error("Unable to read Open Clank worker-auth descriptor")
  } finally {
    try {
      closeSync(fd)
    } catch {}
  }
  if (password.length === 0 || password.length > 1024) {
    throw new Error("Invalid Open Clank worker-auth secret")
  }
  return password
}

function truthy(key: string) {
  const value = process.env[key]?.toLowerCase()
  return value === "true" || value === "1"
}

function falsy(key: string) {
  const value = process.env[key]?.toLowerCase()
  return value === "false" || value === "0"
}

function number(key: string) {
  const value = process.env[key]
  if (!value) return undefined
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined
}

function nonNegativeNumber(key: string) {
  const value = process.env[key]
  if (!value) return undefined
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : undefined
}

// A fraction in (0, 1], written either as a decimal ("0.85") or a percentage
// ("85%"). Values outside the range — and anything unparseable — yield undefined
// so the caller keeps its own default.
function ratio(key: string) {
  const value = process.env[key]?.trim()
  if (!value) return undefined
  const parsed = value.endsWith("%") ? Number(value.slice(0, -1)) / 100 : Number(value)
  return Number.isFinite(parsed) && parsed > 0 && parsed <= 1 ? parsed : undefined
}

const OPENCLANK_EXPERIMENTAL = truthy("OPENCLANK_EXPERIMENTAL")

// Defaults to false. When enabled, openclank runs in pure-mimo mode:
//   — does NOT inherit Claude Code prompt files (CLAUDE.md, ~/.claude/CLAUDE.md)
//   — does NOT pick up provider API keys from environment variables
// Set OPENCLANK_ISOLATED_CONFIG=true to disable .claude inheritance and env-based
// provider auto-detection.
const OPENCLANK_ISOLATED_CONFIG = truthy("OPENCLANK_ISOLATED_CONFIG")
const OPENCLANK_DISABLE_CLAUDE_CODE_ENV = truthy("OPENCLANK_DISABLE_CLAUDE_CODE")
const OPENCLANK_DISABLE_CLAUDE_CODE = OPENCLANK_ISOLATED_CONFIG || OPENCLANK_DISABLE_CLAUDE_CODE_ENV

// External skill roots:
//   .agents                       default off — OPENCLANK_ENABLE_AGENTS_SKILLS
//   .claude / .codex / .opencode  default off — OPENCLANK_ENABLE_*_SKILLS
const copy = process.env["OPENCLANK_EXPERIMENTAL_DISABLE_COPY_ON_SELECT"]
// Operator-supplied password (env or inherited worker-auth fd). Captured once:
// consumeInheritedServerPassword() closes the descriptor.
const suppliedServerPassword =
  consumeWindowsServerPassword() ?? consumeInheritedServerPassword() ?? process.env["OPENCLANK_SERVER_PASSWORD"]

let generatedServerPassword: string | undefined

/**
 * Generate the password for an implicit listener, once. Idempotent; a
 * user-supplied password always wins and nothing is generated in that case.
 */
export function generateServerPassword() {
  if (suppliedServerPassword) return
  generatedServerPassword ??= Buffer.from(crypto.getRandomValues(new Uint8Array(32))).toString("base64url")
}

/** Disarm the generated password once its listener is gone. */
export function clearGeneratedServerPassword() {
  generatedServerPassword = undefined
}

export const Flag = {
  // Host-owned process mode; never supplied by an ACP prompt or tool input.
  OPEN_CLANK_PRIVATE_RUNTIME: process.env["OPEN_CLANK_PRIVATE_RUNTIME"] === "1",
  OTEL_EXPORTER_OTLP_ENDPOINT: process.env["OTEL_EXPORTER_OTLP_ENDPOINT"],
  OTEL_EXPORTER_OTLP_HEADERS: process.env["OTEL_EXPORTER_OTLP_HEADERS"],

  OPENCLANK_AUTO_HEAP_SNAPSHOT: truthy("OPENCLANK_AUTO_HEAP_SNAPSHOT"),
  OPENCLANK_GIT_BASH_PATH: process.env["OPENCLANK_GIT_BASH_PATH"],
  OPENCLANK_CONFIG: process.env["OPENCLANK_CONFIG"],
  OPENCLANK_CONFIG_CONTENT: process.env["OPENCLANK_CONFIG_CONTENT"],

  OPENCLANK_DISABLE_AUTOUPDATE: truthy("OPENCLANK_DISABLE_AUTOUPDATE"),

  // Defaults to false (rotation enabled). When enabled, the active log file is
  // never archived to <name>.log.<stamp> on hitting MAX_FILE_SIZE — it grows in
  // place. Useful when an external tool tails/manages the single log file.
  OPENCLANK_DISABLE_LOG_ROTATION: truthy("OPENCLANK_DISABLE_LOG_ROTATION"),

  // Defaults to true (analytics enabled). Set OPENCLANK_ENABLE_ANALYSIS=false
  // to opt out of POSTing model_call/tool_call/agent_request metrics.
  OPENCLANK_ENABLE_ANALYSIS: !falsy("OPENCLANK_ENABLE_ANALYSIS"),
  OPENCLANK_ALWAYS_NOTIFY_UPDATE: truthy("OPENCLANK_ALWAYS_NOTIFY_UPDATE"),
  OPENCLANK_DISABLE_TERMINAL_TITLE: truthy("OPENCLANK_DISABLE_TERMINAL_TITLE"),
  OPENCLANK_SHOW_TTFD: truthy("OPENCLANK_SHOW_TTFD"),
  OPENCLANK_PERMISSION: process.env["OPENCLANK_PERMISSION"],

  // Defaults to false. When false, the bash tool intercepts irreversible
  // deletion commands (rm, rmdir, unlink, shred, del, erase, rd, remove-item,
  // and git destructive subcommands like reset --hard / clean -f / branch -D /
  // worktree remove / push --force / stash drop|clear / tag -d) and forces an
  // extra permission prompt with permission="bash_delete" — separate from the
  // normal bash-permission ask so it can't be silently pre-approved by a broad
  // `bash: allow` rule. Set OPENCLANK_AUTO_APPROVE_DELETE=true to trust the
  // model with deletes and skip the second confirmation.
  // Read lazily (getter, not an eagerly-evaluated literal) so an embedder can
  // flip it at runtime: the desktop app runs the server in-process, so its
  // approval mode — switchable mid-session, like the TUI's /skip-permissions —
  // has no process boundary at which to re-read env. A literal would freeze
  // this at module-evaluation time and make every later write a no-op.
  get OPENCLANK_AUTO_APPROVE_DELETE() {
    return truthy("OPENCLANK_AUTO_APPROVE_DELETE")
  },
  // Open Clank: skip the forced confirmation for approval-worthy shell side effects.
  // The delete-only name stays an environment alias. Lazy for the same reason as above.
  get OPENCLANK_AUTO_APPROVE_DESTRUCTIVE() {
    return truthy("OPENCLANK_AUTO_APPROVE_DESTRUCTIVE") || truthy("OPENCLANK_AUTO_APPROVE_DELETE")
  },
  // Set by the TUI's --dangerously-skip-permissions flag. When truthy, an
  // allow-all base ruleset is injected UNDER the user's config permission so
  // every tool auto-approves unless the user explicitly denied it.
  OPENCLANK_DANGEROUSLY_SKIP_PERMISSIONS: truthy("OPENCLANK_DANGEROUSLY_SKIP_PERMISSIONS"),
  OPENCLANK_DISABLE_DEFAULT_PLUGINS: truthy("OPENCLANK_DISABLE_DEFAULT_PLUGINS"),
  OPENCLANK_DISABLE_LSP_DOWNLOAD: truthy("OPENCLANK_DISABLE_LSP_DOWNLOAD"),
  OPENCLANK_ENABLE_EXPERIMENTAL_MODELS: truthy("OPENCLANK_ENABLE_EXPERIMENTAL_MODELS"),
  // Defaults to false. When enabled, checkpoint writers, checkpoint-based
  // context rebuilds, and checkpoint copy in the system prompt and tool
  // schemas are disabled; context overflow falls back to compaction.
  // Read lazily so tests and in-process embedders can toggle it at runtime.
  get OPENCLANK_DISABLE_CHECKPOINT() {
    return truthy("OPENCLANK_DISABLE_CHECKPOINT")
  },
  // Defaults to protection on. Opt out to continue a batch after tool failure.
  get OPENCLANK_DISABLE_FAIL_CASCADE() {
    return truthy("OPENCLANK_DISABLE_FAIL_CASCADE")
  },
  OPENCLANK_DISABLE_AUTOCOMPACT: truthy("OPENCLANK_DISABLE_AUTOCOMPACT"),
  // Default compaction trigger, used when `compaction.max_context` is not set in
  // config. Same grammar as that config field: an absolute token count
  // ("300000"), a shorthand ("300K", "1M"), or a percentage of the model window
  // ("50%"). Clamped to the model window — it can only lower the trigger, never
  // raise it. An explicit `compaction.max_context` in config overrides this.
  // Pairs with OPENCLANK_DISABLE_CHECKPOINT: on the checkpoint-off fallback path
  // this is how the compaction threshold is tuned via env alone. Read lazily so
  // tests and in-process embedders can toggle it at runtime.
  get OPENCLANK_COMPACTION_MAX_CONTEXT() {
    return process.env["OPENCLANK_COMPACTION_MAX_CONTEXT"]
  },
  // Fraction of the working window at which compaction fires; the remaining
  // headroom is what the summary generation gets to write into. Accepts a decimal
  // ("0.85") or a percentage ("85%"); anything unparseable or outside (0, 1] is
  // ignored and the 0.9 default stands. Applies on top of whatever window
  // `compaction.max_context` / OPENCLANK_COMPACTION_MAX_CONTEXT resolved to, so
  // the two compose rather than override each other. Read lazily so tests and
  // in-process embedders can toggle it at runtime.
  get OPENCLANK_COMPACTION_TRIGGER_RATIO() {
    return ratio("OPENCLANK_COMPACTION_TRIGGER_RATIO") ?? 0.9
  },
  OPENCLANK_DISABLE_MODELS_FETCH: truthy("OPENCLANK_DISABLE_MODELS_FETCH"),
  // Unset means the default harness unless a session explicitly selects
  // harness "codex". Explicit true enables the Codex prompt and toolset for
  // sessions on harness "auto"; explicit false forces the default harness for
  // every session.
  get OPENCLANK_CODEX_MODE() {
    if (truthy("OPENCLANK_CODEX_MODE")) return true
    if (falsy("OPENCLANK_CODEX_MODE")) return false
    return undefined
  },
  OPENCLANK_DISABLE_MOUSE: truthy("OPENCLANK_DISABLE_MOUSE"),
  OPENCLANK_OUTPUT_LENGTH_CONTINUATION_LIMIT: number("OPENCLANK_OUTPUT_LENGTH_CONTINUATION_LIMIT") ?? 3,
  OPENCLANK_TEXT_TOOL_CALL_RETRY_LIMIT: number("OPENCLANK_TEXT_TOOL_CALL_RETRY_LIMIT") ?? 2,
  OPENCLANK_EMPTY_STEP_DROP_LIMIT: number("OPENCLANK_EMPTY_STEP_DROP_LIMIT") ?? 2,
  OPENCLANK_EMPTY_STEP_MAX_RECOVERY: number("OPENCLANK_EMPTY_STEP_MAX_RECOVERY") ?? 2,
  // Defaults to false. When enabled, unsigned historical reasoning sent through
  // the Anthropic Messages format receives an empty placeholder signature so it
  // follows the same native thinking-block serialization path as signed content.
  get OPENCLANK_FORCE_ANTHROPIC_REASONING_CONTENT() {
    return truthy("OPENCLANK_FORCE_ANTHROPIC_REASONING_CONTENT")
  },

  // Consecutive-block repetition detection for streamed reasoning + text.
  // A block of at least N tokens repeating REPEAT_THRESHOLD times consecutively
  // within the last WINDOW_TOKENS tokens triggers recovery (remind → replan → terminate).
  OPENCLANK_TEXT_NGRAM_N: number("OPENCLANK_TEXT_NGRAM_N") ?? 4,
  OPENCLANK_TEXT_REPEAT_THRESHOLD: number("OPENCLANK_TEXT_REPEAT_THRESHOLD") ?? 20,
  OPENCLANK_TEXT_WINDOW_TOKENS: number("OPENCLANK_TEXT_WINDOW_TOKENS") ?? 500,

  // Caps applied to image attachments before a prompt is sent.
  // OPENCLANK_MAX_PROMPT_IMAGES (default undefined = no count limit) bounds how
  // many images may be sent per request (oldest excess images are dropped).
  // OPENCLANK_MAX_PROMPT_IMAGE_SIZE overrides the default per-image byte cap
  // (DEFAULT_MAX_IMAGE_BYTES ~4.5 MB, kept under the provider 5 MB hard limit);
  // oversized images are recompressed under the cap, or stripped to a text
  // placeholder when they can't be compressed. Values must be positive integers.
  OPENCLANK_MAX_PROMPT_IMAGES: number("OPENCLANK_MAX_PROMPT_IMAGES"),
  OPENCLANK_MAX_PROMPT_IMAGE_SIZE: number("OPENCLANK_MAX_PROMPT_IMAGE_SIZE"),
  // Upper bound, in bytes, on a single inline attachment (image, PDF, audio,
  // MCP blob). Enforced where the attachment is produced — the read tool, user
  // prompt attachments, and MCP result normalization — with the size taken
  // from stat or the base64 length so an under-limit payload costs nothing
  // extra. Over the limit, an image is read and recompressed to fit; anything
  // that still cannot fit (PDFs, audio, video, undecodable images) is replaced
  // by a notice, so nothing oversized ever becomes a stored part. Defaults to
  // 50 MB. Provider limits are lower (the Claude API takes 10 MB per image,
  // Bedrock and Vertex 5 MB) and are still enforced at send time by the
  // OPENCLANK_MAX_PROMPT_IMAGE_SIZE / provider cap in provider/transform.ts.
  // Read lazily so tests can flip it at runtime.
  get OPENCLANK_MAX_ATTACHMENT_SIZE() {
    return number("OPENCLANK_MAX_ATTACHMENT_SIZE") ?? 50 * 1024 * 1024
  },
  // Ceiling, in bytes, above which an oversized image is refused outright
  // instead of being read and recompressed. Defaults to 150 MB: compressImage
  // already refuses anything over MAX_DECODE_IMAGE_PIXELS (64 MP), and a PNG
  // that decodes within that budget is at most ~150-200 MB on disk, so a
  // larger file would only be read into memory to fail the pixel guard.
  // Read lazily so tests can flip it at runtime.
  get OPENCLANK_MAX_ATTACHMENT_SOURCE_SIZE() {
    return number("OPENCLANK_MAX_ATTACHMENT_SOURCE_SIZE") ?? 150 * 1024 * 1024
  },
  OPENCLANK_ISOLATED_CONFIG,
  OPENCLANK_DISABLE_PROVIDER_ENV: OPENCLANK_ISOLATED_CONFIG || truthy("OPENCLANK_DISABLE_PROVIDER_ENV"),
  OPENCLANK_DISABLE_CLAUDE_CODE,
  get OPENCLANK_DISABLE_CLAUDE_CODE_MCP() {
    // MCP compatibility stays on in mimo-only mode so users can reuse Claude Code
    // MCP servers without inheriting prompts or provider env keys.
    return OPENCLANK_DISABLE_CLAUDE_CODE_ENV || truthy("OPENCLANK_DISABLE_CLAUDE_CODE_MCP")
  },
  OPENCLANK_DISABLE_CLAUDE_CODE_PROMPT: OPENCLANK_DISABLE_CLAUDE_CODE || truthy("OPENCLANK_DISABLE_CLAUDE_CODE_PROMPT"),
  // Defaults to false (enabled): markdown commands under ~/.claude/commands and
  // {project}/.claude/commands load as slash commands. Independent of the
  // mimo-only master switch. Set OPENCLANK_DISABLE_CLAUDE_CODE_COMMANDS=true to disable.
  OPENCLANK_DISABLE_CLAUDE_CODE_COMMANDS: truthy("OPENCLANK_DISABLE_CLAUDE_CODE_COMMANDS"),
  // Defaults to false. Set to true or 1 to import Claude Code sessions on startup.
  OPENCLANK_ENABLE_CLAUDE_IMPORT: truthy("OPENCLANK_ENABLE_CLAUDE_IMPORT"),
  // External skill-root switches. Read lazily so tests can flip env.
  get OPENCLANK_DISABLE_EXTERNAL_SKILLS() {
    return truthy("OPENCLANK_DISABLE_EXTERNAL_SKILLS")
  },
  get OPENCLANK_ENABLE_AGENTS_SKILLS() {
    return truthy("OPENCLANK_ENABLE_AGENTS_SKILLS")
  },
  get OPENCLANK_DISABLE_AGENTS_SKILLS() {
    return truthy("OPENCLANK_DISABLE_AGENTS_SKILLS")
  },
  get OPENCLANK_ENABLE_CLAUDE_CODE_SKILLS() {
    return truthy("OPENCLANK_ENABLE_CLAUDE_CODE_SKILLS")
  },
  get OPENCLANK_ENABLE_CODEX_SKILLS() {
    return truthy("OPENCLANK_ENABLE_CODEX_SKILLS")
  },
  get OPENCLANK_ENABLE_OPENCODE_SKILLS() {
    return truthy("OPENCLANK_ENABLE_OPENCODE_SKILLS")
  },
  get OPENCLANK_DISABLE_COMPOSE_SKILLS() {
    return truthy("OPENCLANK_DISABLE_COMPOSE_SKILLS")
  },
  get OPENCLANK_DISABLE_REMOTE_SKILLS() {
    return truthy("OPENCLANK_DISABLE_REMOTE_SKILLS")
  },

  // Skill-search ranking and loading policy. Exact mentions stay above BM25;
  // the BM25/coverage blend has a 0.90 ceiling, and near-max results auto-load.
  OPENCLANK_SKILL_SEARCH_EXACT_SCORE: 1,
  OPENCLANK_SKILL_SEARCH_BM25_K1: 1.5,
  OPENCLANK_SKILL_SEARCH_BM25_LENGTH_NORMALIZATION: 0.75,
  OPENCLANK_SKILL_SEARCH_BM25_IDF_SMOOTHING: 0.5,
  OPENCLANK_SKILL_SEARCH_BM25_SCORE_WEIGHT: 0.55,
  OPENCLANK_SKILL_SEARCH_QUERY_COVERAGE_WEIGHT: 0.35,
  OPENCLANK_SKILL_SEARCH_AUTO_LOAD_THRESHOLD: 0.85,
  OPENCLANK_SKILL_SEARCH_SCORE_PRECISION: 4,
  OPENCLANK_SKILL_SEARCH_MAX_RESULTS: 3,
  OPENCLANK_SKILL_SEARCH_STEM_MIN_LENGTH: 3,
  OPENCLANK_SKILL_SEARCH_FILE_SAMPLE_LIMIT: 10,
  OPENCLANK_SKILL_SEARCH_REFRESH_INTERVAL_MS: 12 * 60 * 60 * 1000,
  OPENCLANK_ENABLE_SKILL_SEARCH_REMINDER: !falsy("OPENCLANK_ENABLE_SKILL_SEARCH_REMINDER"),

  // Defaults to false. When enabled, skill-source commands appear in the `/`
  // autocomplete dropdown alongside user commands and MCP prompts. Skills are
  // surfaced in `/` completion by default; set OPENCLANK_DISABLE_SLASH_SKILLS=1
  // to hide them and fall back to the `/skills` picker + model-driven
  // invocation only.
  OPENCLANK_DISABLE_SLASH_SKILLS: truthy("OPENCLANK_DISABLE_SLASH_SKILLS"),
  OPENCLANK_FAKE_VCS: process.env["OPENCLANK_FAKE_VCS"],

  // When enabled, skips all git subprocess calls during project discovery
  // (which git, rev-parse --git-common-dir, rev-parse --show-toplevel) and
  // branch detection. The project is treated as a non-git directory rooted at
  // the working directory. Use to avoid touching git in restricted/sandboxed
  // environments or where git startup probing is undesirable.
  OPENCLANK_DISABLE_GIT: truthy("OPENCLANK_DISABLE_GIT"),
  /** Live: falls back to the throwaway generated for an implicit listener. */
  get OPENCLANK_SERVER_PASSWORD() {
    return suppliedServerPassword ?? generatedServerPassword
  },
  /**
   * True only when the operator supplied a password. Containment rule keys on
   * this: a generated password must not look like a user-asked-for listener.
   */
  get OPENCLANK_SERVER_PASSWORD_SUPPLIED() {
    return Boolean(suppliedServerPassword)
  },
  OPENCLANK_SERVER_USERNAME: process.env["OPENCLANK_SERVER_USERNAME"],
  OPENCLANK_ENABLE_QUESTION_TOOL: truthy("OPENCLANK_ENABLE_QUESTION_TOOL"),
  // Owner worker-generation default; independent of tool approval permission.
  OPENCLANK_NEVER_ASK: truthy("OPENCLANK_NEVER_ASK"),

  // Defaults to false. Set OPENCLANK_ENABLE_TRY_BEST_HANDOFF=true (or 1) to
  // enable try-best loop detection, automatic turn pausing, and handoff UI.
  OPENCLANK_ENABLE_TRY_BEST_HANDOFF: truthy("OPENCLANK_ENABLE_TRY_BEST_HANDOFF"),

  // Defaults to false. Opt in to append the runtime-derived environment block
  // (working directory, platform, shell, git status/branch/commits) to the model's
  // system prompt. Instruction files (AGENTS.md / CLAUDE.md) are appended
  // regardless — suppress the whole block with OPENCLANK_DISABLE_INSTRUCTIONS, or
  // individual sources with OPENCLANK_DISABLE_PROJECT_CONFIG /
  // OPENCLANK_DISABLE_CLAUDE_CODE_PROMPT.
  get OPENCLANK_ENABLE_DYNAMIC_SYSTEM_PROMPT() {
    return truthy("OPENCLANK_ENABLE_DYNAMIC_SYSTEM_PROMPT")
  },

  // Defaults to false (enabled): instruction-file content (AGENTS.md / CLAUDE.md)
  // is appended to the model's system prompt. Set OPENCLANK_DISABLE_INSTRUCTIONS=true
  // to drop the whole instruction block regardless of which files resolve.
  get OPENCLANK_DISABLE_INSTRUCTIONS() {
    return truthy("OPENCLANK_DISABLE_INSTRUCTIONS")
  },

  // Defaults to false. The edit tool does pure exact-string matching with
  // explicit error signals. Set OPENCLANK_ENABLE_FUZZY_EDIT=true to opt into the
  // legacy multi-stage fuzzy fallback chain (line-trimmed / block-anchor /
  // whitespace-normalized / indentation-flexible / etc.) when old_string fails
  // to match exactly.
  OPENCLANK_ENABLE_FUZZY_EDIT: truthy("OPENCLANK_ENABLE_FUZZY_EDIT"),

  // Experimental
  OPENCLANK_EXPERIMENTAL,
  OPENCLANK_EXPERIMENTAL_FILEWATCHER: Config.boolean("OPENCLANK_EXPERIMENTAL_FILEWATCHER").pipe(
    Config.withDefault(false),
  ),
  OPENCLANK_EXPERIMENTAL_DISABLE_FILEWATCHER: Config.boolean("OPENCLANK_EXPERIMENTAL_DISABLE_FILEWATCHER").pipe(
    Config.withDefault(false),
  ),
  OPENCLANK_EXPERIMENTAL_ICON_DISCOVERY: OPENCLANK_EXPERIMENTAL || truthy("OPENCLANK_EXPERIMENTAL_ICON_DISCOVERY"),
  OPENCLANK_EXPERIMENTAL_DISABLE_COPY_ON_SELECT:
    copy === undefined ? process.platform === "win32" : truthy("OPENCLANK_EXPERIMENTAL_DISABLE_COPY_ON_SELECT"),
  OPENCLANK_ENABLE_EXA: truthy("OPENCLANK_ENABLE_EXA") || OPENCLANK_EXPERIMENTAL || truthy("OPENCLANK_EXPERIMENTAL_EXA"),
  OPENCLANK_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS: number("OPENCLANK_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS"),
  // Token-efficient post-cleanse: strip ANSI / fold \r progress bars / redact
  // secrets / elide super-long lines from bash tool output before it is
  // returned to the model. Only applies when the output fits inline — if the
  // output spills to a truncation file, cleaning is skipped so the on-disk
  // archive stays raw. Off by default. Set to 1/true to opt in.
  OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY: truthy("OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY"),
  // Tunables for the token-efficient post-cleanse pipeline (see
  // src/tool/bash_token_efficient_pipeline.ts). Positive integers only;
  // unset / non-positive values fall back to the documented defaults.
  //   MAX_LINE_CHARS   threshold above which a single line is elided  (default 500)
  //   LINE_HEAD_KEEP   chars kept from the head of an elided line     (default 160)
  //   NEVER_WORSE_MARGIN  bytes the cleaned output must beat the raw  (default 0)
  OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY_MAX_LINE_CHARS: number("OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY_MAX_LINE_CHARS") ?? 500,
  OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY_LINE_HEAD_KEEP: number("OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY_LINE_HEAD_KEEP") ?? 160,
  OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY_NEVER_WORSE_MARGIN: number("OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY_NEVER_WORSE_MARGIN") ?? 0,
  // Heuristic (shape-based) filter pipeline for bash output. Runs AFTER the
  // common pipeline, only when the common pipeline is enabled AND this flag is
  // explicitly opted in. Each shape (gitdiff / pytest / npm / make /
  // stacktrace / tsc / kubectl / json / md / gostest) recognises a command
  // pattern or body fingerprint and rewrites the body to strip predictable
  // noise. Off by default. Set to 1/true to opt in.
  OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY_HEURISTIC: truthy("OPENCLANK_EXPERIMENTAL_TOKEN_EFFICIENCY_HEURISTIC"),
  OPENCLANK_EXPERIMENTAL_OXFMT: OPENCLANK_EXPERIMENTAL || truthy("OPENCLANK_EXPERIMENTAL_OXFMT"),
  OPENCLANK_EXPERIMENTAL_LSP_TY: truthy("OPENCLANK_EXPERIMENTAL_LSP_TY"),
  OPENCLANK_EXPERIMENTAL_LSP_TOOL: OPENCLANK_EXPERIMENTAL || truthy("OPENCLANK_EXPERIMENTAL_LSP_TOOL"),
  // Defaults to OFF: exec (tool_script orchestration) is registered only in
  // Codex mode. Opt in here to expose it in every session.
  OPENCLANK_ENABLE_EXEC_TOOL: truthy("OPENCLANK_ENABLE_EXEC_TOOL"),
  // MCP tool lazy-loading: `true` (default) always defers MCP tool schemas
  // behind mcp_tool_search, `auto` defers once they exceed 10% of the context
  // window, `auto:N` uses N%, `false` never defers. Codex-mode sessions always defer.
  get OPENCLANK_MCP_TOOL_SEARCH() {
    return process.env["OPENCLANK_MCP_TOOL_SEARCH"]
  },
  // Defaults to OFF (opt-in): dynamic workflows and built-in workflows.
  // Enable with OPENCLANK_EXPERIMENTAL_WORKFLOW_TOOL=true (or the umbrella
  // OPENCLANK_EXPERIMENTAL flag).
  OPENCLANK_EXPERIMENTAL_WORKFLOW_TOOL:
    OPENCLANK_EXPERIMENTAL || truthy("OPENCLANK_EXPERIMENTAL_WORKFLOW_TOOL"),
  // Defaults to true: cron + self-paced loop scheduling are on by default.
  // Set OPENCLANK_EXPERIMENTAL_CRON=false to opt out. Runtime kill switch is
  // OPENCLANK_DISABLE_CRON (checked live every tick).
  OPENCLANK_EXPERIMENTAL_CRON: !falsy("OPENCLANK_EXPERIMENTAL_CRON"),
  // Keepalive contract for self-paced loops (spec [S8]). Budget = how many
  // "forget" turns the model gets before the loop is declared model_stopped;
  // delay seconds = the auto-arm horizon used for the keepalive fire. Budget
  // accepts 0 (end immediately on the first turn without a re-arm) for tests
  // and aggressive policies. Both are getters so tests can flip the env var
  // between cases without restarting the process.
  get OPENCLANK_LOOP_KEEPALIVE_BUDGET() {
    return nonNegativeNumber("OPENCLANK_LOOP_KEEPALIVE_BUDGET") ?? 1
  },
  get OPENCLANK_LOOP_KEEPALIVE_DELAY_S() {
    return number("OPENCLANK_LOOP_KEEPALIVE_DELAY_S") ?? 1200
  },
  OPENCLANK_EXPERIMENTAL_MARKDOWN: !falsy("OPENCLANK_EXPERIMENTAL_MARKDOWN"),
  OPENCLANK_MODELS_URL: process.env["OPENCLANK_MODELS_URL"],
  OPENCLANK_MODELS_PATH: process.env["OPENCLANK_MODELS_PATH"],
  OPENCLANK_DISABLE_EMBEDDED_WEB_UI: truthy("OPENCLANK_DISABLE_EMBEDDED_WEB_UI"),
  OPENCLANK_DB: process.env["OPENCLANK_DB"],

  // Defaults to true — all channels share a single openclank.db. The per-channel
  // DB isolation (openclank-{channel}.db) is unnecessary for openclank since we
  // don't ship multiple release channels yet. Use OPENCLANK_HOME to isolate dev
  // environments instead. Set OPENCLANK_DISABLE_CHANNEL_DB=false to restore
  // per-channel isolation.
  OPENCLANK_DISABLE_CHANNEL_DB: !falsy("OPENCLANK_DISABLE_CHANNEL_DB"),
  OPENCLANK_SKIP_MIGRATIONS: truthy("OPENCLANK_SKIP_MIGRATIONS"),
  OPENCLANK_STRICT_CONFIG_DEPS: truthy("OPENCLANK_STRICT_CONFIG_DEPS"),

  OPENCLANK_WORKSPACE_ID: process.env["OPENCLANK_WORKSPACE_ID"],
  OPENCLANK_EXPERIMENTAL_HTTPAPI: truthy("OPENCLANK_EXPERIMENTAL_HTTPAPI"),
  OPENCLANK_EXPERIMENTAL_WORKSPACES: OPENCLANK_EXPERIMENTAL || truthy("OPENCLANK_EXPERIMENTAL_WORKSPACES"),

  // Evaluated at access time (not module load) because tests, the CLI, and
  // external tooling set these env vars at runtime.

  // Disables user-facing builtin skills shipped with the binary (e.g.
  // openclank-docs).
  get OPENCLANK_DISABLE_BUILTIN_SKILLS() {
    return truthy("OPENCLANK_DISABLE_BUILTIN_SKILLS")
  },
  // Disables the built-in official skills (docx, pdf, pptx, xlsx,
  // html-to-video-pipeline) while keeping the rest of the builtin bundle
  // available. Defaults to false (all skills are extracted and loaded). Set
  // OPENCLANK_DISABLE_OFFICIAL_SKILLS=true to skip them.
  get OPENCLANK_DISABLE_OFFICIAL_SKILLS() {
    return truthy("OPENCLANK_DISABLE_OFFICIAL_SKILLS")
  },
  // Opt-in for bundled skills that operate other agent CLIs (claude-code,
  // codex, grok-build). Defaults to false so the preinstalled skill surface
  // stays quiet; set OPENCLANK_ENABLE_AGENT_CLI_SKILLS=true to extract and load
  // them. Env value alone decides the extract set — binary presence is not
  // consulted.
  get OPENCLANK_ENABLE_AGENT_CLI_SKILLS() {
    return truthy("OPENCLANK_ENABLE_AGENT_CLI_SKILLS")
  },
  get OPENCLANK_DISABLE_PROJECT_CONFIG() {
    return truthy("OPENCLANK_DISABLE_PROJECT_CONFIG")
  },
  get OPENCLANK_TUI_CONFIG() {
    return process.env["OPENCLANK_TUI_CONFIG"]
  },
  get OPENCLANK_CONFIG_DIR() {
    return process.env["OPENCLANK_CONFIG_DIR"]
  },
  get OPENCLANK_HOME() {
    return process.env["OPENCLANK_HOME"]
  },
  get OPENCLANK_PURE() {
    return truthy("OPENCLANK_PURE")
  },
  // Loads only external plugins, file hooks and TUI plugins provided by
  // OPENCLANK_CONFIG_DIR. Always on for the Node build, which cannot serve the
  // plugin SDK to user code.
  get OPENCLANK_ONLY_CONFIG_DIR_PLUGINS() {
    return truthy("OPENCLANK_ONLY_CONFIG_DIR_PLUGINS")
  },
  get OPENCLANK_PLUGIN_META_FILE() {
    return process.env["OPENCLANK_PLUGIN_META_FILE"]
  },
  get OPENCLANK_CLIENT() {
    return process.env["OPENCLANK_CLIENT"] ?? "cli"
  },
}
