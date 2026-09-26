# S28 id receipt — Bahasa Indonesia catalog completion

Date: 2026-09-26
Locale: **id (Bahasa Indonesia)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 id translation author (MiMoCode subagent; Sol-only rule revoked 2026-09-26)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 7070 | **8718** |
| English-identical fallback | 2552 | **904** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **354 authored / 2 code-seed intentional** |
| UI keys translated this session | — | **1644** (incl. S27) |

### What was authored this session

1. **356 S27 structured-prose keys (priority):**
   - `award.oc.*` — 74 achievement titles + summaries (puns rendered as natural ID achievement titles)
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1290 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, long help prose.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors / style strings | ~110 | not user prose |
| Shell / code / regex / SVG paths / HTML fragments | ~80 | code exemption |
| Garbled / binary-looking fixtures | ~90 | not translatable content |
| Locked brand / protocol tokens / keybindings / bare identifiers | ~450 | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (2) | 2 | fixture seeds (YAML props, Python sample) |
| Format-only / metric labels (`4K — 3840 × 2160`, CQL keyword dump) | rest | technical constants |

House terms used consistently with existing id.json + glossary:
Workspace→ruang kerja, Location→Lokasi, document→dokumen, chat→obrolan,
Memory→Memori, lesson→pelajaran, Class→Kelas, Base→Basis, Timeline→Garis Waktu,
Graph→Graf, Wiki→Wiki, template→templat, checkpoint→titik pemeriksaan,
scoped export→ekspor berlingkup, manifest→manifes, achievement→pencapaian,
durable goal→sasaran tahan lama, preimage→preimage, evidence→bukti,
provider→penyedia, task→tugas, skill→keterampilan, folder→folder, file→file.
Voice: concise labels; complete sentences for errors/help (matches file).

Locked brands kept byte-identical: Open Clank, Copal, Clanker, Imps, Lore,
TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| Locked-brand drops (word-boundary) | **0** (5 pre-existing drops repaired: Meatbag Tasks ×4, Matrix in Clanker Matrix Rain) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| `node scripts/i18n-catalog.mjs validate` | `id: keys=9622 catalog=id dir=ltr` — **0 id errors** (1 pre-existing `ar` locked-token `URL` error — not id) |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **13 passed** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command.

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **« Nakal Imps »** (keeps brand).
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** — those keys **do not exist** in the frozen catalog (only `.body`); headings stay `# Editor` / `# Graf` inside the body.
- **Theme effect names** (`Clanker Gem Drift`, `Clanker Matrix Rain`, …) — `Clanker` kept; descriptive half localized matching existing `Clanker Permata Melayang`. `Matrix` kept as stable token in `Clanker Hujan Matrix`.
- **`ui.meatbag.tasks*`** — existing translations had localized the locked brand to « Tugas Kantong Daging ». Repaired to keep **Meatbag Tasks** per glossary lock.
- **Product surface names** — Editor, Files, Graph→Graf, Timeline→Garis Waktu, Brain→Otak, Base→Basis, Galaxy kept as product nouns matching existing id.json usage.
- **`ui.add.all.allow.alter…`** CQL keyword dump left English-identical: language keyword list, not UI prose.
