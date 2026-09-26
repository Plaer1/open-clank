# S28 sv receipt — Swedish catalog authoring

Date: 2026-09-26
Locale: **sv (Svenska)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author model: this session (Sol-only rule revoked 2026-09-26)
Director review: parent assignment + `freeze/glossary.json` + `freeze/rules.json` applied inline
Note: `execution/s28/gate-prep.md` was **not present** on disk; `pt-receipt.md` + freeze glossary/rules + existing `sv.json` were used as the quality bar.

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 4900 | **3653** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **352 authored / 4 intentional** |
| UI keys translated this session | — | **~895 `ui.*` prose** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~895 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, long help prose.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors | ~292 | not user prose |
| Garbled / binary-looking fixtures | included in short-token | not translatable content |
| Shell / code / regex / SVG paths / HTML fragments / identifiers | ~1240 | code / identifier exemption |
| Keyboard shortcuts / bare tokens / acronyms | ~250 | non-prose |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (Python sample, YAML props, Markdown table) — user content / code |
| `docs.openclank-docs-home.title` | 1 | product handbook title `Open Clank Handbook` |
| Remaining `ui.*` prose still English | ~1875 | out of this session's authoring budget; next pass target |

House terms used consistently with existing `sv.json` + glossary:
Arbetsyta, dokument, chatt, minne/Memory, lektion, Class, Base, Timeline/Graph/Wiki (product nouns),
modell, kontrollpunkt, avgränsad export, manifest, prestation, bestående mål, preimage.
Voice: concise labels; complete sentences for errors and help. Style matches existing `sv.json`
(du-form, natural UI Swedish, sentence-case labels).

Locked brands kept byte-identical in translations: Open Clank, Open Clanker, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory, plus provider brands/tokens.
Swedish compounds avoid hyphenating the brand itself (`Open Clank-appen` → `appen Open Clank`).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Markdown structure (docs bodies) | link/heading/table/fence counts preserved |
| Locked-brand drops | **0** |
| Hyphenated `Open-Clank` / `Open Clank-` | **0** |
| `node scripts/i18n-catalog.mjs validate` | `sv: keys=9622 catalog=sv dir=ltr` — **0 sv errors** |

Not run (out of this lease): browser acceptance, full downstream suite.
`openclank hex explain` CLI was **not on PATH** in this session; pre-commit hex hook is the gate.

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **«Busig»** rather than leaving "Impish".
- **`treehouse.lesson.house-stewardship.theme-effects.explanation`** — theme effect names (Clanker Signal Routes, Clanker LCARS, …) kept in English as product/feature names; descriptive prose translated.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.body`** — those title keys **do not exist** in the frozen catalog (only `.body`); headings stay in the body. No orphan writes.
- **Product surface names**: Settings→Inställningar, Files→Filer, Workspace→Arbetsyta, Timeline kept as product noun, Graph kept as product noun. Editor, TreeHouse, Wiki kept as product nouns in prose.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: language keyword list, not UI prose.
- Remaining ~1875 `ui.*` prose keys are still English-identical; they are ordinary labels/help text and are the natural next-pass target. Identities/CSS/code/shortcuts were deliberately left English per the code exemption.

---

## Files

- `static/i18n/sv.json` — catalog (only file besides this receipt in the commit)
- Scratch (not committed): `.s28-sv-scratch/`
