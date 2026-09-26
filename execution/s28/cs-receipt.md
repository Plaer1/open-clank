# S28 cs receipt — Czech catalog authoring

Date: 2026-09-26
Locale: **cs (Čeština)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author model: this session (Sol-only rule revoked 2026-09-26)
Director review: parent assignment + glossary/rules applied inline
Note: `execution/s28/gate-prep.md` was **not present** on disk; `freeze/glossary.json` + `freeze/rules.json` + existing `cs.json` + `pt-receipt.md` were used as the quality bar.

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 4754 | **3584** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **352 authored / 4 intentional** |
| UI keys translated this session | — | **~1682 `ui.*` prose** |
| Translated total | 4868 | **6038** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1682 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, long help prose.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors / data-attributes | ~430 | not user prose |
| Garbled / binary-looking fixtures | ~220 | not translatable content |
| Shell / code / regex / SVG paths / HTML fragments | ~180 | code exemption |
| Locked brand / protocol tokens / bare identifiers / short tokens | ~2300 | `glossary.json` + `brands.json` lock + non-prose |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (Python sample, YAML props, Markdown table) — user content / code |
| `docs.openclank-docs-home.title` | 1 | product handbook title `Open Clank Handbook` |

House terms used consistently with existing cs.json + glossary:
pracovní prostor, dokument, chat, paměť/Menmery, lekce, Třída, Base, Timeline/Časová osa,
Graph/Graf, Wiki, model, kontrolní bod, export s rozsahem, manifest, ocenění, trvalý cíl, preimage.
Voice: concise labels; complete sentences for errors and help. Style matches existing `cs.json`
(ukládat, mazat, uživatel, kliknutím, nastavení).

Locked brands kept byte-identical in translations: Open Clank, Open Clanker, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory, plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Markdown structure (docs bodies) | heading/link/wikilink/fence/table counts **all preserved** |
| Locked-brand drops | **0** |
| Hyphenated `Open-Clank` | **0** |
| `node scripts/i18n-catalog.mjs validate` | `cs: keys=9622 catalog=cs dir=ltr` — **0 cs errors** |

Not run (out of this lease): browser acceptance, full downstream suite.
`openclank hex explain` CLI was **not on PATH** in this session; pre-commit hex hook is the gate.

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **«Rozpustilý»** rather than leaving "Impish".
- **`treehouse.lesson.house-stewardship.theme-effects.explanation`** / docs settings page — theme effect names (Clanker Signal Routes, Clanker LCARS, …) kept in English as product/feature names; descriptive prose translated.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.body`** — those title keys **do not exist** in the frozen catalog (only `.body`); headings stay in the body. No orphan writes.
- **Product surface names**: Settings→Nastavení, Files→Soubory, Workspace→pracovní prostor, Timeline→Časová osa (matching existing cs.json). Graph→Graf (existing cs.json already uses Graf). Editor, TreeHouse, Wiki, Base kept as product nouns in prose where existing style does.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: it is a language keyword list, not UI prose.
- Wikilinks inside docs bodies use the Czech page titles (`[[Jak se pustit do práce]]`, `[[Graf]]`, …) so intra-handbook links resolve to the translated titles.
- **`ui.meatbag.tasks*`** brand lock preserved as `Meatbag Tasks`.
