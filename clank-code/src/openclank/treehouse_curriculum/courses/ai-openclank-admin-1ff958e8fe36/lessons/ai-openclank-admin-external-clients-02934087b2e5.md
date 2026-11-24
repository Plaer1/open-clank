# Save the inbound client's actual scopes

- Settings → Integrations can offer Add Codex Agent or Add Claude Agent for an external client calling Open Clank. Token administration needs the appropriate app authority.
- Follow the direction: client C → Open Clank uses external-client token T; Open Clank → provider uses provider credential P. Neither is the application's browser login session.

**Offline card:** owner A has provider route P and creates client C's token T. The secret is displayed once; the server stores its hash. Newly created T has persisted scopes `[chat]`. The visible preview has all tool toggles selected, but no scope-saving PATCH has succeeded. T still has only chat. Save succeeds with scopes `[chat, documents:read]`; only then is that added scope persisted. For an existing token, a toggle can issue its PATCH immediately; inspect the actual result.

**Access variants:** saved documents:read scope plus an available backend permits reading A's authorized document. A request for another owner's document is denied. An email action is not in the saved scopes. A revoked T authorizes none of these actions.

T and P are symbolic identifiers, not secrets. This exercise needs no token creation, agent installation or live external request.

**Unavailable and revocation cards:** T has saved documents:read but the document backend is unavailable: access cannot complete. Revoke T to end this client’s inbound access; that does not revoke provider credential P or log out the browser application session.
