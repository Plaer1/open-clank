# MiMo-Code Personality & Opinion Audit

> **Audit Date:** 2026-06-12
> **Scope:** Hardcoded personality traits, opinion directives, style instructions, and agent identity baked into prompts.

## Executive Summary

**Yes — MiMo-Code has personality, tone, and style directives hardcoded into its system prompts, but no overt "opinion" directives in the political/social sense.** The personality is a **technical engineering persona**: rigorous, direct, fact-driven, and willing to disagree with the user on technical matters. Different model families get different prompt variants with distinct tone/personality calibrations.

---

## 1. Prompt Selection by Model Family

`packages/opencode/src/session/system.ts:21` — `provider()` function selects prompt template based on model API id:

| Model Family | Prompt File | Identity / Persona |
|---|---|---|
| `claude-*` | `anthropic.txt` | "MiMoCode, the best coding agent on the planet" |
| `gpt-4*`, `o1*`, `o3*` | `beast.txt` | "MiMoCode, an agent" (highly autonomous, must-complete mode) |
| `gpt-*` (plain) | `gpt.txt` | "MiMoCode... deeply pragmatic, effective software engineer" |
| `gpt-*` + `codex` | `codex.txt` | "OpenCode, the best coding agent on the planet" |
| `gemini-*` | `gemini.txt` | "opencode, an interactive CLI agent" |
| `kimi-*` | `kimi.txt` | "MiMoCode, an interactive general AI agent" |
| `trinity-*` | `trinity.txt` | "MiMoCode, an interactive CLI tool" |
| Fallback | `default.txt` | "MiMoCode, an interactive CLI tool" |
| Compose mode | + `compose.txt` | "MiMoCode Compose Agent — an orchestrator" |

Additionally, `system.ts:64` injects a universal environment header:
> "You are MiMo Code Agent, built by Xiaomi MiMo Team. You are an interactive agent that helps users with software engineering tasks."

---

## 2. Hardcoded Agent Identity Declarations

### anthropic.txt
> "You are **MiMoCode, the best coding agent on the planet**."
> — Grandiose, superlative identity claim. "Best coding agent" is subjective/opinionated branding.

### codex.txt
> "You are **OpenCode, the best coding agent on the planet**."
> — Same claim, different name. Only used for GPT-codex models.

### gpt.txt
> "You are MiMoCode... a **deeply pragmatic, effective software engineer**. You take engineering quality seriously... **direct, factual statements**... embody the mentality of a **skilled senior software engineer**."
> — Embeds a specific engineering philosophy + seniority claim.

### beast.txt
> "You are MiMoCode, **an agent** — please keep going until the user's query is completely resolved"

### default.txt, trinity.txt, gemini.txt, kimi.txt
> "You are MiMoCode/opencode, an interactive CLI tool/agent"
> — Neutral operational identity, no grand claims.

---

## 3. "Professional Objectivity" → The Strongest Opinion Directive

`anthropic.txt:27-30` has an explicit **professional objectivity** section:

> "Prioritize **technical accuracy and truthfulness** over validating the user's beliefs. Focus on facts and problem-solving, providing direct, objective technical info without any unnecessary superlatives, praise, or emotional validation. It is best for the user if MiMoCode **honestly applies the same rigorous standards to all ideas and disagrees when necessary**, even if it may not be what the user wants to hear. **Objective guidance and respectful correction are more valuable than false agreement.** Whenever there is uncertainty, it's best to investigate to find the truth first rather than instinctively confirming the user's beliefs."

**Assessment:** This is the closest thing to an "opinion/personality" directive. It actively instructs the agent to:
- Disagree with the user when technically wrong
- Push back on incorrect beliefs
- Prioritize truth over politeness
- Act as a rigorous technical peer, not a sycophant

No other prompt variant has this section. Only `anthropic.txt`.

---

## 4. Tone & Style Directives (Per Prompt)

| Prompt File | Tone Directive |
|---|---|
| **anthropic.txt** | No emojis unless asked. Concise, CLI-appropriate. GitHub-flavored markdown, monospace. No narrating deliberation. |
| **default.txt** | Concise, direct, to the point. No emojis. Minimize output tokens. One-word answers preferred. No preamble/postamble. |
| **gpt.txt** | No emojis. No conversational interjections. Direct, factual, no framing phrases. "Senior software engineer" mentality. Keep lists flat. |
| **codex.txt** | "Very concise; **friendly coding teammate tone**." Collaborative, factual, active voice. Self-contained. |
| **beast.txt** | "**Casual, friendly yet professional tone.** " Use clear, direct answers. Avoid unnecessary explanations/repetition/filler. |
| **copilot-gpt-5.txt** (embedded in compose) | "Short and impersonal." Also: "**Warm and friendly yet professional. Use upbeat language and sprinkle in light, witty humor where appropriate.** " |
| **gemini.txt** | "**Professional, direct, and concise tone suitable for a CLI environment.** Minimal output: fewer than 3 lines per response." |
| **trinity.txt** | Same as default.txt (concise, direct, no emojis, minimize output). |
| **kimi.txt** | Same as default.txt (concise, direct, no emojis). |
| **compose.txt** | No tone directives — purely operational orchestration instructions. |

