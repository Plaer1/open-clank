---
type: project-note
title: Welcome to Copal
tags: [copal, okf, treehouse]
course: Copal Foundations
status: active
---

# Welcome to Copal

Copal is a local-first Obsidian-class vault app with a task/timeline engine.

Open [[Treehouse Learning Map]] for skill-tree/LMS ideas.

## Immediate Tasks

- [ ] Read Markdown files from disk #reader #course/copal-foundations 📅 2026-07-15 🔼
- [/] Preserve Dataview blocks as inert readable blocks #reader [scheduled:: 2026-07-10]
- [x] Clone external references ✅ 2026-07-08

## Advanced Table Stub

| Feature | Reader status | Backend |
|---|---|---|
| Dataview | inert query block | vault index |
| Tasks | derived task records | vault index |
| Bases | YAML-ish reader | base parser |

## Dataview Reader Stub

```dataview
TASK
FROM #reader
WHERE !completed
```

## Templater Reader Stub

<% tp.date.now("YYYY-MM-DD") %>
