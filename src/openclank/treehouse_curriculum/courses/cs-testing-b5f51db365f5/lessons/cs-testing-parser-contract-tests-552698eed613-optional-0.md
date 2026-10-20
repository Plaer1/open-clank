Expected valid results: [2,-1,2], [2,0], [], []. All remaining cases require ValueError. The broken parser already rejects empty fields and Unicode minus, but accepts10, +1, too many fields, fullwidth digits and overlength whitespace. Those accepted inputs violate the contract.

One reference repair:

```python
def parse_counts(text):
    if len(text) > 64:
        raise ValueError("too long")
    if not text.strip():
        return []
    fields = text.split(",")
    if len(fields) > 4:
        raise ValueError("too many fields")
    result = []
    for field in fields:
        token = field.strip()
        digits = token[1:] if token.startswith("-") else token
        if not digits or any(c < "0" or c > "9" for c in digits):
            raise ValueError("bad field")
        value = int(token)
        if not -9 <= value <= 9:
            raise ValueError("out of range")
        result.append(value)
    return result
```

The length check must precede the whitespace shortcut. Accepting empty input is correct. Raising ValueError for a forbidden field is correct, not a crash bug.