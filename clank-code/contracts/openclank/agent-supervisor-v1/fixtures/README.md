# Agent-supervisor compatibility fixtures

`compatibility-v1.json` is a secret-free, provider-neutral characterization
fixture. It records the shapes that the ACP adapter and future Rust supervisor
must preserve: readiness, owner generation, leases, model catalog, session
mapping/config, SSE ordering, permission/question/plan interactions, managed
provider control, owner lifecycle, and idempotent shutdown.

The fixture is not a production transcript and contains no credentials,
provider URLs, filesystem paths, raw PTY bytes, or user content.
