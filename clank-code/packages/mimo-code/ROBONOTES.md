# MiMo-Code Investigation Report
**Date:** 2026-06-12
**Source:** ~/sauce/ai/agents/mimo-code (XiaomiMiMo/MiMo-Code)

## Summary

MiMoCode is Xiaomi's fork of OpenCode — an AI-powered coding agent (Cursor/Windsurf competitor). It's a terminal-native AI assistant with persistent memory, subagent orchestration, and self-improvement capabilities. Built as a monorepo with 16 packages.

**Growth:** ~58K installs in late June 2025 → ~10M+ by end of January 2026.

---

## Package Overview

| Package | Type | Purpose |
|---------|------|---------|
| `opencode` | Core CLI | Main engine — CLI, TUI, server, agent loop, tools, plugins |
| `app` | Web UI | SolidJS web app — chat, file tree, diff review, terminal |
| `desktop` | Electron | Native desktop wrapper around web app |
| `console` | Web Console | Management UI — accounts, billing, analytics |
| `enterprise` | Enterprise | Session sharing, S3/R2 storage, Cloudflare deployment |
| `plugin` | SDK | Plugin system — 22 hooks, server + TUI plugins |
| `sdk` | SDK | Client SDK, server launcher, TUI launcher |
| `extensions` | Plugins | 15 bundled extensions (git, github, slack, vscode, etc.) |
| `containers` | Dev Containers | VS Code-style Dev Container support |
| `identity` | Branding | Just logos (SVG + PNG) |
| `shared` | Utilities | 5 files — debounce, path, repo helpers |
| `ui` | Components | UI component library |
| `slack` | Integration | Slack bot integration |
| `function` | Serverless | Cloudflare Worker API endpoints |
| `storybook` | Docs | Component documentation |
| `script` | Tooling | Build/release helpers |

---

## Key Features

### From README (differentiators vs OpenCode)
- **Persistent memory** via SQLite FTS5 (project MEMORY.md, checkpoints, notes)
- **Intelligent context management** — automatic checkpointing + budgeted context reconstruction
- **Subagent orchestration** — spawn/fork/cancel subagents sharing session context
- **Goal-driven autonomous loops** (`/goal` with independent judge evaluation)
- **Compose workflows** — specs-driven dev lifecycle (plan → execute → review → TDD → debug → verify → merge)
- **Self-improvement** via `/dream` (extract patterns → MEMORY.md) and `/distill` (package workflows into reusable skills)
- **Voice input** — TenVAD + MiMo ASR streaming transcription
- **Max Mode** — parallel best-of-N reasoning with judge selection

### Core CLI Commands
| Command | Purpose |
|---------|---------|
| `mimo` | Interactive TUI (SolidJS-powered) |
| `mimo run` | One-shot headless run |
| `mimo agent` | Reusable AI agent definitions |
| `mimo serve` | HTTP/WebSocket server (Hono) |
| `mimo web` | Web UI server |
| `mimo mcp` | MCP server management |
| `mimo acp` | Agent-to-Agent Communication Protocol |
| `mimo models` | Model/provider management |
| `mimo github` / `mimo pr` | GitHub integration |
| `mimo plug` | Plugin management |
| `mimo debug` | Debug subcommands (LSP, ripgrep, config, etc.) |
| `mimo stats` | Usage statistics |
| `mimo export` / `mimo import` | Session portability |
| `mimo upgrade` | Self-update |

### Tools (20+)
bash, read, write, edit, apply_patch, multiedit, glob, grep, codesearch, webfetch, websearch, actor, task, memory, history, plan, workflow, lsp, mcp, skill

### Plugin System
- **22 hooks** across categories: event, config, tool, auth, provider, chat, permission, shell, actor
- Two plugin types: Server Plugin or TUI Plugin (mutually exclusive)
- Actor lifecycle hooks with matchers (preStop/postStop)
- Custom tool registration via zod schemas
- Workspace adaptors (experimental)

### Extensions (15 bundled)
acpx, bootstrapper, codegenius, feedback, git, github, issue-tracker, playwright, plugin-store, skill-store, slack, task-master, vscode, web-research, zsh-history

