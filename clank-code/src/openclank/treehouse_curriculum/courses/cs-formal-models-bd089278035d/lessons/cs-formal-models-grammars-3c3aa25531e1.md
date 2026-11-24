# Grammars and nested structure

- A grammar generates allowed syntax. With S→(S)S or empty, derive balanced parentheses such as `()()`.
- A parse tree records which rules built the string; grammar ambiguity means multiple parses for the same string.
- Context-free nesting can need unbounded stack-like memory beyond a fixed finite-state machine.

**Try:** Give a derivation of `(())`. Explain why a syntactically valid arithmetic expression may still fail during evaluation.
