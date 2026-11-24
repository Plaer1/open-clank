# Text, code points, bytes

- Unicode assigns code points; UTF-8 encodes them as bytes. A user-perceived character may contain several code points.
- `A` is U+0041 and one UTF-8 byte. `é` as U+00E9 uses two UTF-8 bytes; `e` plus a combining acute accent uses two code points.
- Declare the encoding at a file or network boundary; decoding the wrong bytes can fail or alter text.

**Try:** Explain why a 10-byte limit is not a 10-character limit. Give one example with different byte and code-point counts.
