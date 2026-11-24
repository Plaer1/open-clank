After the tiny program, try this independent contract: exactly two strings, each one ASCII digit0…9. Wrong count, signs, spaces, multidigit or fullwidth input is invalid;0 is valid, not absence.

**sum_digits.py**

```python
import sys


def add(a, b):
    return a + b


def solve(args):
    if len(args) != 2:
        return None
    for text in args:
        if len(text) != 1 or not ("0" <= text <= "9"):
            return None
    return add(int(args[0]), int(args[1]))


if sys.argv[1:] == ["--test"]:
    cases = [(["2", "3"], 5), (["0", "9"], 9),
             (["pear", "3"], None), ([], None)]
    for args, expected in cases:
        if solve(args) != expected:
            print("test failure")
            sys.exit(1)
    print("tests=4 passed")
else:
    result = solve(sys.argv[1:])
    if result is None:
        print("error: expected two digits 0..9")
        sys.exit(2)
    print(f"sum={result}")
```

Use `python3 sum_digits.py` with arguments `2 3`, `0 9`, `0 0`, `--test`: expect sum=5, sum=9, sum=0, tests=4 passed, each exit0. Invalid arguments `pear 3`, `10 3`, `-1 3`, `" 2" 3`, `2`, `2 3 4`, `"２" 3` each print error: expected two digits 0..9 and exit2. In a POSIX terminal use `echo $?` immediately after an invocation to see its status.

Keep the baseline; separately change add to subtraction and rerun the four-case test: expect test failure with process/returned status1. Restore addition and try4,4 for sum=8. Distinguish text concatenation from numeric addition and absence from zero. A hand table is predicted work; only an actual run provides execution evidence. This deeper practice is not added to the first-program run criterion.