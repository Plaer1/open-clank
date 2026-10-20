# S28 ko receipt — Korean catalog authoring

Date: 2026-09-26
Locale: **ko (한국어)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 ko translation author (Sol-only rule revoked 2026-09-26; `L-S28-MODEL-UNBINDABLE` closed)

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | 7222 | **8876** |
| English-identical fallback | 2400 | **746** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 code-seed intentional** |

### What was authored

1. **356 S27 structured-prose keys** (priority cohort):
   - `award.oc.*` — 74 achievement titles + summaries (playful names rendered as natural
     Korean achievement titles: 수하물 포함, 첫 빛, 시간 조각가, …)
   - `docs.openclank-docs-*.title/.body` — 24 handbook keys / 11 pages (Markdown structure
     preserved: headings, tables, lists, `[[wikilinks]]` retargeted to Korean page titles,
     `[text](clank://…)` app links, fenced code left byte-identical)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section
     strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title,
     result, whyThisHelps, practice.*) — instructional Korean consistent with existing
     `ko.json` voice
2. **~1300 UI labels/messages** (`ui.*`): short labels, dialogs, errors, empty states,
   settings copy, long help prose.

### Verification (post-edit, all green)

| Check | Result |
| --- | --- |
| Key parity | **9622 keys, exact set match with `en.json`** |
| Placeholder mismatches | **0** (English `{name}`/`{0}` sets preserved; reorder only where grammar needed) |
| Locked-brand drops | **0** (word-boundary check with Korean-particle awareness) |
| HTML introduced | **0** (no tags beyond source) |
| Bidi controls | **0** |
| `node scripts/i18n-catalog.mjs validate` | **pass** (`validated locales=69 keys=9622`; English-fallback warnings expected) |
| `node scripts/i18n-catalog.mjs manifests` | pass (generated 69; manifest files not committed) |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **13 passed** |
| Focused suite (i18n + treehouse + official docs) | **111 passed** |

Note: `python3 scripts/i18n_freeze.py --check` reports pre-existing drift
(`english_keys=9623` vs `freeze_keys=9622`); `en.json` is untouched by this session.

### Locked brands kept byte-identical (examples)

`Open Clank` (never hyphenated), `OpenClank`, `Copal`, `Clanker`, `Imps`, `Menmery`,
`Lore`, `TreeHouse`, `MiMo`, `Frankenmemory`, `LCARS`, `Matrix` (in
`Clanker Matrix Rain`), `Field Guide`, `Meatbag Tasks`, `Hugging Face`, `llama.cpp`.
Product surface names kept in English: Editor, Graph, Files, Chat, Wiki, Timeline,
Base, Galaxy, Compare, Settings→ panels.

### Intentionally unchanged (exempt: code / brands / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| Identifier / CSS class lists / selectors | ~506 | not user prose (`copal-btn …`, `adm-epApi…`, `action-{0}-{1}`) |
| Garbled / binary-looking fixtures | ~118 | not translatable content |
| Shell / code / regex / HTML fragments / command lines | ~87 | code exemption |
| Locked brand / protocol tokens / keybindings / bare identifiers | ~35 | `glossary.json` + `brands.json` lock (incl. `⌘Z`, `SKILL.md`, `SQLite DB`) |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) |

House terms consistent with existing `ko.json`: workspace→작업 공간, lesson→수업,
draft→초안, backlink→백링크, attachment→첨부 파일, appearance→외관, provider→프로바이더,
history→히스토리, restore→복원, undo→실행 취소. Class kept as product name (Class).

### Research / style notes

- Korean software UI conventions (해요체 for instructions, noun phrases for labels,
  `-할 수 없습니다` for errors) matching the existing 7222 translated strings.
- Latin brand + Korean particle attachment (`Open Clank은`, `Copal이`) follows the
  established `ko.json` convention; brand token remains byte-identical as a substring.
- Wikilink targets retargeted to Korean page titles so `[[…]]` resolution matches the
  translated `docs.*.title` values (e.g. `[[제한과 플랫폼 지원]]`).
- Clanker theme effect display names translated where the token is not locked
  (`Clanker 이모지 드리프트`, `Clanker 매트릭스 비` reverted to keep `Matrix`).

### Files in this commit

- `static/i18n/ko.json`
- `execution/s28/ko-receipt.md` (this file)

No other locales, no `.s28-*-scratch/`, no `static/manifest.*.json`.
