# Test a format hypothesis

- Original toy save format: bytes 0–1 are ASCII `GQ`; byte 2 is version 1; bytes 3–4 are an unsigned little-endian coin count; byte 5 is room ID; total length is exactly 6.
- Sample hex `47 51 01 02 01 03` means coins=258, room=3. This is a declared teaching format, not an inferred real-game format.
- A parser checks magic, version, length and value constraints before creating game state. Test truncation, unknown versions and out-of-range rooms as well as the happy path.
- Archive extraction, editor reconstruction and runtime code analysis answer different questions. Recovering a texture does not recover its material graph, source license or gameplay behavior.

**Self-check:** Decode `47 51 01 05 00 02`. Then give two malformed samples and the exact check each should fail, using valid room IDs 0–3.
