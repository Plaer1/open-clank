# S28 ms receipt — Malay catalog authoring

Date: 2026-09-26
Locale: **ms (Bahasa Melayu)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 ms translation author
Director review: parent assignment + gate-prep quality rules applied inline

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 4812 | **7819** |
| English-identical fallback | 4810 | **1803** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **354 authored / 2 code-seed intentional** |
| Keys authored this session | — | **3007** (2516 merged from prior scratch + 491 newly authored) |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (English puns rendered as natural Malay achievement titles; locked brands kept)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]` retargeted to Malay page titles, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields (body, explanation, title, result, whyThisHelps, practice.*)
2. **~2651 UI labels/messages** (`ui.*`): empty states, errors, loading, Add/Choose/Delete/Move/Create/Open/Search families, settings copy, provider/agent status, dialogs and confirmations, save/share/track/workspace families, long help prose.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors (`copal-*`, `rs-*`, `msg-*`, style attrs) | ~1200 | not user prose |
| Garbled / binary-looking fixtures (`hOOQO1G…`, `cFxF{PP6cGR…`) | ~150 | not translatable content |
| Shell / command / regex / SVG / HTML fragments / curl/docker/pip lines | ~150 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers (`Alt`, `css`, `Esc`, …) | ~250 | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (2) | 2 | fixture seeds (YAML props, Python sample) — code/user content |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |
| Format-only / metric labels (`4K — 3840 × 2160`, `Cpu_cores={0}` style) | rest | technical constants |

House terms used consistently with existing ms.json + glossary:
Workspace→ruang kerja, Location→Lokasi, document→dokumen, chat→sembang,
Memory→Ingatan, lesson→pelajaran, Class→Kelas, Base→Asas, Timeline→Garis Masa,
Graph→Graf, Wiki→Wiki, template→templat, checkpoint→pusat pemeriksaan,
provider→pembekal, skill→kemahiran, task→tugasan, goal→matlamat, draft→draf,
guarded save→simpan terjaga, manifest→manifes, achievement→pencapaian,
persona→persona, vault→peti besi, Notes→Nota, Editor→Penyunting, Files→Fail.
Voice: concise noun-phrase labels; complete sentences for errors/help (matches file).

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Open Clanker, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| Locked-brand drops (`glossary.json` + extras) | **0** |
| HTML tags introduced | **0** (one prior-worker tag translation on `ui.think.time` reverted to source token) |
| Unicode bidi controls | **0** |
| Hyphenated `Open-Clank` | **0** |
| `node scripts/i18n-catalog.mjs validate` | `ms` errors: **0** (5 remaining errors are pre-existing `sw`/`ur`/`uk`/`fi`, not ms) |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | 12 passed; 1 failed on `sw` placeholder (pre-existing other-locale dirty file, not ms) |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite.

---

## Ambiguity / director notes

- **Award titles** are English puns; rendered as natural Malay achievement titles with meaning kept.
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** — those keys **do not exist** in the frozen catalog (only `.body`); headings stay in the body. No orphan writes.
- **Wikilinks** retargeted to Malay page titles matching the `.title` values.
- **Product surface names** (Editor→Penyunting, Files→Fail, Notes→Nota, Timeline→Garis Masa, Graph→Graf, Wiki, Base→Asas) translated per house style; locked brands kept.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: language keyword list, not UI prose.
- **`ui.think.time`** (`<think time="`) — prior scratch had translated the HTML-ish tag to `<fikir masa="`; reverted to source token (code fragment, no HTML translation).
- **`ui.open.clanker.tasks`** (`Open Clanker Tasks`) — prior scratch dropped the brand to `Buka Tugasan Clanker`; fixed to `Tugasan Open Clanker` (Open Clanker stays together).
- Residual 1803 English-identical values are the exempt classes above (largely CSS class strings, code/commands, brand tokens, and short technical identifiers). If the director wants any subset localized, they can be assigned as a follow-on slice.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/ms.json` |
| Freeze rules / glossary consulted | `worktree/static/i18n/freeze/{rules,glossary,brands}.json` |
| Prior-worker scratch (merged, not committed) | `worktree/.s28-ms-scratch/` |
| Batch staging (scratch, not committed) | `worktree/.s28-ms-scratch/tr/out*.json` |
