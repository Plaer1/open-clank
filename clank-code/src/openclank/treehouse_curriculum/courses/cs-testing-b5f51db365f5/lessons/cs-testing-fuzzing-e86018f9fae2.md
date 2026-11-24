# Generate inputs with an oracle

- Fuzzing generates or mutates inputs; an oracle decides whether behavior violates a contract. More coverage is not proof of correctness.
- Input is a string of at most64 characters. Empty or whitespace-only input returns []. Otherwise split on commas into at most4 fields; trim each. Each field is optional ASCII minus `-` followed by at least one ASCII digit0…9, with value−9…9. No plus sign, empty field or Unicode numeral is accepted. Any contract-invalid string raises ValueError. Leading zeroes and -0 are allowed. Non-string input is outside this contract.

Supplied broken Python parser:

```python
def parse_counts(text):
    if not text.strip():
        return []
    return [int(part) for part in text.split(",")]
```

Given hand-derived case: it accepts `" +1 "` as [1]. Failure predicate: **a token with a leading plus is accepted instead of rejected**. This is a contract violation, not a claim of an executed fuzzer.

**Try:** Propose generated cases covering empty, truncated and unexpected-token inputs. State the oracle's expected behavior for each. Propose a smaller form of the given failing case that preserves the predicate and a simplification that removes it; explain the difference.
