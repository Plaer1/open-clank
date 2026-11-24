# Copal Servo Shell

Status: scaffold/probe, not production shell.

Copal desktop target is Servo-only. This crate exists to keep the product target concrete while the React/CodeMirror workbench stabilizes.

Commands:

```bash
bun run servo:check
bun run servo:probe
bun run servo:runtime-check
```

`servo:check` verifies the scaffold. `servo:probe` cargo-checks direct imports from the public `servo = 0.3.0` crate: `ServoBuilder`, `WebViewBuilder`, and `SoftwareRenderingContext`.

`servo:runtime-check` cargo-checks the first real runtime shell path. It creates a winit window, a Servo `WindowRenderingContext`, a `Servo` runtime with an event-loop waker, and a `WebView` navigated to `COPAL_URL`.

The native API keeps standalone data in a `standalone/` child of Copal's normal data directory. With this repo's debug setting, that is `packages/Copal/db/standalone/`, separate from any retained older-version hosted Redb `packages/Copal/db/` store. Current hosted source uses only Files at its configured vault root; the standalone native API is a separate scaffold/store and is not qualified as hosted-platform parity. An explicit `COPAL_DB=/path` is honored exactly for operator-managed deployments.

Next shell work:

- Run the runtime shell interactively on Linux with the local Copal server.
- Add keyboard/text input forwarding.
- Add mouse click/move forwarding.
- Bridge vault commands into `rust/copal-core`.
- Test CodeMirror selection, IME, clipboard, focus, and scroll in Servo.

## Public builds and private runtime data

`bun run build:public` checks public inputs, runs Next and checks the exported
assets. `build:native-assets` and native release commands use this guarded path.
Direct Cargo embedding also rejects private mutable payloads and requires the
synthetic planning fallback. Build from a reviewed clean export: a local
`public/data/move-data.json` is personal runtime input and must remain outside
release builds. Guards refuse it without moving, deleting or overwriting it.
Normal local mutable data remains separate from the neutral default scenario.
The public default and `public/examples/planning.json` have matching schema;
Timeline, Calendar, shared/fuzzy dates and open-ended tracks remain available.
These guards are packaging evidence, not native platform feature acceptance.
