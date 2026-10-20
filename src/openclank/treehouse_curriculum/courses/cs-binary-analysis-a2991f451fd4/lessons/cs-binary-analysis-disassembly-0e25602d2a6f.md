# Instructions from bytes

- Disassembly decodes bytes into instructions for a specified architecture. Selecting the wrong mode or code boundary can produce misleading output.
- In the observed arm64 toy, `ldr w8,[sp,#0xc]` loads a saved value; `add w0,w8,#0x1` adds1; `ret` returns control. Surrounding instructions manage stack space.
- Register meanings and calling conventions are target-specific. Names and source-level types are additional evidence.

**Try:** Given the loaded value 2, predict w0 after the add. State one thing the instruction alone cannot establish.
