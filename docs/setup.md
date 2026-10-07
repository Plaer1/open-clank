# Open Clank Setup Guide

This guide covers installation, deployment, troubleshooting and configuration.
**Beta 1 is the first of several beta releases.** macOS is the current
build/dogfood focus. Windows source support is working and tested, including
Files, Editor save/reopen and History restore, and native image/video thumbnails.
Native ARM64 and x64 Windows packages are being qualified separately; install
only the architectures offered by the published release. Linux support is
coming later. See [known limits](known-limits.md) and
[Beta 1 notes](beta1.md). Machine version: `1.0.2`.

Docker is not officially supported or tested. Docker/Compose instructions in
this guide are retained only as unsupported legacy reference, not an official
installation path or a Beta 1 release qualification requirement.

Native setup is the primary path, especially on Apple Silicon when local
serving needs Metal. The commands below describe a **fresh installation**.
For existing data, begin with [upgrading](upgrading.md). Some environment/helper
names retain the `ODYSSEUS_` prefix; use identifiers as written.

## Find the handbook

**Settings → Help** has two buttons: **Wiki documentation** opens Wiki, and
**Copal handbook** opens the Editor presentation of the same official articles.
Official pages are read-only. Practice examples in a new personal Wiki page or
Editor document.

Start document work in Files with **File → New file…**;
choose destination/name and continue in Editor. Wiki handles linked pages,
rich authoring and section embeds. The inherited Odysseus document editor is no longer
an improvement target; existing document data and email compatibility remain.

Media in the shared Copal/Wiki handbook describes the visible scene. Setup or empty
screens do not attest to provider access, OS privileges or completed external
actions.

## Recommended: native installation

### macOS

#### macOS app package

