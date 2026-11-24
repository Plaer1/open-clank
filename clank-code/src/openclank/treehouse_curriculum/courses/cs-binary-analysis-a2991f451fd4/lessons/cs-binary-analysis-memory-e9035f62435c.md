# Inspect live values carefully

- A memory snapshot belongs to a process, point in time, and mapping. Byte order, width, alignment, and type affect interpretation.
- Bytes `01 00` represent1 as a little-endian unsigned16-bit value, but256 as big-endian.
- Reading beyond an object's lifetime or authorized region can give unrelated data. Use owned toy data.

**Try:** Interpret the bytes both ways and list the context needed before interpreting a memory address.
