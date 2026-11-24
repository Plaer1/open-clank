# S28 fa receipt — Persian catalog completion

Date: 2026-09-26
Locale: **fa (فارسی, RTL)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 fa translation author (manual resume from dirty worktree; no reset)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 1147 | **1137** |
| `treehouse.*` / `award.*` / `docs.*` English | 3 | **2 (code seeds — intentional)** |
| Placeholder mismatches | 4 | **0** |
| Locked-brand drops | 0 | **0** |
| Bidi control characters | 0 | **0** |

Prior work in the dirty worktree had already translated ~2053 lines (full
`award.*` / `docs.*` / `treehouse.*` cohort except fixtures, plus UI batches
00–12 from `.s28-fa-scratch/tr/`). This session finished the residual
English-identical prose keys, localized the typed-tables fixture, and repaired
four placeholder mismatches carried in the prior catalog.

### Priority cohort

- **`award.*` (74)** — complete before this session (0 English left).
- **`docs.*` (24)** — complete before this session (0 English left).
- **`treehouse.*` (258)** — 256 localized; this session localized
  `treehouse.lesson.house-documents.typed-tables.practice.seed` (Markdown table
  headers + `sum(مقدار)`, structure kept) to match de/fr/zh-Hans.
- **2 residual `practice.seed` fixtures kept as source** — YAML props
  (`status: ready\ntopic: field-guide`) and the Python docstring sample, same
  exemption as de/fr/ja/zh-Hans.

### UI prose this session (11 keys + 4 placeholder repairs)

- `ui.cut.out` → برش زدن
- `ui.add.file.value.every.content.line.must.start.with` (error, `{path}` kept)
- `ui.import.staging.rename.target.already.contains.state`
- `ui.import.staging.source.remains.after.rename`
- `ui.import.staging.tombstone.already.contains.state`
- `ui.export.9662` → صادرات ▾ (plain Unicode triangle, no HTML entity)
- `ui.ln.value` → سطر {0}
- `ui.settings.providers` → تنظیمات → ارائه‌دهندگان
- `ui.phone1.phone2` → تلفن1، تلفن2
- `ui.tag1.tag2` → برچسب1، برچسب2

### Placeholder repairs (cleared verify gate)

| Key | Issue | Fix |
| --- | --- | --- |
| `ui.1.go.to.notion.so.my.integrations.2.create.a` | JSON example `{\"Authorization\"…}` dropped; step 5 text truncated | Restored full step 5 + exact JSON example |
| `ui.message.content` | `{{ message['content'] }}` internals translated | Restored literal template `{{ message['content'] }}` |
| `ui.no.integration.matching.value…` | `{available or 'none configured'}` partially translated | Restored exact `{available or 'none configured'}` |
| `ui.run.a.shell.command.return.exit.code.stdout.stderr` | Persian comma inside `{exit_code, stdout, stderr}` | Restored ASCII commas |

### Locked brands

Verified present in every source string that contains them (0 drops):
Open Clank (never hyphenated), Open Clanker, Copal, Imps, Lore, TreeHouse,
Menmery, LCARS, Clanker, Frankenmemory, MiMo, Meatbag Tasks, Field Guide.

### Intentionally unchanged (exempt: code / CSS / brands / fixtures / tokens)

| Class | ~Count | Reason |
| --- | --- | --- |
| Code identifiers, CSS selectors/class lists, format tokens | ~610 | not user prose |
| Garbled / binary-looking fixtures (`aOOQO-…`, `dQPO'#…`) | ~30 | not translatable content |
| Shell / pip / docker / env / tmux / PowerShell fragments | ~80 | code exemption |
| CSS-in-string style dumps and HTML attribute fragments | ~20 | not user prose; "no HTML" kept |
| Locked brands / product names (Field Guide · {0}, Meatbag Tasks, GitHub Copilot, xAI Grok, MiniMax M2 / M2.7, Clanker LCARS, Copal · Redb, …) | ~40 | `brands.json` lock / product names |
| Technical constants (4K — 3840 × 2160, A–Z, abc def…, Q4 / AWQ, HF ↗, auto 100%) | rest | technical constants |
| `treehouse.lesson.*.practice.seed` code fixtures (2) | 2 | YAML props + Python sample |

### Style notes

- House terms match existing fa.json: Settings→تنظیمات, Provider→ارائه‌دهنده,
  line→سطر, Export→صادرات, Import→واردات, Phone→تلفن, Tag→برچسب,
  Table→جدول, Amount→مقدار, Total→مجموع, Date→تاریخ, Item→مورد.
- Plain Persian only: no bidi controls (LRM/RLM/ALM/isolates/embeddings),
  no HTML introduced (Export triangle is U+25BE, not `&#9662;`).
- Placeholders `{0}` / `{path}` / `{timestamp}` / template braces preserved
  byte-identical to en.json.
- Markdown structure preserved (tables, `clank://` links, bold, code fences).
