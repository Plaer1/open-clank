# S28 it receipt — Italian catalog authoring

Date: 2026-09-26
Locale: **it (Italiano)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 it translation author (Sol-only rule revoked 2026-09-26; `L-S28-MODEL-UNBINDABLE` closed)
Director review: parent assignment + gate-prep quality rules applied inline

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | ~4860 | **6427** |
| English-identical fallback | 4762 | **3195** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |
| Keys authored this session | — | **1567** |

### What was authored (1567 keys)

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (playful EN puns rendered as natural IT achievement titles; see notes)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]` retargeted to IT page titles, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1211 UI labels/messages** (`ui.*`): empty states (`Nessun…`), errors (`Impossibile…`), loading (`Caricamento…`), Add/Choose/Delete/Move/Create/Open/Search families, settings copy, provider/agent status, dialogs and confirmations.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors (`copal-*`, `rs-*`, `msg-*`, style attrs) | ~1200 | not user prose |
| Garbled / binary-looking fixtures (`hOOQO1G…`, `cFxF{PP6cGR…`) | ~150 | not translatable content |
| Shell / command / regex / SVG / HTML fragments / curl/docker/pip lines | ~150 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers (`Alt`, `css`, `Esc`, …) | ~250 | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) — code/user content |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |
| Format-only / metric labels (`4K — 3840 × 2160`, `Cpu_cores={0}` style) | rest | technical constants |

House terms used consistently with existing it.json + glossary:
Workspace→area di lavoro, Location→Location, document→documento, chat→chat,
Memory→memoria, lesson→lezione, Class→Classe, Base→Base, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, template→modello, checkpoint→checkpoint,
scoped export→esportazione con ambito, manifest→manifesto, achievement→traguardo,
durable goal→obiettivo persistente, preimage→preimage, draft→bozza,
guarded save→salvataggio protetto, provider→provider, skill→abilità, task→attività,
goal→obiettivo, backlink→backlink, shell→shell.
Voice: concise noun-phrase labels; complete sentences for errors/help (matches file).

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| Locked-brand drops (`glossary.json` + extras) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Markdown structure (docs bodies) | heading / wikilink / clank-link / table / fence counts preserved |
| `node scripts/i18n-catalog.mjs validate` | `it: keys=9622 catalog=it dir=ltr` — **0 it errors** (one pre-existing `ar` locked-token error unrelated) |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **13 passed** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command.

---

## Ambiguity / director notes

- **Award titles** are English puns; rendered as natural IT achievement titles with meaning kept:
  `Impish`→`Dispettoso` (Imps pun lost, noted), `Nothing Up My Sleeve`→`Niente dietro il risvolto`,
  `Return of the Byte`→`Il ritorno del byte`, `Same Clank, New Digs`→`Stesso Clank, nuova dimora`,
  `Up the Downpour`→`Il rovescio all'insù`, `House Colours`→`Colori della casa`.
- **`award.oc.house-colours.summary`** — "rehydrate" rendered **`reidratarsi`** to keep the product hydration metaphor used elsewhere in the UI.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** — those keys **do not exist** in the frozen catalog (only `.body`); headings stay `# Editor` / `# Graph` inside the body. No orphan writes.
- **Wikilinks** retargeted to Italian page titles (e.g. `[[Limiti e supporto della piattaforma]]`) matching the `.title` values, consistent with the ja run.
- **Product surface names** (Editor, Graph, Galaxy, Files, Chat, Timeline, Base, Brain) kept in English as product nouns; generic UI (Impostazioni, Aspetto, Provider, Cronologia, Accesso ai file) translated.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: language keyword list, not UI prose.
- Residual 3195 English-identical values are the exempt classes above (largely CSS class strings and code/commands). If the director wants any subset localized (e.g. technical error strings), they can be assigned as a follow-on slice.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/it.json` |
| Freeze rules / glossary consulted | `worktree/static/i18n/freeze/{rules,glossary,brands}.json` |
| Gate prep | `.clankers/robonotes/upstream-sync-2026-09-17/execution/s28/gate-prep.md` |
| Batch staging scripts (scratch, not committed) | `worktree/execution/s28/.s28-it-*.py` / `.s28-it-*.json` |
