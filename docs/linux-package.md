# Open Clank Linux package

This local Linux x86_64 GNU/glibc package targets the recorded Garuda Linux
KDE Wayland host. See its accompanying verification report for completed checks. Run `./openclank --version`, `./openclank engine verify --json`, then
`./openclank server start --open-browser`. Use `server status` and `server stop` for the
server owned by this installation. Open the loopback URL printed by start.
Persistent data belongs in `~/.open-clank/data` or the configured data root,
not inside the extracted directory. Keep the complete directory together.

The host supplies glibc and ordinary system libraries. The optional native
KDE adapter requires compatible Qt6 and KF6 KIO libraries. Without those,
the native thumbnail backend is unavailable. Ordinary Files operations and
eligible direct image previews remain available. KDE is the required desktop qualification; GNOME/COSMIC are deferred.
No universal distribution, ARM64, musl, older glibc or old CPU support is claimed.
The managed Engine uses Bun's Linux x64 AVX2 target. `build-provenance.json`
records actual ELF dependencies, symbol versions and build host; a measured
symbol floor does not prove compatibility with an older distribution.

The manifest detects accidental payload changes; it is not a signature or
an independent authenticity proof. Check the separately delivered archive
SHA256 before extraction. Preserve relative internal symbolic links and modes.
No system installation, provider credentials or model download is required.

Build from matching source using Rust 1.99.0, Bun 1.4.0, standalone CPython
3.13.17, PyInstaller 6.16.0 and patchelf 0.17.2.4. Install requirements in that private Python;
run `pip check`. Build locked release binaries `fm-mcp`,
`openclank-history-service`, `odysseus-files-service`, and
`odysseus-linux-thumbnail-helper`. Build `odysseus-kde-thumbnail-adapter` from
`native/linux` with CMake, Qt6 and KF6KIO. Put all five
binaries in one staging directory. Build and verify the Engine using
`scripts/openclank_engine.py --install-root ENGINE build --target linux-x64`.
Then run:

```sh
OPENCLANK_BUILD_PYTHON=/private/python/bin/python3 ./build-linux-portable.sh \
  --output /fresh/output --python-root /private/python \
  --engine-root /verified/engine --native-bin /staged/native \
  --notices /verified/component-notices
python3 scripts/linux_package.py verify --bundle /extracted/openclank
```

The optional Copal native shell is outside this browser-app package. Builds,
local tests and packaging do not deploy or publish anything. Root LICENSE
and component notices under `_internal/licenses` accompany this AGPL source.

The builder records exact hashes of all public source inputs, including the
uncommitted Linux changes, in build provenance. The accompanying source
archive contains those corresponding files without checkout metadata or build
products. `scripts/linux_notices.py --output NOTICES --python-archive FULL`
collects notices using the same installed Python environment and native Cargo
cache. FULL is the standalone Python 3.13.17+20261021 x86_64 GNU full archive,
SHA256 `344eec7e761e7b3dccce0ce2d91a3e70d40f8ff5d3ce7d907d979c23e8451fc7`.
The install-only Python asset URL and SHA256 are pinned in the builder.
Engine dependencies require node-gyp 11.5.0 and Node in the build PATH;
keep their package manifest and caches in a separate build-tools directory.
Use `cargo build --locked --release --jobs 2 --manifest-path CRATE/Cargo.toml
--bin NAME` for each listed Rust binary and `cmake -S native/linux -B BUILD
-DCMAKE_BUILD_TYPE=Release; cmake --build BUILD --parallel 2` for KDE.
No Rust fixture/test features are allowed in the production FM executable.

The builder removes the verified standalone Tcl libraries' `/tools/deps/lib`
build-time RUNPATH using patchelf. Before/after SHA256 and removed paths are
recorded in provenance; unexpected absolute loader paths fail the build.
Run `scripts/qualify_linux_portable.py --archive ARCHIVE --output FRESH`
for isolated relocation, child Python, first-run auth, native Files/Editor,
KDE thumbnail, and public CLI start/status/stop/restart qualification. It uses
a disposable HOME/XDG/data root and `PATH=/usr/bin:/bin`; Qt/KDE desktop IPC
must be available in the execution environment.

KIO selects plugins through the adapter application's `PreviewSettings` and
the KDE configuration cascade. It does not automatically read Dolphin's
`dolphinrc` preferences. The tested empty configuration cold defaults selected
directory/image/JPEG plugins. SVG/PDF/video cold checks explicitly enabled
those already-installed plugins in an isolated adapter configuration. Validated
shared-cache passthrough is independent of that plugin list while the native
backend is admitted; no additional desktop/plugin installation is implied.

Known CLI limitation: after authentication is configured, bare `server start`
receives HTTP 401 from the protected readiness endpoint and tears down its
new process. Use `server start --open-browser` for initial launch and restart;
that supported browser-app path admits the owned authentication endpoint.
`server status` may report running with readiness unavailable without
authentication. Authenticated application routes remain protected.
