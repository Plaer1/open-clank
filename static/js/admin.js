import { uiIcon } from './uiIcons.js';
// static/js/admin.js — Server administration and account-owned model endpoints

import uiModule from './ui.js';
import settingsModule from './settings.js';
import { sortModelObjects } from './modelSort.js';

let initialized = false;
let modalEl = null;
// When the user adds an endpoint, store its id so the next render of
// the endpoints list can flash a glow on that row. Cleared once the
// animation fires.
let _recentlyAddedEpId = null;
let _authPolicy = { password_min_length: 8, reserved_usernames: [] };

function el(id) { return document.getElementById(id); }
function esc(s) { return uiModule.esc(s); }
async function checkedFetch(input, init) {
  const response = await window.fetch(input, init);
  if (response.ok) return response;
  let detail = {};
  try { detail = await response.clone().json(); } catch (_) {}
  const message = detail.detail?.message || detail.detail || detail.error || `Request failed (${response.status})`;
  throw new Error(String(message));
}

/* ═══════════════════════════════════════════
   USERS TAB
   ═══════════════════════════════════════════ */
const PRIV_LABELS = {
  can_use_agent: 'Agent mode',
  can_use_browser: 'Browser automation',
  can_use_bash: 'Shell / Python / Files',
  can_use_documents: 'Document editor',
  can_use_research: 'Deep research',
  can_generate_images: 'Image generation',
  can_manage_memory: 'Memory & skills',
};

async function loadUsers() {
  const list = el('adm-userList');
  try {
    const res = await checkedFetch('/api/auth/users', { credentials: 'same-origin' });
    if (res.status === 401 || res.status === 403) { list.innerHTML = '<div class="admin-empty">Access denied</div>'; return; }
    const data = await res.json();
    if (!data.users || data.users.length === 0) { list.innerHTML = '<div class="admin-empty">No users found</div>'; return; }
    list.innerHTML = '';
    data.users.forEach(u => {
      const row = document.createElement('div');
      row.className = 'admin-user-row';

      // Header: name + badges + delete
      const header = document.createElement('div');
      header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;cursor:pointer;padding:4px 0;';
      const initial = u.username.charAt(0).toUpperCase();
      header.innerHTML = `
        <div class="admin-user-info">
          <div style="width:28px;height:28px;border-radius:50%;background:color-mix(in srgb, var(--accent) 20%, var(--panel));display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;flex-shrink:0;color:var(--accent);">${esc(initial)}</div>
          <div>
            <span class="admin-user-name">${esc(u.username)}</span>
            ${u.is_admin ? '<span class="admin-badge" style="margin-left:6px;">ADMIN</span>' : '<span style="font-size:10px;opacity:0.4;display:block;">Click to manage privileges</span>'}
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;">
          <button class="admin-btn-sm" data-adm-toggle-admin="${esc(u.username)}" data-make-admin="${u.is_admin ? '0' : '1'}" style="font-size:11px;">${u.is_admin ? 'Revoke admin' : 'Make admin'}</button>
          <button class="admin-btn-sm" data-adm-rename-user="${esc(u.username)}" style="font-size:11px;">Rename</button>
          ${u.is_admin ? '' : `<button class="admin-btn-delete" data-adm-del-user="${esc(u.username)}" style="font-size:11px;">Remove</button>`}
          ${u.is_admin ? '' : uiIcon("chevron-down", 12, {"className":"admin-user-chevron","style":"opacity:0.3;transition:transform 0.2s,opacity 0.2s;"})}
        </div>
      `;
      row.appendChild(header);

      // Privileges panel (hidden by default, not for admins)
      if (!u.is_admin) {
        const privPanel = document.createElement('div');
        privPanel.className = 'admin-priv-panel hidden';
        privPanel.style.cssText = 'padding:8px 0 4px;border-top:1px solid var(--border);margin-top:8px;';

        // Boolean toggles
        let html = '<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.35;font-weight:600;margin-bottom:4px;">Features</div>';
        for (const [key, label] of Object.entries(PRIV_LABELS)) {
          const checked = u.privileges && u.privileges[key] ? 'checked' : '';
          html += `<div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;">
            <span style="font-size:12px;">${label}</span>
            <label class="admin-switch" style="transform:scale(0.85);"><input type="checkbox" data-priv="${key}" data-user="${esc(u.username)}" ${checked}><span class="admin-slider"></span></label>
          </div>`;
        }
        // Rate limit
        html += '<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.35;font-weight:600;margin:10px 0 4px;">Limits</div>';
        const maxMsg = (u.privileges && u.privileges.max_messages_per_day) || 0;
        html += `<div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;">
          <div>
            <span style="font-size:12px;">Daily message limit</span>
            <div style="font-size:10px;opacity:0.4;">0 = no limit</div>
          </div>
          <input type="number" min="0" value="${maxMsg}" data-priv="max_messages_per_day" data-user="${esc(u.username)}" style="width:70px;padding:4px 6px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--fg);font-size:12px;text-align:center;">
        </div>`;
        // Allowed models — checkbox list
        const allowedModels = Array.isArray(u.privileges && u.privileges.allowed_models)
          ? u.privileges.allowed_models
          : [];
        const allowedSet = new Set(allowedModels);
        const modelsRestricted = !!(u.privileges && u.privileges.allowed_models_restricted);
        const blockAllModels = !!(u.privileges && u.privileges.block_all_models);
        html += `<div style="padding:4px 0;">
          <div style="display:flex;align-items:center;justify-content:space-between;">
            <span style="font-size:12px;">Allowed models</span>
            <div style="display:flex;gap:8px;">
              <a href="#" class="priv-models-all" data-user="${esc(u.username)}" style="font-size:10px;opacity:0.5;">All</a>
              <a href="#" class="priv-models-none" data-user="${esc(u.username)}" style="font-size:10px;opacity:0.5;">None</a>
            </div>
          </div>
          <div style="font-size:10px;opacity:0.4;margin-bottom:4px;">${blockAllModels ? 'No models allowed' : (!modelsRestricted ? 'All models allowed (no restrictions)' : (allowedSet.size === 0 ? 'No models allowed' : allowedSet.size + ' model(s) allowed'))}</div>
          <div class="priv-models-list" data-user="${esc(u.username)}">
            <span style="opacity:0.4;font-size:11px;">Loading models...</span>
          </div>
        </div>`;
        privPanel.innerHTML = html;
        row.appendChild(privPanel);

        // Toggle panel visibility + rotate chevron + load models
        let _modelsLoaded = false;
        header.addEventListener('click', (e) => {
          if (e.target.closest('.admin-btn-delete, [data-adm-rename-user], [data-adm-toggle-admin]')) return;
          privPanel.classList.toggle('hidden');
          const chevron = header.querySelector('.admin-user-chevron');
          if (chevron) {
            const isOpen = !privPanel.classList.contains('hidden');
            chevron.style.transform = isOpen ? 'rotate(180deg)' : '';
            chevron.style.opacity = isOpen ? '0.7' : '0.3';
          }
          // Load models list on first expand
          if (!_modelsLoaded && !privPanel.classList.contains('hidden')) {
            _modelsLoaded = true;
            _loadModelsForUser(u.username, allowedSet, modelsRestricted, blockAllModels, privPanel);
          }
        });

        // Wire privilege changes (boolean + number inputs, not model checkboxes)
        privPanel.querySelectorAll('[data-priv]').forEach(input => {
          const handler = async () => {
            const username = input.dataset.user;
            const key = input.dataset.priv;
            let value;
            if (input.type === 'checkbox') value = input.checked;
            else if (input.type === 'number') value = parseInt(input.value) || 0;
            else value = input.value;
            try {
              await checkedFetch(`/api/auth/users/${encodeURIComponent(username)}/privileges`, {
                method: 'PUT', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ [key]: value }),
              });
            } catch (e) { uiModule.showError('Failed to update privilege'); }
          };
          if (input.type === 'checkbox') input.addEventListener('change', handler);
          else input.addEventListener('change', handler);
        });
      }

      // Rename button
      const renameBtn = row.querySelector('[data-adm-rename-user]');
      if (renameBtn) {
        renameBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const oldUsername = renameBtn.dataset.admRenameUser;
          const next = await uiModule.styledPrompt(`Rename "${oldUsername}"`, {
            defaultValue: oldUsername,
            placeholder: 'New username',
            confirmText: 'Rename',
          });
          const username = (next || '').trim();
          if (!username || username === oldUsername) return;
          try {
            const res = await checkedFetch(`/api/auth/users/${encodeURIComponent(oldUsername)}/rename`, {
              method: 'PUT',
              credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ username }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
              uiModule.showError(data.detail || 'Failed to rename user');
              return;
            }
            if (data.renamed_self) {
              window.location.reload();
              return;
            }
            loadUsers();
          } catch (err) {
            uiModule.showError('Failed to rename user');
          }
        });
      }

      // Delete button
      const delBtn = row.querySelector('[data-adm-del-user]');
      if (delBtn) {
        delBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const username = delBtn.dataset.admDelUser;
          if (!await uiModule.styledConfirm(`Remove user "${username}"?`, { confirmText: 'Remove', danger: true })) return;
          const res = await checkedFetch('/api/auth/users', { method: 'DELETE', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username }) });
          if (res.ok) loadUsers();
          else uiModule.showError('Failed to delete user');
        });
      }

      // Promote / demote (admin toggle) — present on every row
      const adminToggleBtn = row.querySelector('[data-adm-toggle-admin]');
      if (adminToggleBtn) {
        adminToggleBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const username = adminToggleBtn.dataset.admToggleAdmin;
          const makeAdmin = adminToggleBtn.dataset.makeAdmin === '1';
          const confirmMsg = makeAdmin
            ? `Grant admin rights to "${username}"? They'll get full access to all settings and users — including the power to demote or remove other admins (you included).`
            : `Revoke admin rights from "${username}"? They'll lose access to the admin panel.`;
          if (!await uiModule.styledConfirm(confirmMsg, { confirmText: makeAdmin ? 'Make admin' : 'Revoke admin', danger: !makeAdmin })) return;
          adminToggleBtn.disabled = true;
          try {
            const res = await checkedFetch(`/api/auth/users/${encodeURIComponent(username)}/admin`, {
              method: 'PUT',
              credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ is_admin: makeAdmin }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
              uiModule.showError(data.detail || 'Failed to change admin status');
              adminToggleBtn.disabled = false;
              return;
            }
            // Demoting yourself drops your own admin access — reload into the
            // normal-user view (mirrors the rename-self reload above).
            if (data.self) { window.location.reload(); return; }
            loadUsers();
          } catch (err) {
            uiModule.showError('Failed to change admin status');
            adminToggleBtn.disabled = false;
          }
        });
      }

      list.appendChild(row);
    });
  } catch (e) { list.innerHTML = '<div class="admin-error">Failed to load users</div>'; }
}

