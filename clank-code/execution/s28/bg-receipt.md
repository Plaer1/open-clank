# S28 bg receipt — Bulgarian catalog completion

Date: 2026-09-26
Locale: **bg (Български)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 bg translation finisher (in-progress catalog completed; no reset)

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9623 (1 spurious) | **9622** (parity with `en.json`) |
| Translated | ~6210 | **7573** |
| English-identical fallback | ~3414 | **2049** (exempt: code / CSS / brands / fixtures) |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 353 / 356 authored | **353 authored / 3 code-seed intentional** |
| Keys authored this session | — | **~1365** (plus 1 typo-key removal) |

### What was authored this session

1. **Priority already complete on arrival**: `award.oc.*` (74), `docs.openclank-docs-*` (24), `treehouse.*` (255/258) were already translated in the in-progress diff. The 3 remaining `treehouse.lesson.*.practice.seed` keys are intentional code seeds (YAML props, Python sample, Markdown table).
2. **UI gap vs completed locales (~783 keys)**: strings Italian and Polish both translated but bg still had in English — empty states (`No …`), errors (`… failed` / `… unavailable`), loading (`Зареждане …`), Import/Export/Move/Search/Filter/Open families, provider/settings copy, dialogs and confirmations.
3. **Leftover English sentences (~182)**: longer help/error copy still in English (session tips, install guidance, permission explanations, confirmation dialogs).
4. **Remaining UI labels (~404)**: short surface labels (`Export Memory`, `Hide done`, `Sort by due`, `Quick switcher`, `Skill points`, theme/state chips, `Open Clank agent …` status lines).
5. **Key repair**: removed spurious `ui.capture.useful.memories.autonomatically.…` (typo duplicate of `…automatically.…`, already correctly translated). Restored 9622-key parity.

### Intentionally unchanged (exempt)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / style attrs / selectors (`copal-*`, `rs-*`, `msg-*`, `display:flex…`) | ~1100 | not user prose |
| Slug / token / constants (`ADD_TOAST`, `adm-mcpCommand`, `2d`, `Ctrl+S`) | ~700 | not user prose |
| Shell / curl / docker / pip / SVG / HTML fragments | ~150 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers | ~250 | `brands.json` + extras lock |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds |
| Garbled / binary-looking fixtures (`cFxF{PP6cGR…`) | ~40 | not translatable |
| Format-only / metric labels (`4K — 3840 × 2160`, `X-Auth-Token`, `{7}s`) | rest | technical constants |

House terms used consistently with existing bg.json:
Workspace→работно пространство, Location→локация, document→документ, chat→чат,
Memory→памет/спомен, lesson→урок, Class→клас, Base→Base, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, template→шаблон, checkpoint→контролна точка,
manifest→манифест, achievement→постижение, provider→доставчик, skill→умение,
task→задача, goal→цел, draft→чернова, shell→shell, Trash→кошче, folder→папка,
view→изглед, Agent→агент. Quotes: „…“. Ellipsis: …

Locked brands kept byte-identical: Open Clank, OpenClank, Open Clanker, Copal, Clanker,
Imps, Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands/tokens. Open Clank never hyphenated.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{0}` / `{1}` / `{name}` preserved) |
| Locked-brand drops | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Hyphenated `Open Clank` introduced | **0** |
| `node scripts/i18n-catalog.mjs validate` | `bg: keys=9622 catalog=bg dir=ltr` — **0 bg errors** (4 pre-existing errors in sw/uk/fi unrelated) |

Not run: browser acceptance, full downstream suite, `openclank hex check .` as a separate command.

---

## Notes

- Did not reset the in-progress `bg.json` diff; extended it.
- Committed only `static/i18n/bg.json` + this receipt. Other locales, manifests, and `.s28-*-scratch/` trees left uncommitted.
- Remaining ~2049 English-identical keys are exempt technical tokens, matching the intentional remainder in it/pl receipts.
