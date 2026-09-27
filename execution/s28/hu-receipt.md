# S28 hu receipt — Hungarian catalog authoring

Date: 2026-09-26
Locale: **hu (Magyar)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`

---

## Result

| Metric | Before (HEAD) | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 4825 | **7620** |
| English-identical fallback | 4797 | **2002** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | mixed | **done — 3 code-seed intentional** |
| Keys authored this session | — | **~1170 new + 30 truncation/brand repairs** |

### What was authored

1. **Priority S27 structured-prose keys** (completed in the in-progress work, verified finished):
   - `award.oc.*` — achievement titles + summaries (English puns rendered as natural HU titles)
   - `docs.openclank-docs-*.title/.body` — handbook pages (Markdown structure preserved)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` / `treehouse.lesson.*`
2. **~1170 UI labels/messages** (`ui.*`) completed this session via majority-vote against
   de/fr/ru/tr/pl/nl/it/cs/sv: empty states, errors (`Nem sikerült…`), loading, Add/Choose/
   Delete/Move/Create/Open/Search families, settings copy, provider/agent status, dialogs,
   onboarding step headings, single-word surface labels, confirmation prompts.
3. **30 repair fixes** on the in-progress file:
   - 26 mid-sentence truncations (previous pass cut long strings with `…`) rewritten in full
   - 2 Copal brand drops restored (`ui.move.value.to.copal.trash*`)
   - 1 truncated placeholder string restored (`{5}/{6},{7}` kept)
   - 1 code block (`ui.mlx.metallib…`) restored to English-identical (code exemption)

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| Single-token / const / slug (`Ctrl`, `span`, `2d`, `backend={0}`) | ~1226 | not user prose |
| CSS / class lists / selectors / HTML fragments | ~20 | not user prose |
| Command / regex / SVG / curl/docker/py lines | rest | code exemption |
| Locked brand / protocol tokens / bare tech identifiers | rest | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) |
| Format-only / metric labels (`Port{23}`, `Flash Attn{1}`) | rest | technical constants |

House terms used consistently with existing hu.json:
Workspace→munkaterület, Location→Hely, document→dokumentum, chat→csevegés,
Memory→memória, lesson→lecke, Class→osztály, Base→Base, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, Galaxy→Galaxy, template→sablon, checkpoint→ellenőrzőpont,
provider→szolgáltató, skill→készség, task→feladat, goal→cél, shell→shell,
Brain→Agy, Canvas→Vászon, achievement→teljesítmény, manifest→manifestum.
Voice: formal address (magázódó) matching the dominant existing file voice;
concise noun-phrase labels; complete sentences for errors/help.

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json`; key order preserved |
| Placeholder sets (all keys) | **0 mismatches** (`{0}`/`{name}` preserved, none invented) |
| Locked-brand drops (`glossary.json` + extras) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Mid-sentence truncations | **0** (26 pre-existing repairs verified) |
| `node scripts/i18n-catalog.mjs validate` | **0 hu errors** (3 errors present are pre-existing in `fi`/`bg`, other leases) |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | 11 passed, 2 failed — **failures are `fi`/`bg` side, not hu**; hu-only assertion: **PASS** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite.

---

## Ambiguity / director notes

- **Award titles** are English puns; rendered as natural HU achievement titles with meaning kept.
- **Product surface names**: Editor→Editor, Files→Fájlok, Chat→csevegés, Brain→Agy,
  Canvas→Vászon follow existing hu.json; Graph, Timeline, Base, Wiki, Galaxy kept as product nouns.
- **`ui.mlx.metallib…`** left English-identical: shell/Python code block, not UI prose.
- **`treehouse.lesson.*.practice.seed` (3)** left English-identical: YAML/Python/Markdown fixtures.
- Residual 2002 English-identical values are the exempt classes above (largely tokens,
  single-word consts, CSS selectors, and code/commands). Comparable to de (2212) and
  ahead of pl (3065) / it (3195). Follow-on slice can localize any remaining subset on request.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/hu.json` |
| Freeze rules / glossary consulted | `worktree/static/i18n/freeze/{rules,glossary,brands}.json` |
| Batch staging (scratch, not committed) | `worktree/.s28-hu-scratch/` |
