# Names and shared objects

- A name refers to a value/object. Two names can refer to the same mutable list.
- In Python: `a = [1]; b = a; b.append(2)` leaves both names referring to `[1, 2]`.
- Rebinding `b = [9]` changes what `b` refers to; it does not rewrite `a`.

**Try:** Draw the two references after append, then after rebinding. Explain when a copy would change the result.
