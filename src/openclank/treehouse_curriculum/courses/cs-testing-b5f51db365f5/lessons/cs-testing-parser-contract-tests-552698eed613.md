# Test a parser against its whole contract

- A parser's contract defines valid input and rejection; successful conversion alone is weaker.
- Input is a string of at most64 characters. Empty or whitespace-only input returns []. Otherwise split on commas into at most4 fields; trim each. Each field is optional ASCII minus `-` followed by at least one ASCII digit0…9, with value−9…9. No plus sign, empty field or Unicode numeral is accepted. Any contract-invalid string raises ValueError. Leading zeroes and -0 are allowed. Non-string input is outside this contract.

Supplied **broken** Python implementation:

```python
def parse_counts(text):
    if not text.strip():
        return []
    return [int(part) for part in text.split(",")]
```

**Try:** Write an expected-versus-predicted-actual test table for `"2,-1,2"`, `" 2 , 0 "`, `""`, `"  "`, `"1,,2"`, `"10"`, `"+1"`, `"1,2,3,4,5"`, `"−1"` (Unicode minus), `"２"` (fullwidth digit), and65 spaces. Identify actual contract violations; propose a repair. Label hand predictions separately from any later execution.
