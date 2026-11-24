# S28 ur receipt — Urdu catalog authoring (RTL)

Date: 2026-09-26
Locale: **ur (اردو)** — RTL
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: this session (continued in-progress S28 Urdu work; no reset)

---

## Result

| Metric | Before (in-progress) | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 7848 (714-line diff already present) | **8539** |
| English-identical fallback | 1774 | **1083** (intentional exempt class) |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 3 `practice.seed` fixtures | **3 code-seed intentional; award/docs complete** |

### What was authored / finished

1. **Merged prior in-progress batches** (`.s28-ur-scratch/b-00` … `b-39`, 1559 keys) without reset.
2. **Priority namespaces completed**:
   - `award.oc.*` — already authored in prior work; 0 English-identical remain.
   - `docs.openclank-docs-*.title/.body` — already authored; 0 English-identical remain. Fixed `docs.openclank-docs-home.body` so `[Editor](clank://editor)` stayed a clank link (not a wikilink); heading/link/wikilink/list/bold counts now match `en.json` on every docs body.
   - `treehouse.*` — complete except 3 `practice.seed` fixtures (YAML props, Python sample, Markdown table) left English-identical on purpose.
3. **UI prose finish** (`.s28-ur-scratch/b-40` … `b-42`, 659 keys): labels, dialogs, errors, settings copy, long help prose, truncated `…` progress strings (ellipsis preserved).

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Reason |
| --- | --- |
| CSS / class lists / selectors / SVG paths / `style=` fragments | not user prose |
| Garbled / binary-looking fixtures | not translatable content |
| Shell / code / regex / HTML fragments / pip·docker·tmux commands | code exemption |
| Locked brand / protocol tokens / keybindings / bare identifiers | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3: YAML props, Python sample, Markdown table) | fixture seeds — user content / code |
| CQL keyword dump, format-only metric labels, `filter:*`, `*-task_id` tokens | technical constants |
| Effect names kept byte-identical (`Clanker Matrix Rain`, `Clanker LCARS`, …) | locked `Matrix` / `LCARS` tokens |

House terms used consistently with existing ur.json + glossary:
Workspace→ورک اسپیس, document→دستاویز, chat→چیٹ, Memory→میموری, lesson→سبق,
Location→مقام, Agent→ایجنٹ, Brain→دماغ, Editor→ایڈیٹر, Wiki→وکی, Base→بیس,
Settings→سیٹنگز, folder→فولڈر, People→لوگ, Handler→ہینڈلر, Field Guide→Field Guide.
Voice: concise UI labels; complete sentences for errors/help. Plain Urdu only —
**no bidi control characters**.

Locked brands kept byte-identical: Open Clank (never hyphenated), OpenClank (as source),
Open Clanker / Clanker, Copal, Imps, Lore, TreeHouse, Menmery, LCARS, Frankenmemory,
MiMo, Meatbag Tasks, Field Guide, Matrix, plus provider brands.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` / `{pattern!r}` preserved) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Locked-brand drops | **0** |
| Docs body Markdown structure | **0 mismatches** (heading/link/wikilink/list/bold counts) |
| `node scripts/i18n-catalog.mjs validate` | `ur: keys=9622 catalog=ur dir=rtl` — 0 ur errors |
| `pytest tests/test_i18n_contract.py …` | **not run** — not required for this lease |

Not run (out of this lease): browser acceptance, full downstream suite.

---

## Ambiguity / director notes

- **3 `treehouse.lesson.*.practice.seed`** values are code/fixture seeds left English-identical (same exemption as ar/ja).
- **1083 remaining English-identical `ui.*` keys** are the intentional exempt class (code/CSS/tokens/garbled fixtures/format strings), not missed prose. Comparable to ar’s 911 after its authoring pass.
- **`Clanker Matrix Rain`** restored to English effect name after catalog validation flagged `Matrix` as a locked token.
- Truncated progress strings keep the source `…` / `...` ellipsis.
