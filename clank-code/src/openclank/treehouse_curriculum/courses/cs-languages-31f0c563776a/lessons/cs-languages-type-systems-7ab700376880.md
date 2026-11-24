# Types and checking time

- Static checking rejects selected programs before execution; dynamic checks inspect values during execution. Implementations may use both.
- Type inference discovers types without requiring every annotation. It does not mean checks are dynamic.
- A function returning an integer can still return the wrong integer; types express only the properties their system models.

**Try:** Classify a rejected integer-plus-text operation and a wrong but integer result from double(3). What extra evidence does the second need?
