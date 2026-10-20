# Reuse a prompt without confusing it with a Persona

- Prompt's Inject, Persona and Group controls serve different purposes. An injection wraps a message; a Persona supplies a reusable identity/instruction configuration; a Group coordinates participants.
- Injection requires a selected active preset and a custom injection that is not disabled. Closing or cancelling a modal does not activate a new choice.

**Offline Card A:** preset P selected, custom enabled. Prefix: `Check arithmetic.` Body: `3 + 4` Suffix: `Show one equation.` The composition joins them with one space: `Check arithmetic. 3 + 4 Show one equation.` Card B has the same text but no selected preset. Card C selects P with custom enabled=false. Neither B nor C applies that wrapper.

Persona Q is titled Careful reviewer and contains `State one uncertainty before recommending a change.` Selecting or editing Q requires the applicable owner/edit permission; its name alone proves neither activation nor permission. The supplied cancel receipt leaves the previous selection unchanged. The supplied deactivate receipt clears the active selection; it does not delete Q.

**Persistence card:** Persona Q is saved successfully to active session C9 and reloaded from that session record; it is not automatically every chat’s default. With no active session, the save fails. A server save failure is not a persisted Persona change. A saved reusable template and a session’s selected Persona are distinct identities.
