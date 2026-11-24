# Open Clank Setup
Open index.html to walk through the offline design. Switch platform or scenario below the window. On Linux choose desktop integration or a portable launch. On macOS the final commit copies the app bundle to Applications. The preview simulates progress, errors, retries, cancellation and completion.

## Reuse the theme
Load theme.css and setup.js, supply a container with class oc-setup, then mount one view. The host owns state, localization for host messages, package discovery, and OS operations. The preview toolbar and preview.js belong to the demo only.

```js
const view = OpenClankSetup(container, handleIntent);
view.render({
  platform: 'mac', phase: 'review', existing: false,
  destination: '/Applications/Open Clank.app', options: []
});
```

Phase | Host input | Intents
--- | --- | ---
welcome | platform (mac, linux, windows), Linux mode (integrated or portable) | close, mode, review, portable
review | resolved destination, existing flag, optional choices with id/label/checked | back, option, install
working | message, percent (null for unknown), canCancel | cancel while available
error | actionable message, optional technical details, canRetry | close, retry if offered
cancelled | cleanup result in message | close, review
complete | committed destination and completion message | launch, close
closed | optional farewell for standalone preview | none

Mode and option intents carry id and checked. Render a new snapshot after accepting a selection. Disable duplicate install/retry at the host boundary. Tie progress and callbacks to the current operation, ignore late events, and report completion only after the destination is usable. During rollback, show working with a cleanup message until the host can report the actual result. An update needs explicit confirmation in Review. If an installation appears after review, return to Review with existing set. Resolve paths and user identity in the host.

## Visual primitives
The portable token names and values are in the :root block of theme.css. The primitives cover the attached title strip, six-color accent rule, numbered-free step rail, well for paths, radio and checkbox rows, primary and secondary actions, determinate and indeterminate progress, success state and expandable error detail. A compact window moves the rail above the content. Keyboard focus is cyan, progress changes use a polite status region and do not move focus, and state is conveyed with text as well as color.

Package the relative assets used by the reference renderer: ../SVG/open-clank-pyramid-eye.svg, ../SVG/open-clank-wordmark-wide.svg, and the three Comic Clank font files declared in theme.css with ../../../fonts/ComicClank-OFL.txt. Preserve their relative layout or rewrite URLs during packaging. With a different renderer, reuse the colors, typography, measurements and interaction states. The shared surface is entirely local and can render before the main server starts. Keep web content local-only and expose the narrow intent bridge instead of arbitrary commands or paths. Window-manager decorations, system authentication and permission prompts remain OS-owned.

## Platform handoff
- **macOS** — resolve Applications and show the actual bundle name. Check eligibility, stage the intact verified bundle on the destination volume, then copy/replace as the final operation. Preserve bundle metadata and signatures. Handle a running previous version, denied authorization, insufficient space, and a failed replacement with an actionable status. Launch the installed copy. The UI requires no Finder drag.
- **Windows** — pass the packager's resolved per-user destination and supported options. Existing installer tasks include desktop shortcut and offline artwork. Supply their current availability, defaults and sizes from the release package. Keep system consent authentic.
- **Linux AppImage** — first-run Setup offers Run once and Add to my applications. The proposed per-user target is ~/Applications/Open-Clank.AppImage plus an Education desktop entry. Normal subsequent launch goes directly to Open Clank. See the implementation plan for mount lifetime, external state, upgrades and FUSE recovery.

The sample paths, timer and errors are preview data. Packaging, updating, elevation and service startup are integration work.
