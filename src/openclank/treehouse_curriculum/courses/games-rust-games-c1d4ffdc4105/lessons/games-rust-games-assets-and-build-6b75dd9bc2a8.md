# Keep a reproducible game build

- Keep original artwork under `assets/` if you replace shapes. An asset handle identifies a load request; it is not proof that loading has completed.
- Run `cargo build --locked`, then run the built executable from the project directory and repeat movement, win and reset checks. This is local qualification; release packaging may need asset-path handling and platform work.
- Preserve exact source, Cargo.lock, toolchain and observed OS. Record a failed build honestly instead of changing the dependency version until it happens to compile.
- Add a pure overlap function test at exactly 20 and just above 20; this tests a meaningful boundary without relying on screenshots.

**Self-check:** Submit build/run observations, the two overlap-boundary results, and one packaging assumption such as the working directory.
