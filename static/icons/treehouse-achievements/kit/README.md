# Customize achievement artwork

This kit contains the actual vector source and deterministic recipe used for the Open Clank achievement medallions. There was no image-model generation, random seed, raster prompt or third-party mascot asset. The artwork extends Open Clank's original SVG icon family. Sources and tools are distributed under AGPL-3.0; see LICENSE. Your replacement mascot remains your own content and must be yours to use.

## Contents

- `composition.json`: all 37 opaque artwork slots, editable foreground motif geometry, frame shape and perimeter details. It contains no achievement titles or unlocking criteria.
- `mascot.svg`: default Open Clank triangle/eye mark in the actual source coordinate system.
- `mascot-example.svg`: original cat mascot demonstrating fixed mascot colors.
- `generate.py`: standard-library Python generator; exports editable SVGs with `background`, `mascot` and `foreground` groups. The transparent canvas is 256 × 256. No filter effects or external resources.
- `pack.py`: packages your raster exports as a JSON pack. Partial replacement is supported.
- `default/`: shipped SVG output. `example/`: one actual alternate mascot SVG, PNG and matching partial pack.
- `PROVENANCE.json`: source and tool details. There are no fabricated seeds/settings.

## Swap the mascot

1. Edit a copy of `mascot.svg` in your vector editor. Keep its `viewBox="0 0 100 100"`. Use paths, groups, circles, ellipses, rectangles, polygons and lines. Convert text to outlines. Scripts, stylesheets, images and linked resources are unsupported. Fixed fills preserve your mascot colors; `currentColor` follows the selected app accent.
2. Run `python3 generate.py --mascot my-mascot.svg --output my-icons`. The tool never replaces existing artwork. To try the included cat, use `--mascot mascot-example.svg`.
3. Adjust `composition.json` for framing or motifs and export again to a fresh folder. The mascot occupies the central upper medallion; the lower-right foreground symbol should remain readable. Keep artwork inside the 256px canvas with transparent outer corners.
4. Export the desired SVGs as **256 × 256 transparent PNGs**, keeping their opaque filenames. Use your usual vector editor or Imps. For Inkscape users, an equivalent export is `inkscape my-icons/OPAQUE-KEY.svg --export-type=png --export-width=256 --export-height=256 --export-filename=my-pngs/OPAQUE-KEY.png`. Raster exports bake mascot and artwork colors; the app still supplies the theme-responsive card frame. SVG defaults retain live theme tokens.
5. Run `python3 pack.py my-pngs --name "My mascot" --output my-mascot-pack.json`. Packs may contain 1–37 known slots, each under 192 KiB and the entire JSON under 4 MiB. The runtime decodes and re-encodes PNGs before applying them; it does not run custom SVG.
6. In Achievements, use the artwork pack import control once the gallery integration is available. Select the JSON through the normal file chooser/Files convention. Preferences are local to this browser and signed-in account. Restore defaults removes the local pack. Unspecified slots and invalid cached artwork use bundled defaults; locked mysteries always use concealed art, regardless of the pack.

The kit supplies appearance only. It cannot grant achievements, change criteria, expose hidden presentation entries or assign XP. Opaque slots are stable appearance references; use the earned gallery's supplied key when replacing a particular entry, rather than guessing hidden titles. The downloadable source bundle is public material, so asset discoverability is not a security boundary.

## Palette and accessibility

Default medallions use `--oc-art-accent`, `--oc-art-panel`, `--oc-art-ground`, `--oc-art-outline` and `--oc-art-mascot`, bound by the app renderer to installed theme tokens. There is no always-on glow, animation or color filter applied to user PNGs. Preserve contrast and silhouette at small card sizes. Frames and foreground motifs are separate editable groups, not rarity inferred from filenames. The server owns the actual rarity/state and supplies concealed art for locked mysteries; unearned ultras remain absent. Artwork is decorative within a labelled card and uses a generic accessible artwork label.
