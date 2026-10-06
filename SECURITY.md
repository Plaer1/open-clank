# Security Policy

Open Clank is a self-hosted workspace with privileged local capabilities.
Application entrypoints always require authentication, including localhost.

## Supported versions

Beta 1 is the first of several betas, with machine version `1.0.2`. Security
fixes are handled on the public default branch, `main`; a release label is not
a promise of long-term support for older revisions. macOS is the current
dogfood focus; [Windows/Linux limits](docs/known-limits.md) remain.

## Deployment guidance

- Keep the default loopback bind unless a trusted private network/proxy needs
  another address. Use HTTPS and an authenticated reverse proxy/private gateway
  for access beyond localhost; keep the app's own authentication in place.
- Set `SECURE_COOKIES=true` for an HTTPS proxy entrypoint. Restrict
  `ALLOWED_ORIGINS` to the actual cross-origin clients you need. Caller-provided
  forwarding headers alone do not establish a trusted HTTPS scheme.
- Authentication bypass switches such as `AUTH_ENABLED` and `LOCALHOST_BYPASS`
  are not supported configuration. A model-provider login or mailbox OAuth
  session does not replace the Open Clank account login.
- Keep SearXNG, ntfy, databases, Ollama, Apfel, vLLM, llama.cpp and raw model/API
  ports internal. Default/example ports include app `7777`, SearXNG `8080`, ntfy
  `8091`, Ollama `11434`, Apfel `11435` and model APIs such as `8000–8020`.
  Chroma is retired as a live authority, not a default exposed service.
- Review account signup policy, strong administrator passwords and 2FA. Remove
  disposable demo accounts from real deployments. Review each user's feature
  privileges and each integration token's agent scopes.
- Shell/Python/file access is restricted by account privileges; administrative
  surfaces such as MCP management, tokens, webhooks, serving, backup/vault and
  app settings have their own gates. Email, calendar, memory and task features
  can be granted to ordinary users; they are not universally admin-only.
- Protect `.env`, encryption keys, all configured stores, auth/session files,
  uploads, generated media, integration credentials, logs and backups. A
  `data/` snapshot can contain secrets and can omit separate stores. See
  [backup coverage](docs/backup-restore.md).
- Use separate tokens per integration, revoke unused tokens, and rotate secrets
  exposed in logs, screenshots, demos or shared chats.

## Process execution boundary

Agent shell, Python, serving and background processes run with the server
account's **OS permissions**. Open Clank enforces identity, People/Agent scopes
and typed interactive approvals; these controls do not confine an OS process.
For confinement, run the server under an appropriately restricted OS account,
container or VM. Docker-daemon socket access is an explicit high-trust opt-in.
[Setup](docs/setup.md) explains the supported launcher/proxy interfaces.

## Publishing a fork

Before publishing, inspect the exact changes and ignore rules:

```bash
git status --short
git check-ignore -v .env history-credentials.json .clanker/hexes/contract.yaml data/auth.json data/app.db logs/compound.log
python scripts/check_release_artifacts.py --paths-only
# Supply the installed, verified Gitleaks executable for value-silent content checks:
python scripts/check_release_artifacts.py --gitleaks /path/to/gitleaks
```

This is a useful review, not proof that every secret format is detected. Never
commit live `.env`, personal databases/documents, password hashes, keys, tokens,
logs, uploads or backups. Public demonstration media must be intentionally
reviewed and free of personal data. Every singular `.clanker/` directory stays private and Git-ignored, including
Hex contracts, plans, tools and notes. Image/release exports also exclude it.
History credentials, authority/root sidecars and temporary credential files
are runtime state. Ignore rules do not remove already indexed or historical
content: review the exact outgoing ancestry and actual export separately.
Use the artifact gate’s `--export-root /path/to/assembled-package` for package
contents, with Gitleaks for content checks. Preserve local recovery refs and
never publish them through `push --all` or `push --mirror`.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/Plaer1/open-clank/security/advisories/new)
when available. If that route is unavailable, open a minimal issue requesting
a private reporting channel without exploit details or sensitive material.
Include affected version/revision and deployment context in the private report.
[Security CI](docs/security-ci.md) describes repository scanners; scanners do
not replace deployment controls or prove a release is secure.
