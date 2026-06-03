---
name: odysseus
description: Use when the user asks Codex to read or write Odysseus data from a terminal Codex session through the scoped Codex Agent API. Requires ODYSSEUS_URL and ODYSSEUS_API_TOKEN.
---

# Odysseus

Use this skill when a user asks to interact with Odysseus from Codex.

## Configuration

Expect these environment variables:

- `ODYSSEUS_URL`: Base URL for the user's Odysseus instance, for example `http://127.0.0.1:7000`.
- `ODYSSEUS_API_TOKEN`: Scoped API token created in Odysseus Settings > Integrations > Add Integration > Codex Agent.

If either value is missing, do not guess credentials. Tell the user to create a Codex Agent token in Odysseus Settings and expose both values to the terminal session.

## Safety

- All Odysseus data access MUST go through the scoped HTTP API under `/api/codex/*`.
- Check `/api/codex/capabilities` before using a tool surface.
- Treat `403` as an intentional Settings restriction. Do not work around it.
- Do not use SSH, Docker, direct Python imports, SQLite queries, MCP internals, browser cookies, or local files to read/write Odysseus user data.
- Do not call helpers like `do_manage_notes`, email MCP internals, or database sessions directly for user data, even if shell access exists.
- Never send email directly unless the user explicitly asks to send and the token has a send-capable scope.
- Keep actions scoped to the token owner.

## Todos

The Codex API supports todos/checklists:

- `GET /api/codex/todos`
- `POST /api/codex/todos`

Use the bundled helper script when available:

```bash
python3 integrations/codex/scripts/odysseus_api.py capabilities
python3 integrations/codex/scripts/odysseus_api.py todos list
python3 integrations/codex/scripts/odysseus_api.py todos add "Follow up"
```

Supported todo actions are `list`, `add`, `update`, `delete`, and `toggle_item`.

## Email

The Codex API supports scoped email reads:

- `GET /api/codex/emails?folder=INBOX&limit=10&offset=0&filter=all`
- `GET /api/codex/emails/{uid}?folder=INBOX`

Use the bundled helper script when available:

```bash
python3 integrations/codex/scripts/odysseus_api.py emails list 5
python3 integrations/codex/scripts/odysseus_api.py emails read UID
```

If `/api/codex/capabilities` does not show `email.read: true`, do not inspect email. Ask the user to enable Email read in the Codex Agent settings.

## Forbidden Bypass Pattern

If you are about to reach the Odysseus host/container, import app internals, query the database, or call MCP helper modules directly, stop. Those paths bypass Odysseus Settings and token scopes. Ask the user to enable the relevant Codex Agent tool toggle instead.
