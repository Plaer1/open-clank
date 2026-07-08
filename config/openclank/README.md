# Roster — agent identity source of truth

## Files

- `roster.json5` — agent roster listing. Each entry:
  - `id` (str, required) — stable key, matches `CrewMember.id`
  - `name` (str, required) — display name
  - `home` (str, required) — absolute path to the agent's md bootstrap directory
  - `model` (str, optional) — `provider/model` override (e.g. `anthropic/claude-sonnet-4-20250514`)
  - `default` (bool, optional) — if true, this agent is the default primary

## Per-agent home layout

Each `home` directory contains a subset of these markdown files:

| File          | Purpose                              | Loaded? |
|---------------|--------------------------------------|---------|
| `AGENTS.md`   | Behavioral rules + session bootstrap | Yes     |
| `SOUL.md`     | Personality, voice, core truths      | Yes     |
| `IDENTITY.md` | Name, creature type, machine info    | Yes     |
| `TOOLS.md`    | Operational notes, local specifics   | Yes     |
| `USER.md`     | About the human being served         | Yes     |

Missing files are silently skipped. The load order is fixed:
`AGENTS.md` → `SOUL.md` → `IDENTITY.md` → `TOOLS.md` → `USER.md`.

## Field mapping (AGENTS.md frontmatter → mimo agent config)

Structured fields live as YAML frontmatter on `AGENTS.md`:

```yaml
---
description: "Short description of the agent"
mode: primary
model: anthropic/claude-sonnet-4-20250514
temperature: 0.7
permission:
  bash: allow
  lifetools:send_email: ask
tool_allowlist: [bash, read, edit, lifetools:send_email]
---
```

Frontmatter fields map to mimo `Agent.Info` via `ConfigAgent.load()` (`config/agent.ts:149-159`):
- `description` → `Agent.Info.description`
- `mode` → `Agent.Info.mode` (`primary` | `subagent` | `all`)
- `model` → `Agent.Info.model` (`{providerID, modelID}`)
- `temperature` → `Agent.Info.temperature`
- `permission` → `Agent.Info.permission` (ruleset map)
- `tool_allowlist` → `Agent.Info.tool_allowlist`

The markdown body becomes `Agent.Info.prompt` (the full system prompt).

## How the loader composes the system prompt

1. Read all 5 md files from `home` in order
2. Concatenate their bodies into the **persona block**
3. Prepend the **base-behavior block** (mimo's `SystemPrompt.provider()` text)
4. Result = `base_behavior + "\n\n" + persona`

Because `agent.prompt` REPLACES the provider base prompt (`session/llm.ts:246`),
the loader must include it — mimo won't add it when a prompt is set.
