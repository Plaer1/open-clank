# Email accounts: Google OAuth and Microsoft limits

Open Clank supports configured IMAP/SMTP accounts using mailbox/app passwords
where the provider permits them, and a **Google OAuth** flow. App login,
model-provider authentication and mailbox authorization are separate.

## Google OAuth

For the supported Google Workspace / .edu account flow:

1. The installation administrator configures a Google OAuth web client with
   the required mail/email scopes and an authorized callback URI. The exact
   configuration names are in [`.env.example`](../.env.example):
   `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
   `GOOGLE_OAUTH_REDIRECT_URI`.
2. Set the redirect URI explicitly for HTTPS/reverse-proxy/hosted deployments,
   exactly matching the Google client. The callback path is
   `/api/email/oauth/google/callback`; local HTTP can use the inferred host/port.
   Restart through the normal startup path after environment configuration.
3. In **Settings → Integrations**, add the supported Google email account and
   choose **Connect with Google**. The form saves the account before redirecting
   to Google; sign in and authorize access, then return to Open Clank.

Mail uses refreshed Google tokens with IMAP/SMTP XOAUTH2, not the model-provider
Google connection. Google policy, consent/client configuration and the mailbox's
permissions can still prevent access. Keep client secrets and stored refresh
tokens private. Password-capable Gmail accounts can use the appropriate mail
account option when their account permits app passwords.

## Outlook / Office 365

**Microsoft OAuth and Graph Mail are not currently implemented here.** Accounts
that require OAuth cannot be added through the IMAP/SMTP password form. Basic
authentication failures may include:

- `IMAP: AUTHENTICATE failed`
- `SMTP: 535 5.7.139 Authentication unsuccessful, basic authentication is disabled`

Changing the model-provider connection or Open Clank login will not fix that
mailbox limitation. Use a mailbox/provider compatible with an implemented flow;
do not assume a Microsoft app password bypasses its tenant's policy. Existing
mail/compose and document compatibility pathways remain available despite the
inherited document editor no longer being an improvement target.

[Setup](setup.md) · [Known limits](known-limits.md) · [Security](../SECURITY.md)
