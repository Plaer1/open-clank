# State transitions

- State records the values relevant now. Assignment changes a binding; an equation states a relationship.
- Start `x = 2`; execute `x = x + 3`, then `y = x * 2`: x becomes 5, y becomes 10.
- Read each right-hand side from the state before that assignment.

**Try:** Trace `x = 1; y = x; x = 7`. Record x and y after each statement.
