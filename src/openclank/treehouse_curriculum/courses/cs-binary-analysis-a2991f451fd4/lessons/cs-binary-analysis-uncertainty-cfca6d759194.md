# Optimization, packing, concealment

- Optimization can remove variables, inline calls, or reshape loops while respecting the language's allowed behavior.
- Packing may restore code at runtime; obfuscation makes structure harder to recover. Anti-analysis behavior can depend on environment.
- Record observed, inferred, and unknown separately; corroborate high-impact claims with another view.

**Try:** A static view finds a small stub; a runtime trace reaches newly produced code. Give two hypotheses and one distinguishing observation.
