# Linux source build

These are source setup instructions. For a relocatable Linux x86_64 package and
its platform limits, see [Linux packaging](docs/linux-package.md).

Use a glibc Linux x86_64 or ARM64 machine with Python 3.11+ (including venv/pip),
recent Rust/Cargo supporting edition 2024, Git and C/C++ build tools. Setup needs
network access and fetches checksum-pinned Bun 1.4.0 when necessary.
On ARM64, the C compiler must accept `-mcpu=neoverse-512tvb` for the History worker.

From `clank-code/` in the source checkout:

```sh
python3 -m venv venv
. venv/bin/activate
python -m pip install -r requirements.txt
python setup.py
python scripts/openclank_engine.py verify
```

Setup builds or verifies the managed engine and builds Memery, History,
and native Files with its Linux thumbnail helper. The optional KDE thumbnail
adapter uses CMake, Qt6 and KF6 KIO development libraries. Setup initializes
local data and installs the `openclank` command. Follow [upgrading](docs/upgrading.md)
before pointing this checkout at an existing profile.

For the complete offline artwork, obtain the matching release parts described in
[setup](docs/setup.md#offline-emoji-artwork), then assemble and launch:

```sh
python scripts/emoji_asset_bundle.py assemble --parts /path/to/emoji-parts
python scripts/openclank_bootstrap.py serve --host 127.0.0.1 --port 7777
```

Open `http://127.0.0.1:7777` and sign in with the account created during setup.
