// Cookbook Schedule modal. Click the "Schedule…" button in a serve
// panel → opens this modal → user picks days + time slots → POST to
// /api/cookbook/schedule/from-cookbook which writes the calendar event.
//
// Whole feature is gated on /api/cookbook/schedule/upcoming returning
// `enabled: true`. If the server says it's disabled, this module hides
// all Schedule buttons and never opens the modal.
//
// To remove the feature entirely: delete this file + the `<script>`
// tag that loads it + the `.hwfit-serve-schedule` button in
// cookbookServe.js. No other code depends on it.

(function () {
  const DAYS = [
    { key: "MO", label: "Mon" },
    { key: "TU", label: "Tue" },
    { key: "WE", label: "Wed" },
    { key: "TH", label: "Thu" },
    { key: "FR", label: "Fri" },
    { key: "SA", label: "Sat" },
    { key: "SU", label: "Sun" },
  ];
  const WEEKDAYS = ["MO", "TU", "WE", "TH", "FR"];

  let _enabledCache = null;

  async function isEnabled() {
    if (_enabledCache !== null) return _enabledCache;
    try {
      const r = await fetch("/api/cookbook/schedule/upcoming?hours=1", { credentials: "same-origin" });
      if (!r.ok) { _enabledCache = false; return false; }
      const data = await r.json();
      _enabledCache = !!data.enabled;
      return _enabledCache;
    } catch (_) {
      _enabledCache = false;
      return false;
    }
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function slotRowHtml(start, end) {
    return `
      <div class="cookbook-schedule-slot">
        <input type="time" class="cookbook-schedule-start" value="${esc(start || "09:00")}" />
        <span class="cookbook-schedule-dash">–</span>
        <input type="time" class="cookbook-schedule-end" value="${esc(end || "17:00")}" />
        <button type="button" class="cookbook-schedule-slot-remove" title="Remove slot">×</button>
      </div>`;
  }

  function dayChipsHtml(selected) {
    const sel = new Set(selected || WEEKDAYS);
    return DAYS.map(d =>
      `<label class="cookbook-schedule-day${sel.has(d.key) ? " active" : ""}">
        <input type="checkbox" value="${d.key}" ${sel.has(d.key) ? "checked" : ""} />
        ${esc(d.label)}
      </label>`).join("");
  }

  function openModal(config) {
    // config = {title, preset, repo_id, cmd, host, port}
    const wrap = document.createElement("div");
    wrap.className = "cookbook-schedule-modal-backdrop";
    wrap.innerHTML = `
      <div class="cookbook-schedule-modal">
        <div class="cookbook-schedule-modal-header">
          <strong>Schedule: ${esc(config.title || config.preset || "model")}</strong>
          <button type="button" class="cookbook-schedule-close" title="Close">×</button>
        </div>
        <div class="cookbook-schedule-modal-body">
          <div class="cookbook-schedule-section">
            <label class="cookbook-schedule-section-label">When</label>
            <div class="cookbook-schedule-slots">
              ${slotRowHtml("09:00", "17:00")}
            </div>
            <button type="button" class="cookbook-schedule-add-slot">+ add another time slot</button>
          </div>
          <div class="cookbook-schedule-section">
            <label class="cookbook-schedule-section-label">Repeat on</label>
            <div class="cookbook-schedule-days">${dayChipsHtml(WEEKDAYS)}</div>
            <div class="cookbook-schedule-day-quickset">
              <button type="button" data-set="weekdays">Weekdays</button>
              <button type="button" data-set="weekend">Weekend</button>
              <button type="button" data-set="all">Every day</button>
            </div>
          </div>
          <div class="cookbook-schedule-section">
            <label class="cookbook-schedule-section-label">Until</label>
            <div class="cookbook-schedule-until">
              <label><input type="radio" name="until-mode" value="forever" checked /> Forever</label>
              <label><input type="radio" name="until-mode" value="date" /> Until
                <input type="date" class="cookbook-schedule-until-date" disabled />
              </label>
            </div>
          </div>
          <div class="cookbook-schedule-error" style="display:none;"></div>
        </div>
        <div class="cookbook-schedule-modal-footer">
          <button type="button" class="cookbook-btn cookbook-schedule-cancel">Cancel</button>
          <button type="button" class="cookbook-btn cookbook-schedule-save">Save schedule</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);

    const $ = (sel) => wrap.querySelector(sel);
    const $$ = (sel) => Array.from(wrap.querySelectorAll(sel));

    const close = () => wrap.remove();
    $(".cookbook-schedule-close").onclick = close;
    $(".cookbook-schedule-cancel").onclick = close;
    wrap.addEventListener("click", (e) => { if (e.target === wrap) close(); });

    // Add / remove slot rows.
    $(".cookbook-schedule-add-slot").onclick = () => {
      const slots = $(".cookbook-schedule-slots");
      const tmp = document.createElement("div");
      tmp.innerHTML = slotRowHtml("18:00", "23:00");
      slots.appendChild(tmp.firstElementChild);
    };
    wrap.addEventListener("click", (e) => {
      if (e.target.classList && e.target.classList.contains("cookbook-schedule-slot-remove")) {
        const slots = $$(".cookbook-schedule-slot");
        if (slots.length > 1) e.target.closest(".cookbook-schedule-slot").remove();
      }
    });

    // Day quickset chips.
    $$(".cookbook-schedule-day-quickset button").forEach(btn => {
      btn.onclick = () => {
        const sel = btn.dataset.set;
        const want = sel === "weekdays" ? new Set(WEEKDAYS)
                   : sel === "weekend" ? new Set(["SA", "SU"])
                   : new Set(DAYS.map(d => d.key));
        $$(".cookbook-schedule-day input").forEach(inp => {
          inp.checked = want.has(inp.value);
          inp.closest(".cookbook-schedule-day").classList.toggle("active", inp.checked);
        });
      };
    });
    $$(".cookbook-schedule-day input").forEach(inp => {
      inp.onchange = () => inp.closest(".cookbook-schedule-day").classList.toggle("active", inp.checked);
    });

    // Until-date radio enables / disables the date picker.
    $$('input[name="until-mode"]').forEach(r => {
      r.onchange = () => {
        const datePicker = $(".cookbook-schedule-until-date");
        datePicker.disabled = $('input[name="until-mode"]:checked').value !== "date";
      };
    });

    $(".cookbook-schedule-save").onclick = async () => {
      const slots = $$(".cookbook-schedule-slot").map(row => ({
        start: row.querySelector(".cookbook-schedule-start").value,
        end: row.querySelector(".cookbook-schedule-end").value,
      }));
      const days = $$(".cookbook-schedule-day input:checked").map(i => i.value);
      const untilMode = $('input[name="until-mode"]:checked').value;
      const untilDate = untilMode === "date" ? $(".cookbook-schedule-until-date").value : "";

      const errEl = $(".cookbook-schedule-error");
      errEl.style.display = "none";

      const body = {
        model: config.title || config.preset || "",
        preset: config.preset,
        repo_id: config.repo_id,
        cmd: config.cmd,
        host: config.host,
        port: config.port,
        slots, days,
      };
      if (untilDate) body.until = untilDate;

      try {
        const r = await fetch("/api/cookbook/schedule/from-cookbook", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await r.json();
        if (!r.ok) {
          errEl.textContent = data.detail || data.error || `HTTP ${r.status}`;
          errEl.style.display = "block";
          return;
        }
        close();
        if (window.toast) window.toast(`Scheduled ${slots.length} window(s) on ${days.length} day(s).`, "success");
      } catch (e) {
        errEl.textContent = String(e);
        errEl.style.display = "block";
      }
    };
  }

  // Click-binding: any .hwfit-serve-schedule button inside a serve
  // panel routes to the STANDARD calendar event-creation form, with the
  // model's name pre-filled as the event title and a `cookbook:` YAML
  // block in the description. The event lands on the auto-created
  // "Cookbook" calendar so the reconciler picks it up. The custom
  // openModal() above is kept as a fallback in case the calendar
  // module hasn't loaded.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest && e.target.closest(".hwfit-serve-schedule");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();

    const panel = btn.closest("[data-cookbook-serve-panel]") || btn.closest(".doclib-card-expanded") || btn.closest(".doclib-card");
    const ds = panel ? panel.dataset || {} : {};
    const config = {
      title: ds.modelName || ds.preset || panel?.querySelector(".doclib-card-title")?.textContent?.trim() || "model",
      preset: ds.preset || "",
      repo_id: ds.repoId || "",
      cmd: ds.cmd || "",
      host: ds.host || "",
      port: ds.port ? Number(ds.port) : undefined,
    };

    // Ensure the Cookbook calendar exists and is configured. Returns
    // the href to feed into the event form.
    btn.disabled = true;
    let calHref = "";
    try {
      const r = await fetch("/api/cookbook/schedule/ensure-calendar", {
        method: "POST", credentials: "same-origin",
      });
      if (r.ok) {
        const data = await r.json();
        calHref = data.href || "";
      }
    } catch (_) {}
    btn.disabled = false;

    // Build the cookbook: YAML block that goes into the event description.
    // The reconciler parses this to know HOW to launch when the window
    // opens. If only the title is set, the reconciler title-matches
    // against saved presets.
    const yamlLines = ["cookbook:"];
    for (const k of ["preset", "repo_id", "cmd", "host", "port"]) {
      if (config[k]) yamlLines.push(`  ${k}: ${config[k]}`);
    }
    if (yamlLines.length === 1 && config.title) {
      yamlLines.push(`  preset: ${config.title}`);
    }
    const draft = {
      summary: config.title,
      description: yamlLines.join("\n"),
      rrule: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",  // default: weekdays
      calendar_href: calHref,
    };

    if (typeof window.cookbookOpenScheduleForm === "function") {
      window.cookbookOpenScheduleForm(draft);
    } else {
      // Fallback to the legacy in-house modal if the calendar module
      // hasn't loaded for some reason.
      openModal(config);
    }
  });

  // Reveal Schedule buttons once we confirm the feature is enabled.
  async function refreshScheduleButtonVisibility() {
    const enabled = await isEnabled();
    document.querySelectorAll(".hwfit-serve-schedule").forEach(btn => {
      btn.style.display = enabled ? "" : "none";
    });
  }

  // Periodically re-check (cheap) so toggling the feature in Settings
  // takes effect without a full reload.
  document.addEventListener("DOMContentLoaded", () => {
    refreshScheduleButtonVisibility();
    setInterval(refreshScheduleButtonVisibility, 30000);
  });
  // Also re-check whenever a serve panel expands.
  const obs = new MutationObserver(() => refreshScheduleButtonVisibility());
  obs.observe(document.body, { childList: true, subtree: true });

  // ── Settings card injected at the top of the Cookbook tab ─────────────
  // Lives here (not in settings.js) so the whole feature is in one file.
  // When you delete cookbookSchedule.js, this UI vanishes with it.

  async function fetchSettings() {
    try {
      const r = await fetch("/api/auth/settings", { credentials: "same-origin" });
      if (!r.ok) return {};
      return await r.json();
    } catch (_) { return {}; }
  }

  async function saveSettings(body) {
    try {
      const r = await fetch("/api/auth/settings", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return r.ok;
    } catch (_) { return false; }
  }

  async function fetchCalendars() {
    try {
      const r = await fetch("/api/calendar/calendars", { credentials: "same-origin" });
      if (!r.ok) return [];
      const d = await r.json();
      // Endpoint shape varies — accept either an array or { calendars: [...] }.
      const list = Array.isArray(d) ? d : (d.calendars || []);
      return list.map(c => ({
        href: c.href || c.url || c.id || "",
        name: c.display_name || c.name || c.summary || c.href || "Calendar",
      })).filter(c => c.href);
    } catch (_) { return []; }
  }

  async function fetchUpcoming() {
    try {
      const r = await fetch("/api/cookbook/schedule/upcoming?hours=24", { credentials: "same-origin" });
      if (!r.ok) return null;
      return await r.json();
    } catch (_) { return null; }
  }

  function buildCardHtml(s, calendars, upcoming) {
    const enabled = !!s.cookbook_scheduler_enabled;
    const calHref = s.cookbook_schedule_calendar_href || "";
    const events = (upcoming && upcoming.events) || [];
    const running = events.filter(e => e.status === "running" || e.status === "adopted").length;
    const skipped = events.filter(e => e.status === "skipped" || e.status === "failed").length;
    let statusLine = "";
    if (!enabled) {
      statusLine = "Scheduler is off. Toggle on to start launching models on a schedule.";
    } else if (!calHref) {
      statusLine = "Pick a calendar — events on it become serve windows.";
    } else if (events.length === 0) {
      statusLine = "Enabled. No scheduled windows in the next 24h.";
    } else {
      const parts = [`${events.length} scheduled in next 24h`];
      if (running) parts.push(`${running} running now`);
      if (skipped) parts.push(`${skipped} skipped`);
      statusLine = parts.join(" · ");
    }
    const calOptions = ['<option value="">— pick a calendar —</option>']
      .concat(calendars.map(c => `<option value="${esc(c.href)}"${c.href === calHref ? " selected" : ""}>${esc(c.name)}</option>`))
      .join("");
    return `
      <div class="cookbook-schedule-card" style="border:1px solid var(--border,#2d2d33);border-radius:10px;padding:12px 14px;margin:8px 0 14px;background:var(--bg-secondary,#1a1a1e);">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer;font-weight:600;font-size:13px;">
            <input type="checkbox" class="cookbook-sched-enabled" ${enabled ? "checked" : ""} />
            Cookbook scheduler <span style="opacity:.55;font-weight:400;font-size:11px;">(beta)</span>
          </label>
          <span class="cookbook-sched-status" style="opacity:.7;font-size:12px;flex:1;min-width:200px;">${esc(statusLine)}</span>
          <button type="button" class="cookbook-sched-reconcile cookbook-btn" style="font-size:11px;padding:4px 8px;" title="Force the reconciler to run now">Reconcile now</button>
        </div>
        <div class="cookbook-sched-calrow" style="margin-top:10px;display:${enabled ? "flex" : "none"};align-items:center;gap:8px;flex-wrap:wrap;">
          <label style="font-size:12px;opacity:.7;">Schedule calendar</label>
          <select class="cookbook-sched-calendar" style="background:var(--bg-primary,#131316);color:inherit;border:1px solid var(--border,#2d2d33);border-radius:6px;padding:4px 8px;min-width:220px;">${calOptions}</select>
          <span class="cookbook-sched-save-msg" style="font-size:11px;opacity:0;transition:opacity .2s;color:var(--green,#50fa7b);">Saved</span>
        </div>
      </div>`;
  }

  async function renderCard() {
    const body = document.querySelector("#cookbook-modal .cookbook-body");
    if (!body) return;
    // Skip if cookbook modal is hidden — wait until next open.
    const modal = document.getElementById("cookbook-modal");
    if (modal && modal.classList.contains("hidden")) return;
    let existing = body.querySelector(".cookbook-schedule-card");

    const [s, cals, upcoming] = await Promise.all([fetchSettings(), fetchCalendars(), fetchUpcoming()]);
    const html = buildCardHtml(s, cals, upcoming);

    if (existing) {
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      existing.replaceWith(tmp.firstElementChild);
    } else {
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      body.insertBefore(tmp.firstElementChild, body.firstChild);
    }
    wireCard();
  }

  function wireCard() {
    const card = document.querySelector(".cookbook-schedule-card");
    if (!card || card.dataset.wired === "1") return;
    card.dataset.wired = "1";

    const enabledChk = card.querySelector(".cookbook-sched-enabled");
    const calSel = card.querySelector(".cookbook-sched-calendar");
    const reconcileBtn = card.querySelector(".cookbook-sched-reconcile");
    const saveMsg = card.querySelector(".cookbook-sched-save-msg");
    const calRow = card.querySelector(".cookbook-sched-calrow");

    function flashSaved() {
      if (!saveMsg) return;
      saveMsg.style.opacity = "1";
      setTimeout(() => { saveMsg.style.opacity = "0"; }, 1500);
    }

    enabledChk.addEventListener("change", async () => {
      _enabledCache = null; // bust cache
      await saveSettings({ cookbook_scheduler_enabled: enabledChk.checked });
      calRow.style.display = enabledChk.checked ? "flex" : "none";
      flashSaved();
      // Toggle Schedule buttons immediately + refresh card status.
      refreshScheduleButtonVisibility();
      setTimeout(renderCard, 200);
    });

    calSel.addEventListener("change", async () => {
      await saveSettings({ cookbook_schedule_calendar_href: calSel.value });
      flashSaved();
      setTimeout(renderCard, 300);
    });

    reconcileBtn.addEventListener("click", async () => {
      reconcileBtn.disabled = true;
      reconcileBtn.textContent = "Reconciling…";
      try {
        await fetch("/api/cookbook/schedule/reconcile-now", { method: "POST", credentials: "same-origin" });
      } catch (_) {}
      reconcileBtn.disabled = false;
      reconcileBtn.textContent = "Reconcile now";
      renderCard();
    });
  }

  // Re-render the card whenever the cookbook modal becomes visible.
  function watchCookbookOpen() {
    const modal = document.getElementById("cookbook-modal");
    if (!modal) return;
    let lastHidden = modal.classList.contains("hidden");
    const mo = new MutationObserver(() => {
      const nowHidden = modal.classList.contains("hidden");
      if (lastHidden && !nowHidden) renderCard();
      lastHidden = nowHidden;
    });
    mo.observe(modal, { attributes: true, attributeFilter: ["class"] });
    // Also render on first open if modal is already visible at load time.
    if (!lastHidden) renderCard();
  }
  document.addEventListener("DOMContentLoaded", watchCookbookOpen);
  // Settings tab may load AFTER DOMContentLoaded; recheck once.
  setTimeout(watchCookbookOpen, 500);
})();