---

## 5. Agent Generation System (generate.txt) — Self-referential Persona Design

`packages/opencode/src/agent/generate.txt:9` — The agent-creation agent is instructed to:

> "**Design Expert Persona**: Create a compelling expert identity that embodies deep domain knowledge relevant to the task. The persona should inspire confidence and guide the agent's decision-making approach."

This is the agent that creates new custom agents. Its own system prompt uses:
> "You are an **elite AI agent architect** specializing in crafting high-performance agent configurations."

**Assessment:** The system propagates persona-based prompting as a design pattern. Custom agents created through this system will have deliberate personas.

---

## 6. Subagent Prompts (Hidden/Internal Agents)

These are not user-facing coding agents, but internal infrastructure:

| Agent | Prompt | Personality/Opinion |
|---|---|---|
| **explore** | `explore.txt` | "You are a file search specialist." — Operational, no personality |
| **checkpoint-writer** | Composed in `checkpoint.ts` | Strict formatting constraints (exact sections, no invented data). Machine-like, no personality |
| **dream** | `dream.txt` | Memory consolidation. Operational rules only. "Keep the memory folder compact and high-signal." |
| **distill** | `distill.txt` | Workflow packaging. Operational rules only. — but mentions "analysis, and personal administration" |
| **compaction** | `compaction.txt` | Summarization. "Do not mention that you are summarizing." Operational only. |
| **title** | `title.txt` | "Vary your phrasing - avoid repetitive patterns." Operational only. |
| **summary** | `summary.txt` | "If the conversation ends with an imperative statement or request to the user... always include that exact request." Operational only. |

---

## 7. Project-Level Config (.mimocode / AGENTS.md)

| File | Content |
|---|---|
| `.mimocode/mimocode.jsonc` | Minimal — provider, permission, MCP config. No prompt/personality content. |
| `AGENTS.md` / `CLAUDE.md` | Code style conventions only: avoid try/catch, prefer const, avoid destructuring, prefer early returns. No opinion/personality directives. |
| `.mimocode/agent/translator.md` | "You are a **professional translator and localization specialist**." + massive do-not-translate glossary. Professional translator persona. |
| `.mimocode/command/*.md` | Purely operational command templates (spellcheck, commit, changelog, issues, etc.). |

**Assessment:** The project-level configs are neutral — no personality or opinion content. The translator agent has a professional persona suited to its domain.

---

## 8. Compose Skills (`.bundle/`)

The compose skills (TDD, debug, brainstorm, review, etc.) are entirely operational workflow instructions. No personality, opinion, or tone directives beyond the composition orchestration framework itself.

The `code-reviewer` and `plan-document-reviewer` subagent prompts mention "stylistic preferences" only in the context of **what the reviewer should NOT flag** (i.e., "stylistic preferences are not actionable feedback"), not as directives to impose a style.

---

## Key Findings Summary

| Category | Has Personality/Opinion Content? | Details |
|---|---|---|
| **Agent Identity (name/title)** | ✅ Yes | "MiMoCode, the best/elite coding agent." Grandiose identity in 2 of 9 prompts. |
| **Professional Objectivity** | ✅ Yes (anthropic.txt only) | Explicit instruction to disagree with user on technical matters, prioritize truth over politeness. |
| **Tone Directives** | ✅ Yes | Each prompt variant defines a distinct tone: senior engineer (gpt.txt), friendly teammate (codex.txt), casual-professional (beast.txt), warm+humorous (copilot-gpt-5.txt), cold and short (default.txt). |
| **Political/Social Opinions** | ❌ No | No political, social, or cultural opinion directives anywhere. |
| **Code Style Opinions** | ✅ Yes (gpt.txt, AGENTS.md) | Prefer minimal code, no comments, no try/catch, no premature abstraction, avoid avoid destructuring, prefer early returns. |
| **Emoji Policy** | ✅ Yes | All prompt variants explicitly forbid emojis (or require user opt-in). |
| **Agent Generation** | ✅ Design pattern | The agent-creation agent is explicitly told to "Design Expert Persona". |
| **Project-level Config** | ❌ No | `.mimocode/`, `AGENTS.md`, `CLAUDE.md` — purely operational/style, no personality. |

## Conclusion

MiMo-Code's system prompts **do** hardcode personality/identity traits and tone directives. The strongest "opinion" is the professional-objectivity directive in `anthropic.txt`, instructing the agent to disagree with the user when technically wrong. However, there are **no social, political, or cultural opinion directives** baked in — the opinions are exclusively about **engineering rigor, communication style, and persona framing**.

The prompt variant selected depends entirely on which model provider the user chooses. An OpenAI GPT user gets a "senior software engineer" persona; an Anthropic Claude user gets a "best coding agent" persona with truth-over-politeness directives; a Gemini user gets a stripped-down "efficient CLI agent" with minimal personality.
