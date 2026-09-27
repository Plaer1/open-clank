# S28 fi receipt — Finnish catalog completion

Date: 2026-09-26
Locale: **fi (Suomi)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 fi translation author

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 4773 | **3471** |
| `treehouse.*` / `award.*` / `docs.*` English | 356 | **2 (fixture seeds — intentional)** |
| UI / priority keys newly translated vs HEAD | — | **1302** |

Prior work had already translated 4849 values. The uncommitted worktree already held
the S27 priority cohort (356 treehouse/award/docs keys). This session authored **948
UI labels/messages**, kept that priority cohort, and fixed one brand-invariant slip.

### Priority cohort (356 keys) — COMPLETE

- **`award.oc.*`** (74) — achievement titles + summaries (wordplay kept natural in Finnish).
- **`docs.*`** (24) — 13 handbook titles + 11 long Markdown bodies, structure preserved
  (headings, lists, tables, `[[wikilinks]]`, `clank://` links, code fences).
- **`treehouse.class.*` / `section.*` / `manifest.*`** (20).
- **`treehouse.lesson.*`** (238) — 30 lessons across 5 Houses.

### UI pass this session (948 keys)

Long help/course blurbs (32), medium messages and settings copy (~370), short
labels and empty states (~550): Add/Create/Delete/Save/Cancel families, Loading…
family, No… empty states, confirmations with `{0}`, provider/agent errors,
permissions and Location flows, Field Guide / TreeHouse / Copal surface copy.

### Intentionally unchanged (exempt)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS class lists / selectors / inline style strings | ~600 | not user prose |
| Code / shell / command / log fragments (`Remove-Item`, `sudo dnf`, `[group]` logs) | ~500 | code exemption |
| Garbled / binary-looking fixtures (`aOOQO-E;s…`) | ~300 | not translatable |
| Locked brands + stable tokens (API, JSON, Markdown, provider brands) | ~400 | `brands.json` / `glossary.json` lock |
| Tech constants, measurements, keyboard maps, HTML entities | ~800 | technical constants |
| `treehouse.lesson.*.practice.seed` (2) | 2 | fixture seeds (YAML props, Python sample) |

## House terms (consistent with existing fi.json)

Workspace→työtila, document→asiakirja, chat→keskustelu, memory→Muisti,
lesson→oppitunti, template→malli, skill→taito, provider→tarjoaja, task→tehtävä,
note→muistiinpano, file→tiedosto, folder→kansio, permission→oikeus,
theme→teema, Graph→Graph, Timeline→aikajana, Editor→Editor, achievement→
saavutus/merkki, restore→palauta, publish→julkaise, learner→oppija,
draft→luonnos, clipboard→leikepöytä, guarded save→suojattu tallennus.

Locked brands kept (with Finnish case suffixes): Open Clank, OpenClank, Copal,
Clanker, Imps, Lore, TreeHouse, Menmery, LCARS, Frankenmemory, MiMo,
Meatbag Tasks, Field Guide, plus provider brands (Anthropic, OpenAI, Ollama, …).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{0}` / `{name}` preserved, none invented) |
| Locked-brand drops (stem match, Finnish suffixes allowed) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| JSON format | 2-space indent, trailing newline, UTF-8 |

## Ambiguity / director notes

- **Product surfaces** kept where English does: Editor, Base/Bases, Graph, Galaxy,
  Cookbook, Canvas, Persona, Wiki, Location, Handler, Brain, Odysseus.
- **`Timeline` → `aikajana`**; **`Memory` → `Muisti`**; **`Workspace` → `työtila`**.
- **`Field Guide` / `Meatbag Tasks` / `TreeHouse`** left as locked brands.
- **`practice.seed`** fixture values left as-is (YAML `status: ready`, Python sample).
- One prior string used *Treessa* for TreeHouse; corrected to *TreeHousessa* to keep
  the brand stem under the product-name rule.
- Residual 3471 English-identical values are the exempt classes above (CSS, code,
  brands, tech tokens, keyboard-test strings, fixture seeds). A follow-on slice can
  target any remaining prose the exempt filter missed.
