# S28 hi receipt — Hindi catalog authoring

Date: 2026-09-26
Locale: **hi (हिन्दी)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 hi translation author (Sol-only rule revoked 2026-09-26; `L-S28-MODEL-UNBINDABLE` closed)
Director review: parent assignment + gate-prep quality rules applied inline

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | ~6675 | **8816** |
| English-identical fallback | ~2947 | **806** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |
| Keys authored this session | — | **2374** |

### What was authored (2374 keys)

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (playful EN puns rendered as natural HI achievement titles; see notes)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]` retargeted to HI page titles, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~2018 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, provider/agent status, loading states, Add/Choose/Delete/Move/Create/Open/Search families, long help prose, email/cookbook/admin copy.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors (`copal-*`, `rs-*`, `msg-*`, style attrs) | ~280 | not user prose |
| Garbled / binary-looking fixtures (`aOOQO-E…`, `bWSMQOY#`) | ~170 | not translatable content |
| Shell / command / regex / SVG / HTML fragments / pip install lines | ~80 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers (`Alt`, `API`, `Esc`, `Ctrl+B`, …) | ~300 | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` code fixtures (3) | 3 | YAML props, Python sample, Markdown table — user content / code |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |
| Format-only / metric labels (`4K — 3840 × 2160`, `cpu_cores={0}`, `min(700px, 95vw)`) | rest | technical constants |

House terms used consistently with existing hi.json + glossary:
Workspace→कार्यक्षेत्र, Location→स्थान, document→दस्तावेज़, chat→चैट,
Memory→मेमोरी/स्मृति, lesson→सबक, Class→Class, Base→Base/आधार, Timeline→टाइमलाइन,
Graph→ग्राफ़, Wiki→विकी, template→टेम्पलेट, checkpoint→चेकपॉइंट,
scoped export→स्कोप्ड निर्यात, manifest→मैनिफेस्ट, achievement→उपलब्धि,
durable goal→स्थायी लक्ष्य, preimage→प्रीइमेज, draft→ड्राफ्ट,
guarded save→सुरक्षित सहेजन, provider→प्रदाता, skill→कौशल, task→कार्य,
goal→लक्ष्य, backlink→बैकलिंक, shell→शेल, Editor→संपादक.
Voice: concise labels; complete sentences for errors/help (matches file).

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
| `node scripts/i18n-catalog.mjs validate` | `hi: keys=9622 catalog=hi dir=ltr` — **0 hi errors** (pre-existing warnings on other locales) |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **13 passed** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command (hex explain on
`static/i18n/hi.json` was run before mutation).

---

## Ambiguity / director notes

- **Award titles** are English puns; rendered as natural HI achievement titles with meaning kept:
  `Baggage Included`→**सामान साथ में**, `Impish`→**शरारती**, `Same Clank, New Digs`→**वही Clank, नया ठिकाना**,
  `Return of the Byte`→**बाइट की वापसी**, `Under the Markdown`→**Markdown के नीचे**,
  `Nothing Up My Sleeve`→**आस्तीन में कुछ नहीं**, `Not My First Rodeo`→**यह मेरा पहला रोडियो नहीं**.
- **`docs.openclank-docs-editor.body` / `docs.openclank-docs-graph.body`** — those keys have only `.body`
  (no `.title`); H1s stay `# Editor` / `# Graph` and wikilinks `[[Editor]]` / `[[Graph]]` stay English,
  matching ja/it treatment. Other wikilinks retargeted to HI page titles (`[[काम पूरा करना]]`,
  `[[फ़ाइलें और Imps]]`, `[[मेमोरी और Lore]]`, etc.).
- **Effect names** (Solid, Clanker Signal Routes, Shipibo Kene-Inspired Signal Weave, Clanker LCARS, …)
  kept English as product names, matching ja/it.
- **3 `practice.seed` code fixtures** left English: `house-connections.bases` (YAML),
  `house-documents.rich-markdown` (Python), `house-documents.typed-tables` (Markdown table).
  The other 20 instructional seeds were translated.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: language keyword list.
- Residual 806 English-identical values are the exempt classes above. If the director wants any
  subset localized (e.g. technical error strings like `filesystem request failed ({0})`), they can
  be assigned as a follow-on slice.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Translation staging (source of this run) | `.references/upstream-sync-2026-09-22/execution/s28/staging/*.json` (EN batches) |
| Updated catalog | `worktree/static/i18n/hi.json` |
| Freeze rules / glossary consulted | `worktree/static/i18n/freeze/{rules,glossary}.json` + `static/i18n/brands.json` |
| Gate prep | `.clankers/robonotes/upstream-sync-2026-09-17/execution/s28/gate-prep.md` |
