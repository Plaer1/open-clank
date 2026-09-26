# S28 ar receipt — Arabic catalog authoring (RTL)

Date: 2026-09-26
Locale: **ar (العربية)** — RTL
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: this session (Sol-only rule revoked 2026-09-26; authorized)

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | ~7277 | **8711** |
| English-identical fallback | ~2345 | **911** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries
   - `docs.openclank-docs-*.title/.body` — 24 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section/manifest strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (title, body, explanation, practice.{title,seed,cleanup,expectedEvidence}, result, whyThisHelps)
2. **~1440 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, long help prose.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Reason |
| --- | --- |
| CSS / class lists / selectors / SVG paths | not user prose |
| Garbled / binary-looking fixtures | not translatable content |
| Shell / code / regex / HTML fragments / pip commands | code exemption |
| Locked brand / protocol tokens / keybindings / bare identifiers | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3: YAML props, Python sample, Markdown table) | fixture seeds — user content / code |
| CQL keyword dump, format-only metric labels | technical constants |

House terms used consistently with existing ar.json + glossary:
Workspace→مساحة العمل, document→مستند, chat→دردشة, Memory→الذاكرة, lesson→درس,
Class→فصل, Base→قاعدة, Timeline→الجدول الزمني, Graph→الرسم البياني, Wiki→ويكي,
template→قالب, checkpoint→نقطة تفتيش, scoped export→تصدير محدد النطاق, manifest→مانيفست,
achievement→إنجاز, durable goal→هدف مستمر, preimage→صورة مسبقة. Voice: concise labels;
complete sentences for errors/help. Plain Arabic only — **no bidi control characters**.

Locked brands kept byte-identical: Open Clank (never hyphenated), Open Clanker, Copal,
Clanker, Imps, Lore, TreeHouse, Menmery, LCARS, Frankenmemory, MiMo, Field Guide,
Meatbag Tasks (4 pre-existing translations corrected to keep the brand), plus provider brands.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Locked-brand drops | **0** (fixed 4 pre-existing Meatbag Tasks drops) |
| CJK / stray-script leftovers | **0** |
| Markdown structure (docs bodies) | heading/link/wikilink/list/bold counts preserved (13/13 bodies) |
| `node scripts/i18n-catalog.mjs validate` | `ar: keys=9266 dir=rtl` — 0 ar errors |
| `pytest tests/test_i18n_contract.py …` | **not run** — pytest unavailable in this environment |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, freeze `--check` (script not present at expected path).

---

## Ambiguity / director notes

- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **«شقيّ»** rather than leaving "Impish".
- **`treehouse.lesson.*.practice.*`** — nested keys (`practice.title` / `.seed` / `.cleanup` / `.expectedEvidence`), not a flat `practice` string. Three `.seed` values are code/fixture seeds left English-identical (same exemption as ja).
- **Product surface names** — translated to natural Arabic matching existing ar.json (المحرر، الملفات، الإعدادات، الرسم البياني، الجدول الزمني) except locked brands and effect names (Clanker LCARS, Shipibo Kene-Inspired Signal Weave, …) kept byte-identical.
- **911 remaining English-identical `ui.*` keys** are intentional exempt class (code/CSS/tokens/garbled fixtures/format strings), not missed prose.
