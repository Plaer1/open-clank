# Trace a complete toy tokenizer

- Tokenization turns source text into encoded units; embeddings represent those units numerically. They are different operations.
- **ToyGreedy-v1** is case-sensitive and scans exact Unicode codepoints left to right, taking the longest vocabulary match. No normalization. If no match exists, report the unknown codepoint and stop. No fallback.

| ID | String |
| --- | --- |
| 1 | cat |
| 2 | s |
| 3 | ␠ (one U+0020 space) |
| 4 | = |
| 5 | 1 |
| 6 | ; |
| 7 | cats |
| 8 | c |
| 9 | a |
| 10 | t |

Input A: `cats =1;` → [7,3,4,5,6]: five tokens, eight characters.
Input B: `cat s=1;` has eight characters too. Trace it using the same rule.

Changed vocabulary **ToyGreedy-v2** removes only `cats` (ID 7). Retrace A and B.
Changed input `cats=2;` with v1 reaches an unknown character.

This is not a provider tokenizer or a billing estimate. Real tokenizers can have different vocabularies, normalization and byte/fallback rules.
