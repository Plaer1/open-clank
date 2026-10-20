# S28 th receipt — Thai catalog authoring

Date: 2026-09-26
Locale: **th (ไทย)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Scope: finish in-progress Thai S28 translations in `static/i18n/th.json` (no reset)

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 7810 | **8197** |
| English-identical fallback | 1812 | **1425** |
| Priority S28 keys (`treehouse.*` / `award.*` / `docs.*`) | 5 English | **2 docs titles authored / 3 practice-seed fixtures intentional** |
| Locked-token errors in validate | 1 (`ui.open.clanker.tasks`) | **0** |

### What was authored this session

1. **Priority structured keys** (2 keys):
   - `docs.openclank-docs-home.title` — "Open Clank Handbook" → "คู่มือ Open Clank" (locked `Open Clank` kept)
   - `docs.openclank-docs-formatting-demo.title` — "Markdown Formatting Demo" → "ตัวอย่างการจัดรูปแบบ Markdown" (locked `Markdown` kept)
2. **Locked-token repair** (1 key):
   - `ui.open.clanker.tasks` — "Open Clanker Tasks" was "เปิด Clanker Tasks", which dropped the `Open Clank` brand substring and broke the `Open Clanker` compound. Fixed to "เปิด Open Clanker Tasks".
3. **~545 UI labels / messages / help strings** (`ui.*`): short labels, statuses, confirmations (`Delete activity "{0}"?` → `ลบกิจกรรม "{0}" หรือไม่`), loading/search/saving states, error paths (`Could not load {0} accounts: {1}`), formatted counters (`{0} activities · {1} assignments`), dialog copy, theme-name descriptors, `filename.py — description` module descriptors, and long help paragraphs (Memory trust, Brain reset, persona defaults, location registration, module gating, visibility ceiling).

### Intentionally unchanged (exempt: code / brands / fixtures / constants)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / style props / class lists / selectors (`copal-*`, `oc-*`, `provider-control-*`, `confirm-btn*`) | ~440 | not user prose |
| Garbled / binary-looking fixtures (`cFxF{PP6cGR…`, `]QYO…`, `nO+0fQUO…`) | ~110 | not translatable content |
| Shell / command / regex / SVG path / HTML attr / template-fragment / pip-docker-curl lines | ~230 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers (`Ctrl+B`, `Esc`, `Alt`, `API`, `CUDA`, `LoRA{0}`) | ~580 | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) — code/user content, never rewritten |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |
| Format-only / metric labels (`4K — 3840 × 2160`, `A4 (300dpi) — 2480 × 3508`, `Gravity ∝ 1 / \|anchor − today\|`) | rest | technical constants |
| Language endonyms (`Español`, `Português`, `Kiswahili`, `Bahasa Indonesia`) | 4 | keep as-is per catalog convention |

House terms used consistently with existing `th.json` + glossary:
Workspace→พื้นที่ทำงาน, Location→ตำแหน่ง, document→เอกสาร, chat→แชท,
Memory→หน่วยความจำ / Memory, lesson→บทเรียน, Class→Class, Base→Base, Timeline→Timeline,
Graph→Graph, Wiki→Wiki, template→เทมเพลต, checkpoint→checkpoint, manifest→manifest,
provider→ผู้ให้บริการ, skill→ทักษะ, task→งาน, goal→เป้าหมาย, achievement→ความสำเร็จ,
draft→ฉบับร่าง, shell→shell, persona→บุคลิก, evidence→หลักฐาน, quest→เควสต์.
Voice: concise noun-phrase labels; complete sentences for errors/help (matches file).

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Open Clanker, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands/tokens (OpenAI, Anthropic, Claude, Codex, Gemini, GitHub, Gmail, NVIDIA, Ollama, …).

---

## Validation

Run from the worktree (`cd …/execution/s28/worktree`):

```
node scripts/i18n-catalog.mjs validate
```

| Check | Result |
| --- | --- |
| Key parity vs `en.json` | 9622 / 9622 (exact set match) |
| Placeholder mismatches | **0** |
| Bidi controls | **0** |
| HTML tags | **0** |
| Locked-brand drops (`brands.json` + `stable_tokens`) | **0** |
| `Open Clanker` compound drops | **0** |
| Empty / non-string values | **0** |
| `error: th:…` lines in `i18n-catalog.mjs validate` | **0** |

Independent re-check (Python, same contract as `tests/test_i18n_contract.py:78`):
keys=9622, empty=0, placeholder mismatches=0, bidi=0, html=0, brand drops=0.

Note: `i18n-catalog.mjs validate` currently reports errors for **other** locales
(`fi`, `bg`, `hu`) that were already dirty in this shared worktree. Those are
out of scope for this receipt and are **not** committed here.

---

## Deliverable

- `static/i18n/th.json` — Thai catalog (9622 keys, 8197 translated, 1425 English-identical exempt fallbacks)
- `execution/s28/th-receipt.md` — this receipt

Commit scope: **only** the two files above. No other locales, no scratch
(`.s28-th-scratch/`, `execution/s28/.s28-th-*.py`), no `static/manifest.*`.
