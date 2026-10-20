# S28 bs receipt — Bosnian catalog authoring

Date: 2026-09-26
Locale: **bs (Bosanski)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Director review: parent assignment + gate-prep quality rules applied inline

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 0 | **1712** |
| English-identical fallback | 9622 | **7910** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (English puns rendered as natural BS achievement titles)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 52 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1359 UI labels/messages** (`ui.*`): error messages (`Nije moguće…`, `Neuspješno…`), empty states (`Nema…`), loading (`Učitavanje…`), action labels (Add/Create/Save/Delete/Move/Open/Search families), settings copy, dialogs and confirmations.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors (`copal-*`, `rs-*`, `msg-*`, style attrs) | ~1040 | not user prose |
| Slug / token / constant strings (`admin-empty provider-control-empty`, `2d`, `Ctrl+S`) | ~900 | not user prose |
| Shell / command / regex / SVG / HTML fragments / curl/docker/pip lines | ~140 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers (`Alt`, `css`, `Esc`, …) | rest | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) — code/user content |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |
| Format-only / metric labels (`4K — 3840 × 2160`, `Port{23}` style) | rest | technical constants |

House terms used consistently with glossary:
Workspace→radni prostor, Location→Lokacija, document→dokument, chat→chat/razgovor,
Memory→memorija, lesson→lekcija, Class→Classa, Base→Base, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, template→predložak, checkpoint→checkpoint,
scoped export→izvoz s ograničenim obimom, manifest→manifest, achievement→dostignuće,
durable goal→trajni cilj, preimage→preimage, draft→nacrt,
guarded save→zaštićeno čuvanje, provider→pružalac, skill→vještina, task→zadatak,
goal→cilj, backlink→povratni link, shell→shell, Brain→Brain, Canvas→Canvas.
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
| Key order | preserved (matches `en.json` order) |
| Markdown structure (docs bodies) | heading / wikilink / clank-link / table / fence counts preserved |
| `node scripts/i18n-catalog.mjs validate` | `bs: keys=9622 catalog=bs dir=ltr` — **0 bs errors** |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **13 passed** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command.

---

## Ambiguity / director notes

- **Award titles** are English puns; rendered as natural BS achievement titles with meaning kept:
  `Baggage Included`→`Prtljag uključen`, `Nothing Up My Sleeve`→`Ništa u rukavu`,
  `Return of the Byte`→`Povratak bajta`, `Same Clank, New Digs`→`Isti Clank, novo stanište`,
  `Up the Downpour`→`Kiša uzvodno`, `House Colours`→`Boje kuće`.
- **`award.oc.house-colours.summary`** — "rehydrate" rendered **`rehidrira`** to keep the product hydration metaphor used elsewhere in the UI.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** — those keys **do not exist** in the frozen catalog (only `.body`); headings stay `# Editor` / `# Graph` inside the body. No orphan writes.
- **Wikilinks** retargeted to Bosnian page titles (e.g. `[[Limiti i podrška platforme]]`) matching the `.title` values.
- **Product surface names**: Editor→Editor, Files→Datoteke, Chat→chat/razgovor, Brain→Brain, Canvas→Canvas follow glossary; Graph, Timeline, Base, Wiki, Galaxy kept as product nouns. Locked brands byte-identical.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: language keyword list, not UI prose.
- **`treehouse.lesson.*.practice.seed` (3)** left English-identical: YAML props, Python sample, Markdown table fixtures — code/user content.
- **Service worker** (`static/sw.js`) updated to precache `bs.json` — required by `test_service_worker_precaches_runtime_and_all_catalogs`.
- Residual 7910 English-identical values are the exempt classes above (largely CSS class strings, tokens, and code/commands). If the director wants any subset localized (e.g. technical error strings or onboarding step labels not yet covered), they can be assigned as a follow-on slice.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/bs.json` |
| Registry update | `worktree/static/i18n/registry.json` (`locales.bs.catalog: "bs"`) |
| Service worker update | `worktree/static/sw.js` (precache `bs.json`) |
| Freeze rules / glossary consulted | `worktree/static/i18n/freeze/{rules,glossary,brands}.json` |