async function _loadModelsForUser(username, allowedSet, modelsRestricted, blockAllModels, privPanel) {
  const listEl = privPanel.querySelector(`.priv-models-list[data-user="${username}"]`);
  if (!listEl) return;
  try {
    // The normalized compatibility catalogue exposes only managed, live,
    // credential-free selectors. Stable route IDs let an allowlist distinguish
    // two connections that happen to expose the same provider model name.
    const res = await checkedFetch('/api/models', { credentials: 'same-origin' });
    const data = await res.json();
    const allModels = [];
    (Array.isArray(data.items) ? data.items : []).forEach(ep => {
      (ep.catalog || []).forEach(entry => {
        const mid = entry.provider_model_route_id || entry.model_id;
        if (!mid) return;
        allModels.push({
          mid,
          epName: ep.endpoint_name || '',
          display: entry.display_name || entry.model_id || mid,
        });
      });
    });
    if (!allModels.length) {
      listEl.innerHTML = '<span style="opacity:0.4;font-size:11px;">No models available</span>';
      return;
    }
    let restricted = modelsRestricted;
    let blockAll = blockAllModels;
    listEl.innerHTML = sortModelObjects(allModels).map(m => {
      const checked = !blockAll && (!restricted || allowedSet.has(m.mid)) ? 'checked' : '';
      return `<label>
        <input type="checkbox" class="priv-model-cb" data-mid="${esc(m.mid)}" ${checked}>
        <span>${esc(m.display)}</span>
        <span style="opacity:0.3;font-size:10px;margin-left:auto;">${esc(m.epName)}</span>
      </label>`;
    }).join('');

    // Save on change
    function _saveModels() {
      const checked = [];
      listEl.querySelectorAll('.priv-model-cb').forEach(cb => {
        if (cb.checked) checked.push(cb.dataset.mid);
      });
      // Three distinct states the backend must be able to tell apart:
      //  - all checked   -> no restriction (allowed_models: [], block_all_models: false)
      //  - none checked  -> block everything (allowed_models: [], block_all_models: true)
      //  - some checked  -> allowlist (allowed_models: checked, block_all_models: false)
      let value, hintText;
      if (checked.length === allModels.length) {
        restricted = false;
        blockAll = false;
        value = [];
        hintText = 'All models allowed (no restrictions)';
      } else if (checked.length === 0) {
        restricted = true;
        blockAll = true;
        value = [];
        hintText = 'No models allowed';
      } else {
        restricted = true;
        blockAll = false;
        value = checked;
        hintText = value.length + ' model(s) allowed';
      }
      const hint = privPanel.querySelector('.priv-models-list[data-user]')?.previousElementSibling?.querySelector('div[style*="opacity"]');
      if (hint) hint.textContent = hintText;
      fetch(`/api/auth/users/${encodeURIComponent(username)}/privileges`, {
        method: 'PUT', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allowed_models: value, allowed_models_restricted: restricted, block_all_models: blockAll }),
      }).catch(() => {});
    }
    listEl.querySelectorAll('.priv-model-cb').forEach(cb => cb.addEventListener('change', _saveModels));

    // All / None buttons
    privPanel.querySelector(`.priv-models-all[data-user="${username}"]`)?.addEventListener('click', (e) => {
      e.preventDefault();
      listEl.querySelectorAll('.priv-model-cb').forEach(cb => cb.checked = true);
      _saveModels();
    });
    privPanel.querySelector(`.priv-models-none[data-user="${username}"]`)?.addEventListener('click', (e) => {
      e.preventDefault();
      listEl.querySelectorAll('.priv-model-cb').forEach(cb => cb.checked = false);
      _saveModels();
    });
  } catch (e) {
    listEl.innerHTML = '<span style="opacity:0.4;font-size:11px;">Failed to load models</span>';
  }
}

function initSignupToggle() {
  const toggle = el('adm-signupToggle');
  fetch('/api/auth/status', { credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => { toggle.checked = !!d.signup_enabled; })
    .catch(e => console.warn('Auth status fetch failed:', e));
  toggle.addEventListener('change', async () => {
    try {
      const res = await checkedFetch('/api/auth/signup-toggle', { method: 'POST', credentials: 'same-origin' });
      const data = await res.json();
      toggle.checked = data.signup_enabled;
    } catch (e) { toggle.checked = !toggle.checked; }
  });
}

function initShareDefaultsToggle() {
  const toggle = el('adm-shareDefaultsToggle');
  fetch('/api/auth/settings', { credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => { toggle.checked = !!d.share_defaults_with_users; })
    .catch(e => console.warn('Settings fetch failed:', e));
  toggle.addEventListener('change', async () => {
    try {
      const res = await checkedFetch('/api/auth/settings', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ share_defaults_with_users: toggle.checked }),
      });
      const data = await res.json();
      toggle.checked = !!data.share_defaults_with_users;
    } catch (e) {
      toggle.checked = !toggle.checked;
    }
  });
}

function initAddUser() {
  fetch('/api/auth/policy', { credentials: 'same-origin' })
    .then(r => r.ok ? r.json() : null)
    .then(policy => {
      if (!policy) return;
      _authPolicy = policy;
      const admPw = el('adm-newPassword');
      if (admPw) admPw.placeholder = `Password (min ${policy.password_min_length})`;
    })
    .catch(() => {});
  el('adm-addBtn').addEventListener('click', async () => {
    const msg = el('adm-addMsg');
    msg.textContent = ''; msg.className = '';
    const username = el('adm-newUsername').value.trim();
    const password = el('adm-newPassword').value;
    const is_admin = el('adm-newIsAdmin').checked;
    if (!username) { msg.textContent = 'Username required'; msg.className = 'admin-error'; return; }
    if (password.length < _authPolicy.password_min_length) { msg.textContent = `Password must be at least ${_authPolicy.password_min_length} characters`; msg.className = 'admin-error'; return; }
    if (_authPolicy.reserved_usernames.includes(username.toLowerCase())) { msg.textContent = 'This username is reserved'; msg.className = 'admin-error'; return; }
    el('adm-addBtn').disabled = true;
    try {
      const res = await checkedFetch('/api/auth/users', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password, is_admin }) });
      const data = await res.json();
      if (res.ok) { msg.textContent = 'User created'; msg.className = 'admin-success'; el('adm-newUsername').value = ''; el('adm-newPassword').value = ''; el('adm-newIsAdmin').checked = false; loadUsers(); }
      else { msg.textContent = data.detail || 'Failed'; msg.className = 'admin-error'; }
    } catch (e) { msg.textContent = 'Request failed'; msg.className = 'admin-error'; }
    el('adm-addBtn').disabled = false;
  });
}

/* ═══════════════════════════════════════════
   TOOLS TAB — MCP
   ═══════════════════════════════════════════ */

const _GOOGLE_OAUTH_HELP = `To get Google OAuth credentials:
1. Go to console.cloud.google.com
2. Click the project dropdown (top left) > New Project > name it > Create
3. APIs & Services > Library > enable the API you need (Gmail, Calendar, Drive, etc.)
4. APIs & Services > OAuth consent screen > configure (External, app name + email)
5. Under Audience, click Add Users > add your Google email as a test user
6. APIs & Services > Credentials > + Create Credentials > OAuth Client ID > Desktop App
7. Copy the Client ID and Client Secret into the fields above
8. After adding the server, click Authorize to sign in with Google
9. If accessing remotely: sign in, then copy the URL from the error page and paste it back`;