The Apple Silicon download for macOS 15 or later is `Open-Clank-1.0.2-macos-arm64.dmg` from
the [`v1.0.2-beta.1` application release](https://github.com/Plaer1/open-clank/releases/tag/v1.0.2-beta.1)
when published. Native package and menu-bar Open/Quit qualification have passed;
no Intel/universal or older macOS qualification is claimed. This Beta app is
ad-hoc signed, without Developer ID signing or notarization. Check its release
checksum and signing information before deciding to open it; these instructions
do not disable Gatekeeper.

Mount the DMG, drag **OpenClank.app** to Applications, then eject the DMG.
The app includes private Python, Engine and native helpers and needs no checkout,
Homebrew, Cargo or system Python. Download the artwork part and
`emoji-assets.parts.json` from the separate
[`v1.0.2-beta.1-artwork-compact` release](https://github.com/Plaer1/open-clank/releases/tag/v1.0.2-beta.1-artwork-compact)
which becomes available when published, keeping them together in one directory. Install them with the
app's packaged command in Terminal:

```bash
"/Applications/OpenClank.app/Contents/Resources/runtime/openclank" assets assemble --parts /path/to/emoji-parts
"/Applications/OpenClank.app/Contents/Resources/runtime/openclank" assets verify
```

The default pack destination is `~/.open-clank/data/assets/google-emoji/emoji-assets.pack`.
Assembly verifies the full pinned payload and writes outside the sealed `.app`;
never add artwork or editable data inside the application bundle. If using
`OPEN_CLANK_DATA_DIR`, use the same configured directory for assembly and launch.

Open **OpenClank.app**. Its pyramid-and-eye menu-bar icon offers **Open** to
show the browser UI and **Quit** to stop the server generation owned by that
app. It has no Dock icon. Open uses the configured loopback address, normally
`http://127.0.0.1:7777`. Create the first administrator through mandatory browser
setup, then sign in. Reopening retains user data. Missing artwork returns an
install hint rather than fetching a CDN. Removing the app does not remove
personal application data or the installed pack.

For a custom data directory or port, packaged Finder launches use the supported
per-user launch profile at
`~/Library/Application Support/OpenClank/macos-launch-profile.json`. The profile
selects `OPEN_CLANK_DATA_DIR` and optionally `APP_PORT`; use the same profile for
the packaged artwork command. Setting an environment variable in an unrelated
Terminal does not change a running Finder-launched app. Source launcher settings
are described separately below.

#### macOS source alternative

The Apple Silicon source installation has passed fresh-install qualification.
Install Homebrew, ARM-native Python 3.11 or later, [Rust/Cargo](https://rustup.rs/)
and Xcode Command Line Tools (`xcode-select --install`). This alternative builds
locally; Intel Macs remain unqualified.

Downloads become available when `v1.0.2-beta.1` is published on the
[application release](https://github.com/Plaer1/open-clank/releases/tag/v1.0.2-beta.1). Obtain
`open-clank-1.0.2-beta.1-source.tar.gz` from the application release. Obtain
`emoji-assets.parts.json` and `emoji-assets.pack.part-001` from the separate `v1.0.2-beta.1-artwork-compact` supporting
[release](https://github.com/Plaer1/open-clank/releases/tag/v1.0.2-beta.1-artwork-compact), when published. Check the source archive against the release checksum
file. Keep both artwork files together in a separate directory.

```bash
tar -xzf open-clank-1.0.2-beta.1-source.tar.gz
cd open-clank-1.0.2-beta.1
python3 scripts/emoji_asset_bundle.py assemble --parts /path/to/emoji-parts
python3 scripts/emoji_asset_bundle.py verify
./start-macos.sh
```

[Artwork assembly](#offline-emoji-artwork) verifies the pinned payload before
first launch. Alternatively, a Git source checkout can use the same assembly
and launcher commands, with the artwork matching its bundled manifest.

The launcher prepares the venv, runs setup and starts the validated bootstrap.
On Apple Silicon, use ARM-native Python rather than a Rosetta interpreter.
Setup builds/verifies the managed engine and may download pinned Bun/build
dependencies. It also runs locked Cargo release builds for the native memory,
History, Files and QuickLook workers; these artifacts are required for the
workspace features. Keep Cargo and the platform compiler available when rerunning
setup. The first build may take several minutes and needs additional disk space
for dependency and compiler outputs. Python requirements alone are insufficient.

The default address is `http://127.0.0.1:7777`. The launcher loads missing values
from `.env`; exported shell values win. Legacy `ODYSSEUS_PORT` / `ODYSSEUS_HOST`
then take precedence over `APP_PORT` / `APP_BIND`. Keep loopback until a trusted
network path and HTTPS are configured.

### Linux

**Experimental in Beta 1.** Files/native integration remains unfinished;
support is coming as soon as possible. Commands and engine builds do not prove
parity with macOS dogfooding.

From a fresh source checkout, create a virtual environment, install the
matching artwork, and run setup:

```bash
git clone https://github.com/Plaer1/open-clank.git
cd open-clank
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Obtain matching parts first; see Offline emoji artwork below.
python scripts/emoji_asset_bundle.py assemble --parts /path/to/emoji-parts
python setup.py
openclank server start
openclank server status
```

Python 3.11 or later, Rust/Cargo and a C/C++ compiler are required. Setup builds
the memory and History workers; Linux native Files remains unsupported and
unqualified. Setup verifies the managed engine and may
fetch the pinned Bun toolchain if that engine needs rebuilding. Local model
serving is optional; GPU, runtime, model and memory requirements depend on the
selected model. Cookbook background downloads and serves use `tmux`.

### Windows release package

The easiest planned installation is the per-user Setup executable:
`Open-Clank-1.0.2-windows-x64-Setup.exe` or
`Open-Clank-1.0.2-windows-arm64-Setup.exe`. Installer and native package
qualification is still pending; downloads become available after publication,
and only architectures actually offered by the release should be installed.
This Beta is unsigned. Windows may show an unsigned-app warning; check the
release source and checksum before deciding to run it.

Setup installs beneath your account's
`%LOCALAPPDATA%\Programs\Open Clank Beta\windows-x64` (or `windows-arm64`)
without administrator installation or global Python/PATH changes. Its private
Python runtime, managed engine and native workers require no system Python,
Cargo or Bun. The ARM64 package contains a native ARM64 application payload;
the Inno Setup launcher itself is x64 and uses Windows ARM x64 emulation.

The complete offline-artwork task is selected by default. It downloads about
449 MB of pinned release artwork with progress and retry, or accepts a folder
containing the part and `emoji-assets.parts.json`. Setup verifies every
file and assembles the pinned pack into writable personal application data.
You may skip artwork and later run the assembly command below without
uninstalling; missing artwork does not enable any runtime CDN fallback.

Start menu **Open Clank** runs `openclank.exe server start --open-browser`;
**Stop Open Clank** runs `openclank.exe server stop`. The desktop shortcut is
optional and off by default. Setup does not automatically launch the app when
installation finishes. Launch Open Clank and follow mandatory browser setup to
create the first administrator, then sign in normally. The server remains on
loopback by default. Uninstall removes the installed program and shortcuts,
while preserving personal data and installed artwork; it is not a data reset.

For a portable alternative, download `Open-Clank-1.0.2-windows-x64.zip` or
`Open-Clank-1.0.2-windows-arm64.zip` from the same qualified release.
Extract the ZIP and keep its entire `openclank` directory together, including
`_internal` and checksum files. Download the shared artwork part and
`emoji-assets.parts.json` into a separate directory. From the extracted app
directory, run in PowerShell:

```powershell
.\openclank.exe assets assemble --parts C:\path\to\emoji-parts
.\openclank.exe assets verify
.\openclank.exe server start --open-browser
```

Assembly installs the pinned pack in normal writable application data, outside
`_internal`. `OPEN_CLANK_DATA_DIR` can select an explicit data directory; use the
same value for assembly, verification and server startup. Open
`http://127.0.0.1:7777` and follow first-run setup to create the administrator.
Run `.\openclank.exe server stop` to stop the owned server. Running
`.\openclank.exe` without arguments opens the TUI/device authorization flow. Missing artwork leaves artwork endpoints unavailable with an install
hint; it does not silently fetch from a CDN.

### Windows

**Windows source support is working and tested.** Use the repository's [one-command launcher](#native-windows), which prepares
the environment and starts the service. The manual Windows setup is also
documented there.

<a id="docker-compose-alternative"></a>

## Docker Compose (unsupported legacy reference)

Docker has no official support or testing. The following retained instructions
are legacy reference only; use native/source setup for Open Clank.

Historical Compose invocation:

```bash
docker compose up -d --build
```

The app binds to `127.0.0.1:7777` by default. If the initial administrator
password is generated for a non-interactive setup, inspect the local setup
output with `docker compose logs odysseus`; change the password after first
login and do not share those logs. Set `APP_BIND=0.0.0.0` only when the
installation is behind a trusted LAN/VPN or reverse proxy with authentication
configured. Review the Compose volumes before recreating containers so data
stays in the intended locations.

On Apple Silicon, Docker cannot access the host's Metal GPU. Use the native
launcher when Cookbook needs Metal acceleration. The details below cover
unsupported legacy Docker GPU and host-daemon integrations.

## First sign-in and model access

Interactive setup asks you to create the first Open Clank administrator. A
non-interactive setup can use `OPEN_CLANK_ADMIN_USER` and
`OPEN_CLANK_ADMIN_PASSWORD`, or it generates a temporary password and prints
it in local setup output. Existing `ODYSSEUS_ADMIN_USER` and
`ODYSSEUS_ADMIN_PASSWORD` aliases remain accepted. Keep the credential private
and change it after the first login.

The Open Clank administrator account is separate from a model provider
account. In **Settings → Add Models**, choose the **Local**, **API**, or
**Subscription** setup card, then select a model in Chat. **Added Models** is
the inventory of connections already added to the account. A subscription
login and API key may have different billing and quota rules; check the
provider's account controls before sending requests.

## Start, stop and inspect

After setup installs the command into the virtual environment, activate that
environment and use the local service profile:

```bash
openclank server start
openclank server status
openclank server stop
```

These commands manage the service started through the CLI. Stop a foreground
launcher/bootstrap with Ctrl+C in its terminal; stop Compose with
`docker compose stop`. Stop the actual deployment's writers and any other
workers sharing its stores before restore or conversion.

The CLI uses the selected client profile's address, initially
`http://127.0.0.1:7777`, rather than `.env` bind settings. For a different
loopback port:

```bash
openclank profile add local-7900 http://127.0.0.1:7900 --auto-start --use
openclank server start --profile local-7900
```

For a foreground source run with an explicit address:

```bash
python scripts/openclank_bootstrap.py serve --host 127.0.0.1 --port 7777
```

Bootstrap verifies managed-engine provenance and current provider-store state
before importing the app. Pass `--data-dir /absolute/data` **before** `serve`
to select an explicit store. Direct `python -m uvicorn app:app` skips this
contract and is not the normal startup recipe.

## Updates and data

Use [the upgrade preflight](upgrading.md) for existing data: record the installed
revision/deployment and actual stores, stop relevant writers, preserve verified
recovery copies, and apply only the conversions that installation needs.
Setup refuses certain legacy stores/settings; changing a path does not move
data. A dependency refresh or Compose rebuild is not a migration.

Review [backup coverage](backup-restore.md), including Copal, History and host
workspaces. The unsupported legacy `update_windows.bat` is a Docker updater (fast-forward pull, Compose
rebuild/restart and dangling-image cleanup), not a native Windows updater or
conversion tool.

<details>
<summary>Cookbook and troubleshooting notes; Docker instructions are unsupported legacy reference</summary>

Docker/Compose commands and helper files below are unsupported legacy reference.
They carry no official support, testing or release qualification requirement.
The native macOS notes describe the native launcher separately.

**Unsupported legacy Docker bundled services.** Compose starts Open Clank, SearXNG, and ntfy.
Frankenmemory stores memory/RAG locally in the mounted SQLite data directory;
it does not need a second vector service. The bundled service ports bind to
`127.0.0.1` by default, so
they are reachable from the host but not exposed to your LAN/public internet
unless you opt in.

**Cookbook storage in Docker.** Downloads live in `./data/huggingface`
(`~/.cache/huggingface` in the container). Cookbook-installed Python CLIs and
serve engines live in `./data/local` (`~/.local` in the container), so they
survive container recreation.

**Remote servers.** In **Cookbook -> Settings -> Servers**, generate the
Open Clank SSH key and add the public key to the remote server's
`~/.ssh/authorized_keys`. From the host you can also run:

```bash
ssh-copy-id -i data/ssh/id_ed25519.pub user@server
```

**Host Docker access (explicit opt-in).** Default Docker Compose intentionally
does not mount `/var/run/docker.sock`. You can still connect Open Clank to
existing Ollama, vLLM, and other OpenAI-compatible endpoints without Docker
socket access.

Cookbook/local Docker-daemon management requires the opt-in overlay below. Raw
Docker socket access is high-trust because it can effectively grant broad
control over the host Docker daemon. Remote server Docker workflows over SSH
remain preferred.

Place these values in `.env`, or export them in the shell before running
`docker compose`:

```bash
COMPOSE_FILE=docker-compose.yml:docker/host-docker.yml
DOCKER_GID=<host docker group gid>
```

Combine host Docker access with a GPU overlay when both are intentionally
required:

```bash
COMPOSE_FILE=docker-compose.yml:docker/gpu.nvidia.yml:docker/host-docker.yml
# or
COMPOSE_FILE=docker-compose.yml:docker/gpu.amd.yml:docker/host-docker.yml
```

**Docker GPU overlays.** CPU-only users can skip this section. Cookbook can
only detect GPUs that Docker exposes to the container — if the host runtime or
device passthrough is not configured, Cookbook sees the iGPU, another card, or
CPU instead of your intended GPU.

For NVIDIA, `scripts/check-docker-gpu.sh` diagnoses GPU passthrough and can
optionally install the host runtime or update `.env`.

```bash
# Read-only diagnostic (default — installs nothing, never edits .env):
scripts/check-docker-gpu.sh

# Print OS-specific install commands without running them:
scripts/check-docker-gpu.sh --print-install-commands

# Install NVIDIA Container Toolkit on Ubuntu/Debian (requires sudo):
scripts/check-docker-gpu.sh --install-nvidia-toolkit

# Write COMPOSE_FILE to .env (only when GPU passthrough is confirmed working):
scripts/check-docker-gpu.sh --enable-nvidia-overlay

# Full assisted setup — install toolkit, then enable overlay if passthrough works:
scripts/check-docker-gpu.sh --install-nvidia-toolkit --enable-nvidia-overlay
```
#### Arch Linux NVIDIA Docker notes

On Arch Linux, verify the host NVIDIA driver and Docker GPU passthrough before enabling the Open Clank NVIDIA overlay.

Install the required packages:

```bash
sudo pacman -Syu
sudo pacman -S docker docker-compose nvidia-container-toolkit nvidia-utils
sudo systemctl enable --now docker
```

Configure Docker to use the NVIDIA container runtime:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Verify the host GPU:

```bash
nvidia-smi
```

Verify Docker GPU passthrough:

```bash
docker run --rm --gpus all nvidia/cuda:12.9.0-base-ubuntu22.04 nvidia-smi
```

Then enable the Open Clank NVIDIA compose overlay:

```env
COMPOSE_FILE=docker-compose.yml:docker/gpu.nvidia.yml
```

Rebuild and verify the GPU inside the Open Clank container:

```bash
docker compose up -d --build
docker compose exec odysseus nvidia-smi -L
```

For first-time local model testing on 8 GB laptop GPUs, start with GGUF/Q4 models on llama.cpp before trying GPTQ/AWQ models on vLLM or SGLang. This keeps the first run simpler while confirming GPU passthrough works.

**WSL2 + snap Docker.** If the NVIDIA check fails with this error, Docker may be
installed via snap:

```text
failed to fulfil mount request: open /usr/lib/wsl/lib/libdxcore.so: no such file or directory
```

Check with `snap list docker` or:

```bash
docker info --format '{{.DockerRootDir}}'
```

A Docker root under `/var/snap/docker/` means snap confinement can prevent
Docker from seeing WSL2's `/usr/lib/wsl/lib` GPU libraries even when the files
exist on the host. Reinstalling or reconfiguring `nvidia-container-toolkit` will
not fix that. Remove snap Docker, install the official apt-based Docker Engine
([Docker docs](https://docs.docker.com/engine/install/ubuntu/)), then configure
the NVIDIA runtime again:

```bash
sudo snap remove docker
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Then re-run `scripts/check-docker-gpu.sh`.

Safety notes:
- The app never installs host GPU runtime automatically.
- The app never edits `.env` automatically.
- `.env` is only modified when `--enable-nvidia-overlay` is explicitly passed,
  and only after GPU passthrough succeeds. `--yes` skips prompts but does not
  bypass the passthrough gate.
- `.env.bak.*` backups created by `--enable-nvidia-overlay` are ignored by
  Git and the Docker build context.

To enable manually without the script, add this to `.env`:

```bash
COMPOSE_FILE=docker-compose.yml:docker/gpu.nvidia.yml
```

**AMD / ROCm.** AMD setup is read-only diagnostic plus manual `.env` edit. Run:

```bash
scripts/check-docker-amd-gpu.sh
```

Then add the reported values to `.env`, replacing `RENDER_GID` with your host's
numeric render group id:

```bash
COMPOSE_FILE=docker-compose.yml:docker/gpu.amd.yml
RENDER_GID=989
```

For NVIDIA/AMD GPU support, also read the comments in the selected overlay file: docker/gpu.nvidia.yml or docker/gpu.amd.yml.

**Stack-management UIs (Portainer, Coolify, Dockhand, etc.).** These tools
often accept only a single Compose file and do not reliably honor `COMPOSE_FILE`
or multiple `-f` overlays. CLI users should keep using the `COMPOSE_FILE`
overlay workflow above. For stack UIs, point the stack at one of the standalone
files instead, which bundle the base stack plus the GPU settings:

- `docker-compose.gpu-nvidia.yml` — still requires the NVIDIA Container Toolkit
  on the host.
- `docker-compose.gpu-amd.yml` — still requires host ROCm/kfd/DRI setup, the
  `video`/`render` group membership, and `RENDER_GID` when needed.

The base `docker-compose.yml` plus the `docker/gpu.*.yml` overlays remain the
source of truth; the standalone files mirror them for single-file deployments.

Verify after enabling either overlay:

```bash
docker compose exec odysseus nvidia-smi -L   # NVIDIA
docker compose exec odysseus sh -lc 'test -e /dev/kfd && test -d /dev/dri && ls -l /dev/kfd /dev/dri/renderD*'  # AMD
```

> **GPU passthrough ≠ llama.cpp CUDA.** `nvidia-smi` passing inside the
> container confirms Docker GPU access, but llama.cpp also needs `cudart` and
> the CUDA Toolkit at runtime. If Cookbook logs show `Unable to find cudart
> library`, `Could NOT find CUDAToolkit`, `CUDA Toolkit not found`, or
> tensors/layers assigned to CPU, that is a Cookbook/llama.cpp build issue —
> not a Docker passthrough failure. Reinstall the serve engine via
> **Cookbook → Dependencies** to get a CUDA-enabled build.
>
> The same split applies to AMD/ROCm: seeing `/dev/kfd` and `/dev/dri` inside
> the container confirms device passthrough, not ROCm userspace or a
> ROCm-enabled vLLM/llama.cpp build. `rocm-smi` and `rocminfo` are not expected
> inside the slim Open Clank image.

**Ollama with Docker.** If Ollama runs on the host, add this endpoint in
Settings:

```text
http://host.docker.internal:11434/v1
```

Ollama must listen outside its own loopback interface:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

This connects Open Clank in Docker to an Ollama server that is already running on
your host machine; it does not start Ollama inside the container.
`host.docker.internal` is Docker's hostname for the host machine from inside the
container. Cookbook **Serve** is a separate workflow for serving downloaded
models through Open Clank/llama.cpp, so Windows users with an existing Ollama
install usually only need to add the endpoint in Settings.

**Useful checks.**

```bash
docker compose ps
docker compose logs --tail=120 odysseus
  docker compose logs odysseus | grep -E 'Frankenmemory|DEGRADED|ERROR'
```

**macOS details.** The launcher attempts optional Homebrew installs for `tmux`,
`llama.cpp` and Apfel. On Apple Silicon, when Apfel is available, it starts an
Apfel endpoint on port `11435`. Metal/Apple-native and GGUF serving depend on
the backend and model format. Apfel availability does not promise every
MLX/Apple model works. vLLM/SGLang GPU workflows need their supported Linux
CUDA/ROCm environment. Connect compatible endpoints through Add Models.

</details>

### Native Windows

Windows source Setup/Check, authenticated startup, Files, Editor save/reopen
and History restore, native PNG/JPEG/MP4 thumbnails, and host application dispatch
have passed focused Windows checks. Shared fixes were also tested on macOS.
The tested source setup used x64 Python with ARM Engine and native sidecars.
Native ARM64 and x64 frozen release packages remain separately unqualified;
CI artifact definitions do not qualify those packages.

**Launcher for a fresh installation** (creates the venv, installs
dependencies, runs setup and starts the validated bootstrap). First clone
and [obtain the matching artwork parts](#offline-emoji-artwork):

```powershell
git clone https://github.com/Plaer1/open-clank.git
cd open-clank
# Download the matching manifest and single part before this command.
py -3.11 scripts/emoji_asset_bundle.py assemble --parts C:\path\to\emoji-parts
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1
```

Or do it by hand:

```powershell
git clone https://github.com/Plaer1/open-clank.git
cd open-clank
py -3.11 -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Obtain matching parts first; see Offline emoji artwork below.
python scripts/emoji_asset_bundle.py assemble --parts C:\path\to\emoji-parts
python setup.py
python scripts/openclank_bootstrap.py serve --host 127.0.0.1 --port 7777
```

If `python` points at an older interpreter, use `py -3.12` (or another installed
3.11+ version) for the venv step.

**Exposing on a LAN/Tailscale (Windows):** the launcher binds to `127.0.0.1` and
does **not** read `APP_BIND` / `ODYSSEUS_HOST` from `.env`, so editing `.env`
alone leaves the native Windows server on loopback. Pass the launcher's
`-BindHost` flag instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -BindHost 0.0.0.0
```

The foreground bootstrap takes the same address as `--host 0.0.0.0`. Bind
outside loopback only for a trusted LAN/VPN such as Tailscale; authenticate
with a real account and do not expose the port directly to the public internet.

**Requirements:** Python 3.11+, [Rust/Cargo](https://rustup.rs/) and the Visual
Studio C++ Build Tools/Windows SDK for your Python/host architecture. Setup
builds the native memory, History, Files and Windows thumbnail workers from
source and may fetch pinned Bun/build dependencies for the managed engine.
This source build wiring does not qualify frozen ARM64 or x64 packages. For Cookbook background commands and the agent shell tool,
also install
[Git for Windows](https://git-scm.com/download/win) (provides `bash.exe`).
Local GPU *serving* of vLLM/SGLang needs Linux/WSL2; for a local model on Windows,
[Ollama](https://ollama.com/download) is one endpoint option — connect it in Add Models at
`http://localhost:11434/v1` in Settings.

Open the address printed by the launcher, normally `http://127.0.0.1:7777`,
and sign in with the administrator created during setup. Configure model
connections in **Settings → Add Models**.

## Troubleshooting & Advanced Setup

### Legacy Chroma data

Chroma is retired as a live vector/RAG authority. An environment switch cannot
enable a second Chroma backend. Preserve old payloads as historical data for
reviewed explicit conversion matched to the installation; tools and receipts
are kept privately by the operator and are not included in a source clone.
Frankenmemory is the default intended memory/RAG authority.
`NativeMemoryProvider` compatibility selection remains in source; it does not
restore Chroma and is not a second recommended deployment path.

### HTTPS + LAN/Tailscale exposure

Keep the app on loopback when the proxy shares its host, terminate HTTPS at a
trusted reverse proxy/private gateway, and forward to the selected app port.
Set `SECURE_COOKIES=true` for that HTTPS entrypoint. Remote CLI profiles require
verified HTTPS. See [private deployments](#private-or-proxied-deployments) and
[SECURITY.md](../SECURITY.md).

When a proxy must reach the app across a private network, choose the bind with
the actual interface: macOS `APP_BIND` (or `ODYSSEUS_HOST`), Windows `-BindHost`,
or foreground bootstrap `--host`. `openclank server` lifecycle management uses
loopback profiles. Changing `.env` does not change a running process or override
an explicit launcher flag.

### Common self-host traps (30-second fixes)
A grab-bag of small gotchas that otherwise turn into long debugging sessions.

- **Copy buttons do nothing over a plain-HTTP Tailscale/LAN URL.** Browsers only expose the clipboard API (`navigator.clipboard`) on **secure origins** — HTTPS, or `localhost`. Over `http://100.x.y.z:7777` it is blocked. Serve over HTTPS (see *HTTPS + LAN/Tailscale exposure* above); `localhost` is exempt, so copy still works on the host itself.
- **Self-hosted ntfy reminders don't reach your phone.** Two things: (1) the bundled ntfy binds to loopback by default — to reach it from your phone set `NTFY_BIND` to your host/Tailscale IP and `NTFY_BASE_URL` to the same server URL in `.env`, then recreate the ntfy container (see the `NTFY_*` block in `.env.example`); (2) in the ntfy **Android** app, subscribe to the topic with **Instant delivery** enabled — non-`ntfy.sh` servers don't get instant push otherwise.
- **Local mail (Dovecot) login fails: "Plaintext authentication disallowed on non-encrypted connections."** Your IMAP/SMTP server is refusing cleartext auth over an unencrypted link. Prefer enabling TLS on the mail server; on a trusted LAN only, you can allow cleartext (Dovecot: `disable_plaintext_auth = no`).
- **Calendar/contacts (Radicale) won't sync.** Point Open Clank at the **full collection URL** with its trailing slash — e.g. `http://host:5232/<user>/<collection-id>/` — not just the server root. Radicale shows this address for each calendar/address book in its web UI.

### Optional Dependencies
`requirements-optional.txt` contains packages that unlock extra features. It is not installed by default.

| Package | Feature unlocked |
|---------|-----------------|
| `faster-whisper` | Local speech-to-text (microphone -> text) via the "local" STT provider. |
| `ddgs` | DuckDuckGo as a search provider option. |
| `PyMuPDF` | PDF page rendering in the side viewer panel and form-filling. (Note: AGPL-3.0) |
| `markitdown` | Office/EPUB document text extraction (converts .docx/.xlsx/.pptx/.xls/.epub to Markdown). |

### Faster, reproducible installs with uv (optional)

[uv](https://docs.astral.sh/uv/) can replace the venv/pip steps. A requirements
install does not create a lockfile. For a fresh macOS/Linux source environment:

```bash
uv venv venv --python 3.13
uv pip install --python venv/bin/python -r requirements.txt
source venv/bin/activate
# Obtain matching parts first; see Offline emoji artwork below.
python scripts/emoji_asset_bundle.py assemble --parts /path/to/emoji-parts
python setup.py
openclank server start
```

On Windows, select `venv\Scripts\python.exe` instead and activate the Windows
venv. `requirements.txt` is not a complete lock: it mixes unpinned requirements,
exact pins and version ranges. Resolve a lock separately for your target OS:

```bash
uv pip compile requirements.txt -o requirements.lock
uv pip sync --python venv/bin/python requirements.lock
```

Compile resolves the declared requirements; it does not snapshot currently
installed packages. The ignored lock is platform-specific. Regenerate it
deliberately for dependency updates, and keep the existing-data preflight
separate from dependency installation.

### Email: Google and Outlook / Office 365

Mail supports configured IMAP/SMTP accounts and Google OAuth. Google Workspace /
.edu setup uses **Connect with Google** after the administrator configures the
OAuth client/callback. Microsoft OAuth and Graph Mail are not implemented;
Microsoft accounts requiring OAuth cannot use the password form. See
[mail setup and Microsoft limits](email-outlook.md). App login, model-provider
login and mailbox authorization are separate.

## Security Notes
Open Clank is a self-hosted workspace with powerful local tools: shell access, file uploads, model downloads, web research, email/calendar integrations, and API tokens. Treat it like an admin console.

- Application access always requires an authenticated account, including local development.
- Use `SECURE_COOKIES=true` when Open Clank is served through HTTPS by a trusted reverse proxy or private access gateway.
- Do not expose it directly to the public internet without HTTPS and a trusted reverse proxy or private access layer.
- Keep `.env`, `data/`, `logs/`, databases, uploads, generated media, backups, auth/session files, API keys, and model/provider tokens out of Git and private shares. They are ignored by default.
- Review `data/auth.json` after first boot: disable open signup unless you intentionally want it, make only your own account admin, and keep demo/test accounts non-admin.
- Non-admin users do not get shell/Python/file read/write by default, and admin-only routes/tools such as MCP management, API tokens, webhooks, model/cookbook serving, backup/vault, and app settings are admin-gated. Other features are controlled by per-user privileges, so review each user's privileges before exposing a deployment.
- Rotate any API keys or tokens that were ever pasted into a shared chat, demo, screenshot, or log.
- If you enable API tokens or webhooks, create separate tokens per integration and delete unused ones.
- Prefer binding manual development runs to `127.0.0.1`; bind to `0.0.0.0` only when you intentionally want LAN/reverse-proxy access.
- Keep SearXNG, ntfy, Ollama, vLLM, llama.cpp, databases, and raw model/provider APIs internal-only. Expose only the authenticated Open Clank web/API entrypoint through your trusted proxy or private access layer.
- Before publishing a fork, run `git status --short` and confirm no private files from `.env`, `data/`, `logs/`, uploads, backups, or local databases are staged.

### Private or proxied deployments
Open Clank serves plain HTTP on its app port. For a native/source installation, a typical private setup is:

1. Keep Open Clank on localhost, for example `127.0.0.1:7777`.
2. Terminate HTTPS at a trusted reverse proxy or private access gateway.
3. Put the authenticated Open Clank web/API entrypoint behind that layer.
4. Keep raw service and model ports internal-only.

Cloudflare Access, Tailscale, Caddy, nginx, and Traefik can all fit this pattern; none are required by Open Clank. If your access layer reaches Open Clank on the same host, proxy to `http://127.0.0.1:7777` and use `SECURE_COOKIES=true` for the HTTPS entrypoint.
`ALLOWED_ORIGINS` lists exact permitted origins for cross-origin browser/API clients; ordinary same-origin reverse-proxy access usually does not need a special CORS entry.

Common internal-only service ports (legacy Compose examples do not establish Docker support):

| Port | Service |
|---|---|
| `7777` | Open Clank raw app port |
| `8080` | SearXNG |
| `8091` | ntfy |
| `11434` | Ollama |
| `8000-8020` | Common local model/provider APIs |

## Configuration
Most model setup is done in **Settings**. Use **Add Models** to choose the
**Local**, **API**, or **Subscription** setup card, then choose the model in
Chat. **Added Models** is the inventory of connections already added to the
account. Provider credentials belong in the supported connection form, not in
chat prompts or source control. Use `.env` for application and non-model
integration settings needed before first boot. Some older provider environment
variables are rejected during setup; use the current Settings flow instead.
Key settings:

| Variable | Default | Description |
|---|---|---|
| `SEARXNG_INSTANCE` | `http://localhost:8080` | SearXNG URL. Unsupported legacy Compose overrides this to `http://searxng:8080`. |
| `SEARXNG_SECRET` | generated by legacy Compose bootstrap | Optional SearXNG cookie/CSRF secret. Leave blank unless you need to pin it. |
| `APP_BIND` | `127.0.0.1` | macOS/foreground bootstrap default; also used by unsupported legacy Compose. Windows uses `-BindHost`; CLI uses its profile. |
| `APP_PORT` | `7777` | macOS/foreground bootstrap default; also used by unsupported legacy Compose. Windows uses `-Port`; CLI uses its profile. |
| `APP_DATA_DIR` | `./data` | Unsupported legacy Compose host directory for application data volumes. |
| `APP_LOGS_DIR` | `./logs` | Unsupported legacy Compose host directory for application logs. |
| `ALLOWED_ORIGINS` | `http://localhost,http://127.0.0.1` | Comma-separated exact permitted origins for cross-origin browser/API clients. |
| `SECURE_COOKIES` | request scheme | When unset, HTTPS ASGI requests receive Secure cookies and HTTP requests do not. Set `true` behind a trusted proxy or private access gateway unless its configured proxy middleware establishes the HTTPS ASGI scheme; a caller-supplied `X-Forwarded-Proto` header is never trusted directly. |
| `OPEN_CLANK_DATA_DIR` | source `data/`; frozen `~/.open-clank/data` | Native data root; legacy `ODYSSEUS_DATA_DIR` alias accepted. Does not move existing data. |
| `DATABASE_URL` | SQLite `app.db` under resolved data root | Validated startup requires that installation SQLite URL. External/historical stores need separate backup/migration review. |
| `FM_DB_PATH` | `frankenmemory.db` under resolved data root | Memory/RAG store; independent overrides need independent backup. |
| `OPENCLANK_HISTORY_ROOT` | `history/` beside History settings | History root override; preserve separately if outside live `data/`. |
| `COPAL_LOOSE_ROOT` | `copal-vaults/` under resolved data root | Loose vault root; preserve the entire vault, including `.copal` metadata and associated media. Back up separately when outside repository `data/`. |
| `FM_CODE_INDEX_LEASE_SECONDS` | `99999` | Code-index stale-run recovery lease in seconds (60–99999). This is not an indexing timeout; lower it only when faster abandoned-run recovery is preferred. |
| `ODYSSEUS_CHAT_UPLOAD_MAX_BYTES` | `10485760` | Chat/agent attachment cap in bytes. Raise for larger local PDFs or text documents. |
| `ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES` | `104857600` | Legacy-named Files/Imps image upload cap in bytes (100 MB). |
| `ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES` | `26214400` | Legacy-named Imps transform input cap in bytes (25 MB). |
| `ODYSSEUS_MEMORY_IMPORT_MAX_BYTES` | `10485760` | Memory import file cap in bytes (10 MB). |
| `ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES` | `26214400` | Personal document upload cap in bytes (25 MB). |
| `ODYSSEUS_EMAIL_COMPOSE_UPLOAD_MAX_BYTES` | `26214400` | Email compose attachment cap in bytes (25 MB). |
| `ODYSSEUS_STT_MAX_AUDIO_BYTES` | `26214400` | Speech-to-text audio cap in bytes (25 MB). |
| `ODYSSEUS_ICS_MAX_BYTES` | `10485760` | Calendar `.ics` import cap in bytes (10 MB). |

Setup/app dotenv loading preserves exported environment values. Bootstrap resolves
data/host/port before app import: export critical native path settings or pass
`--data-dir` explicitly, rather than assuming a file-only change is sufficient.
macOS loads `.env` in the launcher; Windows bind/port flags and CLI profiles use
the separate precedence above. Compose `APP_DATA_DIR`/`APP_LOGS_DIR` are host
mounts, not native data-root settings.

All upload-limit vars are validated (must be a positive integer) and optional; an invalid value fails fast at startup.

### Built-in MCP servers (optional setup)

Open Clank registers a few built-in MCP servers at startup. The npx-based ones (currently the browser server, `@playwright/mcp`) only start when their npm package is already in the local npx cache. If a package isn't cached, that server is skipped with a startup log message explaining what to do, so a fresh install does not block on a multi-minute npm download or hang if Playwright system dependencies are missing.

To enable the browser MCP (page navigation, screenshots, vision), run once:

```bash
npx -y @playwright/mcp@latest --version
```

That installs `@playwright/mcp` plus Playwright (~300 MB total). Restart Open Clank and the server will register it at startup.

## Architecture
```
app.py                   # FastAPI entry point
core/      auth, database, middleware, constants
src/       llm_core, agent_loop, agent_tools, chat_processor, search/
routes/    chat, session, document, memory, model … endpoints
services/  docs, memory, search, hwfit (Cookbook) …
static/    index.html + app.js + style.css + js/ (modular front-end)
static/docs/media/       reusable media for the shared Copal/Wiki handbook
docs/      operator setup, upgrade and reference guides
```

## Data

Source defaults put application state in repository `data/` (gitignored).
Frozen builds default to `~/.open-clank/data`; native overrides use
`OPEN_CLANK_DATA_DIR` (with `ODYSSEUS_DATA_DIR` compatibility). Compose host
mounts use `APP_DATA_DIR`. Current hosted source uses one Files-backed backend:
Editor/Wiki uses the loose vault at `COPAL_LOOSE_ROOT` or `DATA_DIR/copal-vaults`.
Preserve that entire root, including document files, `.copal` identity/revision/
history/trash metadata and associated media. An independent loose-root override
needs independent backup. Older installed revisions may retain a Redb database
and assets under their configured root; preserve that complete store and its
matching installed revision independently. The current hosted source does not
select that backend. Standalone Copal’s native store is separate.

Inventory the installed build's selected backend and actual configured root;
do not infer active storage from a source default or switch backends to match
these docs. History, Frankenmemory, mail attachments and approved host workspaces
may also live separately. Preserve every actual path, database and encryption
key. Historical Chroma data is migration input, not an optional live backend.

To back up or restore the configured data paths, see the
[Backup & Restore guide](backup-restore.md).

## Offline emoji artwork

**Complete this step before first launch from source or a packaged app.**
Git checkouts, source archives, macOS DMGs and Windows ZIPs omit the large pack; the shared
one part installs the full offline artwork once. The supporting artwork release
becomes available when published; application downloads remain separate and become available
when the application release is published. Source code alone does not complete
installation.

Obtain these two installation files from the release matching your source or package on the
[artwork release](https://github.com/Plaer1/open-clank/releases/tag/v1.0.2-beta.1-artwork-compact), when published:

- `emoji-assets.parts.json`
- `emoji-assets.pack.part-001`

Save them together in one directory. With GitHub CLI installed, the equivalent
commands below download assets only; they do not create or publish a release.
Beta 1 uses the separate `v1.0.2-beta.1-artwork-compact` supporting release. Use the
command after publication to obtain the artwork and confirm both installation files are present.
The application binaries and source use the separate `v1.0.2-beta.1` release.

```bash
gh release download v1.0.2-beta.1-artwork-compact --repo Plaer1/open-clank --dir emoji-parts --pattern 'emoji-assets.parts.json' --pattern 'emoji-assets.pack.part-*'
python3 scripts/emoji_asset_bundle.py assemble --parts emoji-parts
python3 scripts/emoji_asset_bundle.py verify
```

For the macOS app, use its [packaged CLI](#macos-app-package) for assembly and
verification rather than the source Python script. On Windows, the same `gh`
command works in PowerShell. A source installation
uses `py -3.11` in place of `python3`; a release package uses
`.\openclank.exe assets assemble --parts <directory>` and
`.\openclank.exe assets verify`. GitHub CLI is optional: downloading both installation files in a browser
and running the assembly command with their directory works too. For files
provided directly by the maintainer, skip the `gh` command and supply that local
directory with `--parts`.

The compact runtime uses schema2 SQLite: exact Google SVG (compressed only at rest) and 160px lossless Kitchen WebP. All available identities remain offline. Raw local acquisition provenance is retained privately and is not a release asset.

Assembly verifies every part, the pinned total hash/size, SQLite integrity and
runtime catalog counts, then publishes the pack atomically at
`static/vendor/google-emoji/emoji-assets.pack` for source installations, or
`assets/google-emoji/emoji-assets.pack` beneath normal application data for
frozen Windows/macOS packages. The pinned pack is 449,189,888
bytes with SHA-256
`42adf0a79e4012ae71be7f5b3c348b51cdbfddc254a02829547cb60a58a4074c`.
An existing pack is verified and never silently replaced. Wrong, incomplete or
mismatched assets are refused; obtain the matching parts before continuing.
Allow about 0.84 GiB for the downloaded part and temporary assembled pack.
The macOS archive, expanded source, parts and pack require roughly 1.1 GiB
before Python/Bun/Cargo dependencies, native build outputs and application data;
reserve several additional GiB for setup. An uncached installation disk ceiling
has not been measured. After successful
assembly and verification, the downloaded parts can be removed to reclaim
about 0.42 GiB. Runtime reads the local pack; it never fetches artwork from a CDN.

Maintainers prepare local parts, each at most 512 MiB, with:

```bash
python3 scripts/emoji_asset_bundle.py split --parts /path/to/local-release-parts
```

This copies the verified installed pack and writes a part manifest; it does not
upload artwork, modify the pack, or fetch upstream sources. Preserve the shipped
artwork notices. Private acquisition tools/receipts are not public installation
or build dependencies. The retained Dockerfile is unsupported legacy reference,
not an official build or release target.
