# Identify whose credential is used

- Follow the route to its credential owner. Application sessions, provider credentials, local server endpoints and shared model subscriptions have separate lifecycles.

**Offline card:** learner-a is logged into Open Clank. Cedar uses learner-a's provider credential. A local server uses the configured local endpoint; this fictional endpoint is explicitly owned and administered by learner-a. That ownership is a supplied fact, not inferred from the Open Clank login. Shared route S7 uses teacher-b's source credential. Open Clank logout leaves Cedar's provider credential active. Revoking Cedar's credential leaves the Open Clank application session active.

Choose the control for the relationship you intend to end. Shared use does not transfer the owner's credential to the recipient or grant access to their files.
