# Linux source build

Experimental: Linux app/release support is unqualified; native Files is not built.
These instructions were checked against source, not executed on Linux.

Use a glibc Linux x86_64 or ARM64 machine with Python 3.11+ (including venv/pip),
recent Rust/Cargo supporting edition 2024, Git and C/C++ build tools. Setup needs
network access and fetches checksum-pinned Bun 1.4.0 when necessary.
On ARM64, the C compiler must accept `-mcpu=neoverse-512tvb` for the History worker.

From this source checkout:

```sh
python3 -m venv venv
. venv/bin/activate
python -m pip install -r requirements.txt
python setup.py
python scripts/openclank_engine.py verify
```

Setup builds the managed engine, Frankenmemory and History workers, initializes
local data and installs the `openclank` command. For existing data, follow
[upgrading](docs/upgrading.md) first.

For the complete offline artwork, obtain the matching release parts described in
[setup](docs/setup.md#offline-emoji-artwork), then assemble and launch:

```sh
python scripts/emoji_asset_bundle.py assemble --parts /path/to/emoji-parts
python scripts/openclank_bootstrap.py serve --host 127.0.0.1 --port 7777
```

Open `http://127.0.0.1:7777` and sign in with the account created during setup.
