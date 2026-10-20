# Diagnose a connection without exposing a secret

- Check the endpoint, requested model identifier and redacted error before replacing credentials.
- Preserve an unsent draft. Do not paste a key or token into a support example.

**Offline cards:** A: HTTP 401, `authentication failed`; B: HTTP 403, `access denied`; C: HTTP 429, `quota or rate limit`; D: endpoint reachable, `unknown model tiny-practice-v2`; E: catalog success, no chat attempt.

These outcomes point to different checks: authentication, authorization, limit/account state, model identifier, and a still-untried request. A retry is a new attempt, not proof that the cause is fixed.
