# Choose operations by value type

- Python integers count, floats approximate numeric quantities, strings hold text, booleans express a logical choice, and `None` marks absence.
- `6 + 1` computes a count; `"6" + "1"` joins text. Parsing text needs an explicit failure policy.
- Python allows arithmetic on booleans because `bool` subclasses `int`; that does not make arithmetic appropriate for a logical flag.

**Try:** For `3`, `3.5`, `"3"`, `False`, `None`, name the exact Python type and role. Predict `value + 1`, distinguishing an error from an inappropriate operation. For an optional count, write a branch policy distinguishing `0`, `"0"`, and `None`.
