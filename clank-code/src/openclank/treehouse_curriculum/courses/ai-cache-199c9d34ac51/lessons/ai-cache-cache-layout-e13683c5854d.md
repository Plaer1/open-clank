# Keep reusable material stable

- Where the provider uses prefix matching, stable shared material early in a request can preserve more reuse; variable material early can break that prefix.
- Tool definitions, settings and rendered instructions can affect eligibility. Reusing a session does not guarantee a hit.
- Do not move instructions across authority roles just to optimize caching.

Toy requests share a policy and tool definitions but differ in timestamp and question. Keep stable material stable within its proper role, then append changing content.
