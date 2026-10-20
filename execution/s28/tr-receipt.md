# S28 tr receipt — Turkish catalog completion

Date: 2026-09-26
Locale: **tr (Türkçe)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 tr translation author (Sol-rule revoked; authorized session author)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 4808 | **3026** |
| `treehouse.*` / `award.*` / `docs.*` English | 356 | **2 (fixture seeds — intentional)** |
| UI / priority keys translated this session | — | **1792** |

Prior work had already translated ~4814 lines. This session authored the full S27
priority cohort (treehouse / award / docs, 356 keys) and a large UI prose pass.

### Priority cohort (356 keys) — COMPLETE

- **`award.oc.*`** (74) — all achievement titles + summaries. Wordplay titles kept
  in natural Turkish with brand voice (e.g. *Bagaj Dahil*, *İlk Işık*, *İş Kanıtı*).
- **`docs.*`** (24) — 13 handbook titles + 11 long Markdown bodies, structure
  preserved (headings, lists, tables, `[[wikilinks]]`, `clank://` links, code fences).
- **`treehouse.class.*` / `section.*` / `manifest.*`** (20).
- **`treehouse.lesson.*`** (238) — 30 lessons across 5 Houses: body, explanation,
  result, title, whyThisHelps, practice.* fields.

### Intentionally unchanged (exempt)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS class lists / inline style strings | ~390 | not user prose |
| Code / shell / command / log fragments | ~400 | code exemption |
| Locked brands + effect names (Open Clank, Copal, Clanker, Clanker LCARS Signal Routes, TreeHouse, Menmery, Lore, Imps, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory, provider brands) | ~250 | `brands.json` lock |
| Tech tokens, measurements, keyboard-test junk, HTML entities | ~1800 | technical constants |
| `treehouse.lesson.*.practice.seed` (2) | 2 | fixture seeds (YAML props, Python sample) |

## House terms (consistent with existing tr.json)

Workspace→Çalışma Alanı, document→belge, chat→Sohbet, memory→Hafıza,
lesson→ders, template→şablon, skill→yetenek, provider→Sağlayıcı, task→Görev,
note→Not, file→dosya, folder→klasör, token→anahtar, permission→izin,
theme→Tema, Graph→Graf, Timeline→Zaman Çizelgesi, Editor→Editor (product),
Gallery→Galeri, achievement→başarı, evidence→kanıt, restore→Geri Yükle,
publish→yayımla, learner→öğrenci, facet→faset, draft→taslak, clipboard→Pano.

Locked brands kept untranslated: Open Clank, OpenClank, Copal, Imps, Lore,
TreeHouse, Menmery, LCARS, Clanker, Frankenmemory, MiMo, Meatbag Tasks,
Field Guide, plus provider brands (Anthropic, Claude, Ollama, …).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{0}` / `{name}` preserved, none invented) |
| Locked-brand drops (word-boundary) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| JSON format | 2-space indent, trailing newline, UTF-8 |

## Ambiguity / director notes

- **Product surfaces** kept as product names where English does: Editor, Base/Bases,
  Galaxy, Mind (legacy structure-mode name), Cookbook, Capstone, Persona, Canvas.
- **`Graph` → `Graf`** (network graph sense); **`Timeline` → `Zaman Çizelgesi`**.
- **`Meatbag Tasks`** left as locked brand (not literal *Etçil Görevler*).
- **Effect names** (Clanker Signal Routes, Clanker LCARS, Shipibo Kene-Inspired
  Signal Weave, …) left untranslated as shipped theme identifiers.
- **`practice.seed`** fixture values left as-is (YAML `status: ready`,
  Python `def summarize(...)`); prose seeds were translated.
- Residual 3026 English-identical values are the exempt classes above (CSS, code,
  brands, tech tokens, keyboard-test strings). A follow-on slice can target any
  subset if the director wants stricter localization of bare tokens.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/tr.json` |
| Analysis scratch | `worktree/.s28-tr-scratch/` (not committed) |
| This receipt | `worktree/execution/s28/tr-receipt.md` |
