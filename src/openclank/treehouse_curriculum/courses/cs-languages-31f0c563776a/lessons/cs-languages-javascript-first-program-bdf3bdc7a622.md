# A first browser JavaScript program

- `return` gives a value to a caller; `console.log` sends a message to the browser console.
- Save these sibling files to try the program locally. The HTML loads the script; no server or package is needed.

```javascript
// first_sum.js
function add(a, b) { return a + b; }
console.log(add(2, 3));
```

```html
<!-- first_sum.html -->
<!doctype html>
<html lang="en"><meta charset="utf-8"><title>First sum</title>
<script defer src="first_sum.js"></script><body><p>Read the browser console.</p></body></html>
```

**Try:** Predict the helper's result and console message. Change only the second argument to `0`; then separately restore the arguments and change `+` to `-`. Explain why a console message is neither a visible page paragraph nor an OS exit status.
