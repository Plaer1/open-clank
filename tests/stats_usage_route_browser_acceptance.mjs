import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { withCopalBrowser } from "./helpers/copal_browser_fixture.mjs";
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const localPython = path.join(repo, "venv", "bin", "python");
const python = process.env.PYTHON || (fs.existsSync(localPython) ? localPython : "python3");
const fixture = path.join(
  fs.mkdtempSync(path.join(os.tmpdir(), "stats-route-")),
  "envelopes.json",
);
const run = spawnSync(
  python,
  ["tests/stats_usage_route_fixture.py", fixture],
  {
    cwd: process.cwd(),
    env: { ...process.env, PYTHONPATH: process.cwd(), AUTH_ENABLED: "false" },
    encoding: "utf8",
  },
);
assert.equal(run.status, 0, run.stderr);
const envelopes = JSON.parse(fs.readFileSync(fixture));
const quotaEnvelope = {
  schema: "open-clank.stats.v1",
  owner_scope: "b71ec76a185479c0",
  observations: [
    {
      provider_id: "anthropic",
      provider_label: "Anthropic",
      account_id: "a",
      account_label: "Primary",
      label: "Requests",
      window_kind: "discrete",
      utilization_numerator: 80,
      utilization_denominator: 100,
      is_headline: true,
      state: "official",
      observed_at: "2026-09-27T00:00:00Z",
    },
    {
      provider_id: "openai",
      provider_label: "OpenAI",
      account_id: "b",
      account_label: "Primary",
      label: "Requests",
      window_kind: "discrete",
      utilization_numerator: 40,
      utilization_denominator: 100,
      is_headline: true,
      state: "official",
      observed_at: "2026-09-27T00:00:00Z",
    },
    {
      provider_id: "google",
      provider_label: "Google",
      account_id: "c",
      account_label: "Primary",
      label: "Requests",
      window_kind: "discrete",
      utilization_numerator: 20,
      utilization_denominator: 100,
      is_headline: true,
      state: "official",
      observed_at: "2026-09-27T00:00:00Z",
    },
  ],
  timeline: [],
};
const summaryTotals = envelopes.summary.totals;
let failCost = false;
assert.equal(summaryTotals.input_tokens.state, "estimated");
assert.equal(summaryTotals.output_tokens.state, "estimated");
assert.equal(summaryTotals.cache_read_tokens.state, "estimated");
assert.equal(envelopes.cost.coverage.state, "partial_unpriced");
assert.equal(envelopes.cache.cache_rate.state, "estimated");
assert(
  envelopes.buckets.buckets.every(
    (row, index, rows) => index === 0 || row.start >= rows[index - 1].start,
  ),
);
assert(!JSON.stringify(envelopes.summary.rows).includes("model-secret"));
assert.equal(envelopes.compare.comparison.state, "reported");
assert.equal(envelopes.compare.comparison.currency, "USD");
assert.equal(envelopes.sessions.sessions.length, 2);
assert.equal(envelopes.safe_choices.actual_model.length, 2);
assert(
  envelopes.safe_choices.actual_model.every(
    (choice) => choice.handle.length >= 16 && !choice.handle.includes("model-"),
  ),
);
assert(!JSON.stringify(envelopes.safe_choices).includes("model-a"));
assert(!JSON.stringify(envelopes.sessions).includes("session-a"));
assert(!JSON.stringify(envelopes.sessions).includes("session-b"));
const page =
  '<link rel="stylesheet" href="/static/style.css"><button id="tool-usage-btn">Usage</button><main></main><script>window.sessionModule={selectSession:(id)=>{window.__selectedSession=id;}};</script><script type="module">import("/static/js/usageEntry.js");</script>';
