/* Focused presentation adapter for the authenticated Lore history settings API. */
(function (global) {
  "use strict";

  const endpoint = "/api/history/settings";
  const mounted = new WeakMap();
  const activationObservers = new WeakMap();

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let amount = bytes;
    let index = -1;
    do { amount /= 1024; index += 1; } while (amount >= 1024 && index < units.length - 1);
    return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${units[index]}`;
  }

  function detailMessage(detail) {
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (detail && typeof detail === "object") {
      if (typeof detail.message === "string" && detail.message.trim()) return detail.message.trim();
      if (typeof detail.detail === "string" && detail.detail.trim()) return detail.detail.trim();
      if (typeof detail.code === "string" && detail.code.trim()) return detail.code.trim();
    }
    return "";
  }

  async function responseError(response, fallback) {
    const body = await response.json().catch(() => ({}));
    const message = detailMessage(body?.detail) || detailMessage(body?.error) || detailMessage(body?.message) || fallback;
    const error = new Error(message);
    error.status = response.status;
    error.code = body?.detail && typeof body.detail === "object" ? body.detail.code : undefined;
    error.body = body;
    return error;
  }

  async function load(options) {
    const response = await fetch(endpoint, { credentials: "same-origin", signal: options?.signal });
    if (!response.ok) throw await responseError(response, `History settings unavailable (${response.status})`);
    return response.json();
  }

  async function save(policy, revision) {
    const response = await fetch(endpoint, {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: revision, policy }),
    });
    if (!response.ok) throw await responseError(response, `History settings rejected (${response.status})`);
    const body = await response.json().catch(() => ({}));
    return body;
  }

  function render(container, snapshot) {
    if (!container) return;
    const usage = snapshot.usage || {};
    const status = snapshot.status || {};
    const globalPolicy = (snapshot.policy || {}).global || {};
    container.replaceChildren();
    const heading = document.createElement("h3");
    heading.textContent = "History storage";
    const details = document.createElement("p");
    details.textContent = `${formatBytes(usage.physical_allocated_bytes)} allocated of ${formatBytes(globalPolicy.total_bytes)} target`;
    const quality = document.createElement("small");
    quality.textContent = ` ${usage.measurement_quality || "unavailable"} measurement; ${usage.retained_version_count || 0} retained versions`;
    details.append(quality);
    const health = document.createElement("p");
    health.dataset.historyStatus = status.state || "ready";
    health.textContent = status.history_paused
      ? `History capture paused: ${status.state}. Ordinary saves continue.`
      : `History capture ${status.state || "ready"}.`;
    container.append(heading, details, health);
  }

  function mount(container, options = {}) {
    if (!container) return Promise.resolve();
    const accountKey = String(options.accountId || document.body?.dataset?.accountId || global.currentAccountId || "");
    const previous = mounted.get(container);
    const force = Boolean(options.force);
    if (!force && previous?.accountKey === accountKey && previous.promise) return previous.promise;
    previous?.cleanup?.();
    const abort = new AbortController();
    const entry = {
      accountKey,
      generation: (previous?.generation || 0) + 1,
      abort,
      promise: null,
      saveListener: null,
    };
    mounted.set(container, entry);
    const isCurrent = () => mounted.get(container) === entry;
    let state;
    const usage = container.querySelector("[data-history-usage]");
    const inherited = container.querySelector("[data-history-inherited]");
    const total = container.querySelector("[data-history-total]");
    const list = container.querySelector("[data-history-scope-list]");
    const status = container.querySelector("[data-history-status]");
    const saveButton = container.querySelector("[data-history-save]");
    const setStatus = (message, error) => {
      if (!status) return;
      status.textContent = message || "";
      status.dataset.state = error ? "error" : "ok";
    };
    const ensureRetryButton = () => {
      let button = container.querySelector("[data-history-retry]");
      if (!button) {
        button = document.createElement("button");
        button.type = "button";
        button.className = "admin-btn-add";
        button.dataset.historyRetry = "true";
        button.textContent = "Retry History";
        button.style.marginTop = "8px";
        container.append(button);
      }
      button.disabled = false;
      return button;
    };
    const sync = () => {
      const policy = state.policy || {};
      const globalPolicy = policy.global || {};
      const measured = state.usage || {};
      if (usage) usage.textContent = `${formatBytes(measured.physical_allocated_bytes)} allocated of ${formatBytes(globalPolicy.total_bytes)} target; ${formatBytes(measured.logical_retained_bytes)} logical retained (${measured.measurement_quality || "unavailable"} measurement)`;
      if (total) {
        total.value = globalPolicy.total_bytes || "";
        total.disabled = Boolean(globalPolicy.inherited);
      }
      if (inherited) inherited.textContent = globalPolicy.inherited
        ? "The installation target is inherited. An administrator can change it."
        : "The installation target includes Lore payloads, indexes, catalog, journal and staging.";
      if (list) {
        list.replaceChildren();
        (policy.scopes || []).forEach((scope) => {
          const row = document.createElement("div");
          row.className = "settings-row";
          row.style.cssText = "align-items:center;gap:8px;margin-top:6px;";
          const label = document.createElement("label");
          label.className = "settings-label";
          label.textContent = scope.kind === "directory" ? `Directory: ${scope.display_path || scope.root || "assigned Files root"}` : `Workspace: ${scope.workspace_id || scope.value}`;
          label.style.flex = "1";
          const input = document.createElement("input");
          input.type = "number";
          input.min = "1";
          input.step = "1048576";
          input.className = "settings-select";
          input.dataset.historyScopeId = scope.scope_id || "";
          input.value = scope.limit_bytes || "";
          input.disabled = scope.enabled === false;
          const enabled = document.createElement("input");
          enabled.type = "checkbox";
          enabled.checked = scope.enabled !== false;
          enabled.title = "Enable this limit";
          enabled.addEventListener("change", () => { input.disabled = !enabled.checked; });
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "admin-btn-add";
          remove.textContent = "Remove";
          remove.addEventListener("click", () => { row.dataset.historyRemoved = "true"; row.hidden = true; });
          row.append(label, input, enabled, remove);
          list.append(row);
        });
        const addWorkspace = document.createElement("button");
        addWorkspace.type = "button";
        addWorkspace.className = "admin-btn-add";
        addWorkspace.dataset.historyAction = "add-workspace";
        addWorkspace.textContent = "Add workspace limit";
        list.append(addWorkspace);
        const addDirectory = document.createElement("button");
        addDirectory.type = "button";
        addDirectory.className = "admin-btn-add";
        addDirectory.dataset.historyAction = "add-directory";
        addDirectory.textContent = "Add directory limit";
        list.append(addDirectory);
      }
    };
    const workspaceEditor = () => {
      const row = document.createElement("div");
      row.className = "settings-row";
      row.style.cssText = "align-items:center;gap:8px;margin-top:6px;";
      row.dataset.historyNewWorkspace = "true";
      row.innerHTML = '<label class="settings-label">Workspace</label><select data-history-workspace class="settings-select"></select><input data-history-workspace-limit type="number" min="1" step="1048576" class="settings-select" placeholder="bytes" />';
      const select = row.querySelector("[data-history-workspace]");
      (state.workspace_options || []).forEach((workspace) => {
        const option = document.createElement("option");
        option.value = String(workspace);
        option.textContent = String(workspace);
        select.append(option);
      });
      return row;
    };
    const directoryEditor = () => {
      const row = document.createElement("div");
      row.className = "settings-row";
      row.style.cssText = "align-items:center;gap:8px;margin-top:6px;";
      row.dataset.historyNewDirectory = "true";
      row.innerHTML = '<label class="settings-label">Files root</label><select data-history-directory class="settings-select"></select><input data-history-directory-limit type="number" min="1" step="1048576" class="settings-select" placeholder="bytes" />';
      const select = row.querySelector("[data-history-directory]");
      (state.directory_options || []).forEach((directory) => {
        const option = document.createElement("option");
        option.value = String(directory.id);
        option.textContent = String(directory.label);
        select.append(option);
      });
      return row;
    };
    const actionHandlers = container.__historyActionHandlers || (container.__historyActionHandlers = {});
    actionHandlers.workspace = workspaceEditor;
    actionHandlers.directory = directoryEditor;
    // Settings may remount the scope list while the panel remains open. Bind
    // on the stable history card so newly-rendered action buttons remain live.
    if (container && container.dataset.historyActionsBound !== "1") {
      container.dataset.historyActionsBound = "1";
      container.addEventListener("click", (event) => {
        const retry = event.target.closest?.("[data-history-retry]");
        if (retry) {
          event.preventDefault();
          retry.disabled = true;
          setStatus("Retrying History service…", false);
          void mount(container, { force: true }).catch(() => {});
          return;
        }
        const button = event.target.closest?.("[data-history-action]");
        const action = button?.dataset.historyAction;
        if (action === "add-workspace") {
          const editor = actionHandlers.workspace?.();
          if (editor) button.replaceWith(editor);
        }
        if (action === "add-directory") {
          const editor = actionHandlers.directory?.();
          if (editor) button.replaceWith(editor);
        }
      }, true);
    }
    const saveCurrent = async () => {
      const policy = state.policy || {};
      let validationError = "";
      const scopes = (policy.scopes || []).filter((scope) => {
        const input = list?.querySelector(`[data-history-scope-id="${CSS.escape(scope.scope_id || "")}"]`);
        const row = input?.parentElement;
        // Removal state belongs to the rendered scope row, while the limit
        // value belongs to its input. Inspecting the input silently resurrects
        // a Files-root/workspace scope on the next save.
        return Boolean(row && !row.dataset.historyRemoved);
      }).map((scope) => {
        const input = list?.querySelector(`[data-history-scope-id="${CSS.escape(scope.scope_id || "")}"]`);
        const row = input?.parentElement;
        const enabled = row?.querySelector('input[type="checkbox"]')?.checked !== false;
        const raw = String(input?.value ?? "").trim();
        const value = Number(raw);
        if (!raw || !Number.isFinite(value) || value <= 0) {
          validationError = "Each history scope limit must be greater than zero.";
          return null;
        }
        return { ...scope, enabled, limit_bytes: value };
      }).filter(Boolean);
      if (validationError) { setStatus(validationError, true); return; }
      const newRow = list?.querySelector("[data-history-new-workspace]");
      if (newRow?.querySelector("[data-history-workspace]")?.value) {
        const value = Number(newRow.querySelector("[data-history-workspace-limit]").value);
        if (!Number.isFinite(value) || value <= 0) { setStatus("Workspace limit must be greater than zero.", true); return; }
        scopes.push({ kind: "workspace", workspace_id: newRow.querySelector("[data-history-workspace]").value.trim(), limit_bytes: value, enabled: true });
      }
      const newDirectory = list?.querySelector("[data-history-new-directory]");
      if (newDirectory?.querySelector("[data-history-directory]")?.value) {
        const value = Number(newDirectory.querySelector("[data-history-directory-limit]").value);
        if (!Number.isFinite(value) || value <= 0) { setStatus("Directory limit must be greater than zero.", true); return; }
        scopes.push({ kind: "directory", root_id: newDirectory.querySelector("[data-history-directory]").value, limit_bytes: value, enabled: true });
      }
      const patch = { scopes };
      if (!total?.disabled) {
        const value = Number(total.value);
        if (!Number.isFinite(value) || value <= 0) { setStatus("Global limit must be greater than zero.", true); return; }
        patch.global = { total_bytes: value };
      }
      if (!state || state.status?.available === false) {
        setStatus("History service is unavailable. Retry before saving settings.", true);
        ensureRetryButton();
        if (saveButton) saveButton.disabled = true;
        return;
      }
      if (saveButton) saveButton.disabled = true;
      setStatus("Saving…", false);
      try {
        const loaded = await save(patch, state.policy.revision);
        if (!isCurrent()) return;
        state = loaded;
        sync();
        setStatus("History settings saved.", false);
      } catch (error) {
        if (isCurrent()) {
          setStatus(error.message || "History settings could not be saved.", true);
          if (!error.status || error.status >= 500) {
            state.status = { ...(state.status || {}), available: false };
            if (saveButton) saveButton.disabled = true;
            ensureRetryButton();
          }
        }
      } finally {
        if (isCurrent() && saveButton && state?.status?.available !== false) saveButton.disabled = false;
      }
    };
    if (saveButton) saveButton.disabled = true;
    if (usage) usage.textContent = "Loading measured History usage…";
    setStatus("Connecting to History service…", false);
    const promise = load({ signal: abort.signal }).then((loaded) => {
      if (!isCurrent()) return loaded;
      state = loaded;
      sync();
      entry.saveListener = saveCurrent;
      saveButton?.addEventListener("click", entry.saveListener);
      const available = loaded.status?.available !== false;
      if (saveButton) saveButton.disabled = !available;
      if (available) {
        container.querySelector("[data-history-retry]")?.remove();
        setStatus(loaded.status?.history_paused ? `History capture paused: ${loaded.status.state}. Ordinary saves continue.` : `History capture ${loaded.status?.state || "ready"}.`, false);
      } else {
        setStatus("History service is unavailable. Ordinary saves continue; retry to load History settings.", true);
        ensureRetryButton();
      }
      return loaded;
    }).catch((error) => {
      if (isCurrent()) {
        if (usage) usage.textContent = "History usage is unavailable because the History service did not respond.";
        if (saveButton) saveButton.disabled = true;
        setStatus(error.message || "History settings unavailable. Ordinary saves continue.", true);
        ensureRetryButton();
      }
      return null;
    });
    entry.promise = promise;
    entry.cleanup = () => {
      abort.abort();
      if (entry.saveListener) saveButton?.removeEventListener("click", entry.saveListener);
    };
    return promise;
  }

  function refreshAll(options = {}) {
    return Promise.all(Array.from(document.querySelectorAll('[data-settings-panel="history"]'))
      .filter((panel) => !panel.classList.contains("hidden"))
      .map((panel) => panel.querySelector("[data-history-settings]"))
      .filter(Boolean)
      .map((container) => mount(container, options)));
  }

  function observeHistoryActivation() {
    document.querySelectorAll('[data-settings-panel="history"]').forEach((panel) => {
      if (activationObservers.has(panel)) return;
      const container = panel.querySelector("[data-history-settings]");
      if (!container) return;
      let wasVisible = !panel.classList.contains("hidden");
      const observer = new MutationObserver(() => {
        const visible = !panel.classList.contains("hidden");
        if (visible && !wasVisible) void mount(container, { force: true });
        wasVisible = visible;
      });
      observer.observe(panel, { attributes: true, attributeFilter: ["class"] });
      activationObservers.set(panel, observer);
      if (wasVisible) void mount(container, { force: true });
    });
  }

  global.OpenClankHistorySettings = { endpoint, formatBytes, load, save, render, mount, refreshAll };
  document.addEventListener("DOMContentLoaded", observeHistoryActivation);
  document.addEventListener("openclank:auth-context-changed", (event) => { refreshAll({ accountId: event.detail?.accountId, force: true }); });
})(window);
