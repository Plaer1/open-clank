# S28 pa-Guru receipt — Punjabi (Gurmukhi) catalog authoring

Date: 2026-09-26
Locale: **pa-Guru (ਪੰਜਾਬੀ, Gurmukhi)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 pa-Guru translation author (continuation; prior session left ~1646-line in-progress diff)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated (non-identical) | ~7050 | **8181** |
| English-identical fallback | ~2572 | **1441** (all intentional) |
| Priority `treehouse.*` / `award.*` / `docs.*` | 3 code-seed intentional | **unchanged (3 seeds only)** |
| Keys authored this session | — | **1253** (1130 prose + brand/placeholder repairs) |

### What was authored this session

1. **Priority prefixes already complete** from prior work in the same worktree diff:
   - `award.oc.*` (74), `docs.openclank-docs-*` (24 titles + bodies), `treehouse.class/section/manifest/lesson.*` — all authored; only 3 `practice.seed` fixtures intentionally left as code.
2. **UI labels/messages (`ui.*`)** — 1130 remaining prose keys translated to natural Gurmukhi:
   choose/add/delete families, provider & endpoint errors, loading states, editor/files/notes copy,
   memory/export/recovery help, model-serving diagnostics, permission and workspace prose,
   Open Clank agent protocol errors, email/calendar strings, long help paragraphs.
3. **Repairs on earlier partial work** (found during validation):
   - 6 placeholder mismatches fixed (Notion header JSON, `@font-face` CSS block, `{{ message['content'] }}` templates, `{available or 'none configured'}` expression restored byte-exact).
   - 4 locked-brand drops fixed: **Meatbag Tasks** restored (was rendered as ਮੀਟਬੈਗ ਟਾਸਕ).

### Intentionally unchanged (exempt)

| Class | ~Count | Reason |
| --- | --- | --- |
| Skip-list fixtures / CSS class lists / selectors (`copal-*`, `rs-*`, `msg-*`) | ~1040 | not user prose (pre-existing skip lists) |
| Technical tokens / bare identifiers (`Ctrl+K`, `Esc`, `AWQ`, `Q2`, `yaml`, `br`, `href`) | ~450 | glossary + code exemption |
| Garbled / binary-looking fixtures (`aO!8ZQPO,5:WO`, `bWSMQOY#`) | ~40 | not translatable content |
| Locked brands & product names (Anthropic, Claude, Codex, Google, OpenAI, Odysseus, …) | ~80 | `brands.json` + `glossary.json` lock |
| `treehouse.lesson.*.practice.seed` code fixtures (3) | 3 | YAML props, Python sample, Markdown table |
| Format-only / metric labels (`4K — 3840 × 2160`, `bs 16`, `A4 (300dpi)`) | rest | technical constants |

House terms used consistently with existing pa-Guru.json:
ਟਿਕਾਣਾ (Location), ਦਸਤਾਵੇਜ਼ (document), ਚੈਟ/ਗੱਲਬਾਤ (chat), ਮੈਮਰੀ/ਯਾਦ (memory),
ਪਾਠ (lesson), ਟੀਚਾ (goal), ਕਾਰਜ (task), ਪ੍ਰਦਾਤਾ (provider), ਹੁਨਰ (skill),
ਵਰਕਸਪੇਸ (Workspace), ਐਡੀਟਰ (Editor), ਫਾਈਲਾਂ (Files), ਖੋਜ (Search),
ਸੈਟਿੰਗਾਂ (Settings), ਟੈਮਪਲੇਟ (template), ਸੁਰੱਖਿਆ (security).
Voice: concise labels; complete sentences for errors/help (matches file).

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, LCARS, Frankenmemory, MiMo, Meatbag Tasks, Field Guide,
plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` / `{{ var }}` preserved, none invented) |
| Locked-brand drops | **0** |
| `Open Clank` hyphenated | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Gurmukhi values | 8155 keys contain Gurmukhi script |
| `node scripts/i18n-catalog.mjs validate` | `pa-Guru: keys=9622 catalog=pa-Guru dir=ltr` — **0 pa-Guru errors** (4 pre-existing errors in sw/ur/fi, out of scope) |

Not run (out of this lease): browser acceptance `tests/i18n_browser_acceptance.mjs`,
full downstream suite, `openclank hex check .` as a separate command (hex CLI not on PATH
in this environment; contract digest read from `AGENTS.md` before mutation).

---

## Ambiguity / director notes

- `ui.a4.300dpi.2480.3508`, `ui.bs.16`, sort keys (`kind:asc`, `modified:desc`) left as technical constants.
- Theme names containing locked brand (`Clanker Emoji Drift`, `Clanker Matrix Rain`, `Clanker LCARS`) left intact so the brand token survives.
- `ui.no.meatbag.tasks.yet` sentence is Punjabi with brand preserved inside: "ਅਜੇ ਤੱਕ ਕੋਈ Meatbag Tasks ਨਹੀਂ ਹਨ।"
- Template expressions `{{ message['content'] }}` and CSS `@font-face` block restored to source code form (not translatable).
