# S28 uk receipt — Ukrainian catalog authoring

Date: 2026-09-26
Locale: **uk (Українська)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 uk translation author (resume from dirty worktree + scratch batches)

---

## Result

| Metric | Resume point (dirty worktree) | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 4485 | **1601** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 3 code seeds | **3 code seeds (intentional)** |
| Keys authored / applied this session | — | **~2884** |

Prior work already present in the dirty worktree had completed the full S27
priority cohort (74 `award.oc.*`, 24 `docs.openclank-docs-*`, 258
`treehouse.*` fields) and part of the UI catalog. Scratch batches under
`.s28-uk-scratch/` supplied the remaining UI prose.

### What was authored / applied this session

1. **Applied scratch UI batches `ui-tr-00`…`ui-tr-10`** — 2266 already-translated
   `ui.*` labels/messages merged into `uk.json`.
2. **~590 additional UI prose keys** (labels, dialogs, errors, empty states,
   settings copy, TreeHouse/Timeline/Workspace messages, format strings with
   placeholders preserved exactly).
3. **Priority cohort verification** — `award.*` / `docs.*` / `treehouse.*`
   complete except 3 `practice.seed` fixture seeds (YAML props, Python sample,
   Markdown table) left byte-identical to English, matching other locales.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors (`copal-*`, `rs-*`, `msg-*`, style attrs) | ~700 | not user prose |
| Garbled / binary-looking fixtures (`hOOQO1G…`, `cFxF{PP6cGR…`) | ~120 | not translatable content |
| Shell / command / regex / SVG / HTML fragments / curl/docker/pip lines | ~120 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers (`Alt`, `css`, `Esc`, …) | ~350 | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (code/user content) |
| SQL/CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |
| Format-only / metric labels (`4K — 3840 × 2160`, `A4 (300dpi)…`) | rest | technical constants |

Remaining English-identical: **1601** keys, all in the exempt classes above.

House terms used consistently with existing `uk.json` + glossary:
Workspace→робочий простір, Location→Розташування, document→документ, chat→чат,
Memory→пам’ять/спогади, lesson→урок, Class→Class, Base→Bases, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, template→шаблон, checkpoint→контрольна точка,
scoped export→експорт з областю дії, manifest→маніфест, achievement→досягнення,
durable goal→тривала ціль, preimage→preimage, draft→чернетка,
guarded save→захищене збереження, provider→провайдер, skill→навичка,
task→завдання, goal→ціль, backlink→зворотне посилання, shell→оболонка,
folder→тека, attachment→вкладення, note→нотатка, Brain→Мозок.
Voice: concise noun-phrase labels; complete sentences for errors/help.

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Open Clanker,
Copal, Clanker, Imps, Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks,
LCARS, Frankenmemory, plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| Locked-brand drops (`glossary.json` + extras) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| `Open Clank` hyphenation | **0** |
| Markdown structure (docs bodies) | heading / wikilink / clank-link / table / fence counts preserved |
| `node scripts/i18n-catalog.mjs validate` | `uk: keys=9622 catalog=uk dir=ltr` — **0 uk errors** (4 pre-existing errors in `sw`/`ur`/`fi` unrelated) |
| Contract-style assertions (key parity, placeholders, HTML, bidi) | **pass** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `pytest` (not installed in this worktree image).

---

## Ambiguity / director notes

- **3 `practice.seed` keys** left English intentionally (fixture seeds: YAML
  properties, Python sample, Markdown table). Matches `it` / `ru` / other locales.
- **`ui.open.clanker.tasks`** rendered as «Відкрити завдання Open Clanker» to keep
  the locked `Open Clanker` token intact (validator rejects dropping `Open Clank`
  substring of `Open Clanker`).
- Theme effect names (`Clanker Emoji Drift`, `Clanker LCARS Status Sweep`, …)
  kept in English as locked brand composites.
- Remaining English-identical keys are CSS class lists, selectors, shell/Python
  snippets, garbled fixtures, brand tokens, and format-only constants — not
  user-facing prose.

---

## Commit scope

- `static/i18n/uk.json`
- `execution/s28/uk-receipt.md`

No other locales, manifests, or scratch/manifest files committed. No AI trailers.
