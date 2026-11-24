# Make offline guessing expensive

- Password storage normally verifies an expensive password-derived value rather than retaining recoverable plaintext.
- A unique salt prevents identical passwords from sharing one stored result and limits reuse of precomputed tables; it is not a secret pepper.
- Memory-hard password hashing increases the resource cost of guesses. It cannot turn a predictable password into an unguessable one.

**Try:** Two accounts use the same weak password. Explain what unique salts change and what they do not change.
