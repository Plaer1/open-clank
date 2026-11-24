# Embedded Lore provenance

Open Clank embeds ten local Rust crates from Epic Games Lore v0.14.0, commit `317f48b918f0b4d43de104d99e80ac4a57b697df`. The release workspace identifies itself as `0.13.1-nightly`. The embedded tree contains 313 files. The current release record identifies every local patch, the parent and upstream lockfiles, 15 pregenerated protocol bindings, both crate overrides and the retained license texts. Earlier Stage A and B records document the v0.12.0 and v0.9.1-nightly baselines. [Upstream release](https://github.com/EpicGames/lore/releases/tag/v0.14.0) · [Vendor provenance](vendor-provenance.json)

## Release decisions

- v0.13.0 changed the local read and write paths and introduced strict local read errors. The adapter requests strict reads so damaged local content surfaces as a read failure.
- v0.14.0 changed flush arguments, local bucket publication, write completion tracking, and concurrent read/eviction handling. History uses `FlushOptions::SYNC_DATA` for durable writes. It disables background flush, isolates partitions, protects local fragments and waits for flush before publishing a byte receipt.
- The upstream fast-flush option rewrites bucket indexes in place. An interrupted update can discard all entries in the affected bucket. Durable and relaxed History flushes both keep the temporary-file-and-rename path. The Unix backend propagates errors syncing the parent directory.
- The build reads checked-in protocol bindings. The glob-match override follows the release’s matching fixes. Quinn-proto retains the upstream patched 0.11.19 tree. The package carries the Epic Games, rpmalloc, glob-match and Quinn-proto license texts.

## Store upgrade

New roots carry History format 3 and the exact source and patchset identities. The ordinary service refuses earlier or unknown markers before opening the store. The Linux and macOS offline copy helper recognizes the exact v0.12 format-2 marker and the historical format-1 marker. It compares catalog rows, recomputes retained content digests, flushes the candidate and publishes a preparation receipt. Its limits are available through `openclank-history-prepare-copy limits`.

Stop and drain all History, shell and Files writers. Supply the catalog, Lore root, registry, receipts, shell journals and Files journals in that order when creating the baseline. Run `start`, `advance` and `finalize` on the private working copy. The independent `activate` command rechecks the original inventory and prepared receipt while holding the catalog and store locks. It uses a same-filesystem atomic directory exchange. Nested authorities move with the Lore directory. External components retain their original identities and the contents checked against the candidate. Configured optional authorities that have never been created retain their recorded absence. The previous store is retained at the printed rollback path along with the untouched baseline. A repeated activation recognizes the completed exchange. After writes resume, rollback requires reconciling newer changes.

Linux x86_64 release binaries completed a disposable v0.12-to-v0.14 exchange with seven schema-2 catalog rows and two content references. The ordinary authenticated History client read a pre-upgrade version and restored all 48 bytes to a file. A second activation returned the same rollback location. macOS and Windows execution, other catalog profiles and the browser UI remain unverified in this check. The offline helpers currently rely on Unix locking.

Windows x64 installations with format-2 history should stay on their existing compatible package. Keep that executable and data together until a Windows-native copy migration is available. The v0.14 service requires its own format-3 marker.
