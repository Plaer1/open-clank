# Keep credentials out of evidence

- A credential belongs to an identity and scope. A project token, finished-app session and mailbox credential are not interchangeable.
- Record provider, method, time, redacted status and scope in a diagnostic receipt. Never submit a secret or authorization header.
- Revocation belongs to the credential's issuing authority. Logging out of an unrelated app does not prove that a project token was revoked.

Synthetic receipt: “Lab API, project P, document-read scope, permission denied.” The project-token issuer controls revocation; the mailbox owner controls an unrelated mail credential.