const MCP_PRESETS = [
  { name: "Gmail",           command: "npx", args: ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],      env: { GOOGLE_CLIENT_ID: "", GOOGLE_CLIENT_SECRET: "" },
    oauthFile: { dir: "gmail", filename: "gcp-oauth.keys.json" },
    oauth: {
      provider: "google",
      keys_file: "gmail/gcp-oauth.keys.json",
      token_file: "gmail/credentials.json",
      scopes: ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.settings.basic"],
    },
    help: `Setup:
1. Go to console.cloud.google.com > create or select a project
2. APIs & Services > Library > search "Gmail API" > Enable
3. APIs & Services > OAuth consent screen > set up (External is fine)
4. Under Audience, add your Gmail address as a test user
5. APIs & Services > Credentials > + Create Credentials > OAuth Client ID
6. Application type: Desktop App > Create
7. Copy the Client ID and Client Secret into the fields above
8. Click Add Server, then click the Authorize button
9. Sign in with Google, copy the URL from the error page, paste it back` },
  { name: "Email (IMAP/SMTP)", command: "npx", args: ["-y", "@codefuturist/email-mcp", "stdio"],        env: { MCP_EMAIL_ADDRESS: "", MCP_EMAIL_PASSWORD: "", MCP_EMAIL_IMAP_HOST: "", MCP_EMAIL_SMTP_HOST: "" },
    providerDropdown: {
      label: "Provider",
      targets: { MCP_EMAIL_IMAP_HOST: "imap", MCP_EMAIL_SMTP_HOST: "smtp" },
      options: [
        { name: "Migadu",        imap: "imap.migadu.com",     smtp: "smtp.migadu.com" },
        { name: "Fastmail",      imap: "imap.fastmail.com",   smtp: "smtp.fastmail.com" },
        { name: "Proton Bridge", imap: "127.0.0.1",           smtp: "127.0.0.1" },
        { name: "Outlook/Hotmail", imap: "outlook.office365.com", smtp: "smtp.office365.com" },
        { name: "Yahoo",         imap: "imap.mail.yahoo.com", smtp: "smtp.mail.yahoo.com" },
        { name: "iCloud",        imap: "imap.mail.me.com",    smtp: "smtp.mail.me.com" },
        { name: "Zoho",          imap: "imap.zoho.com",       smtp: "smtp.zoho.com" },
        { name: "Custom",        imap: "",                    smtp: "" },
      ],
    },
    help: "Works with any IMAP/SMTP email provider.\n1. Pick your provider from the dropdown (or choose Custom)\n2. Enter your email address and password (or app password)\n3. Click Add Server" },
  { name: "CalDAV (Radicale/Nextcloud)", command: "npx", args: ["-y", "caldav-mcp"],                     env: { CALDAV_BASE_URL: "http://localhost:5232", CALDAV_USERNAME: "", CALDAV_PASSWORD: "" },
    help: "Works with any CalDAV server (Radicale, Nextcloud, etc.).\n1. Enter your CalDAV server URL (e.g. http://localhost:5232)\n2. Enter your username and password\n3. Click Add Server" },
  { name: "Google Calendar", command: "npx", args: ["-y", "@cocal/google-calendar-mcp"],                 env: { GOOGLE_OAUTH_CREDENTIALS: "" },
    help: `Setup:
1. Go to console.cloud.google.com > create/select a project
2. APIs & Services > Library > enable Google Calendar API
3. APIs & Services > Credentials > + Create Credentials > OAuth Client ID
4. Application type: Desktop App > Create
5. Click "Download JSON" on the credential you just created
6. Set Google Oauth Credentials to the full path of the downloaded JSON file` },
  { name: "Google Drive",    command: "npx", args: ["-y", "@modelcontextprotocol/server-gdrive"],        env: {},
    help: "Google Drive uses browser-based OAuth on first run. No env vars needed — just click Add and authorize when prompted." },
  { name: "GitHub",          command: "npx", args: ["-y", "@modelcontextprotocol/server-github"],        env: { GITHUB_PERSONAL_ACCESS_TOKEN: "" },
    help: "1. Go to github.com > Settings > Developer Settings > Personal Access Tokens > Fine-grained tokens\n2. Generate a new token with the repo permissions you need\n3. Paste it as Github Personal Access Token" },
  { name: "Slack",           command: "npx", args: ["-y", "@modelcontextprotocol/server-slack"],         env: { SLACK_BOT_TOKEN: "", SLACK_TEAM_ID: "" },
    help: "1. Go to api.slack.com/apps > Create New App > From Scratch\n2. Add Bot Token Scopes (channels:read, chat:write, etc.)\n3. Install to workspace, copy the Bot User OAuth Token (xoxb-...)\n4. Team ID is in your workspace URL or Slack admin settings" },
  { name: "Notion",          command: "npx", args: ["-y", "@notionhq/notion-mcp-server"],               env: { OPENAPI_MCP_HEADERS: "" },
    help: "1. Go to notion.so/my-integrations\n2. Create a new integration\n3. Copy the Internal Integration Secret\n4. Share the Notion pages/databases you want accessible with the integration\n5. For Openapi Mcp Headers enter:\n   {\"Authorization\": \"Bearer YOUR_SECRET\", \"Notion-Version\": \"2022-06-28\"}" },
  { name: "Linear",          command: "npx", args: ["-y", "mcp-linear"],                                env: { LINEAR_API_KEY: "" },
    help: "1. Go to linear.app > Settings > API\n2. Create a Personal API Key\n3. Paste it as Linear Api Key" },
  { name: "Brave Search",    command: "npx", args: ["-y", "@modelcontextprotocol/server-brave-search"], env: { BRAVE_API_KEY: "" },
    help: "1. Go to brave.com/search/api\n2. Sign up for a free plan (2000 queries/month)\n3. Copy your API key" },
  { name: "Browser (Playwright)", command: "npx", args: ["-y", "@playwright/mcp@latest", "--headless"],  env: {},
    help: "Browser automation via Playwright. The AI can navigate pages, click, fill forms, and read content.\nRuns headless by default. Remove --headless from Args to see the browser window.\nFirst run installs Chromium automatically." },
  { name: "Filesystem",      command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "/home"], env: {},
    help: "Edit the Args field to change which directory the server has access to." },
  { name: "Memory",          command: "npx", args: ["-y", "@modelcontextprotocol/server-memory"],        env: {} },
  { name: "Postgres",        command: "npx", args: ["-y", "@modelcontextprotocol/server-postgres", "postgresql://user:pass@localhost/db"], env: {},
    help: "Replace the connection string in the Args field with your actual Postgres connection URL." },
  { name: "Todoist",         command: "npx", args: ["-y", "todoist-mcp-server"],                         env: { TODOIST_API_TOKEN: "" },
    help: "1. Go to todoist.com > Settings > Integrations > Developer\n2. Copy your API token" },
];
// ── Built-in tools management ──
const TOOL_META = {
  bash:              { name: 'Shell',            desc: 'Execute bash commands',           cat: 'Code',       ctx: '~200' },
  python:            { name: 'Python',           desc: 'Run Python scripts',              cat: 'Code',       ctx: '~200' },
  read_file:         { name: 'Read File',        desc: 'Read files from disk',            cat: 'Code',       ctx: '~150' },
  write_file:        { name: 'Write File',       desc: 'Write/create files',              cat: 'Code',       ctx: '~150' },
  edit_file:         { name: 'Edit File',        desc: 'Targeted find-and-replace in files', cat: 'Code',       ctx: '~150' },
  apply_patch:       { name: 'Apply Patch',      desc: 'Apply a structured file patch',    cat: 'Code',       ctx: '~200' },
  todowrite:         { name: 'Todo Write',       desc: 'Track a task checklist',           cat: 'Code',       ctx: '~100' },
  ls:                { name: 'List Directory',   desc: 'List workspace files',             cat: 'Code',       ctx: '~100' },
  glob:              { name: 'Find Files',       desc: 'Find files by glob pattern',       cat: 'Code',       ctx: '~120' },
  grep:              { name: 'Search Files',     desc: 'Search workspace file contents',   cat: 'Code',       ctx: '~180' },
  get_workspace:     { name: 'Workspace',        desc: 'Inspect the active workspace',     cat: 'Code',       ctx: '~120' },
  publish_file:      { name: 'Publish File',     desc: 'Expose a file for download',       cat: 'Code',       ctx: '~120' },
  manage_files:      { name: 'Manage Files',     desc: 'Copy, move, and organize files',   cat: 'Code',       ctx: '~160' },
  edit_image:        { name: 'Edit Image',      desc: 'Transform an existing image',      cat: 'Media',      ctx: '~180' },
  web_search:        { name: 'Web Search',       desc: 'Search the web via SearXNG',      cat: 'Search',     ctx: '~300' },
  web_fetch:         { name: 'Fetch Web Page',   desc: 'Read a specific web page',         cat: 'Search',     ctx: '~250' },
  search_chats:      { name: 'Search Chats',     desc: 'Search conversation history',     cat: 'Search',     ctx: '~150' },
  create_document:   { name: 'Create Document',  desc: 'Create new documents',            cat: 'Documents',  ctx: '~200' },
  update_document:   { name: 'Update Document',  desc: 'Modify existing documents',       cat: 'Documents',  ctx: '~200' },
  edit_document:     { name: 'Edit Document',    desc: 'Find & replace in documents',     cat: 'Documents',  ctx: '~200' },
  suggest_document:  { name: 'Suggest Changes',  desc: 'Propose document edits',          cat: 'Documents',  ctx: '~200' },
  manage_documents:  { name: 'Manage Documents', desc: 'List, delete, organize docs',     cat: 'Documents',  ctx: '~150' },
  generate_image:    { name: 'Generate Image',   desc: 'Create images via AI',            cat: 'Media',      ctx: '~150' },
  manage_memory:     { name: 'Memory',           desc: 'Save and recall memories',        cat: 'Knowledge',  ctx: '~200' },
  manage_skills:     { name: 'Skills',           desc: 'Learn and use procedures',        cat: 'Knowledge',  ctx: '~200' },
  manage_rag:        { name: 'RAG / Docs',       desc: 'Query indexed documents',         cat: 'Knowledge',  ctx: '~150' },
  chat_with_model:   { name: 'Chat with Model',  desc: 'Talk to another AI model',        cat: 'Multi-Agent', ctx: '~200' },
  pipeline:          { name: 'Pipeline',         desc: 'Multi-step AI workflows',         cat: 'Multi-Agent', ctx: '~200' },
  ask_teacher:       { name: 'Ask Teacher',      desc: 'Query a more capable model',      cat: 'Multi-Agent', ctx: '~150' },
  manage_bg_jobs:    { name: 'Background Jobs',  desc: 'Inspect and control background jobs', cat: 'Multi-Agent', ctx: '~120' },
  ask_user:          { name: 'Ask User',         desc: 'Request a clarification in the UI', cat: 'Multi-Agent', ctx: '~120' },
  update_plan:       { name: 'Update Plan',      desc: 'Maintain an agent work plan',     cat: 'Multi-Agent', ctx: '~120' },
  send_to_session:   { name: 'Send to Session',  desc: 'Send message to another chat',    cat: 'Sessions',   ctx: '~100' },
  create_session:    { name: 'Create Session',   desc: 'Start a new chat session',        cat: 'Sessions',   ctx: '~100' },
  list_sessions:     { name: 'List Sessions',    desc: 'Browse existing sessions',        cat: 'Sessions',   ctx: '~100' },
  manage_session:    { name: 'Manage Session',   desc: 'Rename, archive, configure',      cat: 'Sessions',   ctx: '~100' },
  list_models:       { name: 'List Models',      desc: 'Show available models',           cat: 'System',     ctx: '~100' },
  ui_control:        { name: 'UI Control',       desc: 'Change theme, layout, settings',  cat: 'System',     ctx: '~150' },
  manage_tasks:      { name: 'Clanker Tasks',    desc: 'Schedule automated tasks',        cat: 'System',     ctx: '~150' },
  api_call:          { name: 'API Call',         desc: 'Make HTTP requests',              cat: 'System',     ctx: '~200' },
  manage_endpoints:  { name: 'Endpoints',        desc: 'Add/remove model endpoints',      cat: 'System',     ctx: '~100' },
  manage_mcp:        { name: 'MCP Servers',      desc: 'Manage MCP connections',          cat: 'System',     ctx: '~100' },
  manage_notes:      { name: 'Notes',            desc: 'Create and manage notes',         cat: 'System',     ctx: '~160' },
  manage_calendar:   { name: 'Calendar',         desc: 'Read and update calendar events',  cat: 'System',     ctx: '~160' },
  manage_copal:      { name: 'Copal',            desc: 'Open and manage Copal views',    cat: 'System',     ctx: '~140' },
  read_copal:        { name: 'Read Copal',       desc: 'Read Copal view data',             cat: 'System',     ctx: '~120' },
  resolve_contact:   { name: 'Resolve Contact',  desc: 'Find a contact by name or address', cat: 'System',    ctx: '~120' },
  manage_contact:    { name: 'Manage Contact',   desc: 'Create and update contacts',       cat: 'System',    ctx: '~140' },
  trigger_research:  { name: 'Start Research',   desc: 'Run a deep research job',          cat: 'System',    ctx: '~140' },
  manage_research:   { name: 'Research Jobs',    desc: 'Inspect and control research jobs', cat: 'System',   ctx: '~140' },
  download_model:    { name: 'Download Model',   desc: 'Download a model through Cookbook', cat: 'System',   ctx: '~180' },
  serve_model:       { name: 'Serve Model',      desc: 'Start a model server',             cat: 'System',    ctx: '~180' },
  list_served_models:{ name: 'Served Models',    desc: 'List running model servers',       cat: 'System',    ctx: '~120' },
  stop_served_model: { name: 'Stop Served Model', desc: 'Stop a running model server',     cat: 'System',    ctx: '~120' },
  list_downloads:    { name: 'Downloads',        desc: 'List model downloads',              cat: 'System',    ctx: '~120' },
  cancel_download:   { name: 'Cancel Download',  desc: 'Cancel a model download',            cat: 'System',    ctx: '~120' },
  search_hf_models:  { name: 'Search Hugging Face', desc: 'Search available model files',   cat: 'System',    ctx: '~160' },
  list_cached_models:{ name: 'Cached Models',    desc: 'List locally cached models',        cat: 'System',    ctx: '~120' },
  list_serve_presets:{ name: 'Serve Presets',    desc: 'List model serving presets',        cat: 'System',    ctx: '~120' },
  serve_preset:      { name: 'Serve Preset',     desc: 'Start a configured serving preset', cat: 'System',    ctx: '~160' },
  adopt_served_model:{ name: 'Adopt Served Model', desc: 'Register an existing model server', cat: 'System',   ctx: '~140' },
  list_cookbook_servers:{ name: 'Cookbook Servers', desc: 'List configured serving hosts',   cat: 'System',    ctx: '~120' },
  app_api:           { name: 'Open Clank API',   desc: 'Call an allowed internal UI endpoint', cat: 'System', ctx: '~200' },
  manage_webhooks:   { name: 'Webhooks',         desc: 'Configure webhook events',        cat: 'System',     ctx: '~100' },
  manage_tokens:     { name: 'API Tokens',       desc: 'Manage API access tokens',        cat: 'System',     ctx: '~100' },
  manage_settings:   { name: 'Settings',         desc: 'Change app settings',             cat: 'System',     ctx: '~100' },
};

