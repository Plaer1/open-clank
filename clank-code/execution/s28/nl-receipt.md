# S28 nl receipt — Dutch catalog authoring

Date: 2026-09-26
Locale: **nl (Nederlands)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author model: this session (Sol-only rule revoked 2026-09-26)
Director review: parent assignment + glossary/rules applied inline
Note: `execution/s28/gate-prep.md` was **not present** on disk; pt receipt + `freeze/glossary.json` + `freeze/rules.json` + existing `nl.json` were used as the quality bar.

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 4777 | **3040** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **352 authored / 4 intentional** |
| UI keys translated this session | — | **~1385 `ui.*`** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 37 achievement titles + 37 summaries (puns rendered in natural Dutch, e.g. *Full House* → *Vol huis*, *Return of the Byte* → *Terugkeer van de byte*)
   - `docs.openclank-docs-*.title/.body` — 12 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]` retargeted to Dutch titles, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 5 classes (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1385 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, Open Clank agent family, No-*/Failed-to-* families, long help prose.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors / SVG paths | ~1513 | not user prose |
| Shell / docker / curl / code identifiers | (in above) | code exemption |
| Locked brand / protocol tokens / bare identifiers / theme names | ~1527 | `glossary.json` + `brands.json` lock + non-prose (Clanker *, API, Alt, Cmd, …) |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) — user content / code |
| `docs.openclank-docs-home.title` | 1 | product handbook title `Open Clank Handbook` |

House terms used consistently with existing nl.json + glossary:
werkruimte, document, chat, geheugen, les, Class, Base, Timeline, Graph, Wiki, model, checkpoint,
afgebakende export, manifest, prestatie, duurzaam doel, preimage, concept, sjabloon, bijlage.
Voice: concise labels; complete sentences for errors and help. Style matches existing `nl.json`
(u-form, je/jouw, geen gij).

Locked brands kept byte-identical in translations: Open Clank, Open Clanker, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory, plus provider brands/tokens.
Clanker theme/effect names (Clanker Signal Routes, Clanker LCARS, …) kept in English as feature names.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Locked-brand drops | **0** |
| Hyphenated `Open-Clank` | **0** |
| `node scripts/i18n-catalog.mjs validate` | **0 nl errors** (4 remaining errors are pre-existing `th`/`fi`/`ro`, not nl) |

Not run (out of this lease): browser acceptance, full downstream suite.
`openclank hex explain` CLI was **not on PATH** in this session; pre-commit hex hook is the gate.

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **«Ondeugend»** rather than leaving "Impish".
- **`treehouse.lesson.house-stewardship.theme-effects.explanation`** — theme effect names (Clanker Signal Routes, Clanker LCARS, …) kept in English as product/feature names; descriptive prose translated.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** — those title keys **do not exist** in the frozen catalog (only `.body`); headings stay in the body. No orphan writes.
- **Product surface names**: Settings→Instellingen, Files→Bestanden, Workspace→werkruimte, Timeline kept as product noun, Graph/Wiki/Base/Class kept as product nouns in prose where existing style does.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: it is a language keyword list, not UI prose.
- **Wikilinks** retargeted to Dutch page titles (`[[Getting Work Done]]` → `[[Werk gedaan krijgen]]`), matching the pt pattern.
- Residual English-identical UI (~3040) is dominated by code/CSS/SVG fixtures (~1513) and locked brand/identifier/theme tokens (~1527) plus a long tail of short tokens not yet reached in this pass.
