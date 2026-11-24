# S28 es-419 receipt — Spanish (Latin America) catalog authoring

Date: 2026-09-26
Locale: **es-419 (Español latinoamericano)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author model: this session (Sol-only rule revoked 2026-09-26; authorized for es-419)
Director review: parent assignment + `freeze/glossary.json` + `freeze/rules.json` applied inline
Note: `execution/s28/es-receipt.md` was **not present** on disk; `ja-receipt.md` / `vi-receipt.md` / `pt-receipt.md` + freeze artifacts + complete `es.json` were used as the quality bar. Regional base: **es.json** (already complete), adapted to LatAm vocabulary.

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | ~4909 | **8773** |
| English-identical fallback | 4712 | **849** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (playful English names rendered as natural LatAm achievement titles)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — class/section/manifest strings
   - `treehouse.lesson.*` — lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~3870 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, long help prose and tool descriptions.

Method: existing `es.json` (neutral international Spanish, complete) used as the regional base; every filled slot adapted for LatAm Spanish and re-checked against `en.json` placeholders.

### LatAm adaptations applied

| Source (es / Spain-leaning) | es-419 (LatAm) | Notes |
| --- | --- | --- |
| `Tecla API` / `llave API` | **`Clave API`** | API keys are *claves*, not keyboard *teclas* |
| `punto final` (endpoint calque) | **`endpoint`** (46 strings) | real software terminology over textbook literalism |
| `reproducción aleatoria` | **`selección aleatoria`** | model shuffle context |
| `recoger manualmente` | **`elegir manualmente`** | *coger* is vulgar in LatAm; wrong sense of "pick" |
| `paseo en coche` | **`paseo en auto`** | regional noun |
| decimal comma (`0,9`) | decimal period (`0.9`) | matches existing es-419 UI style |
| `Brave` MT errors (`Valiente`, `valiente.com`) | **`Brave` / `brave.com`** | product/brand names stay |
| `ARIZONA` / `Automóvil club británico` on `A-Z`/`Aa` keys | **`A–Z` / `Aa`** | sort/letter labels, not dictionary entries |

Kept (correct in both varieties): *computadora*, *aplicación*, *cargar*, *descargar*, *celular* only for the noun "cell phone" (all current `móvil` hits are the adjective "mobile", e.g. `Controles móviles`); *configuración*, *carpeta*, *usuario*, *espacio de trabajo*.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Count | Reason |
| --- | --- | --- |
| Tokens / identifiers / format constants / paths / keybindings | ~800 | not user prose |
| Locked brand / protocol tokens / provider names | rest | `glossary.json` + `brands.json` lock |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list, not UI prose |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (Python sample, YAML props, Markdown table) — user content / code; same exemption as ja/vi/pt |
| Shell / code / regex / SVG / HTML fragments | rest | code exemption |

House terms used consistently with existing es-419 + es.json + glossary:
Workspace→espacio de trabajo, Location→ubicación/Location, document→documento, chat→chat,
Memory→Memory/memoria, lesson→lección, Class→clase, Base→Base, Timeline→línea de tiempo,
Graph→Graph, Wiki→Wiki, template→plantilla, checkpoint→punto de control,
scoped export→exportación con ámbito, manifest→manifiesto, achievement→logro,
durable goal→meta duradera, preimage→preimagen. Voice: concise labels; complete sentences for errors and help.

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Open Clanker, Copal, Clanker, Imps, Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory, plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Markdown structure (docs bodies) | link/heading/wikilink/fence counts preserved |
| Locked-brand drops | **0** |
| Hyphenated `Open-Clank` | **0** |
| `node scripts/i18n-catalog.mjs validate` | `es-419: keys=9622 catalog=es-419 dir=ltr` — **0 es-419 errors** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **13 passed** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`, full downstream suite.
`openclank hex explain` CLI was **not on PATH** in this session; pre-commit hex hook is the gate.

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **«Travieso»** rather than leaving "Impish".
- **`award.oc.time-shaper.title`** — «El que da forma al tiempo» (es.json's «Dador de forma al tiempo» was unidiomatic).
- **`award.oc.room-to-roam.title`** — «Espacio para explorar» (es.json's «Espacio para deambular» too literary for a UI achievement).
- **`docs.openclank-docs-home.title`** — «Manual de Open Clank»; locked `Open Clank` kept intact (never hyphenated).
- **`treehouse.lesson.*.practice.seed` (3)** — left English: Python sample, YAML `status: ready` props, Markdown table fixture are user-content/code seeds (freeze rule: user documents and code snippets are never rewritten by UI translation). Same exemption ja/vi/pt recorded.
- **`ui.probe.endpoint`** — `/probe [endpoint]` kept as command syntax (argument name is a literal).
- **Product surface names**: Editor, Files, Graph, Timeline, Compare, Galaxy, Brain kept as product nouns where the existing catalogs do; Settings→Configuración.
- **`Brave`** is not in `brands.json` but is a product name; es.json's machine-translated «Valiente» / «valiente.com» were corrected in es-419.
- **`cellular` vocabulary** — no string in the catalog means "cell phone"; every `móvil` occurrence is the adjective "mobile" (controls, side preference) and stays `móvil` in LatAm too.
