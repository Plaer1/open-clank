# S28 vi receipt — Vietnamese catalog authoring

Date: 2026-09-26
Locale: **vi (Tiếng Việt)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author model: this session (Sol-only rule revoked 2026-09-26; `L-S28-MODEL-UNBINDABLE` closed)
Director review: parent assignment + gate-prep quality rules applied inline

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 4763 | **6353** |
| English-identical fallback | 4859 | **3269** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (playful names rendered as natural VI achievement titles)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1430 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, long help prose.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Count | Reason |
| --- | --- | --- |
| CSS class dumps / selectors | ~1271 | not user prose |
| Short tokens / identifiers / paths | ~1265 | bare tokens, keybindings, format constants |
| CSS property strings | ~240 | style rules, not UI copy |
| Garbled / binary-looking fixtures | ~163 | not translatable content |
| Shell / code / regex / SVG / HTML fragments | ~56 | code exemption |
| Locked brand / protocol tokens / effect names | rest | `glossary.json` + `brands.json` lock (Clanker effect names, Field Guide, Meatbag Tasks, etc.) |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (Python sample, YAML props, Markdown table) — user content / code |
| Format-only / metric labels (`4K — 3840 × 2160`, `A4 (300dpi)`) | rest | technical constants |

House terms used consistently with existing vi.json + glossary:
Workspace→Không gian làm việc, Location→Location, document→tài liệu, chat→trò chuyện/chat,
Memory→Memory/Bộ nhớ, lesson→bài học, Class→Class, Base→Base, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, template→mẫu, checkpoint→điểm kiểm tra,
scoped export→xuất theo phạm vi, manifest→manifest, achievement→thành tựu,
durable goal→mục tiêu bền vững, preimage→preimage. Voice: concise labels; complete sentences for errors/help (matches file).

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands/tokens (OpenAI, Anthropic, Claude, Codex, Gemini, etc.).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Locked-brand drops | **0** |
| Markdown structure (docs bodies) | link/heading/table structure preserved |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
`node scripts/i18n-catalog.mjs validate`, full pytest suite, `openclank hex check .`
as a separate command (scratch under `.s28-vi-scratch/` is untracked and not committed).

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **"Tinh Nghịch"** rather than leaving "Impish".
- **`award.oc.same-clank-new-digs.title`** — "Same Clank, New Digs" rendered **"Vẫn Clank Đó, Nhà Mới Thôi"** keeping locked `Clank` fragment natural.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** — those keys **do not exist** in the frozen catalog (only `.body`); headings stay `# Editor` / `# Graph` inside the body. No orphan writes.
- **Clanker background effect names** (Clanker Signal Routes, Clanker LCARS, Clanker Emoji Drift, etc.) left English-identical as locked product/effect names.
- **`ui.add.all.allow.alter.and.any.apply.as.asc.authorize`** CQL keyword dump left English-identical: language keyword list, not UI prose.
- **Garbled fixture strings** (`aOOQO-E;s…`, `c!|;'S(o…`) left byte-identical: not translatable content.
