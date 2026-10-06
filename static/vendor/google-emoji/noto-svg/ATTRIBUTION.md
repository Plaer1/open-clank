# Noto Emoji image asset attribution

The 619 SVGs in this legacy directory come from [Google's Noto Emoji project](https://github.com/googlefonts/noto-emoji) commit `e20cbc2bbec1926686be9f9bee7d1d2cfa1fea0e`, under `2D/svg/emoji_u*.svg`. Each file's SHA-256 and source filename are recorded in `catalog.json`.

The per-directory `2D/svg/LICENSE` explicitly licenses these SVGs under Apache License 2.0; its text is included as `SVG-LICENSE`. This bundled subset matches the Emoji Kitchen backend's 619 `knownSupportedEmoji` entries and contains no regional-indicator flag assets. No fonts are included. `FONT-OFL-LICENSE` preserves the separate font license text and does not apply to the bundled SVG subset.

The runtime now serves local artwork from `../emoji-assets.pack`, which includes additional Noto assets and 146,983 Kitchen combinations. Bundle provenance and availability are recorded in `../bundle-manifest.json`; individual source records are retained in the pack.

[Emoji Kitchen](https://www.google.com/fbx?fbx=emoji_kitchen) artwork by Google. Combination catalogue from [Xavier Salazar's emoji-kitchen project](https://github.com/xsalazar/emoji-kitchen). The Kitchen source repositories did not provide a verified artwork redistribution license; this credit does not assert one, and the Noto license does not cover Kitchen combinations.