async function loadBuiltinTools() {
  const list = el('adm-builtin-tools-list');
  if (!list) return;
  try {
    const res = await checkedFetch('/api/tools', { credentials: 'same-origin' });
    const data = await res.json();
    const tools = data.tools || [];
    if (!tools.length) { list.innerHTML = '<div class="admin-empty">No tools found</div>'; return; }
    const catalog = data.catalog || {};
    const freshness = catalog.freshness || 'unknown';
    const freshnessReason = catalog.reason || (freshness === 'engine-stopped'
      ? 'Managed engine is stopped; native tool availability is unknown.'
      : freshness === 'fresh' ? 'Native registry metadata is current.' : 'Native registry metadata is incomplete.');

    // Group by category
    const groups = {};
    for (const t of tools) {
      const meta = TOOL_META[t.id] || { name: t.id, desc: '', cat: 'Other', ctx: '?' };
      const cat = meta.cat;
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push({ ...t, ...meta });
    }

    // Category order
    const catOrder = ['Code', 'Search', 'Documents', 'Media', 'Knowledge', 'Multi-Agent', 'Sessions', 'System', 'Other'];
    let html = '';
    for (const cat of catOrder) {
      const items = groups[cat];
      if (!items) continue;
      const availableItems = items.filter(i => i.availability !== 'unavailable');
      const enabledCount = availableItems.filter(i => i.requested_enabled !== false && i.effective_enabled !== false).length;
      const totalCount = availableItems.length;
      const unavailableCount = items.length - availableItems.length;
      const catId = 'tool-cat-' + cat.replace(/[^a-zA-Z]/g, '');
      const allEnabled = totalCount > 0 && enabledCount === totalCount;
      html += `<div class="admin-tool-category">
        <div class="admin-tool-cat-header" data-tool-cat="${catId}" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;">
          <span>${esc(cat)}</span>
          <span style="display:flex;align-items:center;gap:6px;" class="admin-tool-cat-right">
            <span class="admin-tool-cat-count" style="font-size:10px;opacity:0.5;">${enabledCount}/${totalCount}${unavailableCount ? ` · ${unavailableCount} unavailable` : ''}</span>
            <label class="admin-switch" style="flex-shrink:0;">
              <input type="checkbox" data-tool-cat-toggle="${catId}" ${allEnabled ? 'checked' : ''} ${totalCount ? '' : 'disabled'}>
              <span class="admin-slider"></span>
            </label>
            ${uiIcon("chevron-down", 12, {"className":"admin-tool-cat-chevron","style":"opacity:0.3;transition:transform 0.2s,opacity 0.2s;"})}
          </span>
        </div>
        <div class="admin-tool-cat-body hidden" id="${catId}">`;
      for (const t of items) {
        html += `
        <div class="admin-tool-row${t.availability === 'unavailable' ? ' admin-tool-unavailable' : ''}">
          <div class="admin-tool-info">
            <span class="admin-tool-name">${esc(t.name)}</span>
            <span class="admin-tool-desc">${esc(t.desc)}</span>
            <span class="admin-tool-state">Source: ${esc(t.source || 'unknown')} · Requested: ${t.requested_enabled === false ? 'off' : 'on'} · Effective: ${t.effective_enabled === false ? 'off' : 'on'} · Availability: ${esc(t.availability || 'unknown')} · ${esc(t.reason || 'No reason provided')}</span>
          </div>
          <span class="admin-tool-ctx" title="Approximate context tokens used">${esc(t.ctx)}</span>
          <label class="admin-switch" style="flex-shrink:0;">
            <input type="checkbox" data-tool-id="${esc(t.id)}" ${t.requested_enabled !== false && t.availability !== 'unavailable' ? 'checked' : ''} ${t.availability === 'unavailable' ? 'disabled' : ''}>
            <span class="admin-slider"></span>
          </label>
        </div>`;
      }
      html += '</div></div>';
    }
    list.innerHTML = `<div class="admin-tool-catalog-status" role="status">Catalog freshness: <strong>${esc(freshness)}</strong> · ${esc(freshnessReason)}</div>` + html;

    // Prevent toggle clicks from expanding/collapsing
    list.querySelectorAll('.admin-tool-cat-right').forEach(span => {
      span.addEventListener('click', e => e.stopPropagation());
    });

    // Wire category expand/collapse
    list.querySelectorAll('[data-tool-cat]').forEach(header => {
      header.addEventListener('click', () => {
        const body = el(header.dataset.toolCat);
        if (!body) return;
        body.classList.toggle('hidden');
        const chevron = header.querySelector('.admin-tool-cat-chevron');
        const isOpen = !body.classList.contains('hidden');
        if (chevron) {
          chevron.style.transform = isOpen ? 'rotate(180deg)' : '';
          chevron.style.opacity = isOpen ? '0.7' : '0.3';
        }
      });
    });

    // Helper: save disabled tools + update counters
    async function _saveToolState() {
      const allChecks = list.querySelectorAll('input[data-tool-id]');
      const disabled = [];
      allChecks.forEach(c => { if (!c.disabled && !c.checked) disabled.push(c.dataset.toolId); });
      await checkedFetch('/api/tools', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disabled }),
        credentials: 'same-origin',
      });
    }
    function _updateCatCounter(catEl) {
      if (!catEl) return;
      const catChecks = catEl.querySelectorAll('input[data-tool-id]');
      const catEnabled = Array.from(catChecks).filter(c => !c.disabled && c.checked).length;
      const catAvailable = Array.from(catChecks).filter(c => !c.disabled).length;
      const unavailable = catChecks.length - catAvailable;
      const counter = catEl.querySelector('.admin-tool-cat-count');
      if (counter) counter.textContent = catEnabled + '/' + catAvailable + (unavailable ? ' · ' + unavailable + ' unavailable' : '');
      const catToggle = catEl.querySelector('input[data-tool-cat-toggle]');
      if (catToggle) catToggle.checked = (catAvailable > 0 && catEnabled === catAvailable);
    }

    // Wire individual tool toggles
    list.querySelectorAll('input[data-tool-id]').forEach(chk => {
      chk.addEventListener('change', async () => {
        await _saveToolState();
        _updateCatCounter(chk.closest('.admin-tool-category'));
      });
    });

    // Wire category-level toggle (enable/disable all in category)
    list.querySelectorAll('input[data-tool-cat-toggle]').forEach(chk => {
      chk.addEventListener('change', async () => {
        const catEl = chk.closest('.admin-tool-category');
        if (!catEl) return;
        const checked = chk.checked;
        catEl.querySelectorAll('input[data-tool-id]').forEach(c => { if (!c.disabled) c.checked = checked; });
        await _saveToolState();
        _updateCatCounter(catEl);
      });
    });
  } catch (e) {
    console.error('Failed to load tools:', e);
    list.innerHTML = '<div class="admin-empty">Failed to load tools</div>';
  }
}

async function loadMcpServers() {
  const list = el('adm-mcpList');
  if (!list) return;  // MCP section not visible / not yet rendered
  try {
    const res = await checkedFetch('/api/mcp/servers', { credentials: 'same-origin' });
    const servers = await res.json();
    if (!servers.length) { list.innerHTML = '<div class="admin-empty">No MCP servers configured</div>'; return; }
    list.innerHTML = servers.map(s => {
      const statusColor = s.needs_oauth ? '#e5a33a' : s.status === 'connected' ? 'var(--fg)' : s.status === 'error' ? 'var(--red)' : 'color-mix(in srgb, var(--fg) 50%, transparent)';
      const toolInfo = s.status === 'connected' ? `${s.enabled_tool_count}/${s.tool_count} tools enabled` : '';
      const statusText = s.needs_oauth ? 'Needs authorization' : s.status === 'connected' ? `Connected (${toolInfo})` : s.status === 'error' ? `Error: ${s.error || 'unknown'}` : 'Disconnected';
      const hasTools = s.status === 'connected' && s.tool_count > 0;
      return `<div class="admin-user-row" data-adm-mcp-id="${s.id}">
        <div style="display:flex;align-items:center;justify-content:space-between;${hasTools ? 'cursor:pointer;' : ''}padding:4px 0;" data-adm-mcp-header="${s.id}">
          <div class="admin-user-info" style="flex:1;flex-wrap:wrap;gap:0.3rem;">
            <span class="admin-user-name">${esc(s.name)}</span>
            <span class="admin-badge" style="background:${statusColor}33;color:${statusColor}">${statusText}</span>
            ${hasTools ? `<span style="font-size:10px;opacity:0.4;">Click to manage tools</span>` : ''}
          </div>
          <div style="display:flex;gap:4px;align-items:center;">
            ${s.needs_oauth ? `<a href="/api/mcp/oauth/authorize/${s.id}" target="_blank" class="admin-btn-sm" style="background:var(--red);color:#fff;text-decoration:none;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:600;">Authorize</a>` : ''}
            <button class="admin-btn-sm" data-adm-mcp-reconnect="${s.id}">Reconnect</button>
            <button class="admin-btn-delete" style="border-color:${s.is_enabled ? 'color-mix(in srgb, var(--red) 30%, transparent)' : 'color-mix(in srgb, var(--fg) 30%, transparent)'};color:${s.is_enabled ? 'var(--red)' : 'var(--fg)'};" data-adm-mcp-toggle="${s.id}" data-adm-mcp-enable="${!s.is_enabled}">${s.is_enabled ? 'Disable' : 'Enable'}</button>
            <button class="admin-btn-delete" data-adm-mcp-delete="${s.id}">Delete</button>
            ${hasTools ? uiIcon("chevron-down", 12, {"className":"admin-user-chevron","style":"opacity:0.3;transition:transform 0.2s,opacity 0.2s;"}) : ''}
          </div>
        </div>
        ${hasTools ? `<div class="mcp-tools-panel hidden" data-adm-mcp-tools-panel="${s.id}"></div>` : ''}
      </div>`;
    }).join('');
    list.querySelectorAll('[data-adm-mcp-reconnect]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const msg = el('adm-mcpMsg'); msg.textContent = 'Reconnecting...'; msg.className = '';
        try {
          const res = await checkedFetch(`/api/mcp/servers/${btn.dataset.admMcpReconnect}/reconnect`, { method: 'POST', credentials: 'same-origin' });
          const data = await res.json();
          msg.textContent = data.connected ? `Reconnected (${data.tool_count} tools)` : `Failed: ${data.error || 'unknown'}`;
          msg.className = data.connected ? 'admin-success' : 'admin-error';
          loadMcpServers();
        } catch (e) { msg.textContent = 'Failed: ' + e.message; msg.className = 'admin-error'; }
      });
    });
    list.querySelectorAll('[data-adm-mcp-toggle]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const fd = new FormData(); fd.append('is_enabled', btn.dataset.admMcpEnable);
        await checkedFetch(`/api/mcp/servers/${btn.dataset.admMcpToggle}`, { method: 'PATCH', body: fd, credentials: 'same-origin' });
        loadMcpServers();
      });
    });
    list.querySelectorAll('[data-adm-mcp-delete]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!await uiModule.styledConfirm('Delete this MCP server?', { confirmText: 'Delete', danger: true })) return;
        await checkedFetch(`/api/mcp/servers/${btn.dataset.admMcpDelete}`, { method: 'DELETE', credentials: 'same-origin' });
        loadMcpServers();
      });
    });
    // Tools expand/collapse (click anywhere on card)
    list.querySelectorAll('[data-adm-mcp-id]').forEach(row => {
      const header = row.querySelector('[data-adm-mcp-header]');
      if (!header) return;
      let _toolsLoaded = false;
      row.style.cursor = 'pointer';
      row.addEventListener('click', async (e) => {
        if (e.target.closest('.admin-btn-sm, .admin-btn-delete, a, .mcp-tools-list, .mcp-tools-header')) return;
        const sid = header.dataset.admMcpHeader;
        const panel = row.querySelector(`[data-adm-mcp-tools-panel="${sid}"]`);
        if (!panel) return;
        panel.classList.toggle('hidden');
        const chevron = row.querySelector('.admin-user-chevron');
        const isOpen = !panel.classList.contains('hidden');
        if (chevron) {
          chevron.style.transform = isOpen ? 'rotate(180deg)' : '';
          chevron.style.opacity = isOpen ? '0.7' : '0.3';
        }
        if (!_toolsLoaded && isOpen) {
          _toolsLoaded = true;
          panel.innerHTML = '<span style="opacity:0.5;font-size:11px;">Loading tools...</span>';
          try {
            const res = await checkedFetch(`/api/mcp/servers/${sid}/tools`, { credentials: 'same-origin' });
            const tools = await res.json();
            if (!tools.length) { panel.innerHTML = '<span style="opacity:0.5;font-size:11px;">No tools</span>'; return; }
            const disabled = new Set(tools.filter(t => t.is_disabled).map(t => t.name));
            panel.innerHTML = `<div class="mcp-tools-header">
              <span>Tools</span>
              <span style="display:flex;gap:8px;align-items:center;">
                <span class="mcp-tools-count">${tools.length - disabled.size}/${tools.length} enabled</span>
                <a href="#" data-mcp-select-all="${sid}">All</a>
                <a href="#" data-mcp-select-none="${sid}">None</a>
              </span>
            </div><div class="mcp-tools-list">` + tools.map(t =>
              `<label title="${esc(t.description)}">
                <input type="checkbox" data-mcp-tool-name="${esc(t.name)}" ${!t.is_disabled ? 'checked' : ''}>
                <span><strong>${esc(t.name)}</strong> <span style="opacity:0.5;">— ${esc((t.description || '').slice(0, 80))}</span></span>
              </label>`
            ).join('') + '</div>';
            panel.querySelector(`[data-mcp-select-all="${sid}"]`)?.addEventListener('click', (e) => {
              e.preventDefault();
              panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = true);
              _saveMcpToolState(sid, panel);
            });
            panel.querySelector(`[data-mcp-select-none="${sid}"]`)?.addEventListener('click', (e) => {
              e.preventDefault();
              panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
              _saveMcpToolState(sid, panel);
            });
            panel.querySelectorAll('input[type=checkbox]').forEach(cb => {
              cb.addEventListener('change', () => _saveMcpToolState(sid, panel));
            });
          } catch (e) { panel.innerHTML = '<span class="admin-error" style="font-size:11px;">Failed to load tools</span>'; }
        }
      });
    });
  } catch (e) { if (list) list.innerHTML = '<div class="admin-error">Failed to load MCP servers</div>'; }
}

