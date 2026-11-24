# Included component and asset provenance

Reviewed October 4, 2026. [Acknowledgments](../ACKNOWLEDGMENTS.md) is the reader entry point. This record maps included paths, pins and notices; dependency manifests are not a claim every listed package ships. Component licenses apply to their own material. Root AGPL terms do not replace them. No reference payload is a runtime or build authority.

## Integrated source roots

| Root | Source and acquisition | Included scope / modifications | Retained notice |
| --- | --- | --- | --- |
| Odysseus | [odysseus-dev/odysseus](https://github.com/odysseus-dev/odysseus), incorporated base `d96c7af3df769508de01900b2264520b649caa4c` per canonical upstream-sync receipt | Inherited Python/web workspace across app.py, src/, routes/, services/, static/; Open Clank branding, native/editor/agent integrations and later changes. Pin is an incorporated base, not a claim of full later upstream parity. | AGPLv3 root [LICENSE](../LICENSE); original source notices preserved. No separate author name invented. |
| MiMo Code / opencode | [XiaomiMiMo/mimo-code](https://github.com/XiaomiMiMo/mimo-code); declared upstream `c9c91d7b6fff20164629475218fa0a58e16d14b8`, import `f53c8a3e86f258164ff47eae00a03559a38895cf`, per [vendor manifest](../packages/mimo-code/openclank-vendor.json) | packages/mimo-code/, including packages/opencode/; managed engine, credentials/provider/IPC/history adaptations. The manifest pin is recorded provenance, not a claim every current modified byte equals upstream. | [MiMo/opencode MIT](../packages/mimo-code/LICENSE): Xiaomi Corporation / MiMo Code 2026 and opencode 2025; [historical opencode notice](opencode-MIT-LICENSE.txt). |
| Copal | Local integrated package; package version 0.2.0. No verified public upstream/author or separate package-root license in the inspected source/ledger. | packages/Copal/, src/openclank/copal_*.py, routes/copal_*.py, static/js/copal*; Editor/Wiki/database/native-shell integration. First-party root terms apply where appropriate; no inferred third-party Copal license is assigned. | Existing per-file/dependency notices preserved. Unknown public origin/package-license disposition is explicit. |
| Lore | [EpicGames/lore](https://github.com/EpicGames/lore/tree/317f48b918f0b4d43de104d99e80ac4a57b697df), `317f48b918f0b4d43de104d99e80ac4a57b697df`, v0.14.0 (workspace 0.13.1-nightly) | packages/openclank-history/vendor/lore/ storage dependency closure; Open Clank history service wrapper and explicit durability/compaction repairs. This is not the complete Lore desktop product. | [Epic Games Inc.2026 MIT](../packages/openclank-history/vendor/lore/LICENSE); nested [glob-match MIT](../packages/openclank-history/vendor/lore/vendor/glob-match/LICENSE), [quinn-proto MIT](../packages/openclank-history/vendor/lore/vendor/quinn-proto/LICENSE-MIT)/[Apache](../packages/openclank-history/vendor/lore/vendor/quinn-proto/LICENSE-APACHE), [rpmalloc](../packages/openclank-history/vendor/lore/lore-base/native/thirdparty/rpmalloc/LICENSE). |

## Adapted code and design studies

llmfit (Alex Jones, MIT): services/hwfit/, Cookbook routes/UI and scripts/odysseus-cookbook; [notice](llmfit-MIT-LICENSE.txt), [source](https://github.com/AlexsJones/llmfit). Tongyi DeepResearch (Alibaba-NLP/Tongyi Lab, Apache-2.0): research/search pipeline adaptations under services/research/, services/search/ and src/research_handler.py; [notice](DeepResearch-Apache-2.0.txt), [source](https://github.com/Alibaba-NLP/DeepResearch). These historical adaptation records do not establish a single exact acquisition pin for all current files.

Hexes was inspired by [Henxels](https://github.com/benquemax/henxels) v0.11.1; src/hex_contract.py identifies an Open Clank implementation, not a Henxels runtime dependency. [Existing MIT notice](henxels-MIT-LICENSE.txt) remains.

[AgentsView](https://github.com/kenn-io/agentsview/tree/94ddc2381c68ca39d8e02830385adc73c40dfacc), inspected pin `94ddc2381c68ca39d8e02830385adc73c40dfacc` (Kenn Software LLC 2026, MIT), informed Usage/activity interface layout and workflows. Native first-party Usage files are static/js/usage*.js and services/stats/. The available implementation/study ledger establishes inspiration, not a path-level copied-source ledger; no AgentsView telemetry service, session importer, fonts or kit-ui asset is claimed as included.

Logging design studies: [llm_intercept](https://github.com/mlech26l/llm_intercept/tree/f5c5a11aa3d257c94c42699f79803375c7140dc4), Mathias Lechner 2025 MIT; [llm.log](https://github.com/lanesket/llm.log/tree/9b292ebee2f613e8a0a61df9713bd0aebc645914), lanesket 2025 MIT. The current source/ledger does not establish substantial copied code from either. They are credited as study inputs, not distributed interception/CA tooling. If a future path-level copying record establishes incorporation, retain its exact applicable notice then.

## Google artwork and catalogue

[Local bundle manifest](../static/vendor/google-emoji/bundle-manifest.json): Noto source `e20cbc2bbec1926686be9f9bee7d1d2cfa1fea0e`; Xavier Salazar Kitchen catalogue source `062ffca53643d36946336cdc2c7655e2576f0a42`. Local `static/vendor/google-emoji/emoji-assets.pack` and associated index serve acquired artwork, with no runtime CDN fallback. 146,983 of 147,000 Kitchen combinations are available; 17 recorded upstream 404 exceptions mean `complete:false`. Google source acquisition records 4,038, resolvable runtime identities 1,924, and sampler 1,916 are different counts.

Google [Noto Emoji SVG artwork](https://github.com/googlefonts/noto-emoji/tree/e20cbc2bbec1926686be9f9bee7d1d2cfa1fea0e/svg) is Apache-2.0: [retained SVG license](../static/vendor/google-emoji/noto-svg/SVG-LICENSE). [Separate font OFL notice](../static/vendor/google-emoji/noto-svg/FONT-OFL-LICENSE) is retained from the source family; it does not relicense the SVGs or imply a Noto font is loaded. Google Emoji Kitchen artwork and [Xavier Salazar combination catalogue](https://github.com/xsalazar/emoji-kitchen/tree/062ffca53643d36946336cdc2c7655e2576f0a42) keep the accepted attribution. No MIT, Apache, or CC BY redistribution label is invented for the Google Kitchen mashups. The catalogue code license is distinct from the artwork.

## Editor, spelling and icons

[CodeMirror/Lezer notices](CODEMIRROR-LICENSES.md) enumerate selected source imports, installed closure versions and full copyright/license texts. [Frontend/icon notices](FRONTEND-LICENSES.md) retain Shiki/RegExp-engine dependency texts and Lucide ISC/Feather MIT notices. Spell worker: nspell 2.1.5 plus dictionary-en 4.0.0 en_US/SCOWL 2020.12.07; [complete compound notices](../static/js/copal/SPELLING-LICENSES.md) include Kevin Atkinson, Ispell/Geoff Kuenning, Princeton WordNet, UKACD/J Ross Beresford, VarCon/Benjamin Titze and public-domain sources. Do not flatten the dictionary to MIT. Original shared Open Clank SVGs are identified by static/js/uiIcons.js; separate Copal React sources import lucide-react 0.525.0. No Lucide inclusion is inferred in CodeMirror.

## Other frontend bundles and runtime loads

| Actual artifact / runtime load | Version authority / purpose | Component license and source |
| --- | --- | --- |
| static/js/vendor/pretext-matrix.js | `@chenglou/pretext` 1.0.0, local browser bundle for Matrix Rain glyph preparation | MIT and upstream third-party notices. [Full notices](../static/js/vendor/pretext-LICENSE.txt) · [Build provenance](../static/js/vendor/pretext-PROVENANCE.md) |
| static/lib/shiki.bundle.js | Shiki 3.23.0 selected 39-grammar build; installed build inputs/receipt; custom Open Clank CSS theme, JavaScript regex engine | MIT Shiki; separate Microsoft vscode-textmate and regex dependency notices in [frontend texts](FRONTEND-LICENSES.md). Individual grammar provenance beyond supplied package notices is not fully reconstructed. [Shiki](https://github.com/shikijs/shiki) |
| static/lib/mermaid.min.js |11.16.1 bundle version and build receipt; local, not CDN | MIT Mermaid with retained nested bundle notices (including DOMPurify, D3 and KaTeX); [texts](FRONTEND-LICENSES.md), [upstream](https://github.com/mermaid-js/mermaid) |
| static/lib/xlsx.full.min.js | Embedded SheetJS version 0.20.3; spreadsheet read/write | Apache-2.0 SheetJS; retained SheetJS header; [source](https://github.com/SheetJS/sheetjs). Exact original acquisition URL/companion notices not reconstructed; [upstream notice](FRONTEND-LICENSES.md) retained. |
| static/lib/docx.umd.min.js | DOCX generation; exact top-level version not established from bundle | MIT; [source](https://github.com/dolanmiu/docx). Preserve embedded third-party notices; complete exact build closure unresolved; [upstream notice](FRONTEND-LICENSES.md) retained. |
| static/lib/mammoth.browser.min.js | DOCX-to-HTML; exact top-level version not established | BSD-2-Clause; [source](https://github.com/mwilliamson/mammoth.js); exact acquisition/closure unresolved; [upstream notice](FRONTEND-LICENSES.md) retained. |
| static/lib/html2pdf.bundle.min.js | HTML/PDF export, includes jsPDF/html2canvas; exact html2pdf version not established | MIT components; [html2pdf](https://github.com/eKoopmans/html2pdf.js), [jsPDF](https://github.com/parallax/jsPDF), [html2canvas](https://github.com/niklasvh/html2canvas). Bundle refers to absent html2pdf.bundle.min.js.LICENSE.txt; exact companion/closure remains unresolved, not falsely marked complete; [upstream notice snapshots](FRONTEND-LICENSES.md) are retained separately. |
| static/lib/qrcode.min.js | QR setup; no embedded exact version | MIT node-qrcode; [source](https://github.com/soldair/node-qrcode); original exact build notice/closure unresolved; [upstream notice](FRONTEND-LICENSES.md) retained. |
| KaTeX CDN tags in static/index.html |0.16.22; math | MIT [KaTeX](https://github.com/KaTeX/KaTeX) |
| Pyodide lazy CDN in static/js/codeRunner.js |0.27.5; Python in browser, optional invoked runtime | MPL-2.0 [Pyodide](https://github.com/pyodide/pyodide), with its separately licensed Python/packages |

There is no current static/lib/highlight.min.js; historical highlight.js credit is not evidence of current shipment. PDFObject 2.1.1 (MIT, https://github.com/pipwerks/PDFObject) is a retained historical credit; no current runtime load was established in this review. Fonts and exact unresolved variant chains are in [font notices](FONT-NOTICES.md).

## Services and installation dependencies

Current Compose files pull SearXNG `2026.5.31-7159b8aed` and binwiederhier/ntfy (unversioned image). Their upstream AGPL-3.0 / Apache-2.0-or-GPL-2.0 licenses belong to those services. Chroma is no longer in current Compose or requirements; its older Apache-2.0 credit is historical/compatibility, not a required current memory service. Current requirements and Cargo/Bun lockfiles select installed libraries; their own distributions carry full notices. Optional faster-whisper, ddgs, PyMuPDF and markitdown remain feature-dependent; PyMuPDF has its own AGPL/commercial terms. Open Clank root is already AGPL, so the old MIT-core/feature-only copyleft explanation is withdrawn.

## Asset fingerprints

SHA256 values identify the inspected bytes, not a new license grant or a rebuild claim.

| Artifact | SHA256 |
| --- | --- |
| static/fonts/ComicClank-Bold.woff2 | `b3e7d5b53162f3370192fb8a6d57bbfa5c823ff60859abaaea6568e507f711c2` |
| static/fonts/ComicClank-BoldItalic.woff2 | `b9f86542b1eb57a5e5906c3d9ac880db8fb4dd1462e4da8a15b128c399e45564` |
| static/fonts/ComicClank-Italic.woff2 | `dd24062c080fbb8132aea2c13246243082540569a65bf86e14b7e73b3f531464` |
| static/fonts/ComicClank-Light.woff2 | `9562aaedaaaab156b6047c17551f499192eddf92f37f558243a7f5ab11125ca9` |
| static/fonts/ComicClank-LightItalic.woff2 | `a0ce9764990956ec15538d5b9e15062a4bae22011af858155127c82d1cd7d69a` |
| static/fonts/ComicClank-Medium.woff2 | `0097d13f404fc100739ec61a2f9ed9b19fc2ab3f169fc1a88ba766b477c1d1e0` |
| static/fonts/ComicClank-MediumItalic.woff2 | `fb568d20af05941b22e66a882f07fff5e79a6f3c6b76acbca9459880ee2aee87` |
| static/fonts/ComicClank-Regular.woff2 | `1abdf18c0e312917c1619b3583ff31636cedce7a70add58058522fa0a0e006c6` |
| static/fonts/ComicClank-SemiBold.woff2 | `3095699c1cc068da11df87fe7b5634760faf9b101fce903d45dc7dc70b11c724` |
| static/fonts/ComicClank-SemiBoldItalic.woff2 | `9e45882d25494c47b10e7ddedf7cfcba26c65e1142042535de7f2b0f4ade167f` |
| static/fonts/ComicClankMono-Bold.woff2 | `73a4863ef41c912c18f6f29d1e9b027c45125333de8c202555d6dcd6e08ef487` |
| static/fonts/ComicClankMono-BoldItalic.woff2 | `8539f1ac97c83d720343e5aa210e3ed0384acd802ce0ce41d3f7ea0282b3d063` |
| static/fonts/ComicClankMono-Italic.woff2 | `c9422d541c2800e0f5959c54075ae8e74e03cb8b123c52317c0ec1fd0612b369` |
| static/fonts/ComicClankMono-Light.woff2 | `e19890a9a61a9d46403c10f71ce5db748c6de4fc2735b63b66c20d428fb3f635` |
| static/fonts/ComicClankMono-LightItalic.woff2 | `aa683b03765e94a8cf2a5f22dc59e14e6062df0855c245593b414dd3feaa44b5` |
| static/fonts/ComicClankMono-Medium.woff2 | `ee0d14d69299c6d3be1e5bed32dd2b5791185d923560f0e479dde2059cca9a55` |
| static/fonts/ComicClankMono-MediumItalic.woff2 | `a8174cf8792eb3cac8d6fe3b1d096db89c132b225d4b6f3e98c2c4f0eb3a1e45` |
| static/fonts/ComicClankMono-Regular.woff2 | `8477c7565cd3f7e749a35e1614666751120b8c9ed6f4067a5da512f110a34231` |
| static/fonts/ComicClankMono-SemiBold.woff2 | `4b83d255a6bf8457eae0063b23f91ca95bdb7546594a4f353cf9fc167969a030` |
| static/fonts/ComicClankMono-SemiBoldItalic.woff2 | `be634cded5ef9f84e167d5da68a982e7915ac5a3e770e73d1ce036aa85daa28b` |
| static/fonts/ComicClankMono-Bold-Wordmark.woff2 | `0a2ee83a6b916659f052b598e4dbc6646c0fc6ff5519c2d6769bf608b0d9ab2e` |
| static/fonts/ComicNeue-Regular.woff2 | `dc603c8b97ea803cc1377f15a8cd952ca2fb7408e322b8025099ad1cbf6eaf4f` |
| static/fonts/FiraCode-Light.woff2 | `e3aa3db06cfb19dfc0b0f1f38355add3e8d1ef45d3af39ce95d9ca7d96114e6c` |
| static/fonts/FiraCode-Regular.woff2 | `a6ce59520b90e15d7062ffef214f94c8add5a4085c0bbb1683602ef227a4d1fe` |
| static/fonts/FiraCode-SemiBold.woff2 | `d16779aa6dfc7c4effe686ece5bdf4b1356a7352167e37fa256f596a9d428f11` |
| static/fonts/Fredoka-Variable.woff2 | `9e0bcbe720edcfdc16b33a4fe164cc4aba400f7b5b23755a725e6e333580ae9c` |
| static/fonts/Inter-Medium.woff2 | `7e80d9f65861ee6836a0081d4e75d88fb8789e5651d05edbc49640442a9610ee` |
| static/fonts/Inter-Regular.woff2 | `338239f6b590b8ced3bf857654d32da3fd3663294cd3003651ed57aa3abd7aa1` |
| static/fonts/Inter-SemiBold.woff2 | `5013f48d77ab627b1db7c2415914284ef09abc3f60a8e0d0d8f3cd1bfebefb5e` |
| static/fonts/LigaComicMono-Regular.woff2 | `c6bd8a66f2da82fc8b066514cad298029265fbc28ffa88c9f5b9efb5a8ceb561` |
| static/fonts/OpenDyslexic-Bold.woff2 | `dd9fa9c7991113b0dddefe9506a30ad26b48e302b7a8fb91719a4726e8fde85b` |
| static/fonts/OpenDyslexic-Regular.woff2 | `f007004af3cda5d8076e57c943f8cc8d00a0da25988b1ae1048683d60e7cac1a` |
| static/fonts/custom/GohuFont.ttf | `db1c6de2a3c60e441948f10a5078b11952531850e7cb205b9be6037963ab660a` |
| static/lib/docx.umd.min.js | `02d568d203c0180af37609bcf5ff6c0919d220f933a88ca896eba0556a08faad` |
| static/lib/html2pdf.bundle.min.js | `9edbed630ebd644bc5189b99e8138893041c89a3118a672047fa2b625a12e48c` |
| static/lib/mammoth.browser.min.js | `deb07bf230d1cb3e190bc5adc6743f35c6531b6571d1e5469b24f452a7f0f4ab` |
| static/lib/mermaid.min.js | `18327bef70d96fb505fe7287d9f6a7362ebf07ff6576ddfaffb1a06f3e1a2954` |
| static/lib/qrcode.min.js | `0935de514006b84fe54404b4a98fb3b5cc45478d9efa64d75e19719025663c19` |
| static/lib/shiki.bundle.js | `8d585f7b6e6a7f318281a33df0a64e21578f8c483654cff1a2f4d9e5432fedfc` |
| static/lib/xlsx.full.min.js | `cc015130aa8521e7f088f88898eba949ccdcbfb38df0bd129b44b7273c3a6f41` |

## Open provenance items

Copal public origin/separate package license; Liga exact combined-font acquisition/modification/license chain; file named GohuFont.ttf actual origin/license; exact older DOCX/PDF/QR/Mammoth acquisition and nested build notices; per-grammar Shiki source closure. These are candid source limitations, not claims of permission and not feature-removal instructions. Existing notices and assets are preserved.
