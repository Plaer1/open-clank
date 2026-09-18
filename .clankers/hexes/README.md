# Open Clank Hexes v2

This directory is the trackable, first-party policy contract for the project.
It is a clean-room Open Clank implementation informed by the pinned Henxels
reference in `.references/henxels`; the runtime does not import, execute, or
depend on Henxels.

`contract.yaml` is the canonical v2 authority. The old root `.hex` remains a
bounded compatibility reader until the retirement slice completes. A v1
contract may be parsed for migration, but its activation and executable trust
must never be reused for v2.

Plans and sidecar records are local namespaces:

- `.clanker/futures/` — ignored plan corpus;
- `.clankers/robonotes/` — ignored audit/run corpus;
- `.clankers/hexes/` — trackable policy, schema, and checks.

Semantic memory and RAG can report derived observations about a contract, but
cannot activate it, grant executable trust, suppress findings, or change these
paths. Action-time file, shell, plan, and background-job gates consult the
activated exact hash.
