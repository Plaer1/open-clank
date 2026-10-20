# S28 bn receipt — Bengali catalog authoring

Date: 2026-09-26
Locale: **bn (বাংলা)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 bn translation author (finish pass over in-progress worktree)
Director review: parent assignment + gate-prep quality rules applied inline

---

## Result

| Metric | Before (worktree HEAD) | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | ~2910 (pre-diff) → 1563 (in-progress) | **948** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | done except 2 code seeds | **0 authored remaining / 2 code-seed intentional** |
| Keys changed vs git HEAD | — | **2158** (includes prior in-progress 1527 + this session) |
| Keys authored this session | — | **~632** |

### What was authored (this session)

1. **Priority confirmation**: `treehouse.*` / `award.*` / `docs.*` were already complete
   in the in-progress worktree except two `practice.seed` code fixtures (YAML props,
   Python sample) — left English by code-exemption rule.
2. **~632 UI labels/messages** (`ui.*`): confirm dialogs, status/error messages,
   settings copy, provider/agent/Open Clank status families, search/select/reset
   families, workspace/file/note actions, long help prose, permissions/sharing copy.

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS / class lists / selectors (`copal-*`, `oc-*`, `rs-*`, style attrs) | ~280 | not user prose |
| Garbled / binary-looking fixtures (`aOOQO-E…`, `bWSMQOY#`) | ~170 | not translatable content |
| Shell / command / regex / SVG / HTML fragments / pip install lines | ~80 | code exemption |
| Locked brand / protocol tokens / bare tech identifiers (`Ctrl+B`, `API`, `Esc`, …) | ~350 | `brands.json` + task glossary lock |
| `treehouse.lesson.*.practice.seed` code fixtures (2) | 2 | YAML props, Python sample |
| CQL keyword dump (`ui.add.all.allow.alter…`) | 1 | language keyword list |
| Format-only / metric labels (`4K — 3840 × 2160`, `cpu_cores={0}`, `min(700px, 95vw)`) | rest | technical constants |

House terms used consistently with existing bn.json + glossary:
Workspace→ওয়ার্কস্পেস, Location→লোকেশন, document→নথি, chat→চ্যাট,
Memory→মেমরি, lesson→পাঠ, Base→Base, Timeline→টাইমলাইন,
Graph→গ্রাফ, Wiki→Wiki, template→টেমপ্লেট, checkpoint→চেকপয়েন্ট,
manifest→ম্যানিফেস্ট, achievement→পুরস্কার, durable goal→টেকসই লক্ষ্য,
draft→খসড়া, provider→প্রদানকারী, skill→স্কিল, task→টাস্ক,
goal→লক্ষ্য, shell→শেল, Editor→Editor/এডিটর, folder→ফোল্ডার,
agent→এজেন্ট, settings→সেটিংস, note→নোট.
Voice: concise labels; complete sentences for errors/help (matches file).

Locked brands kept byte-identical in translations: Open Clank (never hyphenated),
OpenClank, Open Clanker, Copal, Clanker, Imps, Lore, TreeHouse, Menmery, LCARS,
Frankenmemory, MiMo, Meatbag Tasks, Field Guide,
plus provider brands/tokens from `brands.json`.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (official `\{(?:[A-Za-z_][A-Za-z0-9_]*\|\d+)\}` regex) | **0 mismatches** |
| Locked-brand drops (`brands.json` + task glossary) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Markdown structure (docs bodies) | heading counts preserved |
| `python3 -m pytest tests/test_i18n_contract.py` | **bn clean**; suite has a pre-existing `sw` placeholder failure (`ui.moved.value.document.value.to.trash`) unrelated to bn |
| Code-seed keys left English | 2 (intentional) |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command (hex explain on
`static/i18n/bn.json` was required before mutation by workspace contract).

---

## Ambiguity / director notes

- Template-literal / JSON-snippet strings that look like placeholders
  (`{ message['content'] }`, `{% if add_generation_prompt %}`,
  `{"Authorization": …}`) must keep those spans byte-identical; only surrounding
  prose is translated.
- `Meatbag Tasks` is a locked brand and must stay in Latin script even when the
  rest of the sentence is Bengali.
- CSS class lists, shell lines, and garbled fixture strings are correctly left
  English-identical; they are not unfinished work.