async function _saveMcpToolState(serverId, panel) {
  const disabled = [];
  panel.querySelectorAll('input[type=checkbox]').forEach(cb => {
    if (!cb.checked) disabled.push(cb.dataset.mcpToolName);
  });
  const total = panel.querySelectorAll('input[type=checkbox]').length;
  try {
    await checkedFetch(`/api/mcp/servers/${serverId}/tools`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ disabled }),
    });
    // Update the count label in the panel
    const countLabel = panel.querySelector('.mcp-tools-count');
    if (countLabel) countLabel.textContent = `${total - disabled.length}/${total} enabled`;
    // Update badge in the server row
    const row = panel.closest('[data-adm-mcp-id]');
    if (row) {
      const badge = row.querySelector('.admin-badge');
      if (badge) badge.textContent = `Connected (${total - disabled.length}/${total} tools enabled)`;
    }
  } catch (e) { /* silent */ }
}

function initMcpForm() {
  const cmdEl = el('adm-mcpCommand');
  if (!cmdEl) return;  // MCP form not present in this build — nothing to wire
  const transportSel = el('adm-mcpTransport');
  const sseRow = el('adm-mcpSseRow');
  const envRow = el('adm-mcpEnvRow');
  const envFieldsWrap = el('adm-mcpEnvFields');
  const helpBox = el('adm-mcpHelp');
  const cmdRow = cmdEl.parentElement;
  let _activeHelp = null;
  let _envKeys = []; // track which env keys have dedicated fields
  let _activeOauthFile = null; // preset oauthFile config (for Google servers)
  let _activeOauth = null;     // preset OAuth flow config (provider, scopes, etc.)

  function _clearEnvFields() {
    envFieldsWrap.innerHTML = '';
    _envKeys = [];
    envRow.style.display = 'none';
    el('adm-mcpEnv').value = '';
    _activeOauth = null;
  }

  function _buildEnvFields(envObj, help, preset) {
    _clearEnvFields();
    const keys = Object.keys(envObj);
    if (!keys.length) return;
    _envKeys = keys;

    // Provider dropdown (e.g. for Email IMAP/SMTP)
    if (preset?.providerDropdown) {
      const pd = preset.providerDropdown;
      const row = document.createElement('div');
      row.className = 'admin-model-form-row';
      row.style.cssText = 'gap:6px;align-items:center;';
      const label = document.createElement('span');
      label.style.cssText = 'font-size:11px;opacity:0.55;min-width:0;white-space:nowrap;';
      label.textContent = pd.label || 'Provider';
      const select = document.createElement('select');
      select.style.cssText = 'flex:1;padding:6px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-secondary);color:var(--text-primary);font-size:12px;';
      pd.options.forEach((opt, i) => {
        const o = document.createElement('option');
        o.value = i;
        o.textContent = opt.name;
        select.appendChild(o);
      });
      select.addEventListener('change', () => {
        const opt = pd.options[parseInt(select.value)];
        for (const [envKey, field] of Object.entries(pd.targets)) {
          const inp = envFieldsWrap.querySelector(`.mcp-env-input[data-env-key="${envKey}"]`);
          if (inp) inp.value = opt[field] || '';
        }
      });
      row.appendChild(label);
      row.appendChild(select);
      envFieldsWrap.appendChild(row);
      // Auto-fill with first provider after inputs are created
      setTimeout(() => {
        const first = pd.options[0];
        for (const [envKey, field] of Object.entries(pd.targets)) {
          const inp = envFieldsWrap.querySelector(`.mcp-env-input[data-env-key="${envKey}"]`);
          if (inp && !inp.value) inp.value = first[field] || '';
        }
      }, 0);
    }

    for (const key of keys) {
      const row = document.createElement('div');
      row.className = 'admin-model-form-row';
      row.style.cssText = 'gap:6px;align-items:center;';
      const label = document.createElement('span');
      label.style.cssText = 'font-size:11px;opacity:0.55;min-width:0;white-space:nowrap;';
      label.textContent = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      const input = document.createElement('input');
      input.type = key.toLowerCase().includes('secret') || key.toLowerCase().includes('token') || key.toLowerCase().includes('key') || key.toLowerCase().includes('password') ? 'password' : 'text';
      input.placeholder = key;
      input.dataset.envKey = key;
      input.className = 'mcp-env-input';
      input.style.cssText = 'flex:1;';
      if (envObj[key]) input.value = envObj[key];
      row.appendChild(label);
      row.appendChild(input);
      envFieldsWrap.appendChild(row);
    }
    // Help toggle link
    if (help) {
      _activeHelp = help;
      const helpLink = document.createElement('a');
      helpLink.textContent = 'How do I get these?';
      helpLink.href = '#';
      helpLink.style.cssText = 'font-size:10.5px;opacity:0.5;margin-top:2px;display:inline-block;';
      helpLink.addEventListener('click', (e) => {
        e.preventDefault();
        helpBox.style.display = helpBox.style.display === 'none' ? '' : 'none';
      });
      envFieldsWrap.appendChild(helpLink);
      helpBox.textContent = help;
      helpBox.style.display = 'none';
    } else {
      _activeHelp = null;
      helpBox.style.display = 'none';
    }
  }

  // Collect env from either dedicated fields or raw JSON fallback
  function _collectEnv() {
    if (_envKeys.length) {
      const obj = {};
      envFieldsWrap.querySelectorAll('.mcp-env-input').forEach(inp => {
        if (inp.value.trim()) obj[inp.dataset.envKey] = inp.value.trim();
      });
      return JSON.stringify(obj);
    }
    return el('adm-mcpEnv').value.trim() || '{}';
  }

  transportSel.addEventListener('change', () => {
    const isSse = transportSel.value === 'sse';
    sseRow.style.display = isSse ? '' : 'none';
    cmdRow.style.display = isSse ? 'none' : '';
    if (isSse) { _clearEnvFields(); helpBox.style.display = 'none'; }
  });

  // Preset catalog
  const presetSel = el('adm-mcpPreset');
  if (presetSel) {
    MCP_PRESETS.forEach((p, i) => {
      const opt = document.createElement('option');
      opt.value = i;
      opt.textContent = p.name + (Object.keys(p.env).length ? '  (requires keys)' : '');
      presetSel.appendChild(opt);
    });
    presetSel.addEventListener('change', () => {
      if (presetSel.value === '') return;
      const p = MCP_PRESETS[parseInt(presetSel.value)];
      el('adm-mcpName').value = p.name.toLowerCase().replace(/\s+/g, '-');
      transportSel.value = 'stdio';
      el('adm-mcpCommand').value = p.command;
      el('adm-mcpArgs').value = JSON.stringify(p.args);
      sseRow.style.display = 'none';
      cmdRow.style.display = '';
      _buildEnvFields(p.env, p.help || null, p);
      _activeOauthFile = p.oauthFile || null;
      _activeOauth = p.oauth || null;
      presetSel.value = '';
      // Focus first env field if keys are needed
      const firstInput = envFieldsWrap.querySelector('.mcp-env-input');
      if (firstInput) firstInput.focus();
      else el('adm-mcpAddBtn').focus();
    });
  }

  el('adm-mcpAddBtn').addEventListener('click', async () => {
    const name = el('adm-mcpName').value.trim();
    const transport = transportSel.value;
    const command = el('adm-mcpCommand').value.trim();
    const args = el('adm-mcpArgs').value.trim() || '[]';
    const env = _collectEnv();
    const url = el('adm-mcpUrl').value.trim();
    const msg = el('adm-mcpMsg');
    if (!name) { msg.textContent = 'Name is required'; msg.className = 'admin-error'; return; }
    if (transport === 'stdio' && !command) { msg.textContent = 'Command is required for stdio'; msg.className = 'admin-error'; return; }
    if (transport === 'sse' && !url) { msg.textContent = 'URL is required for SSE'; msg.className = 'admin-error'; return; }
    try { JSON.parse(env); } catch { msg.textContent = 'Env must be valid JSON'; msg.className = 'admin-error'; return; }
    const fd = new FormData();
    fd.append('name', name); fd.append('transport', transport); fd.append('command', command); fd.append('args', args); fd.append('env', env); fd.append('url', url);
    // If preset has oauthFile config, send credentials for file generation
    if (_activeOauthFile) {
      const envObj = JSON.parse(env);
      fd.append('oauth_file', JSON.stringify({
        dir: _activeOauthFile.dir,
        filename: _activeOauthFile.filename,
        client_id: envObj.GOOGLE_CLIENT_ID || '',
        client_secret: envObj.GOOGLE_CLIENT_SECRET || '',
      }));
    }
    // If preset has OAuth flow config, send it so the server can handle authorization
    if (_activeOauth) {
      fd.append('oauth_config', JSON.stringify(_activeOauth));
    }
    msg.textContent = 'Adding...'; msg.className = '';
    try {
      const res = await checkedFetch('/api/mcp/servers', { method: 'POST', body: fd, credentials: 'same-origin' });
      const data = await res.json();
      if (data.needs_oauth) {
        msg.innerHTML = `Added ${esc(name)} — <a href="/api/mcp/oauth/authorize/${data.id}" target="_blank" style="color:var(--red);font-weight:600;">Authorize with Google</a> to connect`;
        msg.className = 'admin-success';
      } else if (data.connected) {
        msg.textContent = `Added ${name} (${data.tool_count} tools discovered)`; msg.className = 'admin-success';
      } else { msg.textContent = `Added but connection failed: ${data.error || 'unknown'}`; msg.className = 'admin-error'; }
      el('adm-mcpName').value = ''; el('adm-mcpCommand').value = ''; el('adm-mcpArgs').value = ''; el('adm-mcpUrl').value = '';
      _clearEnvFields(); helpBox.style.display = 'none'; _activeHelp = null; _activeOauthFile = null; _activeOauth = null;
      loadMcpServers();
    } catch (e) { msg.textContent = 'Failed: ' + e.message; msg.className = 'admin-error'; }
  });
}

/* ── Embedding model ──
   No settings UI: the embedding model (RAG, semantic memory, tool selection)
   is fixed infrastructure that ships with the app, and swapping it would
   invalidate every existing vector. Configure via the FASTEMBED_MODEL /
   EMBEDDING_URL env vars if you really need to override it. */

/* ── RAG ── */
async function loadRag() {
  try {
    const res = await checkedFetch('/api/personal');
    const data = await res.json();
    const dirList = el('adm-ragDirList');
    const dirs = data.directories || [];
    if (dirs.length === 0) { dirList.innerHTML = '<div class="admin-empty">No directories indexed</div>'; }
    else {
      dirList.innerHTML = dirs.map(d => `<div class="admin-rag-item"><span class="admin-rag-item-name" title="${esc(d)}">${esc(d)}</span><button class="admin-btn-delete" data-adm-rag-dir="${esc(d)}">Remove</button></div>`).join('');
      dirList.querySelectorAll('[data-adm-rag-dir]').forEach(btn => {
        btn.addEventListener('click', async () => {
          if (!await uiModule.styledConfirm(`Remove directory "${btn.dataset.admRagDir}" from RAG?`, { confirmText: 'Remove', danger: true })) return;
          btn.disabled = true; btn.textContent = '...';
          try {
            const res = await checkedFetch('/api/personal/remove_directory?directory=' + encodeURIComponent(btn.dataset.admRagDir), { method: 'DELETE' });
            if (res.ok) { ragMsg('Directory removed'); loadRag(); }
            else { const e = await res.json(); ragMsg(e.detail || 'Failed', true); }
          } catch (e) { ragMsg('Error: ' + e.message, true); }
        });
      });
    }
    const fileList = el('adm-ragFileList');
    const files = data.files || [];
    if (files.length === 0) { fileList.innerHTML = '<div class="admin-empty">No files indexed</div>'; }
    else {
      fileList.innerHTML = files.map(f => {
        const size = f.size ? (f.size > 1024 ? (f.size / 1024).toFixed(1) + ' KB' : f.size + ' B') : '';
        return `<div class="admin-rag-item"><span class="admin-rag-item-name" title="${esc(f.path || f.name)}">${esc(f.name)}</span><span class="admin-rag-item-meta">${size}</span><button class="admin-btn-delete" data-adm-rag-file="${esc(f.path || f.name)}">Delete</button></div>`;
      }).join('');
      fileList.querySelectorAll('[data-adm-rag-file]').forEach(btn => {
        btn.addEventListener('click', async () => {
          if (!await uiModule.styledConfirm(`Delete "${btn.dataset.admRagFile}" from RAG?`, { confirmText: 'Delete', danger: true })) return;
          btn.disabled = true; btn.textContent = '...';
          try {
            const res = await checkedFetch('/api/personal/file?filepath=' + encodeURIComponent(btn.dataset.admRagFile), { method: 'DELETE' });
            if (res.ok) { ragMsg('File removed'); loadRag(); }
            else { const e = await res.json(); ragMsg(e.detail || 'Failed', true); }
          } catch (e) { ragMsg('Error: ' + e.message, true); }
        });
      });
    }
  } catch (e) {
    el('adm-ragDirList').innerHTML = '<div class="admin-error">Failed to load</div>';
    el('adm-ragFileList').innerHTML = '';
  }
}

