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
  // panel opens the modal with the panel's current config.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest(".hwfit-serve-schedule");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();

    // Reach into the serve panel to read current config. cookbookServe.js
    // stores the active config on the panel root via data attributes
    // that we read here without coupling further.
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
    openModal(config);
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
})();
