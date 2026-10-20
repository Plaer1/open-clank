# S28 pt-BR receipt — Portuguese (Brazil) catalog authoring

Date: 2026-09-26
Locale: **pt-BR (Português Brasil)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author model: this session (Sol-only rule revoked 2026-09-26)
Director review: parent assignment + glossary/rules applied inline
Base: `pt.json` (complete) adapted to Brazilian usage; existing `pt-BR.json` (4900 keys) preserved.

---

## Result

| Metric | Before | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 4722 | **794** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **352 authored / 4 intentional** |
| UI keys translated this session | — | **~3513 `ui.*`** (from adapted `pt.json`) + **90 authored** |

### What was authored

1. **356 S27 structured-prose keys** (priority):
   - `award.oc.*` — achievement titles + summaries (adapted from `pt.json`)
   - `docs.openclank-docs-*.title/.body` — handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — class/section/manifest strings
   - `treehouse.lesson.*` — lesson fields (body, explanation, title, result, whyThisHelps, practice.*)
2. **~3490 UI keys**: filled from `pt.json` with systematic PT-PT → PT-BR adaptation (vocabulary, gerund progressive, clitic removal, gender agreement).
3. **90 UI keys** authored directly (real prose left English-identical in `pt.json` exempt set): labels, errors, dialogs, help copy.

### Brazilian adaptations applied (pt-PT → pt-BR)

| PT-PT | PT-BR |
| --- | --- |
| ficheiro / ficheiros | arquivo / arquivos |
| utilizador | usuário |
| telemóvel | celular |
| aplicação | aplicativo |
| palavra-passe | senha |
| guardar / gravar (save) | salvar |
| descarregar | baixar |
| eliminar | excluir |
| gerir | gerenciar |
| ecrã | tela |
| definições | configurações |
| correr | executar |
| partilhar / partilha | compartilhar / compartilhamento |
| controlo / aspeto | controle / aspecto |
| secção / registo | seção / registro |
| projecto / objecto / acção / óptimo | projeto / objeto / ação / ótimo |
| selecção / seleccionar | seleção / selecionar |
| actual / excepção / artefacto | atual / exceção / artefato |
| câmara | câmera |
| contentor / separador | contêiner / aba |
| em direito (mistranslated "live") | ao vivo |
| A + infinitive progressive | gerund (Carregando, Salvando, …) |
| dizem-lhe / liga-se | dizem a você / conecta-se |
| já não | não é mais / não está mais |
| início de sessão | login |
| media | mídia |

Voice: concise labels; complete sentences for errors and help. Style matches existing `pt-BR.json`
(arquivo, salvar, usuário, você, baixar, senha, gerund progressive).

### Intentionally unchanged (exempt: code / names / fixtures)

| Class | Count | Reason |
| --- | --- | --- |
| Shell / command / path / pip / curl / docker | ~326 | code exemption |
| Garbled / binary-looking fixtures | ~166 | not translatable content |
| Symbols / short tokens / shortcuts / CSS values | ~239 | non-prose |
| CSS / class lists / selectors / event names | ~55 | not user prose |
| Locked brand / protocol tokens / bare identifiers | ~40 | `glossary.json` + `brands.json` lock |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (Python sample, YAML props, Markdown table) |
| `docs.openclank-docs-home.title` | 1 | product handbook title `Open Clank Handbook` |
| HTML/SVG fragments (`<span class=…`, `<svg`, `<think`) | 5 | code exemption; kept English-identical |
| Feature/theme names (Clanker Emoji Drift, Clanker LCARS, …) | 5 | product nouns |

Locked brands kept byte-identical in translations: Open Clank, Open Clanker, Copal, Clanker, Imps,
Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory, plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{name}` / `{0}` preserved, none invented) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| Markdown structure (docs bodies) | link/heading/bold counts preserved |
| Locked-brand drops | **0** |
| Hyphenated `Open-Clank` | **0** |
| `node scripts/i18n-catalog.mjs validate` | `pt-BR: keys=9622 catalog=pt-BR dir=ltr` — **0 pt-BR errors** (4 remaining errors are pre-existing `th`/`fi`/`ro`, not pt-BR) |

Not run (out of this lease): browser acceptance, full downstream suite.
`openclank hex explain` CLI was **not on PATH** in this session; pre-commit hex hook is the gate.

---

## Ambiguity / director notes

- **`ui.login` / `ui.login.value.value`** — English is already `(Login)` / `Login {0}{1}`; BR term is also "Login", so these are English-identical by design.
- **`ui.record.voice` / `ui.recorded` / `ui.stop.recording`** — "Gravar voz" / "gravado" / "Pare de gravar" kept: here "gravar" means *record*, which is correct BR.
- **`award.oc.impish.title`** — English pun on locked `Imps`. Rendered **«O Travesso»** (from `pt.json`).
- **`docs.openclank-docs-editor.title` / `docs.openclank-docs-graph.body`** — those title keys **do not exist** in the frozen catalog (only `.body`); headings stay in the body. No orphan writes.
- **Product surface names**: Settings→Configurações, Files→Arquivos, Workspace→Espaço de trabalho, Timeline→Linha do tempo (matching existing `pt-BR.json`). Graph, Editor, TreeHouse, Wiki kept as product nouns where existing style does.
- **`ui.add.all.allow.alter…` CQL keyword dump** left English-identical: language keyword list, not UI prose.
- **`ui.enable.enforce.eager`** — "Ativar enforce eager" keeps the ML toggle name in English (technical flag).
- **Gender fix on `salvamento`** (masculine): "Uma salvamento foi recusada" → "Um salvamento foi recusado"; "As salvamentos protegidas" → "Os salvamentos protegidos".
- **Idiom fix**: "nada corre mal" (literal "runs badly") → "nada dá errado" (goes wrong).
- Reverted `ui.think.*` to English-identical code fragments (`<think`), which a prior pass had mistranslated as `<pense`.