let _ragMsgTimer = null;
function ragMsg(text, isError, persist) {
  const s = el('adm-ragStatus');
  s.textContent = text; s.style.color = isError ? 'var(--red)' : 'var(--fg)';
  if (_ragMsgTimer) { clearTimeout(_ragMsgTimer); _ragMsgTimer = null; }
  if (text && !persist) _ragMsgTimer = setTimeout(() => { s.textContent = ''; }, 5000);
}

async function ragUpload(files) {
  if (!files || files.length === 0) return;
  ragMsg('Uploading ' + files.length + ' file(s)...', false, true);
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  try {
    const res = await checkedFetch('/api/personal/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.success) { ragMsg(`Uploaded ${data.uploaded.length} file(s), ${data.indexed_count} chunks indexed`); loadRag(); }
    else ragMsg(data.detail || 'Upload failed', true);
  } catch (e) { ragMsg('Upload error: ' + e.message, true); }
}

function initRag() {
  const dropZone = el('adm-ragDropZone');
  const fileInput = el('adm-ragFileInput');
  dropZone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => ragUpload(fileInput.files));
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
  dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('dragover'); ragUpload(e.dataTransfer.files); });
  el('adm-ragAddDirBtn').addEventListener('click', async () => {
    const dir = el('adm-ragDirInput').value.trim();
    if (!dir) return;
    const btn = el('adm-ragAddDirBtn');
    btn.disabled = true; btn.textContent = 'Indexing...';
    try {
      const res = await checkedFetch('/api/personal/add_directory', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ directory: dir }) });
      const data = await res.json();
      if (data.success) { ragMsg(`Indexed ${data.indexed_count} chunks from directory`); el('adm-ragDirInput').value = ''; loadRag(); }
      else ragMsg(data.detail || data.message || 'Failed', true);
    } catch (e) { ragMsg('Error: ' + e.message, true); }
    btn.disabled = false; btn.textContent = 'Add Directory';
  });
  el('adm-ragReloadBtn').addEventListener('click', async () => {
    const btn = el('adm-ragReloadBtn');
    btn.disabled = true; btn.textContent = 'Reloading...';
    try {
      const res = await checkedFetch('/api/personal/reload', { method: 'POST' });
      const data = await res.json();
      ragMsg(`Index reloaded: ${data.count} documents`);
      loadRag();
    } catch (e) { ragMsg('Reload failed: ' + e.message, true); }
    btn.disabled = false; btn.textContent = 'Reload Index';
  });
}

/* ═══════════════════════════════════════════
   SYSTEM TAB — Tokens
   ═══════════════════════════════════════════ */
// Catalog mirrors the one in settings.js integration form. Keep keys in
// sync with the backend scope allowlist.
const _TOKEN_SCOPES = [
  { key: 'todos:read',        label: 'Todos read',        detail: 'Read notes and checklists' },
  { key: 'todos:write',       label: 'Todos write',       detail: 'Create, update, delete, and toggle todo items' },
  { key: 'documents:read',    label: 'Documents read',    detail: 'Read documents when a document API is enabled' },
  { key: 'documents:write',   label: 'Documents write',   detail: 'Create and update draft documents' },
  { key: 'email:read',        label: 'Email read',        detail: 'Read email when an email API is enabled' },
  { key: 'email:draft',       label: 'Email draft',       detail: 'Create email reply drafts without sending' },
  { key: 'email:send',        label: 'Email send',        detail: 'Send email directly' },
  { key: 'calendar:read',     label: 'Calendar read',     detail: 'Read calendar events when enabled' },
  { key: 'calendar:write',    label: 'Calendar write',    detail: 'Create and update calendar events' },
  { key: 'memory:read',       label: 'Memory read',       detail: 'Read memory when enabled' },
  { key: 'memory:write',      label: 'Memory write',      detail: 'Write memory when enabled' },
  { key: 'cookbook:read',     label: 'Cookbook read',     detail: 'List cookbook tasks + tail their tmux output' },
  { key: 'cookbook:launch',   label: 'Cookbook launch',   detail: 'Launch and stop cookbook serve tasks' },
];

function _renderTokenScopeRows(t) {
  const have = new Set(t.scopes || []);
  return _TOKEN_SCOPES.map(s => {
    const action = (s.key.split(':')[1] || '').toLowerCase();
    const pill = action === 'read'
      ? 'background:rgba(150,150,150,0.18);color:var(--fg-muted,#888);'
      : 'background:color-mix(in srgb, var(--accent, var(--red)) 18%, transparent);color:var(--accent, var(--red));';
    const tool = s.label.replace(/\s+(read|write|draft|send|launch)$/i, '');
    return `
      <label style="display:flex;align-items:center;gap:8px;min-height:28px;padding:1px 0;">
        <span class="settings-label" style="width:90px;flex-shrink:0;padding:0;font-size:12px;">${esc(tool)}</span>
        <span style="font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;padding:1px 7px;border-radius:999px;flex-shrink:0;min-width:44px;text-align:center;box-sizing:border-box;${pill}">${esc(action)}</span>
        <span style="font-size:11px;line-height:1.35;opacity:0.62;flex:1;min-width:0;">${esc(s.detail)}</span>
        <label class="admin-switch" style="margin-left:auto;flex-shrink:0;"><input type="checkbox" class="adm-tok-scope" data-token-id="${esc(t.id)}" data-scope="${esc(s.key)}" ${have.has(s.key) ? 'checked' : ''}><span class="admin-slider"></span></label>
      </label>`;
  }).join('');
}

async function loadTokens() {
  const list = el('adm-tokenList');
  if (!list) return;
  try {
    const res = await checkedFetch('/api/tokens', { credentials: 'same-origin' });
    const tokens = await res.json();
    if (!tokens.length) { list.innerHTML = '<div class="admin-empty" style="color:var(--accent, var(--red));opacity:0.7;font-size:10px;">No API tokens</div>'; return; }
    list.innerHTML = tokens.map(t => `
      <div class="admin-user-row" data-adm-tok-row="${esc(t.id)}" style="display:block;">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <div class="admin-user-info" style="flex:1;min-width:0;flex-wrap:wrap;gap:0.3rem;">
            <input type="text" class="adm-tok-rename" data-token-id="${esc(t.id)}" value="${esc(t.name || '')}" placeholder="Token name" style="font-size:13px;font-weight:600;padding:3px 6px;background:transparent;border:1px solid transparent;border-radius:4px;min-width:160px;" title="Click to rename">
            <span class="admin-badge">${esc(t.token_prefix)}...</span>
            ${t.owner ? `<span style="font-size:0.75rem;opacity:0.5;">Owner: ${esc(t.owner)}</span>` : ''}
            ${t.last_used_at ? `<span style="font-size:0.75rem;opacity:0.5;">Last used: ${new Date(t.last_used_at).toLocaleDateString()}</span>` : '<span style="font-size:0.75rem;opacity:0.4;">Never used</span>'}
          </div>
          <button class="admin-btn-sm" data-adm-tok-toggle="${esc(t.id)}" style="opacity:0.75;">Permissions</button>
          <button class="admin-btn-delete" data-adm-del-token="${esc(t.id)}">Revoke</button>
        </div>
        <div data-adm-tok-perm="${esc(t.id)}" style="display:none;margin-top:8px;padding:8px 4px 0;border-top:1px solid var(--border);">
          ${_renderTokenScopeRows(t)}
          <div class="adm-tok-scope-msg" data-token-id="${esc(t.id)}" style="font-size:11px;min-height:14px;margin-top:4px;"></div>
        </div>
      </div>`).join('');

    // Revoke
    list.querySelectorAll('[data-adm-del-token]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!await uiModule.styledConfirm('Revoke this API token? External integrations using it will stop working.', { confirmText: 'Revoke', danger: true })) return;
        await checkedFetch(`/api/tokens/${btn.dataset.admDelToken}`, { method: 'DELETE', credentials: 'same-origin' });
        loadTokens();
        // Codex / Claude integration cards on the Integrations panel are
        // backed by these tokens — let them re-render so the deleted token
        // disappears there too.
        try { window.dispatchEvent(new CustomEvent('odysseus-integrations-changed')); } catch (_) {}
      });
    });
    // Toggle permissions panel
    list.querySelectorAll('[data-adm-tok-toggle]').forEach(btn => {
      btn.addEventListener('click', () => {
        const panel = list.querySelector(`[data-adm-tok-perm="${btn.dataset.admTokToggle}"]`);
        if (!panel) return;
        panel.style.display = panel.style.display === 'none' ? '' : 'none';
      });
    });
    // Rename
    list.querySelectorAll('.adm-tok-rename').forEach(input => {
      const original = input.value;
      const commit = async () => {
        const name = (input.value || '').trim();
        if (!name || name === original) return;
        try {
          const r = await checkedFetch(`/api/tokens/${input.dataset.tokenId}`, {
            method: 'PATCH', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
          });
          if (!r.ok) throw new Error('Save failed');
          loadTokens();
        } catch (_) { input.value = original; }
      };
      input.addEventListener('blur', commit);
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
    });
    // Scope toggle change → PATCH the whole scopes array for this token.
    list.querySelectorAll('.adm-tok-scope').forEach(cb => {
      cb.addEventListener('change', async () => {
        const tokenId = cb.dataset.tokenId;
        const panel = list.querySelector(`[data-adm-tok-perm="${tokenId}"]`);
        const msg = list.querySelector(`.adm-tok-scope-msg[data-token-id="${tokenId}"]`);
        const scopes = Array.from(panel.querySelectorAll('.adm-tok-scope:checked')).map(input => input.dataset.scope);
        try {
          const r = await checkedFetch(`/api/tokens/${tokenId}`, {
            method: 'PATCH', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scopes }),
          });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.detail || 'Failed');
          if (msg) { msg.textContent = 'Saved'; msg.style.color = 'var(--green, #50fa7b)'; setTimeout(() => { msg.textContent = ''; }, 1200); }
        } catch (err) {
          cb.checked = !cb.checked;
          if (msg) { msg.textContent = (err && err.message) || 'Failed'; msg.style.color = 'var(--red)'; }
        }
      });
    });
  } catch (e) { list.innerHTML = '<div class="admin-error">Failed to load tokens</div>'; }
}

