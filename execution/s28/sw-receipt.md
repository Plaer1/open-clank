# S28 sw receipt — Swahili catalog authoring

Date: 2026-09-26
Locale: **sw (Kiswahili)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 sw translation author

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | **9622** (parity with `en.json`) |
| Differs from English | ~7165 | **8770** |
| English-identical (non-empty) | ~2457 | **852** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 353 authored earlier + 3 seeds | **353 authored / 3 code-seed intentional** |
| Keys authored / applied this session | — | **~2005** (1605 staged scratch + ~400 new UI) |

### What was authored

1. **Priority (already staged in `.s28-sw-scratch/`, applied to `sw.json`)**:
   - `award.oc.*` — 74 achievement titles + summaries
   - `docs.openclank-docs-*` — 24 handbook pages (Markdown structure preserved)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` / `treehouse.lesson.*` — 255 lesson/class/manifest strings
2. **UI labels/messages (`ui.*`)** — ~400 additional user-facing strings this pass: sharing, workspace, folders/files, plans, tools, model-server, provider login, confirmations, empty states, errors, and placeholder-bearing status lines. Prior scratch batches (`ui00.py`…`ui2427.py`) covering ~1600 UI keys were applied at the start of this pass.

### Intentionally unchanged (exempt)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / selectors / style attrs / class lists | ~120 | not user prose |
| Garbled / binary-looking fixtures | ~165 | not translatable content |
| Shell / install / regex / SVG / template fragments | ~45 | code exemption |
| Brand / tech tokens / lang codes / format constants | ~520 | `glossary.json` + `brands.json` lock; technical constants |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |

House terms used consistently with existing `sw.json`:
Workspace→Nafasi ya kazi, Location→Eneo, document→hati, chat→gumzo,
Memory→Kumbukumbu, lesson→somo, Class→darasa, Base→msingi,
Timeline→Rekodi ya matukio, Graph→Grafu, template→kiolezo, draft→rasimu,
provider→mtoa huduma, skill→ujuzi, task→kazi, goal→lengo, Notes→Vidokezo,
agent→Wakala, permission→Ruhusa, folder→folda, file→faili,
attachment→kiambatisho, source→chanzo, model→modeli, editor→mhariri,
sidebar→upande wa pembeni, vault→kuba, track→wimbo, share→Shiriki.

Locked brands kept byte-identical: Open Clank, OpenClank, Copal, Imps, Lore,
TreeHouse, Menmery, LCARS, Clanker, Frankenmemory, MiMo, Meatbag Tasks,
Field Guide (e.g. `ui.meatbag.tasks` → `Meatbag Tasks`,
`ui.open.clank.field.guide` → `Field Guide ya Open Clank`).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** |
| Locked-brand drops | **0** (fixed Meatbag Tasks / Field Guide during this pass) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| `node scripts/i18n-catalog.mjs validate` | `sw: keys=9622 catalog=sw dir=ltr` — **0 sw errors** (2 pre-existing `fi` TreeHouse locked-token errors unrelated) |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **12 passed, 1 failed** — failure is `fi` locked-token (`TreeHouse`), pre-existing, unrelated to `sw` |

Not run: browser acceptance, full downstream suite, `openclank hex check .`
(`openclank` CLI not on PATH in this environment).

---

## Notes

- 3 `treehouse.lesson.*.practice.seed` keys left English-identical on purpose (fixture seeds), matching the it/other-locale convention.
- Residual 852 English-identical values are the exempt classes above. If any subset should be localized (e.g. technical error strings), assign as a follow-on slice.
- `ui.unsaved.changes` corrected to `Mabadiliko yasiyohifadhiwa` (literal "Unsaved changes").

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/sw.json` |
| Freeze rules / glossary consulted | `worktree/static/i18n/freeze/{rules,glossary,brands}.json` |
| Batch staging scripts (scratch, not committed) | `worktree/.s28-sw-scratch/` |
