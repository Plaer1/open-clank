# Inspect connection, capability and trust

- Installed, connected and authorized are separate states. A local process integration and a remote OAuth service have different execution and credential boundaries.

**Offline card:** MCP endpoint M is connected and exposes read-only capabilities; write is disabled. Plugin P is installed but disconnected. An update for P is proposed, not accepted. Nothing in these cards authorizes a write or makes the update active.

Inspect the concrete capability and target before use. Adding or updating executable integration code is a separate trust decision from reading its description.

**Diagnostic variant:** a read request times out without an authorization result; a write request returns permission denied. The first needs connection/timing inspection, the second a scope check. Safe evidence is the redacted endpoint identity, capability list and scoped result, not raw credentials.