function initTokenForm() {
  const addBtn = el('adm-tokenAddBtn');
  if (!addBtn || addBtn.dataset.bound) return;
  addBtn.dataset.bound = '1';
  addBtn.addEventListener('click', async () => {
    const msg = el('adm-tokenMsg');
    const reveal = el('adm-tokenReveal');
    msg.textContent = ''; msg.className = ''; reveal.style.display = 'none';
    const name = el('adm-tokenName').value.trim();
    if (!name) { msg.textContent = 'Token name is required'; msg.className = 'admin-error'; return; }
    const fd = new FormData(); fd.append('name', name);
    const scopes = (el('adm-tokenScopes')?.value || '').trim();
    if (scopes) fd.append('scopes', scopes);
    try {
      const res = await checkedFetch('/api/tokens', { method: 'POST', body: fd, credentials: 'same-origin' });
      const data = await res.json();
      if (res.ok) {
        el('adm-tokenValue').textContent = data.token;
        reveal.style.display = '';
        el('adm-tokenName').value = '';
        if (el('adm-tokenScopes')) el('adm-tokenScopes').value = '';
        loadTokens();
      }
      else { msg.textContent = data.detail || 'Failed'; msg.className = 'admin-error'; }
    } catch (e) { msg.textContent = 'Request failed'; msg.className = 'admin-error'; }
  });
  const TOKEN_COPY_ICON = uiIcon("copy", 13);
  const TOKEN_CHECK_ICON = uiIcon("check", 13, {"role":"success"});
  el('adm-tokenCopyBtn').addEventListener('click', () => {
    const val = el('adm-tokenValue').textContent;
    const btn = el('adm-tokenCopyBtn');
    navigator.clipboard.writeText(val).then(() => {
      btn.innerHTML = TOKEN_CHECK_ICON;
      btn.style.color = 'var(--accent, var(--red))';
      btn.style.opacity = '1';
      setTimeout(() => {
        btn.innerHTML = TOKEN_COPY_ICON;
        btn.style.color = '';
        btn.style.opacity = '0.7';
      }, 1600);
    });
  });
}

/* ── Webhooks ── */
async function loadWebhooks() {
  const list = el('adm-whList');
  try {
    const res = await checkedFetch('/api/webhooks', { credentials: 'same-origin' });
    const hooks = await res.json();
    if (!hooks.length) { list.innerHTML = '<div class="admin-empty">No webhooks configured</div>'; return; }
    list.innerHTML = hooks.map(w => {
      const events = (w.events || []).map(e => `<span class="admin-badge">${esc(e)}</span>`).join(' ');
      const statusBadge = w.last_status_code
        ? `<span class="admin-badge" style="background:${w.last_status_code < 400 ? 'color-mix(in srgb, var(--fg) 20%, transparent)' : 'color-mix(in srgb, var(--red) 20%, transparent)'};color:${w.last_status_code < 400 ? 'var(--fg)' : 'var(--red)'};">${w.last_status_code}</span>`
        : '';
      const lastTriggered = w.last_triggered_at ? new Date(w.last_triggered_at).toLocaleString() : 'Never';
      const errorText = w.last_error ? `<div style="font-size:0.75rem;color:var(--red);margin-top:0.2rem;">Error: ${esc(w.last_error.substring(0, 80))}</div>` : '';
      return `
        <div class="admin-ep-item" style="flex-wrap:wrap;">
          <div class="admin-ep-info" style="flex:1;min-width:200px;">
            <div class="admin-ep-name">${esc(w.name)} ${w.is_active ? '' : '<span class="admin-badge admin-badge-off">disabled</span>'} ${w.has_secret ? '<span class="admin-badge">signed</span>' : ''}</div>
            <div class="admin-ep-detail">${esc(w.url)}</div>
            <div style="margin-top:0.3rem;">${events}</div>
            <div class="admin-ep-detail">Last: ${lastTriggered} ${statusBadge}</div>
            ${errorText}
          </div>
          <div class="admin-ep-actions">
            <button class="admin-btn-sm" data-adm-wh-test="${w.id}">Test</button>
            <button class="admin-btn-sm" data-adm-wh-toggle="${w.id}">${w.is_active ? 'Disable' : 'Enable'}</button>
            <button class="admin-btn-delete" data-adm-wh-delete="${w.id}">Delete</button>
          </div>
        </div>`;
    }).join('');
    list.querySelectorAll('[data-adm-wh-test]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const msg = el('adm-whMsg'); msg.textContent = 'Sending test...'; msg.className = '';
        try {
          const res = await checkedFetch(`/api/webhooks/${btn.dataset.admWhTest}/test`, { method: 'POST', credentials: 'same-origin' });
          msg.textContent = res.ok ? 'Test sent!' : 'Test failed'; msg.className = res.ok ? 'admin-success' : 'admin-error';
          setTimeout(() => loadWebhooks(), 1000);
        } catch (e) { msg.textContent = 'Failed: ' + e.message; msg.className = 'admin-error'; }
      });
    });
    list.querySelectorAll('[data-adm-wh-toggle]').forEach(btn => {
      btn.addEventListener('click', async () => { await checkedFetch(`/api/webhooks/${btn.dataset.admWhToggle}`, { method: 'PATCH', credentials: 'same-origin' }); loadWebhooks(); });
    });
    list.querySelectorAll('[data-adm-wh-delete]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!await uiModule.styledConfirm('Delete this webhook?', { confirmText: 'Delete', danger: true })) return;
        await checkedFetch(`/api/webhooks/${btn.dataset.admWhDelete}`, { method: 'DELETE', credentials: 'same-origin' }); loadWebhooks();
      });
    });
  } catch (e) { list.innerHTML = '<div class="admin-error">Failed to load webhooks</div>'; }
}

function initWebhookForm() {
  el('adm-whAddBtn').addEventListener('click', async () => {
    const msg = el('adm-whMsg');
    msg.textContent = ''; msg.className = '';
    const name = el('adm-whName').value.trim();
    const url = el('adm-whUrl').value.trim();
    const secret = el('adm-whSecret').value.trim();
    const events = Array.from(modalEl.querySelectorAll('.adm-wh-event:checked')).map(e => e.value).join(',');
    if (!name) { msg.textContent = 'Name is required'; msg.className = 'admin-error'; return; }
    if (!url) { msg.textContent = 'URL is required'; msg.className = 'admin-error'; return; }
    if (!events) { msg.textContent = 'Select at least one event'; msg.className = 'admin-error'; return; }
    const fd = new FormData();
    fd.append('name', name); fd.append('url', url); fd.append('secret', secret); fd.append('events', events);
    try {
      const res = await checkedFetch('/api/webhooks', { method: 'POST', body: fd, credentials: 'same-origin' });
      if (res.ok) { msg.textContent = 'Webhook added'; msg.className = 'admin-success'; el('adm-whName').value = ''; el('adm-whUrl').value = ''; el('adm-whSecret').value = ''; loadWebhooks(); }
      else { const d = await res.json(); msg.textContent = d.detail || 'Failed'; msg.className = 'admin-error'; }
    } catch (e) { msg.textContent = 'Failed: ' + e.message; msg.className = 'admin-error'; }
  });
}

/* ── Features ── */
const featureLabels = {
  web_search: 'Web Search', deep_research: 'Deep Research',
  memory: 'Memory', document_editor: 'Document Editor', rag: 'RAG Knowledge Base', sensitive_filter: 'Sensitive Info Filter',
  gallery: 'Gallery'
};

async function loadFeatures() {
  const container = el('adm-featureToggles');
  try {
    const res = await checkedFetch('/api/auth/features', { credentials: 'same-origin' });
    const features = await res.json();
    container.innerHTML = Object.entries(featureLabels).map(([key, label]) => `
      <div class="admin-toggle-row" style="padding:0.4rem 0;border-bottom:1px solid var(--border);">
        <div class="admin-toggle-label">${label}</div>
        <label class="admin-switch"><input type="checkbox" data-adm-feature="${key}" ${features[key] ? 'checked' : ''}><span class="admin-slider"></span></label>
      </div>`).join('');
    container.querySelectorAll('input[data-adm-feature]').forEach(toggle => {
      toggle.addEventListener('change', async () => {
        const body = {}; body[toggle.dataset.admFeature] = toggle.checked;
        await checkedFetch('/api/auth/features', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      });
    });
  } catch (e) { container.innerHTML = '<div class="admin-error">Failed to load features</div>'; }
}

/* ── CalDAV Config ── */
function initCalDAV() {
  const urlIn = el('caldav-url');
  const userIn = el('caldav-user');
  const passIn = el('caldav-pass');
  const saveBtn = el('caldav-save-btn');
  const testBtn = el('caldav-test-btn');
  const status = el('caldav-status');
  if (!urlIn || !saveBtn) return;

  // Load current config
  fetch(`${API_BASE}/api/calendar/config`, { credentials: 'same-origin' })
    .then(r => r.json()).then(d => {
      urlIn.value = d.caldav_url || '';
      userIn.value = d.caldav_username || '';
      passIn.value = d.caldav_password || '';
    }).catch(() => {});

  saveBtn.addEventListener('click', async () => {
    status.textContent = 'Saving...';
    try {
      const res = await checkedFetch(`${API_BASE}/api/calendar/config`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ caldav_url: urlIn.value, caldav_username: userIn.value, caldav_password: passIn.value }),
      });
      const d = await res.json();
      status.textContent = d.ok ? 'Saved' : 'Error';
      status.style.color = d.ok ? 'var(--green)' : 'var(--red)';
    } catch (e) { status.textContent = 'Error'; status.style.color = 'var(--red)'; }
    setTimeout(() => { status.textContent = ''; status.style.color = ''; }, 3000);
  });

  testBtn.addEventListener('click', async () => {
    status.textContent = 'Testing...';
    try {
      // Save first
      await checkedFetch(`${API_BASE}/api/calendar/config`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ caldav_url: urlIn.value, caldav_username: userIn.value, caldav_password: passIn.value }),
      });
      const res = await checkedFetch(`${API_BASE}/api/calendar/test`, { method: 'POST', credentials: 'same-origin' });
      const d = await res.json();
      status.textContent = d.ok ? `Connected (${d.calendars} calendars)` : `Failed: ${d.error}`;
      status.style.color = d.ok ? 'var(--green)' : 'var(--red)';
    } catch (e) { status.textContent = 'Error'; status.style.color = 'var(--red)'; }
    setTimeout(() => { status.textContent = ''; status.style.color = ''; }, 5000);
  });
}

/* ── Data Backup (export/import) ── */
function initBackup() {
  el('adm-exportDataBtn').addEventListener('click', async () => {
    const btn = el('adm-exportDataBtn');
    const msg = el('adm-backupMsg');
    btn.disabled = true; btn.textContent = 'Exporting...'; msg.textContent = '';
    try {
      const res = await checkedFetch('/api/export', { credentials: 'same-origin' });
      if (!res.ok) throw new Error('Export failed');
      const blob = await res.blob();
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename=(.+)/);
      const filename = match ? match[1] : 'odysseus_backup.json';
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);
      msg.textContent = 'Export downloaded.'; msg.className = 'admin-success';
    } catch (e) { msg.textContent = 'Export failed: ' + e.message; msg.className = 'admin-error'; }
    btn.disabled = false; btn.textContent = 'Export Data';
  });

  const fileInput = el('adm-importFile');
  el('adm-importDataBtn').addEventListener('click', () => { fileInput.value = ''; fileInput.click(); });
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    if (!file) return;
    const msg = el('adm-backupMsg');
    const btn = el('adm-importDataBtn');
    btn.disabled = true; btn.textContent = 'Importing...'; msg.textContent = '';
    try {
      const text = (await file.text()).replace(/^\uFEFF/, '').trim();
      let data;
      try {
        data = JSON.parse(text);
      } catch (e) {
        throw new Error('Invalid backup file: ' + e.message);
      }
      const res = await checkedFetch('/api/import', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      const result = await res.json().catch(() => null);
      if (!result) {
        throw new Error(`Import failed: server returned ${res.status}`);
      }
      if (res.ok && result.ok) {
        msg.textContent = result.message || 'Import successful.'; msg.className = 'admin-success';
      } else {
        msg.textContent = result.message || result.detail || 'Import failed'; msg.className = 'admin-error';
      }
    } catch (e) { msg.textContent = 'Import failed: ' + e.message; msg.className = 'admin-error'; }
    btn.disabled = false; btn.textContent = 'Import Data';
  });
}

