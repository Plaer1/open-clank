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

Next shell work:

- Run the runtime shell interactively on Linux with the local Copal server.
- Add keyboard/text input forwarding.
- Add mouse click/move forwarding.
- Bridge vault commands into `rust/copal-core`.
- Test CodeMirror selection, IME, clipboard, focus, and scroll in Servo.