---

## Tech Stack

| Tool | Purpose |
|------|---------|
| Bun 1.3.11 | Runtime, package manager, test runner |
| Turborepo 2.8.13 | Monorepo orchestration |
| SolidJS | UI framework (TUI + web) |
| Electron | Desktop wrapper |
| SST v3 | Infrastructure (Cloudflare) |
| Hono | HTTP framework |
| Drizzle ORM | Database (PlanetScale MySQL) |
| Effect | Type-safe side effects |
| SQLite FTS5 | Local memory/search |
| Vite | Web bundler |
| Oxlint | Linting |

---

## Architecture

### Core Source (`packages/opencode/src/`)
| Directory | Description |
|-----------|-------------|
| `agent/` | Agent logic + prompt configuration |
| `actor/` | Actor/subagent spawning (spawn.ts is 34KB — largest file) |
| `memory/` | Persistent memory — SQLite FTS5, reconciliation |
| `control-plane/` | Workspace management, SSE streaming |
| `config/` | All configuration (provider, agent, MCP, LSP, etc.) |
| `cli/` | CLI entry points and command handling |
| `lsp/` | Language Server Protocol integration |
| `mcp/` | Model Context Protocol connections |
| `session/` | Session management, LLM loop, compaction |
| `tool/` | 20+ tool definitions |
| `provider/` | 18+ LLM provider abstractions |
| `skill/` | Skill system with discovery + composition |

### Infrastructure
- **Platform:** Cloudflare (Workers, KV, R2, Durable Objects)
- **Database:** PlanetScale (MySQL) for console; local SQLite for agent memory
- **Auth:** OpenAuth (GitHub/Google OAuth), device authorization flow
- **Domains:** opencode.ai, dev.opencode.ai, opncd.ai (short links)
- **Multi-tenant:** Workspace → Account → Org hierarchy

### Multi-Tenancy
- Workspaces (ULID) are top-level tenants
- Accounts can belong to multiple workspaces with roles (admin/member)
- Org model for Mimo Cloud with device authorization flow
- Team system for multi-agent collaboration

---

## Detailed Reports (sub-agent findings)

### Core CLI
- Full yargs CLI with 15+ commands
- SolidJS TUI with 30+ themes, 8 locales, VAD, sound effects, image rendering
- Session management with checkpoints, compaction, pruning, summaries, FTS
- 18+ AI SDK providers
- Max mode (ensemble reasoning with judge)
- Actor system for hierarchical agent spawning

### Web App + Desktop
- SolidJS web app with TanStack Query, TailwindCSS v4
- Features: AI chat, file tree, diff review, terminal (ghostty-web), settings, themes, notifications
- Electron desktop with native OS integration (file dialogs, clipboard, WSL, auto-updater)
- Deep link support (`opencode://` protocol)
- Multi-workspace via git worktrees

### SDK + Plugin
- SDK: client (OpenAPI-generated), server launcher, TUI launcher, v2 data helpers
- Plugin: 22 hooks, server + TUI types, tool registration, workspace adaptors
- Actor lifecycle hooks with matchers are the most sophisticated feature

### Console + Slack
- SolidStart web console with auth, billing (Stripe), analytics
- Slack bot integration for team notifications
- PlanetScale database with Drizzle ORM

### Enterprise + Auth
- Session sharing with S3/R2 storage
- OAuth 2.0 (GitHub/Google) via OpenAuth
- Local credential storage (0o600)
- Multi-workspace tenant model
- Device authorization flow for CLI

### Extensions + Containers
- 15 bundled extensions covering git, github, slack, vscode, playwright, etc.
- Dev Containers support (not sandboxing)
- Shared utilities (minimal — 5 files)

### Overall Architecture
- Turborepo + Bun monorepo with 16 packages
- SST v3 infrastructure on Cloudflare
- ~10M+ downloads by Jan 2026
- Developed internally on GitLab, pushed to GitHub
- Cross-platform binaries (darwin/linux/win32, arm64/x64)
