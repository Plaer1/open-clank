# S28 pt receipt — Portuguese catalog authoring

Date: 2026-09-26
Locale: **pt (Português)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author model: this session (Sol-only rule revoked 2026-09-26)
Director review: parent assignment + glossary/rules applied inline
Note: `execution/s28/gate-prep.md` was **not present** on disk; ja/de receipts + `freeze/glossary.json` + `freeze/rules.json` + existing `pt.json` were used as the quality bar.

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 2532 | **880** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **352 authored / 4 intentional** |
| UI keys translated this session | — | **~1296 `ui.*` prose** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (merged from `.s28-pt-scratch/tr-award.json`)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1296 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, long help prose.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors | ~180 | not user prose |
| Garbled / binary-looking fixtures | ~166 | not translatable content |
| Shell / code / regex / SVG paths / HTML fragments | ~55 | code exemption |
| Locked brand / protocol tokens / bare identifiers / short tokens | ~475 | `glossary.json` + `brands.json` lock + non-prose |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (Python sample, YAML props, Markdown table) — user content / code |
| `docs.openclank-docs-home.title` | 1 | product handbook title `Open Clank Handbook` |

House terms used consistently with existing pt.json + glossary:
Espaço de trabalho, documento, chat, memória/Memory, lição, Classe, Base, Timeline/Linha do tempo,
Graph, Wiki, modelo, checkpoint, exportação com âmbito, manifesto, conquista, meta durável, preimage.
Voice: concise labels; complete sentences for errors and help. Style is Brazilian-leaning Portuguese
matching existing `pt.json` (arquivo, salvar, usuário, você, baixar, senha, clique em).

Locked brands kept byte-identical in translations: Open Clank, Open Clanker, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory, plus provider brands/tokens.
Also fixed pre-existing `Meatbag Tasks` mistranslation (4 keys).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Markdown structure (docs bodies) | link/heading counts preserved |
| Locked-brand drops | **0** |
| Hyphenated `Open-Clank` | **0** |
| `node scripts/i18n-catalog.mjs validate` | `pt: keys=9622 catalog=pt dir=ltr` — **0 pt errors** (2 remaining errors are pre-existing `ar`/`id`, not pt) |

Not run (out of this lease): browser acceptance, full downstream suite.
`openclank hex explain` CLI was **not on PATH** in this session; pre-commit hex hook is the gate.

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **«O Travesso»** rather than leaving "Impish".
- **`treehouse.lesson.house-stewardship.theme-effects.explanation`** — theme effect names (Clanker Signal Routes, Clanker LCARS, …) kept in English as product/feature names; descriptive prose translated.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.body`** — those title keys **do not exist** in the frozen catalog (only `.body`); headings stay in the body. No orphan writes.
- **Product surface names**: Settings→Configurações, Files→Arquivos, Workspace→Espaço de trabalho, Timeline→Linha do tempo (matching existing pt.json). Graph, Editor, TreeHouse, Wiki kept as product nouns in prose where existing style does.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: it is a language keyword list, not UI prose.
- **`ui.meatbag.tasks*`** were pre-existing brand violations; fixed to locked `Meatbag Tasks`.
