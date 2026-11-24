# S28 el receipt — Greek catalog authoring

Date: 2026-09-26
Locale: **el (Ελληνικά)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 el translation finisher (continued in-progress S28 el session; no reset)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 3422 | **2608** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 3 code-seed / 0 / 0 | **3 code-seed intentional / 0 / 0** |
| Keys authored this session | — | **818** (591 IT-gap UI + 223 remaining prose + 4 term fixes) |

### What was authored (818 keys)

1. **Priority namespaces already complete at hand-off** (no reset; preserved existing 1341-key diff):
   - `award.oc.*` — 74 achievement titles + summaries
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` / `treehouse.lesson.*` — all prose fields
2. **591 UI keys that peer locale `it` had already authored but el still had in English** (Host/Import/Loading/Keep/Memory/Move/New/No-*/Open*/Provider/Reload/Remove/Reset/Save/Search/Select/Show/Start/Stop/This-*/Toggle/Workspace families, empty states, errors, dialogs, settings copy).
3. **223 remaining user-facing UI prose** (help text, Open Clank agent errors with brand kept, settings explanations, empty states, confirmations, onboarding copy).
4. **4 terminology corrections** for house consistency (`track`→`κομμάτι` per existing el.json; `Index out of range`→`Δείκτης εκτός ορίων`).

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors (`copal-*`, `rs-*`, `msg-*`, style attrs) | ~1200 | not user prose |
| Garbled / binary-looking fixtures (`cFxF{PP6cGR…`, `vQhO'#DrOOQO…`) | ~150 | not translatable content |
| Shell / command / regex / SVG / HTML / code fragments | ~150 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers (`Alt`, `Ctrl+S`, `CalDAV`, …) | ~250 | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |
| Format-only / metric labels (`4K — 3840 × 2160`, `X-Auth-Token`, `{6} KB`) | rest | technical constants |

House terms used consistently with existing el.json + prior S28 el batches:
Workspace→Χώρος εργασίας, Location→Τοποθεσία, document→έγγραφο, note→σημείωση,
chat→συνομιλία, Memory→Μνήμη, lesson→μάθημα, course→μάθημα, Class→Τάξη,
Base→Base, Timeline→Timeline, Graph→Graph, Wiki→Wiki, template→πρότυπο,
checkpoint→checkpoint, achievement→επίτευγμα, provider→πάροχος, skill→δεξιότητα,
task→εργασία, goal→στόχος, shell→κέλυφος, track→κομμάτι, Trash→Κάδος,
folder→φάκελος, badge→σήμα, quest→αποστολή, Field Guide→Field Guide, Meatbag Tasks→Meatbag Tasks.

Locked brands kept byte-identical in translations: Open Clank (never hyphenated), OpenClank, Open Clanker,
Copal, Imps, Lore, TreeHouse, Menmery, LCARS, Clanker, Frankenmemory, MiMo,
plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` / `{config_id!r}` preserved, none invented) |
| Locked-brand drops (brands.json + extras) | **0** |
| Hyphenated "Open Clank" | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Markdown structure (docs bodies) | heading / wikilink / clank-link / table / fence counts preserved (0 diffs) |
| `node scripts/i18n-catalog.mjs validate` | `el: keys=9622 catalog=el dir=ltr` — **0 el errors** (5 pre-existing errors in `sw`/`ur`/`uk`/`fi` unrelated) |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **12 passed, 1 failed** — failure is `sw:ui.moved.value.document.value.to.trash` placeholder mismatch (other locale, out of this lease) |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command.

---

## Ambiguity / director notes

- **Continuation, not reset**: existing 1341-line `el.json` diff (awards/docs/treehouse/UI batches 00–03) preserved and extended.
- **"Index out of range"** initially glossed as Ευρέτηση; corrected to Δείκτης εκτός ορίων.
- **"track"** (timeline lane) glossed as κομμάτι to match existing `ui.add.track` / `ui.edit.track` / `ui.delete.track`.
- **Open Clank agent error family** (~25 keys) translated with brand kept intact (e.g. «Ο πράκτορας Open Clank δεν εκτελείται»).
- **Slash commands and API verbs preserved** (`/open Cookbook`, `list, add, edit…`, `toggle, switch_model…`).
- Remaining 2608 English-identical values are legitimate technical tokens (CSS selectors, class lists, code, locked brands, format constants) — not a translation gap. Peer locale `it` left 3195 of the same class.
