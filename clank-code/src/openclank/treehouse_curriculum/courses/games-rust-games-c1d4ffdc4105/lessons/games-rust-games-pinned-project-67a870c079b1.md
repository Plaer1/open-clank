# Pin a Rust game project

- In a fresh owned folder, use the official Rust/rustup installation for 1.90.0 if absent; do not substitute latest silently. `cargo +1.90.0 new --bin target_square`; enter that folder.
- Save complete direct configuration:

`rust-toolchain.toml`
```toml
[toolchain]
channel = "1.90.0"
profile = "minimal"
```

`Cargo.toml`
```toml
[package]
name = "target_square"
version = "0.1.0"
edition = "2024"
rust-version = "1.90"
[dependencies]
bevy = "=0.17.3"
```

`src/main.rs` first window:
```rust
use bevy::prelude::*;
fn main() {
    App::new().add_plugins(DefaultPlugins).add_systems(Startup, setup).run();
}
fn setup(mut commands: Commands) { commands.spawn(Camera2d); }
```

- The supplied Bevy starter includes a real resolved `Cargo.lock`. Keep it with the matching manifest and use `cargo run --locked`, including when recreating this first-window project. If resolving a separate project yourself, `cargo generate-lockfile` creates a new resolution record; do not treat it as the supplied checked graph.
- The supplied full game passed `cargo check --locked --jobs 2` with Rust 1.90.0 on arm64 macOS. This checked compilation; it did not link or launch the game window/GPU, and it does not guarantee another platform. Run locally for practical success and record your actual result, tool versions and any compiler/linker/GPU error.

**Self-check:** Create the project, record `rustc --version`, and report whether dependency resolution and a minimal Bevy build succeeded. Keep the lockfile and any actual error.
