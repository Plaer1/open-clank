# Upstream component sync — 2026-07-28

## MiMo Code

- Previous upstream base: `649b93aefccdafbb5f2aa9920498a1a884d045bd`
- Current upstream base: `60af8f1f`
- Nested checkpoint: `2ac2963e`
- Rebased branch: `checkpoint/open-clank-upstream-20260728-075619`
- Open Clank tip after replay fixes: `988d2489`
- Result: all 374 upstream commits incorporated; Open Clank is 19 commits ahead and 0 behind.

Conflict resolutions preserved the Open Clank live Codex catalog, device login,
request cancellation, provider credential handoff, shell secret filtering,
Frankenmemory/session scope, and host-owned identity rules against current
upstream APIs.

Verification:

- `bun typecheck`: passed.
- Focused eight-file run: 147 passed; one identity-neutral prompt assertion
  failed after upstream expanded `gpt.txt`.
- After depersonalizing the new runtime section and making the Codex catalog
  fixture deterministic, the focused Codex and identity suites passed: 22/22.

## Odysseus

- Previous integrated tip: `402022ffe8e24f0e52d0575f6f485d6b5c168186`
- Equivalent commit in rewritten upstream history: `98bcb64192289506379cd147f174a8172cbf5a58`
- Current rewritten upstream tip: `d96c7af3df769508de01900b2264520b649caa4c`
- The old and rewritten integration-point trees differ only in
  `routes/email_routes.py`.
- Synthetic delta bridge: `0b46d44d1314e00f7718b4bf2bda34adf7746db6`
  (parented at the previously integrated `402022ff`, with the exact
  `d96c7af3` tree).
- Result: all 34 rewritten-upstream commits incorporated without replaying the
  unrelated force-rewritten history.

Conflict resolution retained Open Clank's Agent-only chat flow, native MiMo
connections and sharing, provider-owned memory digest/recall, persisted
tool-capability evidence, transcript-v2 layout, and authored frontend. It also
incorporated Odysseus's canonical model capability schema/readers, Google
catalog pagination and pinning, image generation/editing paths, route shims,
tool additions (`apply_patch`, `todowrite`), OAuth and email fixes, hidden-dir
index protections, and session image cleanup.

Integration fixes included:

- Kept `X-Odysseus-*` as the stable email/webhook wire-header namespace while
  retaining Open Clank user-facing branding.
- Removed an upstream Kimi user-agent override that conflicts with Open Clank's
  no-client-impersonation policy.
- Made internal `app_api` discovery preserve the authenticated owner header.
- Combined canonical provider metadata with Open Clank's database-backed
  tool-capability state.
- Preserved API model pinning while keeping personal and shared routes
  independently selectable.

Verification:

- Python compile and conflict-marker scans: passed.
- Focused integration matrix: 314 passed.
- Follow-up regression groups for headers, endpoint resolution, model sharing,
  schedulers, webhooks, session isolation, and owner scope: passed.
- Full root suite: 5,375 passed and 3 skipped. The remaining 26 failures were
  test-order state leaks; the affected memory/model-share/session group passed
  41/41 in a clean run, and the memory test now explicitly clears live provider
  state.
- MiMo changed-file suite: 434 passed, 2 skipped, 0 failed across 16 files.

## Post-sync frontend repair

The first browser refresh exposed merge damage in the authored frontend:

- `chat.js` and `sessions.js` contained duplicate declarations, preventing the
  browser from parsing the chat/session modules.
- The viewport meta tag was dropped, so mobile rendered at a 980px layout
  viewport.
- The raw Models sidebar section was enabled by default and the account label
  could remain at the HTML fallback `User`.
- The service worker could serve cached HTML alongside the newly merged module
  graph on the first refresh.

The repair removed only the duplicate merge blocks, restored the viewport and
Open Clank sidebar/auth defaults, and made root navigation network-first with a
new cache generation.

Verification:

- Syntax check across every `static/**/*.js` module: passed.
- Startup/i18n shell contracts: 8 passed.
- Chat/session focused regressions: 19 passed.
- Streaming invariant matrix: 113 passed.
- Clanker browser acceptance, including the full pattern transition matrix,
  mobile 390x844, DPR resize, and login surfaces: passed.
- A 1536x1606 browser render at 125% UI scale showed the authenticated name,
  hidden Models section, full-height chat/sidebar, and a canvas exactly matching
  the viewport.
