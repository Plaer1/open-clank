# S28 hr receipt — Croatian catalog authoring

Date: 2026-09-26
Locale: **hr (Hrvatski)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Director review: parent assignment + freeze glossary/rules + bs-receipt quality bar applied inline

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept, same key order as `en.json`) |
| Translated | 0 | **2493** |
| English-identical fallback | 9622 | **7129** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |
| UI (`ui.*`) translated | 0 | **2140 / 9266** |

### What was authored

1. **356 S27 structured-prose keys** (priority, complete):
   - `award.oc.*` — 74 achievement titles + summaries (English puns rendered as natural HR achievement titles)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]` retargeted to HR page titles, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 235 lesson fields across 52 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **2140 UI labels/messages** (`ui.*`): action labels, empty states, loading/saving, error messages (`Nije uspjelo…`, `Pogreška…`), settings copy, dialogs and confirmations, including long destructive-action confirms.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors (`copal-*`, `rs-*`, `msg-*`, style attrs) | ~920 | not user prose |
| Slug / token / constant strings (`admin-empty provider-control-empty`, `2d`, `Ctrl+S`) | ~1900 | not user prose |
| Shell / command / regex / SVG / HTML fragments / curl/docker/pip lines | ~180 | code exemption |
| Garbled fixture data (`}QUO`, `cFxF{PP6cGR!…`, `p!`!a%`) | included above | fixture junk |
| Locked brand / protocol tokens / bare tech identifiers | rest | `glossary.json` + `brands.json` lock |
| `treehouse.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) — code/user content |
| Format-only / metric labels (`4K — 3840 × 2160`, `Port{23}` style) | rest | technical constants |
| Residual UI prose not yet covered | rest | explicit English fallback (see Ambiguity notes) |

House terms used consistently with glossary (HR forms):
Workspace→radni prostor, Location→Lokacija, document→dokument, chat→chat/razgovor,
Memory→memorija, lesson→lekcija, Class→Classa, Base→Base, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, template→predložak, checkpoint→checkpoint,
scoped export→izvoz s ograničenim opsegom, manifest→manifest, achievement→postignuće,
durable goal→trajni cilj, preimage→preimage, draft→nacrt,
guarded save→zaštićeno spremanje, provider→pružatelj, skill→vještina, task→zadatak,
goal→cilj, backlink→povratna poveznica, shell→shell, Brain→Brain, Canvas→Canvas,
save→spremanje/spremiti, error→pogreška, button→gumb, screen→zaslon, folder→mapa,
attachment→privitak, section→odjeljak, table→tablica, interface→sučelje.
Voice: concise noun-phrase labels; complete sentences for errors/help (matches file).

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` (same order) |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| Locked-brand drops (`glossary.json` + extras) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Key order | preserved (matches `en.json` order) |
| Markdown structure (docs bodies) | heading / wikilink / clank-link / table / fence counts preserved |
| `node scripts/i18n-catalog.mjs validate` | `hr: keys=9622 catalog=hr dir=ltr` — **0 hr errors** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **11 passed** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command.

---

## Ambiguity / director notes

- **Award titles** are English puns; rendered as natural HR achievement titles with meaning kept:
  `Baggage Included`→`Prtljag uključen`, `Nothing Up My Sleeve`→`Ništa u rukavu`,
  `Return of the Byte`→`Povratak bajta`, `Same Clank, New Digs`→`Isti Clank, novo stanište`,
  `Up the Downpour`→`Kiša uzvodno`, `House Colours`→`Boje kuće`.
- **`award.oc.house-colours.summary`** — "rehydrate" rendered **`rehidrira`** to keep the product hydration metaphor used elsewhere in the UI.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** — those keys **do not exist** in the frozen catalog (only `.body`); headings stay `# Editor` / `# Graph` inside the body. No orphan writes.
- **Wikilinks** retargeted to Croatian page titles (e.g. `[[Ograničenja i podrška platformi]]`) matching the `.title` values. `[[Editor]]` / `[[Graph]]` left as product nouns (no title key).
- **Product surface names**: Editor→Editor, Files→Datoteke, Chat→chat/razgovor, Brain→Brain, Canvas→Canvas follow glossary; Graph, Timeline, Base, Wiki, Galaxy kept as product nouns. Locked brands byte-identical.
- **`treehouse.*.practice.seed` (3)** left English-identical: YAML props (`status: ready`), Python sample (`def summarize…`), Markdown table fixtures — code/user content.
- **`ui.add.ollama`** — brand `Ollama` kept byte-identical (`Dodaj Ollama`, not `Dodaj Ollamu`) to satisfy locked-brand check.
- **Related-language reference**: `bs.json` consulted for house terms and as a seed for UI coverage; Bosnian forms systematically converted to natural Croatian (`pružalac`→`pružatelj`, `čuvanje`→`spremanje`, `greška`→`pogreška`, `tabela`→`tablica`, `dugme`→`gumb`, `ekran`→`zaslon`, `folder`→`mapa`, `sinhronizacija`→`sinkronizacija`, …). Priority S27 strings were hand-authored in Croatian rather than mechanically converted.
- **Residual ~7100 English-identical values** are the exempt classes above (CSS class strings, tokens, garbled fixtures, code/commands) plus UI prose not covered in this slice. Coverage of user-visible UI prose is **2140 / 9266** keys; the remainder renders English via the explicit fallback. If the director wants the long tail localized (settings sections, onboarding, provider docs), it can be assigned as a follow-on slice.
- **Service worker** (`static/sw.js`) updated to precache `hr.json` — required by `test_service_worker_precaches_runtime_and_all_catalogs`.
- **Registry** `locales.hr.catalog` set to `"hr"` (was `"en"`). Freeze manifests (`freeze/locales.json`, `freeze/manifest.json`) were **not** updated (out of commit scope); `python3 scripts/i18n_freeze.py --check` will show pre-existing drift plus an expected hr catalog-kind change. Contract tests that read `registry.json` + `sw.js` pass.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/hr.json` |
| Registry update | `worktree/static/i18n/registry.json` (`locales.hr.catalog: "hr"`) |
| Service worker update | `worktree/static/sw.js` (precache `hr.json`) |
| Freeze rules / glossary consulted | `worktree/static/i18n/freeze/{rules,glossary,brands}.json` |
| Quality bar reference | `worktree/execution/s28/bs-receipt.md` |
