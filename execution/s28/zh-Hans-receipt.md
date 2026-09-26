# S28 zh-Hans receipt — Simplified Chinese catalog completion

Date: 2026-09-26
Locale: **zh-Hans (简体中文)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 zh-Hans translation author (user revoked Sol-only rule; authorship authorized)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 2349 | **876** |
| `treehouse.*` / `award.*` / `docs.*` English | 356 | **2 (code seeds — intentional)** |
| UI keys translated this session | — | **~1120** |
| Priority keys translated this session | — | **354** |

This session filled the full S27 priority cohort and the remaining real UI prose,
and repaired four locked-brand drops (`Meatbag Tasks`) carried in the prior catalog.

### Priority cohort (356 → 2 residual)

- **`award.*` (74)** — all achievement titles and summaries localized with natural
  achievement-name voice (e.g. *Baggage Included* → 行李随行, *Proof of Work* → 工作量证明).
- **`docs.*` (24)** — handbook titles and long-form bodies; Markdown structure,
  `clank://` destinations, wikilinks and code fences preserved. Wikilink display
  text matches translated page titles (`[[Getting Work Done]]` → `[[把工作做完]]`).
  Page titles that are product surfaces stay English where appropriate (`Graph`).
- **`treehouse.class.*` / `treehouse.manifest.*` / `treehouse.section.*` (20)**
- **`treehouse.lesson.*` (236 of 238)** — all lesson titles, bodies, explanations,
  practice titles/seeds/cleanup/expectedEvidence, results, whyThisHelps.
  Practice-seed fixtures kept as source: YAML props (`status: ready…`) and the
  Python docstring sample — same exemption as de/fr/ja. The typed-tables Markdown
  fixture **was** localized (headers + `sum(金额)`) to match de/fr.

### UI prose this session (~1120 `ui.*` keys)

- Label families: Add / Choose / Close / Delete / Download / Open / Search /
  Select / Move / Save / Reset / Share / Loading / Unknown …
- Error, status and empty-state messages (`This folder is empty`,
  `Unsaved changes`, `No models added yet`, `Preview unavailable`, …)
- Long help prose: agent permissions and tool policy, provider sign-in,
  workspace limits, Brain reset scope, import/export, history/retention,
  Field Guide progress and achievements, theme/effects
- `Open Clank agent …` server/protocol messages (agent ACP, lifecycle,
  provider catalog, goals/sessions owner rules)
- Placeholders preserved and reordered where grammar required (`{0}`/`{1}` sets
  unchanged); no HTML introduced; no bidi controls.

### Locked-brand fixes (cleared prior violations)

| Key | Was | Now |
| --- | --- | --- |
| `ui.meatbag.tasks` | 肉包任务 | Meatbag Tasks |
| `ui.meatbag.tasks.value` | 肉包任务·{0} | Meatbag Tasks · {0} |
| `ui.meatbag.tasks.value.value` | 肉包任务·{0}{1} | Meatbag Tasks · {0}{1} |
| `ui.no.meatbag.tasks.yet` | 还没有肉包任务。 | 尚无 Meatbag Tasks。 |

### Intentionally unchanged (exempt: code / CSS / brands / fixtures / cognates)

| Class | ~Count | Reason |
| --- | --- | --- |
| Code identifiers, CSS selectors/class lists, format tokens | ~610 | not user prose |
| Garbled / binary-looking fixtures (`aOOQO-…`, `dQPO'#…`) | ~125 | not translatable content |
| Shell / pip / docker / env fragments | ~27 | code exemption |
| CSS-in-string style dumps | ~14 | not user prose |
| Locked brands / tokens alone (Field Guide · {0}, A–Z, 4K — 3840 × 2160) | ~40 | `brands.json` lock / technical constants |
| `treehouse.lesson.*.practice.seed` code fixtures (2) | 2 | YAML props + Python sample |
| Other format-only / metric tokens | rest | technical constants |

House terms used consistently with existing zh-Hans.json: Workspace→工作空间,
document→文档, chat→聊天, Memory→记忆, lesson→课程, Base→Base, Graph→Graph/图,
Timeline→时间轴, Wiki→Wiki, template→模板, skill→技能, provider→提供商,
task→任务, note→笔记, file→文件, folder→文件夹, Class→Class, checkpoint→检查点,
draft→草稿, attachment→附件, theme→主题, handbook→手册.
Voice: 您-neutral product prose (existing catalog mixes 您/你; new strings use
neutral or 你 for actionable prompts to match dominant UI style).
Locked brands kept byte-identical: Open Clank, OpenClank, Copal, Imps, Lore,
TreeHouse, Menmery, LCARS, Clanker, Frankenmemory, MiMo, Field Guide,
Meatbag Tasks, plus provider brands and stable tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| Locked-brand drops (word-boundary) | **0** (after Meatbag Tasks fixes) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| JSON format | 2-space indent, trailing newline, UTF-8 |
| `node scripts/i18n-catalog.mjs validate` | `zh-Hans: keys=9622 catalog=zh-Hans dir=ltr` — **0 errors** (unchanged-English warnings expected and explicit) |
| `tests/test_i18n_contract.py` (run via import) | **7 passed, 0 failed** |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — pre-existing en-side drift, not from this change |

Not run (no pytest binary in this environment; contract tests executed by direct
import): browser acceptance `tests/i18n_browser_acceptance.mjs`, full downstream
suite.

---

## Ambiguity / director notes

- **`ui.open.clanker.tasks`-style compounds** — brand lock requires `Open Clank`
  substring intact; kept product names untranslated in compounds.
- **`Meatbag Tasks`** is a locked product name (glossary); prior 肉包任务
  translation dropped the brand and was corrected to keep the token.
- **`Graph` / `Editor` / `Files` / `Timeline` / `Wiki` / `Bases`** — product
  surfaces. Prose translates the common noun sense (图/编辑器/文件/时间轴);
  markdown app-link labels sometimes keep the surface name to match `clank://`
  destinations and existing catalog practice.
- **`Class`** (TreeHouse authored course) kept in English as a product term,
  consistent with de/ja; `lesson` → 课程.
- **`ui.what.is.open.clank.local.first.ai.assistant.multi.provider`** — "AI"
  kept as **AI** (not "人工智能") to match existing zh-Hans product-surface AI.
- Residual 876 English-identical values are the exempt classes above. A follow-on
  slice can target bare tokens if the director wants stricter localization.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/zh-Hans.json` |
| Analysis/translation scratch | `worktree/.s28-zh-scratch/` (not committed) |
| This receipt | `worktree/execution/s28/zh-Hans-receipt.md` |
