---
title: Syntax Gallery
tags: [syntax, obsidian, fixture]
course: Copal Compatibility
skill: Obsidian Syntax Reading
depends_on: Copal Foundations
---

# Syntax Gallery

This note is fixture data for Copal readers. Plugin syntax here stays inert.

> [!warning]- Collapsed warning
> Callout body should render as a block and keep source editable.

## Tasks

- [ ] Parse recurring task #tasks [due:: 2026-07-20] [scheduled:: 2026-07-18] [repeat:: every week] [priority:: high]
- [x] Preserve done task ✅ 2026-07-08 #done

## Advanced Table

| Name | Status | Notes |
| :--- | :---: | ---: |
| Alpha | open | right |
| Beta | done | aligned |

## Embeds

![[Welcome to Copal.md]]
![Local image placeholder](Attachments/example.png)

Inline math $a^2 + b^2 = c^2$ should stay readable.

$$
E = mc^2
$$

Footnote reference[^copal-note].

[^copal-note]: Footnote text must be preserved.

```dataview
LIST FROM #syntax
WHERE status != "done"
```

```tasks
not done
tag includes #tasks
```

Templater stays inert: <% tp.date.now("YYYY-MM-DD") %>
