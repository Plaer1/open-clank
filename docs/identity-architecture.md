# Identity architecture — one persona, decided once, both hosts

How open-clank decides WHO the agent is. Companion to
`docs/memory-architecture.md`; same shape of ruling: one decision point,
made where the context lives (Odysseus), rendered blind downstream.

## The default persona

`src/default_persona.py` owns the canonical record — per-owner, stored in
the preset store (`data/presets.json` under `default_personas`). Factory
seed: name **Odysseus**, prompt = upstream's legacy baseline. The record
is EDITABLE and is ONE state across three surfaces (edits anywhere update
everywhere):

1. **Conversation** — a turn with no explicit preset/persona speaks as the
   default persona (`chat_handler.validate_and_extract_preset`).
2. **Personal assistant** — the CrewMember row's name/personality is a
   synced copy (`_push_to_assistant` / `sync_from_assistant`); the
   mechanical operating rules live in `task_scheduler.ASSISTANT_TASK_FRAMING`,
   appended at run time, never stored.
3. **Reminders** — the default synthesis voice reads the record
   (`reminder_personas.synthesis_system_prompt`); novelty voices stay.

Background work that users read — research final reports, chat
auto-titles, and scheduled tasks — speaks as the default persona too;
internal role prompts stay mechanical (ruling R15).

Editing surfaces: the chat-bar persona modal's "Default (…)" entry, the
Settings → Personas pill, the assistant settings page. All write the same
record. API: `GET/PUT /api/presets/default-persona`, `POST …/reset`.

## Persona across the ACP seam

The resolved persona (default or an enabled character) crosses to the mimo
child as **true system authority**: `_turn_envelope.system_prompt` →
`_meta.odysseus.system_prompt` → `PromptInput.system`. The bridge skips
the persona's context-message copy so it never ALSO arrives demoted to
annotated prompt text (`acp_bridge._build_prompt_parts`,
`authoritative_system`).

The endorsed memory guidance block (memory-trust metaplan T6) rides the
same seam: the bridge appends it to `envelope.system_prompt` below the
persona text and skips its in-message copy by `TRUST_SENTINEL` — trusted
memories carry system-tier force on both hosts without touching the
persona itself (see docs/memory-architecture.md, "Trust tiers").

The runtime never self-identifies: every mimo prompt asset and the
SystemPrompt environment header are identity-neutral (ruling R14; frozen
by `test/session/prompt-identity-neutral.test.ts`).

## Lanes

- **Agent lane**: the only conversation lane. Standard, Plan, read-only
  Compare, and Temporary Agent all enter `run_agent(AgentRunRequest)` and the
  MiMo registry is filtered by the authoritative envelope tool policy.
- **Auxiliary inference**: named function-level jobs such as title generation,
  memory extraction, grading, notifications, and bounded summaries enter
  `run_auxiliary_inference(AuxiliaryRequest)`. The child uses its conversational
  profile with a wildcard tool deny. This plumbing is not selectable as a chat
  mode and cannot rescue a failed Agent run.
- Tool policy is a complete per-turn revision: provided map replaces,
  empty map clears, omitted map preserves (no sticky deny-all).
- Temporary Agent disables transcript, memory, skill, webhook, and actor
  residue and deletes its ephemeral child session on every terminal path.

### MiMo is the strict Agent runtime (2026-07-18)

Every tool-bearing turn routes through MiMo. `ModelEndpoint` rows are the sole
endpoint registry; a session persists the exact endpoint ID and never
reverse-routes by URL. Per-model capability evidence decides eligibility. The
supervisor materializes owner-visible rows into child spawn configuration under
the `ody-<endpoint_id>` namespace, with credentials passed over an inherited FD.
MiMo-native catalog entries may coexist and are ignored when checking a selected
projected endpoint/model.

Admission reconciles the committed spawn fingerprint and generation, then
acquires a generation-scoped worker/session lease. Reprojection warms a fenced
candidate, drains the old generation, and publishes atomically. Agent failure
returns a typed terminal error; there is no provider, no-tools, or homegrown
fallback and no runtime kill switch. `src/agent_loop.py` remains compatibility
code only until the separately approved retirement gate deletes it.

## Names in the UX

The enabled persona's name populates the sidebar brand, welcome screen,
composer placeholder, chat header, tab title, and role labels
(`presets.applyAgentName`, `window.__agentName`). "Odysseus" survives
only as the factory default persona's name; personal agent identities are
user data and never appear in first-party source (ruling R9, enforced by
`tests/test_identity_literals.py`).

## Rulings

R1-R16, canonized 2026-07-16 in
`.futures/AGENT-IDENTITY-METAPLAN-INDEX-2026-07-16.md` (evidence in
`.robonotes/identity-audit-2026-07-16-*`). Tool disposition:
`docs/agent-tool-parity.md`.
