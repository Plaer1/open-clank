# S28 pl receipt — Polish catalog authoring

Date: 2026-09-26
Locale: **pl (Polski)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 pl translation author (Sol-only rule revoked 2026-09-26; `L-S28-MODEL-UNBINDABLE` closed)
Director review: parent assignment + gate-prep quality rules applied inline

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 4886 | **6557** |
| English-identical fallback | 4736 | **3065** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |
| Keys authored this session | — | **1671** |

### What was authored (1671 keys)

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (English puns rendered as natural PL achievement titles; see notes)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]` retargeted to PL page titles, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1315 UI labels/messages** (`ui.*`): empty states (`Brak…`), errors (`Nie udało się…`), loading (`Ładowanie…`), Add/Choose/Delete/Move/Create/Open/Search families, settings copy, provider/agent status, dialogs and confirmations, onboarding step headings, single-word surface labels.

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

House terms used consistently with existing pl.json + glossary:
Workspace→obszar roboczy, Location→Lokalizacja, document→dokument, chat→czat,
Memory→pamięć, lesson→lekcja, Class→Klasa, Base→Base, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, template→szablon, checkpoint→punkt kontrolny,
scoped export→eksport z zakresem, manifest→manifest, achievement→osiągnięcie,
durable goal→trwały cel, preimage→preimage, draft→wersja robocza,
guarded save→chroniony zapis, provider→dostawca, skill→umiejętność, task→zadanie,
goal→cel, backlink→link zwrotny, shell→powłoka, Brain→Mózg, Canvas→Płótno.
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
| `node scripts/i18n-catalog.mjs validate` | `pl: keys=9622 catalog=pl dir=ltr` — **0 pl errors** |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **13 passed** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command.

---

## Ambiguity / director notes

- **Award titles** are English puns; rendered as natural PL achievement titles with meaning kept:
  `Baggage Included`→`Bagaż w cenie`, `Nothing Up My Sleeve`→`Nic w rękawie`,
  `Return of the Byte`→`Powrót bajta`, `Same Clank, New Digs`→`Ten sam Clank, nowe lokum`,
  `Up the Downpour`→`Deszcz pod prąd`, `House Colours`→`Barwy domu`.
- **`award.oc.house-colours.summary`** — "rehydrate" rendered **`ponownie nawadnia`** to keep the product hydration metaphor used elsewhere in the UI.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** — those keys **do not exist** in the frozen catalog (only `.body`); headings stay `# Edytor` / `# Graph` inside the body. No orphan writes.
- **Wikilinks** retargeted to Polish page titles (e.g. `[[Limity i wsparcie platformy]]`) matching the `.title` values, consistent with the it run.
- **Product surface names**: Editor→Edytor, Files→Pliki, Chat→czat, Brain→Mózg, Canvas→Płótno follow existing pl.json; Graph, Timeline, Base, Wiki, Galaxy kept as product nouns (consistent with it approach). Locked brands byte-identical.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: language keyword list, not UI prose.
- **`treehouse.lesson.*.practice.seed` (3)** left English-identical: YAML props, Python sample, Markdown table fixtures — code/user content (matches it run).
- Residual 3065 English-identical values are the exempt classes above (largely CSS class strings, tokens, and code/commands). If the director wants any subset localized (e.g. technical error strings or onboarding step labels not yet covered), they can be assigned as a follow-on slice.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/pl.json` |
| Freeze rules / glossary consulted | `worktree/static/i18n/freeze/{rules,glossary,brands}.json` |
| Gate prep | `.clankers/robonotes/upstream-sync-2026-09-17/execution/s28/gate-prep.md` |
| Batch staging scripts (scratch, not committed) | `worktree/execution/s28/.s28-pl-*.py` |
