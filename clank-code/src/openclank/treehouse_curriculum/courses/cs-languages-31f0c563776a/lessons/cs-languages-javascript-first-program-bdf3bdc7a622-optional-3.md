After the tiny program, try this independent contract: exactly two strings, each one ASCII digit0…9. Wrong count, signs, spaces, multidigit or fullwidth input is invalid;0 is valid, not absence.

**sum_digits.js**

```javascript
function add(a, b) {
  return a + b;
}

function solve(args) {
  if (args.length !== 2) return null;
  for (const text of args) {
    if (text.length !== 1 || !/^[0-9]$/.test(text)) return null;
  }
  return add(Number(args[0]), Number(args[1]));
}

function run(args) {
  const result = solve(args);
  console.log(result === null ? "error: expected two digits 0..9" : `sum=${result}`);
  return result === null ? 2 : 0;
}

function tests() {
  const cases = [[["2", "3"], 5], [["0", "9"], 9],
                 [["pear", "3"], null], [[], null]];
  for (const [input, expected] of cases) {
    if (solve(input) !== expected) {
      console.log("test failure");
      return 1;
    }
  }
  console.log("tests=4 passed");
  return 0;
}
```

**javascript-bridge.html** (sibling)

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>JavaScript digit bridge</title>
  <script src="sum_digits.js" defer></script>
</head>
<body>
  <h1>JavaScript digit bridge</h1>
  <p>Open your browser's developer console for this local page.</p>
  <p>Run <code>run(["2", "3"])</code>, then <code>tests()</code>.</p>
</body>
</html>
```

Open this local HTML. In its console run `run(["2","3"])`, `run(["0","9"])`, `run(["0","0"])`, then `tests()`. Expected logs sum=5, sum=9, sum=0, tests=4 passed; each function returns0. Invalid arrays ["pear","3"], ["10","3"], ["-1","3"], [" 2","3"], ["2"], ["2","3","4"], ["２","3"] each log the error and return2. The return is an application convention, not process status.

Keep the baseline; separately change add to subtraction and rerun the four-case test: expect test failure with process/returned status1. Restore addition and try4,4 for sum=8. Distinguish text concatenation from numeric addition and absence from zero. A hand table is predicted work; only an actual run provides execution evidence. This deeper practice is not added to the first-program run criterion.