/* ── Danger Zone ── */
function initDangerZone() {
  // Per-category Danger Zone wipes. Each button declares its target
  // via data-wipe-kind; one delegated handler handles double-confirm,
  // DELETEs to /api/admin/wipe/{kind}, and writes the result.
  const _LABELS = {
    chats: 'chats', skills: 'skills',
    notes: 'notes', tasks: 'tasks', documents: 'documents',
    gallery: 'gallery images', calendar: 'calendar items',
  };
  const _wipeMsg = el('adm-wipeMsg');
  el('adm-open-brain-reset')?.addEventListener('click', () => {
    settingsModule.close?.();
    el('tool-memory-btn')?.click();
    setTimeout(() => document.querySelector('.memory-tab[data-memory-tab="settings"]')?.click(), 0);
  });
  modalEl.querySelectorAll('[data-wipe-kind]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const kind = btn.dataset.wipeKind;
      const isAll = kind === '__all__';
      const label = isAll ? 'data across every remaining category' : (_LABELS[kind] || kind);
      const scopeNote = isAll ? ' Memories, graph, and ingest data are excluded; skills are included.' : '';
      if (!await uiModule.styledConfirm(`Delete ALL ${label} across all accounts? This cannot be undone.${scopeNote}`, { confirmText: 'Delete', danger: true })) return;
      if (!await uiModule.styledConfirm(`Really delete all ${label} across all accounts?${scopeNote}`, { confirmText: isAll ? 'Yes, delete remaining data' : 'Yes, delete this category', danger: true })) return;
      btn.disabled = true;
      const prevHtml = btn.innerHTML;
      btn.innerHTML = isAll ? 'Deleting all…' : 'Deleting…';
      if (_wipeMsg) { _wipeMsg.textContent = ''; _wipeMsg.className = ''; }
      try {
        if (isAll) {
          // Iterate every known category. Failures in one shouldn't stop
          // the rest — record per-category counts and surface a summary.
          const kinds = Object.keys(_LABELS);
          const results = [];
          for (const k of kinds) {
            try {
              const r = await checkedFetch(`/api/admin/wipe/${k}`, { method: 'DELETE', credentials: 'same-origin' });
              const d = await r.json().catch(() => ({}));
              results.push({ k, ok: r.ok, count: d.count ?? 0, error: r.ok ? null : (d.detail || 'failed') });
            } catch (e) {
              results.push({ k, ok: false, count: 0, error: e.message });
            }
          }
          const okCount = results.filter(r => r.ok).length;
          const total = results.reduce((n, r) => n + (r.ok ? r.count : 0), 0);
          const fails = results.filter(r => !r.ok).map(r => r.k);
          if (_wipeMsg) {
            if (!fails.length) {
              _wipeMsg.textContent = `Deleted ${total} items across ${okCount} global categories. Memories, graph, and ingest data were not included.`;
              _wipeMsg.className = 'admin-success';
            } else {
              _wipeMsg.textContent = `Deleted ${total} items; failed: ${fails.join(', ')}.`;
              _wipeMsg.className = 'admin-error';
            }
          }
        } else {
          const res = await checkedFetch(`/api/admin/wipe/${kind}`, { method: 'DELETE', credentials: 'same-origin' });
          const data = await res.json().catch(() => ({}));
          if (res.ok) {
            if (_wipeMsg) { _wipeMsg.textContent = `Deleted ${data.count ?? 0} ${label}.`; _wipeMsg.className = 'admin-success'; }
          } else {
            if (_wipeMsg) { _wipeMsg.textContent = data.detail || 'Failed'; _wipeMsg.className = 'admin-error'; }
          }
        }
      } catch (e) {
        if (_wipeMsg) { _wipeMsg.textContent = 'Request failed: ' + e.message; _wipeMsg.className = 'admin-error'; }
      }
      btn.disabled = false; btn.innerHTML = prevHtml;
    });
  });
}

/* ═══════════════════════════════════════════
   TERMINAL LOGS VIEWER
   ═══════════════════════════════════════════ */
let logsPollInterval = null;
let isLogsPolling = false;
let cachedLogs = [];
let logsAbortController = null;

function renderLogs(isAutoPoll = false) {
  const consoleContainer = el('log-console-container');
  const levelSelect = el('log-level-select');
  const searchInput = el('log-search-input');

  if (!consoleContainer) return;

  const levelFilter = levelSelect ? levelSelect.value : 'ALL';
  const searchQuery = searchInput ? searchInput.value.trim().toLowerCase() : '';

  let logs = cachedLogs;

  // Filter by level locally
  if (levelFilter !== 'ALL') {
    logs = logs.filter(line => line.includes(` - ${levelFilter} - `));
  }

  // Filter by search query locally
  if (searchQuery) {
    logs = logs.filter(line => line.toLowerCase().includes(searchQuery));
  }

  if (logs.length === 0) {
    consoleContainer.innerHTML = '<div class="settings-system-logs-placeholder">No logs found matching current filters.</div>';
    return;
  }

  // Preserve scroll position if user is reading previous logs
  const atBottom = consoleContainer.scrollHeight - consoleContainer.scrollTop - consoleContainer.clientHeight < 40;

  consoleContainer.innerHTML = logs.map(line => {
    let levelClass = 'log-line-default';

    if (line.includes(' - INFO - ')) {
      levelClass = 'log-line-info';
    } else if (line.includes(' - WARNING - ')) {
      levelClass = 'log-line-warning';
    } else if (line.includes(' - ERROR - ') || line.includes(' - CRITICAL - ')) {
      levelClass = 'log-line-error';
    } else if (line.includes(' - DEBUG - ')) {
      levelClass = 'log-line-debug';
    }

    // XSS safe escape
    const escaped = line
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');

    return `<div class="log-line ${levelClass}">${escaped}</div>`;
  }).join('');

  if (!isAutoPoll || atBottom) {
    consoleContainer.scrollTop = consoleContainer.scrollHeight;
  }
}

async function loadLogs(isAutoPoll = false) {
  const consoleContainer = el('log-console-container');
  const limitSelect = el('log-limit-select');

  if (!consoleContainer) return;

  const limit = limitSelect ? limitSelect.value : 200;

  if (logsAbortController) {
    logsAbortController.abort();
  }
  logsAbortController = new AbortController();
  const { signal } = logsAbortController;

  try {
    const res = await checkedFetch(`/api/diagnostics/logs?limit=${limit}`, {
      credentials: 'same-origin',
      signal
    });

    if (!res.ok) {
      if (!isAutoPoll) {
        consoleContainer.innerHTML = '';
        const errDiv = document.createElement('div');
        errDiv.style.color = 'var(--red)';
        errDiv.style.fontWeight = '600';
        errDiv.textContent = `Failed to load logs: HTTP ${res.status}`;
        consoleContainer.appendChild(errDiv);
      }
      return;
    }

    const data = await res.json();
    if (data.status !== 'success' || !data.logs) {
      if (!isAutoPoll) {
        consoleContainer.innerHTML = '';
        const errDiv = document.createElement('div');
        errDiv.style.color = 'var(--red)';
        errDiv.style.fontWeight = '600';
        errDiv.textContent = 'Failed to parse logs data';
        consoleContainer.appendChild(errDiv);
      }
      return;
    }

    cachedLogs = data.logs;
    renderLogs(isAutoPoll);
  } catch (err) {
    if (err.name === 'AbortError') {
      return; // Silently ignore deliberate abort
    }
    if (!isAutoPoll) {
      consoleContainer.innerHTML = '';
      const errDiv = document.createElement('div');
      errDiv.style.color = 'var(--red)';
      errDiv.style.fontWeight = '600';
      errDiv.textContent = `Error retrieving logs: ${err.message}`;
      consoleContainer.appendChild(errDiv);
    }
  } finally {
    if (logsAbortController?.signal === signal) {
      logsAbortController = null;
    }
  }
}

function startLogsPolling() {
  if (isLogsPolling) return;
  isLogsPolling = true;
  const toggle = el('log-auto-refresh-toggle');
  if (toggle) toggle.checked = true;

  logsPollInterval = setInterval(() => {
    const modal = el('settings-modal');
    const systemPanel = el('settings-modal')?.querySelector('[data-settings-panel="system"]');

    // Safe self-cleanup if modal or panel is hidden/closed
    if (!modal || modal.classList.contains('hidden') || !systemPanel || systemPanel.classList.contains('hidden')) {
      stopLogsPolling();
      return;
    }

    loadLogs(true);
  }, 3000);
}

function stopLogsPolling() {
  if (!isLogsPolling) return;
  isLogsPolling = false;
  if (logsPollInterval) {
    clearInterval(logsPollInterval);
    logsPollInterval = null;
  }
  const toggle = el('log-auto-refresh-toggle');
  if (toggle) toggle.checked = false;
}

function initLogsView() {
  const refreshBtn = el('log-refresh-btn');
  const levelSelect = el('log-level-select');
  const limitSelect = el('log-limit-select');
  const searchInput = el('log-search-input');
  const autoRefreshToggle = el('log-auto-refresh-toggle');

  if (refreshBtn) refreshBtn.addEventListener('click', () => loadLogs(false));
  if (levelSelect) levelSelect.addEventListener('change', () => renderLogs(false));
  if (limitSelect) limitSelect.addEventListener('change', () => loadLogs(false));
  if (searchInput) searchInput.addEventListener('input', () => renderLogs(false));

  if (autoRefreshToggle) {
    autoRefreshToggle.addEventListener('change', (e) => {
      if (e.target.checked) {
        startLogsPolling();
      } else {
        stopLogsPolling();
      }
    });
  }

  // Initial fetch on view loading
  loadLogs(false);
}

async function loadStackHealth() {
  const container = el('settings-stack-health');
  if (!container) return;
  container.textContent = 'Checking…';
  try {
    const response = await checkedFetch('/api/diagnostics/services', { credentials: 'same-origin' });
    const report = await response.json();
    container.replaceChildren(...(report.services || []).map((service) => {
      const row = document.createElement('div');
      row.className = 'admin-user-row';
      row.dataset.status = service.status || 'unknown';
      const name = document.createElement('strong');
      name.textContent = service.name || 'service';
      const detail = document.createElement('span');
      detail.textContent = `${service.status || 'unknown'} · ${service.detail || ''}`;
      row.append(name, detail);
      return row;
    }));
  } catch (error) {
    container.textContent = error.message;
  }
}

function initStackHealth() {
  el('settings-stack-health-refresh')?.addEventListener('click', loadStackHealth);
}

/* ═══════════════════════════════════════════
   INIT & REFRESH
   ═══════════════════════════════════════════ */
function initAll() {
  modalEl = el('settings-modal');
  const inits = [
    initSignupToggle, initShareDefaultsToggle, initAddUser, initMcpForm,
    initCalDAV, initBackup, initDangerZone, initTokenForm, initLogsView, initStackHealth,
    () => settingsModule.initIntegrations()
  ];
  for (const fn of inits) {
    try { fn(); } catch (e) { console.error('Admin init error in', fn.name || 'anonymous', e); }
  }
  initialized = true;
  refreshAll();
}

function refreshAll() {
  loadUsers();
  loadBuiltinTools();
  loadMcpServers();
  loadTokens();
  loadLogs(false);
  loadStackHealth();
}

/* ═══════════════════════════════════════════
   PUBLIC API
   ═══════════════════════════════════════════ */
export function _initData() {
  if (!initialized) initAll();
  else refreshAll();
}

export function _initModelData() {
  document.dispatchEvent(new CustomEvent('open-clank:open-providers'));
  return Promise.resolve();
}

export function open(tab) {
  settingsModule.open(tab || 'services');
}

export function close() {
  stopLogsPolling();
  settingsModule.close();
}

const adminModule = {
  open,
  close,
  _initData,
  _initModelData,
  get _initialized() { return initialized; },
};
export default adminModule;
