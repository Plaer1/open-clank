# S28 ru receipt — Russian catalog authoring

Date: 2026-09-26
Locale: **ru (Русский)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 ru translation author (manual resume from dirty worktree)

---

## Result

| Metric | Resume point (dirty worktree) | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 678 | **666** |
| `treehouse.*` / `award.*` / `docs.*` English | 3 code seeds | **3 code seeds (intentional)** |
| UI prose keys authored this session | — | **12** |

Prior work already present in the dirty worktree had completed the full S27
priority cohort (356 `treehouse.*` / `award.*` / `docs.*` keys) and ~1529 `ui.*`
keys (scratch batches `ui-tr-00`…`ui-tr-13`). This session finished the last
English-identical **prose** keys and verified the catalog end to end.

### What was authored this session (12 `ui.*` keys)

| Key | Russian |
| --- | --- |
| `ui.saved.9d4af690` | Сохранено. |
| `ui.share.a.registered.location.with.a.non.admin.user.their` | Поделитесь зарегистрированным расположением… (full share help) |
| `ui.value.registered.location.value.a.location.grants.no.people.or` | {0} зарегистрированных расположений{1}. Расположение само по себе не даёт доступа людям или агенту. |
| `ui.value.reset.value.durable.agent.permission.value.value` | {0}: сброшено долговременных разрешений агента: {1}{2}{3}. |
| `ui.value.value.value.compatibility.policy.value` | {0} · {1} · {2} · политика совместимости {3} |
| `ui.value.value.value.compatibility.root` | {0} · {1} · {2} · корень совместимости |
| `ui.value.value.bytes` | {0} · {1} байт |
| `ui.value.value.chars` | {0} · {1} символов |
| `ui.value.value.file` | {0}{1} файл |
| `ui.value.value.items` | {0}{1} элементов |
| `ui.value.value.mode.selected` | выбран режим {0}{1} |
| `ui.value.value.provider.is.ready` | провайдер {0}:{1} готов |

House terms taken from existing `ru.json`: Location→расположение, People→люди,
Agent→агент, Editor→Редактор, Copal vault→хранилище Copal, watchers→наблюдатели,
durable agent permission→долговременное разрешение агента,
compatibility policy/root→политика/корень совместимости.

### Verification (post-edit, all green)

| Check | Result |
| --- | --- |
| Key parity | **9622 keys, exact set match with `en.json`** |
| Placeholder mismatches | **0** (`{0}`/`{1}`/`{2}`/`{3}` sets preserved) |
| Locked-brand drops | **0** (Open Clank, Copal, Clanker, TreeHouse, Field Guide, Meatbag Tasks, MiMo, …) |
| HTML introduced | **0** |
| Bidi controls | **0** |
| `node scripts/i18n-catalog.mjs validate` | **pass** (`validated locales=69 keys=9622`; English-fallback warnings expected) |
| Manual `test_i18n_contract` assertions (all catalogs) | **0 fails / 365636 entries checked** |

Note: `pytest` is not installed in this worktree image; the contract assertions
were executed directly with the same logic as `tests/test_i18n_contract.py`.

### Locked brands kept byte-identical

`Open Clank` (never hyphenated), `OpenClank`, `Open Clanker`, `Copal`, `Clanker`,
`Imps`, `Lore`, `TreeHouse`, `Menmery`, `LCARS`, `Frankenmemory`, `MiMo`,
`Meatbag Tasks`, `Field Guide`, `Hugging Face`, `llama.cpp`.

### Intentionally unchanged (exempt: code / brands / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS class lists / selectors / style strings | ~240 | not user prose (`copal-btn …`, `display:flex;…`, `.toast.error`) |
| Garbled / binary-looking fixtures (`p!]!^!@Q…`) | ~80 | not translatable content |
| Shell / code / regex / SVG / command fragments | ~90 | code exemption |
| Locked brand / protocol / keybinding / bare identifier tokens | ~180 | `brands.json` lock + technical constants (`Ctrl+S`, `SKILL.md`, `Meatbag Tasks`, `Field Guide · {0}`) |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) |
| SQL keyword lists / format-only tokens | rest | technical constants (`{0} · {1} · Redb`, `active r{0}`) |

Remaining English-identical: **666** keys, all in the exempt classes above.
Zero sentence-like English prose remains in `ru.json`.

### Files in this commit

- `static/i18n/ru.json`
- `execution/s28/ru-receipt.md` (this file)

No other locales, no `.s28-*-scratch/`, no `static/manifest.*.json`.