await withCopalBrowser(
  {
    page,
    request: async (req, res) => {
      const u = new URL(req.url, "http://fixture");
      if (u.pathname === "/api/test/fail-cost") {
        failCost = true;
        res.end("ok");
        return true;
      }
      if (u.pathname === "/api/stats/v1/quota") {
        res.end(JSON.stringify(quotaEnvelope));
        return true;
      }
      if (u.pathname === "/api/stats/v1/activity") {
        res.end(JSON.stringify(envelopes.activity));
        return true;
      }
      if (u.pathname === "/api/stats/v1/summary") {
        res.end(JSON.stringify(envelopes.summary));
        return true;
      }
      if (u.pathname === "/api/stats/v1/buckets") {
        res.end(JSON.stringify(envelopes.buckets));
        return true;
      }
      if (u.pathname === "/api/stats/v1/groups") {
        assert.equal(u.searchParams.get("field"), "actual_model");
        res.end(JSON.stringify(envelopes.groups));
        return true;
      }
      if (u.pathname === "/api/stats/v1/cost") {
        if (failCost) {
          res.writeHead(503);
          res.end("temporary cost failure");
          return true;
        }
        res.end(JSON.stringify(envelopes.cost));
        return true;
      }
      if (u.pathname === "/api/stats/v1/cache") {
        res.end(JSON.stringify(envelopes.cache));
        return true;
      }
      if (u.pathname === "/api/stats/v1/sessions/top") {
        res.end(
          JSON.stringify(
            u.searchParams.get("rank") === "cost"
              ? envelopes.sessions_cost
              : envelopes.sessions,
          ),
        );
        return true;
      }
      if (u.pathname === "/api/stats/v1/compare" && req.method === "POST") {
        res.end(JSON.stringify(envelopes.compare));
        return true;
      }
      if (
        u.pathname === "/api/stats/v1/sessions/open" &&
        req.method === "POST"
      ) {
        let body = "";
        for await (const chunk of req) body += chunk;
        const payload = JSON.parse(body);
        res.end(
          JSON.stringify(
            envelopes.opened_by_handle[payload.handle] || {
              error: "unavailable",
            },
          ),
        );
        return true;
      }
      if (u.pathname === "/api/stats/v1/analysis/quality") {
        res.end(JSON.stringify(envelopes.quality));
        return true;
      }
      if (
        u.pathname === "/api/stats/v1/analysis/trends/query" &&
        req.method === "POST"
      ) {
        let body = "";
        for await (const chunk of req) body += chunk;
        const payload = JSON.parse(body);
        assert.equal(payload.content_opt_in, true);
        assert(!u.searchParams.has("terms"));
        res.end(JSON.stringify(envelopes.trends));
        return true;
      }
      return false;
    },
  },
  async ({ evaluate, until }) => {
    await until('typeof window.__openStatsUsage==="function"');
    await evaluate('void window.__openStatsUsage({view:"usage"})');
    await until('document.querySelector(".stats-usage-breakdown")');
    const text = await evaluate("document.body.innerText");
    if (text.includes("[object Object]")) console.log(text);
    assert(!text.includes("[object Object]"));
    assert(text.includes("Token usage") && text.includes("Token trend"));
    assert(!/safe-provider|safe-model|model-secret|session-secret/.test(text));
    const tokenOrder = await evaluate(
      'document.querySelector(".stats-top-sessions ol")?.innerText',
    );
    await evaluate(
      'document.querySelector("[data-stats-session-rank]").value="cost"; document.querySelector("[data-stats-session-rank]").dispatchEvent(new Event("change",{bubbles:true}))',
    );
    await until(
      'document.querySelector(".stats-top-sessions ol")?.innerText !== ' +
        JSON.stringify(tokenOrder),
    );
    assert(
      (
        await evaluate(
          'document.querySelector(".stats-top-sessions ol").innerText',
        )
      ).includes("45"),
    );
    assert.equal(
      await evaluate(
        'document.querySelector("[data-stats-open-session]").dataset.statsOpenSession',
      ),
      envelopes.sessions_cost.sessions[0].handle,
    );
    await evaluate(
      'window.__selectedSession=null; document.querySelector("[data-stats-open-session]").click()',
    );
    await until("window.__selectedSession !== null");
    assert.equal(await evaluate("window.__selectedSession"), "session-b");
    assert(!(await evaluate("document.body.innerText")).includes("session-b"));
    assert(!(await evaluate("location.href")).includes("session-b"));
    assert(
      !(await evaluate('Object.values(localStorage).join(" ")')).includes(
        "session-b",
      ),
    );
    await evaluate(
      'document.querySelector("[data-stats-compare]")?.click(); document.querySelector("[data-stats-compare]")?.dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", bubbles:true}))',
    );
    await until(
      'document.querySelector("[data-stats-compare-result]")?.textContent.includes("Comparison ready")',
    );
    assert((await evaluate("document.body.innerText")).includes("USD"));
    await evaluate('document.querySelector("[data-stats-exclude]")?.click()');
    await until('document.querySelector("[data-stats-clear-exclusions]")');
    assert(
      (await evaluate("document.body.innerText")).includes(
        "Restore excluded attribution",
      ),
    );
    await evaluate(
      'document.querySelector("[data-stats-clear-exclusions]").click()',
    );
    await evaluate(
      '(async () => { await fetch("/api/test/fail-cost"); window.__openStatsUsage({view:"usage"}); return true; })()',
    );
    await until(
      'document.querySelector(".stats-usage-status")?.textContent === "Partial data"',
    );
    assert(
      (await evaluate("document.body.innerText")).includes("Token trend"),
      "successful token panels survive the failed cost request",
    );
    assert(
      await evaluate(
        'document.querySelectorAll(".stats-attribution-treemap span").length > 0',
      ),
      "successful attribution remains mounted",
    );
    await evaluate('document.querySelector("[data-stats-mode=cost]").click()');
    await until(
      'document.querySelector(".stats-usage-breakdown h3")?.textContent === "Cost usage"',
    );
    assert.equal(
      await evaluate(
        '[...document.querySelectorAll(".stats-usage-metric")].find(card => card.querySelector("h4")?.textContent === "Billed")?.querySelector("strong")?.textContent',
      ),
      "Unavailable",
      "the failed cost panel is typed unavailable",
    );
    await evaluate(
      'document.querySelector("[data-stats-view=activity]").click()',
    );
    await until('document.querySelector(".stats-activity-view")');
    assert(
      await evaluate(
        'Boolean(document.querySelector(".stats-activity-timeline"))',
      ),
    );
    assert(
      await evaluate(
        'Boolean(document.querySelector(".stats-activity-view table"))',
      ),
    );
    assert(
      await evaluate(
        'Boolean(document.querySelector("[data-stats-activity-contribution]"))',
      ),
    );
    assert(
      await evaluate(
        'document.querySelectorAll(".stats-activity-heatmap td[tabindex]").length > 0',
      ),
    );
    await evaluate(
      'document.querySelector(".stats-activity-heatmap td[tabindex]").focus(); document.querySelector(".stats-activity-heatmap td[tabindex]").dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowRight",bubbles:true}))',
    );
    assert(
      !(await evaluate("document.body.innerText")).includes("fixture-message"),
    );
    await evaluate(
      'window.__opened=[]; window.open=(...args)=>window.__opened.push(args); void window.__openStatsUsage({view:"quota"})',
    );
    await until(
      'document.querySelectorAll(".stats-official-usage-links button").length === 2',
    );
    await evaluate(
      'document.querySelectorAll("[data-stats-official-usage]")[0].click(); document.querySelectorAll("[data-stats-official-usage]")[1].click()',
    );
    assert.deepEqual(await evaluate("window.__opened"), [
      ["https://claude.ai/settings/usage", "_blank", "noopener,noreferrer"],
      [
        "https://chatgpt.com/codex/settings/usage",
        "_blank",
        "noopener,noreferrer",
      ],
    ]);
    assert.equal(
      await evaluate(
        'document.querySelectorAll("[data-stats-official-usage]").length',
      ),
      2,
    );
    console.log("route-produced Usage journey passed");
  },
);
