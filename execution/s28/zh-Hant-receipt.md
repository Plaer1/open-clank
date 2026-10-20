# S28 zh-Hant receipt — Traditional Chinese catalog authoring

Date: 2026-09-26
Locale: **zh-Hant (繁體中文)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 zh-Hant translation author (user revoked Sol-only rule; authorship authorized)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | ~5011 | **7764** |
| English-identical fallback | ~4611 | **1858** |
| `treehouse.*` / `award.*` / `docs.*` English | 356 | **3 (fixture seeds — intentional)** |
| Priority keys authored this session | — | **353** |
| UI keys authored / converted this session | — | **~2415** |

### Priority cohort (356 → 3 residual)

- **`award.*` (74)** — achievement titles and summaries with natural
  achievement-name voice (e.g. *Baggage Included* → 行李隨行, *Proof of Work* → 工作證明).
- **`docs.*` (24)** — handbook titles and long-form bodies; Markdown structure,
  `clank://` destinations, wikilinks and code fences preserved. Wikilink display
  text matches translated page titles (`[[Getting Work Done]]` → `[[搞定工作]]`).
  Product surfaces stay English where the product does (`Editor`, `Graph`, `Files`).
- **`treehouse.class.*` / `treehouse.manifest.*` / `treehouse.section.*` (20)**
- **`treehouse.lesson.*` (235 of 238)** — lesson titles, bodies, explanations,
  practice titles/seeds/cleanup/expectedEvidence, results, whyThisHelps.
  Three practice-seed fixtures kept as source: YAML props (`status: ready…`),
  the Python docstring sample, and the typed-tables Markdown sample —
  same code/fixture exemption as ja.

### UI work this session (~2415 `ui.*` keys)

1. **Hand-authored Traditional Chinese** (~900): labels (Add / Choose / Close /
   Delete / Download / Open / Search / Select / Move / Save / Reset / Share /
   Loading / Unknown …), errors, empty states, long help prose (agent
   permissions, provider sign-in, workspace limits, import/export, history,
   Field Guide progress, theme/effects), and `Open Clank agent …` messages.
2. **Converted from existing zh-Hans** (~1500) via OpenCC `s2twp` + house-term
   pass (工作空間→工作區, 檔案/文件 split, 還原/復原, 權杖, 行事曆, 使用者, 預設, …),
   then locked-brand repair.

Placeholders preserved (`{name}` / `{0}` sets unchanged, none invented).
No HTML tags. No Unicode bidi controls. Markdown structure preserved.

### Locked-brand repairs (cleared conversion/legacy violations)

| Key | Was | Now |
| --- | --- | --- |
| `ui.meatbag.tasks` | 肉包任務 | Meatbag Tasks |
| `ui.meatbag.tasks.value` | 肉包任務·{0} | Meatbag Tasks·{0} |
| `ui.meatbag.tasks.value.value` | 肉包任務·{0}{1} | Meatbag Tasks·{0}{1} |
| `ui.no.meatbag.tasks.yet` | 還沒有肉包任務。 | 還沒有 Meatbag Tasks。 |
| `ui.odysseus.app` | 奧德修斯應用程式 | Odysseus應用程式 |
| 13 `Memory *` keys | 記憶體… | Memory… (product surface) |

### Intentionally unchanged (exempt: code / CSS / brands / fixtures / cognates)

| Class | ~Count | Reason |
| --- | --- | --- |
| Nonprose catalog tokens (CSS values, selectors, format constants) | ~440 | not user prose |
| Code identifiers / CSS class lists / shell / pip / docker / env fragments | ~1000 | code exemption |
| Garbled / binary-looking fixtures (`aOOQO-…`, `dQPO'#…`) | ~150 | not translatable content |
| Locked brands / protocol tokens / keybindings / bare identifiers | ~250 | `glossary.json` lock |
| `treehouse.lesson.*.practice.seed` fixtures (3) | 3 | YAML / Python / Markdown samples |
| Short cognates and technical constants (`API`, `Q6`, `2d`, `4K — 3840 × 2160`) | rest | stay byte-identical |

House terms used consistently with existing zh-Hant.json + natural zh-Hant:
Workspace→工作區, document→文件, file→檔案, chat→聊天, Memory→Memory (product),
lesson→課程, Class→Class, Base→Base, Graph→Graph, Timeline→Timeline, Wiki→Wiki,
template→範本, checkpoint→檢查點, achievement→成就, goal→目標, export→匯出,
manifest→清單, draft→草稿, attachment→附件, theme→主題, handbook→手冊,
provider→提供者, token→權杖, key→金鑰, restore→還原, undo→復原.
Locked brands kept: Open Clank (never hyphenated), Copal, Imps, Lore, TreeHouse,
Menmery, LCARS, Clanker, Frankenmemory, MiMo, Field Guide, Meatbag Tasks.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Locked-brand drops (word-boundary) | **0** |
| `node scripts/i18n-catalog.mjs validate zh-Hant` | `zh-Hant: keys=9622 catalog=zh-Hant dir=ltr` — **0 errors** |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — **pre-existing en-side drift, not from this change** |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **13 passed** |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite.

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **「頑皮鬼」**
  rather than leaving "Impish".
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.title`** —
  those keys do not exist in the frozen catalog (only `.body`); headings stay
  `# Editor` / `# Graph` inside the body. No orphan writes.
- **Wikilinks** in docs bodies were aligned to translated page titles so they
  resolve (`[[Tasks and Continuations]]` / `[[Tasks and Continuity]]` both map
  to `[[任務與延續]]`).
- **`ui.is.not.supported`** is a trailing fragment (`" is not supported`);
  rendered as **「」不受支援** to keep the leading quote/bracket pairing with
  the caller string.
- **`Memory` as product surface** kept in English (matches existing catalog and
  `Menmery` lock); generic “memory” translated as 記憶.
- Typed-tables practice seed left as English Markdown fixture (headers +
  `sum(Amount)`); zh-Hans localized it, we followed the ja/de exemption for
  code/table fixtures.
