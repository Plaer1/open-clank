# Match delivery to meaning

- A command, snapshot and chat message have different lifetimes. A newer position can replace an old one; a purchase needs a unique transaction identity and a definite result.
- TCP supplies an ordered byte stream; UDP leaves delivery/order handling to the application. QUIC has reliable streams; its DATAGRAM extension adds unreliable messages.
- Tick rate, send rate, bandwidth, latency, jitter and loss are separate measurements. A 60 Hz simulation need not send 60 snapshots each second.
- Schema defines meaning; encoding defines bytes. Quantization loses precision deliberately; compression removes redundancy; a delta requires a known baseline. Version the protocol.

**Self-check:** At 20 updates/second and 100 bytes/update, compute payload bytes/second before overhead. Specify delivery/staleness rules for position, chat and an inventory spend.
