/**
 * workers/dashboard — Toll Ops audit dashboard.
 *
 * Serves a single-page HTML dashboard, laid out from a mockup the user
 * provided. Every panel is wired to real data from a single GET
 * /api/dashboard call, backed by Turso: the transaction feed, payment
 * status breakdown, detection method mix, throughput, and recent audit
 * events. Two tiles from the original mockup are deliberately not
 * present at all — Detection Accuracy (no ground-truth data exists
 * anywhere to compare detections against) and Mean Latency (not
 * instrumented anywhere in core/main.py) — showing either as a fake
 * empty/zero value would misrepresent them as tracked when they aren't;
 * the footer note says so explicitly.
 *
 * Every request requires HTTP Basic Auth (any username; password checked
 * against env.DASHBOARD_PASSWORD via a constant-time compare). This
 * matters more here than for workers/charge: that Worker only ever
 * receives Paystack's own signed webhook calls, but this one exposes real
 * customer data (phone numbers via vehicles, plate numbers) to whoever can
 * reach the URL, so it must never be servable without the shared secret.
 */

import { createClient } from "@libsql/client";

export interface Env {
  DASHBOARD_PASSWORD: string;
  TURSO_DATABASE_URL: string;
  TURSO_AUTH_TOKEN: string;
}

function timingSafeEqual(a: string, b: string): boolean {
  // Compares up to the longer length so a wrong-length guess doesn't
  // short-circuit early and leak length via timing, same discipline as
  // workers/charge's signature check.
  const maxLength = Math.max(a.length, b.length);
  let diff = a.length === b.length ? 0 : 1;
  for (let i = 0; i < maxLength; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

function isAuthorized(request: Request, env: Env): boolean {
  // Fail closed with a clean 401 (rather than an uncaught TypeError inside
  // timingSafeEqual) if the DASHBOARD_PASSWORD secret was never set — e.g.
  // `wrangler secret put` skipped, or missing from local .dev.vars.
  if (!env.DASHBOARD_PASSWORD) return false;
  const header = request.headers.get("Authorization");
  // Auth-scheme token is case-insensitive per RFC 7235.
  if (!header || !/^basic /i.test(header)) return false;
  let decoded: string;
  try {
    decoded = atob(header.slice("Basic ".length));
  } catch {
    return false;
  }
  const colonIndex = decoded.indexOf(":");
  const password = colonIndex === -1 ? decoded : decoded.slice(colonIndex + 1);
  return timingSafeEqual(password, env.DASHBOARD_PASSWORD);
}

function unauthorized(): Response {
  return new Response("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Toll Ops Dashboard"' },
  });
}

interface TransactionRow {
  created_at: string;
  plate_number: string | null;
  identification_method: string;
  toll_amount: number;
  payment_status: string;
}

interface DashboardData {
  transactions: TransactionRow[];
  total_transactions: number;
  status_counts: Record<string, number>;
  method_counts: Record<string, number>;
  throughput_last_hour: number;
  recent_events: { created_at: string; event_type: string; event_detail: string | null }[];
}

// Reused across requests within the same isolate — env's Turso bindings are
// normally stable for the isolate's lifetime, so there's no reason to
// rebuild the client (and re-parse the URL/token) on every poll. Keyed on
// the actual url+token pair rather than just "have we built one yet", so a
// rotated TURSO_AUTH_TOKEN (e.g. after a leak) takes effect on the very next
// request instead of waiting for Cloudflare to recycle the isolate.
let cachedTurso: ReturnType<typeof createClient> | undefined;
let cachedTursoKey: string | undefined;

function getTursoClient(env: Env): ReturnType<typeof createClient> {
  if (!env.TURSO_DATABASE_URL || !env.TURSO_AUTH_TOKEN) {
    throw new Error("Turso not configured");
  }
  const key = `${env.TURSO_DATABASE_URL} ${env.TURSO_AUTH_TOKEN}`;
  if (!cachedTurso || cachedTursoKey !== key) {
    cachedTurso = createClient({ url: env.TURSO_DATABASE_URL, authToken: env.TURSO_AUTH_TOKEN });
    cachedTursoKey = key;
  }
  return cachedTurso;
}

interface LogEvent {
  log_id: number;
  transaction_id: number | null;
  event_type: string;
  event_detail: string | null;
  created_at: string;
}

// Separate from recent_events above (which caps at 8, just enough for the dashboard's own
// side panel) -- /logs wants real scrollback, not a teaser.
async function fetchLogs(env: Env, limit: number): Promise<LogEvent[]> {
  const turso = getTursoClient(env);
  const result = await turso.execute({
    sql: "SELECT log_id, transaction_id, event_type, event_detail, created_at FROM audit_log ORDER BY log_id DESC LIMIT ?",
    args: [limit],
  });
  return result.rows.map((row) => ({
    log_id: Number(row.log_id),
    transaction_id: row.transaction_id === null ? null : Number(row.transaction_id),
    event_type: row.event_type as string,
    event_detail: (row.event_detail as string | null) ?? null,
    created_at: row.created_at as string,
  }));
}

async function fetchDashboardData(env: Env): Promise<DashboardData> {
  const turso = getTursoClient(env);

  const [transactionsResult, statusResult, methodResult, throughputResult, eventsResult] =
    await Promise.all([
      turso.execute({
        sql: `SELECT t.created_at, v.plate_number, t.identification_method, t.toll_amount, t.payment_status
              FROM transactions t
              LEFT JOIN vehicles v ON v.vehicle_id = t.vehicle_id
              ORDER BY t.created_at DESC
              LIMIT 20`,
        args: [],
      }),
      turso.execute({
        sql: "SELECT payment_status, COUNT(*) as c FROM transactions GROUP BY payment_status",
        args: [],
      }),
      turso.execute({
        sql: "SELECT identification_method, COUNT(*) as c FROM transactions GROUP BY identification_method",
        args: [],
      }),
      turso.execute({
        sql: "SELECT COUNT(*) as c FROM transactions WHERE created_at >= datetime('now', '-1 hour')",
        args: [],
      }),
      turso.execute({
        sql: "SELECT created_at, event_type, event_detail FROM audit_log ORDER BY created_at DESC LIMIT 8",
        args: [],
      }),
    ]);

  const status_counts: Record<string, number> = {};
  for (const row of statusResult.rows) {
    status_counts[row.payment_status as string] = Number(row.c);
  }
  const method_counts: Record<string, number> = {};
  for (const row of methodResult.rows) {
    method_counts[row.identification_method as string] = Number(row.c);
  }
  const total_transactions = Object.values(status_counts).reduce((a, b) => a + b, 0);

  return {
    // Built explicitly as plain objects (not the raw Row array-like
    // values) so the JSON response has real field names regardless of how
    // the libsql client's Row type serializes by default.
    transactions: transactionsResult.rows.map((row) => ({
      created_at: row.created_at as string,
      plate_number: (row.plate_number as string | null) ?? null,
      identification_method: row.identification_method as string,
      toll_amount: row.toll_amount as number,
      payment_status: row.payment_status as string,
    })),
    total_transactions,
    status_counts,
    method_counts,
    throughput_last_hour: Number(throughputResult.rows[0]?.c ?? 0),
    recent_events: eventsResult.rows.map((row) => ({
      created_at: row.created_at as string,
      event_type: row.event_type as string,
      event_detail: (row.event_detail as string | null) ?? null,
    })),
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!isAuthorized(request, env)) return unauthorized();

    const url = new URL(request.url);

    if (url.pathname === "/api/dashboard") {
      try {
        const body = await fetchDashboardData(env);
        return new Response(JSON.stringify(body), {
          headers: { "Content-Type": "application/json" },
        });
      } catch (e) {
        const status = e instanceof Error && e.message === "Turso not configured" ? 503 : 502;
        return new Response(JSON.stringify({ error: String(e) }), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      }
    }

    if (url.pathname === "/api/logs") {
      try {
        const limit = Math.min(500, Math.max(1, Number(url.searchParams.get("limit")) || 200));
        const events = await fetchLogs(env, limit);
        return new Response(JSON.stringify({ events }), {
          headers: { "Content-Type": "application/json" },
        });
      } catch (e) {
        const status = e instanceof Error && e.message === "Turso not configured" ? 503 : 502;
        return new Response(JSON.stringify({ error: String(e) }), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      }
    }

    if (url.pathname === "/") {
      return new Response(DASHBOARD_HTML, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    if (url.pathname === "/logs") {
      return new Response(LOGS_HTML, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    return new Response("Not found", { status: 404 });
  },
};

// Shared by DASHBOARD_HTML and LOGS_HTML -- both are static template strings (no shared
// runtime/templating engine in a Worker), so this is interpolated into each rather than
// fetched separately. Keeps the two pages' chrome (fonts, palette, header, nav, panel/card
// styling) from drifting apart as one gets edited without the other.
const BASE_CSS = `
  :root {
    --bg: #F2F4F7;
    --panel: #FFFFFF;
    --panel-border: #DCE1E8;
    --gold: #B8791A;
    --gold-dim: #E4C077;
    --good: #1E8F5F;
    --bad: #C7392F;
    --text: #1A2233;
    --text-dim: #667085;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'Inter', sans-serif;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    padding: 28px;
    min-height: 100vh;
  }

  /* Caps line length on wide/projector displays -- full-bleed edge-to-edge content past
     ~1500px reads as unfinished, not spacious. */
  .page {
    max-width: 1360px;
    margin: 0 auto;
  }

  .header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 16px;
    flex-wrap: wrap;
    gap: 8px;
  }
  .header h1 {
    font-size: 18px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text);
    margin: 0;
    font-weight: 700;
  }
  .header .clock {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text-dim);
  }

  .nav {
    display: flex;
    gap: 4px;
    margin-bottom: 20px;
  }
  .nav a {
    font-family: var(--sans);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    text-decoration: none;
    padding: 7px 14px;
    border-radius: 6px;
    border: 1px solid transparent;
    transition: color 0.15s, background 0.15s, border-color 0.15s;
  }
  .nav a:hover { color: var(--text); background: rgba(26,34,51,0.04); }
  .nav a.active {
    color: var(--gold);
    background: rgba(184,121,26,0.1);
    border-color: rgba(184,121,26,0.28);
  }

  .panel {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    padding: 16px 18px;
    box-shadow: 0 1px 2px rgba(16,24,40,0.04);
  }
  .panel h2 {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    margin: 0 0 12px 0;
    font-weight: 600;
  }
  .panel h2 .live-tag {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--good);
    background: rgba(30,143,95,0.1);
    border: 1px solid var(--good);
    padding: 2px 6px;
    border-radius: 3px;
    letter-spacing: 0.05em;
    text-transform: none;
    margin-left: 8px;
  }
`;

const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Toll Ops — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
${BASE_CSS}
  .gantry, .metric {
    box-shadow: 0 1px 2px rgba(16,24,40,0.04);
  }

  .gantry {
    position: relative;
    height: 64px;
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    margin-bottom: 20px;
    overflow: hidden;
  }
  .gantry .lane-label {
    position: absolute;
    left: 12px; top: 8px;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .gantry .rail {
    position: absolute;
    top: 30px; left: 0; right: 0;
    height: 1px;
    background: repeating-linear-gradient(90deg, var(--panel-border) 0 8px, transparent 8px 16px);
  }
  .gantry .sensor-post {
    position: absolute;
    top: 22px;
    width: 2px; height: 20px;
    background: var(--gold-dim);
  }
  .vehicle {
    position: absolute;
    top: 24px;
    width: 18px; height: 10px;
    border-radius: 2px;
    background: var(--good);
    box-shadow: 0 0 8px rgba(43,182,115,0.6);
    animation: pass 4s linear infinite;
  }
  .vehicle.fallback { background: var(--gold); box-shadow: 0 0 8px rgba(217,164,65,0.6); }
  @keyframes pass {
    from { left: -20px; }
    to { left: 100%; }
  }

  .metrics {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 20px;
  }
  .metric {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    padding: 16px 18px;
  }
  .metric .label {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 8px;
  }
  .metric .value {
    font-family: var(--mono);
    font-size: 26px;
    font-weight: 700;
  }
  .metric .value.good { color: var(--good); }
  .metric .value.warn { color: var(--gold); }
  .metric .sub {
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 4px;
  }

  .grid {
    display: grid;
    grid-template-columns: 1.6fr 1fr;
    gap: 16px;
  }
  @media (max-width: 900px) {
    .metrics { grid-template-columns: repeat(2,1fr); }
    .grid { grid-template-columns: 1fr; }
  }

  table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12px; }
  th {
    text-align: left;
    font-family: var(--sans);
    font-size: 10px;
    text-transform: uppercase;
    color: var(--text-dim);
    letter-spacing: 0.05em;
    padding: 6px 8px;
    border-bottom: 1px solid var(--panel-border);
  }
  td { padding: 8px; border-bottom: 1px solid var(--panel-border); }
  .status-pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 3px;
    font-size: 10px;
    font-family: var(--sans);
    font-weight: 600;
  }
  .status-pill.settled { background: rgba(30,143,95,0.15); color: var(--good); }
  .status-pill.flagged { background: rgba(199,57,47,0.15); color: var(--bad); }
  .status-pill.pending { background: rgba(184,121,26,0.15); color: var(--gold); }
  .method-tag { color: var(--text-dim); font-size: 11px; }
  .method-tag.rfid { color: var(--good); }
  .method-tag.anpr { color: var(--gold); }
  .method-tag.none { color: var(--bad); }

  .bar-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; font-size: 12px; }
  .bar-row .bar-label { width: 90px; color: var(--text-dim); }
  .bar-track { flex: 1; height: 8px; background: rgba(26,34,51,0.08); border-radius: 4px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; }
</style>
</head>
<body>
<div class="page">

  <div class="header">
    <h1>Smart Cashless Tolling — Audit Console</h1>
    <span class="clock" id="clock">--:--:--</span>
  </div>
  <div class="nav">
    <a href="/" class="active">Dashboard</a>
    <a href="/logs">Logs</a>
  </div>

  <div class="gantry">
    <div class="lane-label">Gantry 01 — Detection Strip</div>
    <div class="rail"></div>
    <div class="sensor-post" style="left: 20%;"></div>
    <div class="sensor-post" style="left: 50%;"></div>
    <div class="sensor-post" style="left: 80%;"></div>
    <div class="vehicle" style="animation-delay: 0s;"></div>
    <div class="vehicle fallback" style="animation-delay: 1.3s;"></div>
    <div class="vehicle" style="animation-delay: 2.6s;"></div>
  </div>

  <div class="metrics">
    <div class="metric">
      <div class="label">Total Transactions</div>
      <div class="value" id="m-total">—</div>
      <div class="sub">All-time, live</div>
    </div>
    <div class="metric">
      <div class="label">Payment Success Rate</div>
      <div class="value" id="m-success-rate">—</div>
      <div class="sub" id="m-success-sub">Live</div>
    </div>
    <div class="metric">
      <div class="label">Pending Charges</div>
      <div class="value" id="m-pending">—</div>
      <div class="sub">Awaiting webhook resolution</div>
    </div>
    <div class="metric">
      <div class="label">Throughput</div>
      <div class="value" id="m-throughput">—</div>
      <div class="sub">Last 60 minutes, live</div>
    </div>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>Live Transaction Feed <span class="live-tag">LIVE — TURSO</span></h2>
      <table>
        <thead><tr><th>Time</th><th>Vehicle</th><th>Method</th><th>Amount</th><th>Status</th></tr></thead>
        <tbody id="feed"><tr><td colspan="5" style="color:var(--text-dim);">Loading…</td></tr></tbody>
      </table>
    </div>

    <div class="panel">
      <h2>Detection Method Mix <span class="live-tag">LIVE — TURSO</span></h2>
      <div id="method-mix">
        <div style="color:var(--text-dim); font-size:12px;">Loading…</div>
      </div>

      <h2 style="margin-top:20px;">Recent Audit Events <span class="live-tag">LIVE — TURSO</span></h2>
      <div id="audit-events">
        <div style="color:var(--text-dim); font-size:12px;">Loading…</div>
      </div>
      <div style="margin-top:10px; text-align:right;">
        <a href="/logs" style="font-size:11px; color:var(--gold); text-decoration:none;">View full log →</a>
      </div>
    </div>
  </div>

</div>
<script>
  function pad(n){ return n.toString().padStart(2,'0'); }
  function tick(){
    const d = new Date();
    document.getElementById('clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  setInterval(tick, 1000); tick();

  const STATUS_LABEL = { SUCCESS: 'Settled', PENDING: 'Pending', FAILED: 'Flagged' };
  const STATUS_CLASS = { SUCCESS: 'settled', PENDING: 'pending', FAILED: 'flagged' };

  // plate_number is free text with no charset constraint (operator-entered,
  // no HTML sanitization upstream) — escape before it ever reaches innerHTML.
  // identification_method/payment_status are DB CHECK-constrained today, but
  // escaping them too costs nothing and removes the assumption entirely.
  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function formatTime(sqliteDatetime){
    // core/db.py stores created_at via SQLite's datetime('now'), which is
    // UTC in "YYYY-MM-DD HH:MM:SS" form — append Z so Date parses it as UTC
    // instead of local time.
    const d = new Date(sqliteDatetime.replace(' ', 'T') + 'Z');
    // Escaped even on the fallback path: this string reaches innerHTML at
    // every call site, so an unparseable created_at (data migration
    // artifact, future schema change) must not be able to inject HTML.
    if (isNaN(d.getTime())) return escapeHtml(sqliteDatetime);
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  const METHOD_LABEL = { RFID: 'RFID', ANPR: 'ANPR fallback', NONE: 'Unidentified' };
  const METHOD_COLOR = { RFID: 'var(--good)', ANPR: 'var(--gold)', NONE: 'var(--bad)' };
  const FALLBACK_COLOR = 'var(--text-dim)';

  function renderFeed(rows){
    const tbody = document.getElementById('feed');
    if (!Array.isArray(rows) || rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-dim);">No transactions yet.</td></tr>';
      return;
    }
    tbody.innerHTML = '';
    for (const r of rows) {
      const methodClass = escapeHtml((r.identification_method || '').toLowerCase());
      const statusClass = STATUS_CLASS[r.payment_status] || 'pending';
      const statusLabel = escapeHtml(STATUS_LABEL[r.payment_status] || r.payment_status);
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + formatTime(r.created_at) + '</td>' +
        '<td>' + (r.plate_number ? escapeHtml(r.plate_number) : '—') + '</td>' +
        '<td><span class="method-tag ' + methodClass + '">' + escapeHtml(r.identification_method) + '</span></td>' +
        '<td>GHS ' + Number(r.toll_amount).toFixed(2) + '</td>' +
        '<td><span class="status-pill ' + statusClass + '">' + statusLabel + '</span></td>';
      tbody.appendChild(tr);
    }
  }

  function renderMethodMix(counts){
    const container = document.getElementById('method-mix');
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    if (total === 0) {
      container.innerHTML = '<div style="color:var(--text-dim); font-size:12px;">No transactions yet.</div>';
      return;
    }
    // Iterates every key Turso actually returned (not a hardcoded
    // RFID/ANPR/NONE list) so a value outside that set still gets its own
    // bar instead of silently inflating the other bars' percentage
    // denominator with no visible representation.
    let html = '';
    for (const key of Object.keys(counts)) {
      const c = counts[key] || 0;
      if (c === 0) continue;
      const pct = Math.round((c / total) * 100);
      html +=
        '<div class="bar-row">' +
        '<div class="bar-label">' + escapeHtml(METHOD_LABEL[key] || key) + '</div>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%; background: ' + (METHOD_COLOR[key] || FALLBACK_COLOR) + ';"></div></div>' +
        '<div style="color:var(--text-dim); font-family: var(--mono);">' + pct + '% (' + c + ')</div>' +
        '</div>';
    }
    container.innerHTML = html;
  }

  function renderAuditEvents(events){
    const container = document.getElementById('audit-events');
    if (!events || events.length === 0) {
      container.innerHTML = '<div style="color:var(--text-dim); font-size:12px;">No events yet.</div>';
      return;
    }
    let html = '<table><tbody>';
    for (const e of events) {
      const detail = e.event_detail ? escapeHtml(e.event_detail.slice(0, 60)) : '';
      html +=
        '<tr>' +
        '<td style="width:64px;">' + formatTime(e.created_at) + '</td>' +
        '<td>' + escapeHtml(e.event_type) + '</td>' +
        '<td style="color:var(--text-dim);">' + detail + '</td>' +
        '</tr>';
    }
    html += '</tbody></table>';
    container.innerHTML = html;
  }

  function renderMetricTiles(m){
    document.getElementById('m-total').textContent = m.total_transactions;

    const success = m.status_counts.SUCCESS || 0;
    const failed = m.status_counts.FAILED || 0;
    const pending = m.status_counts.PENDING || 0;
    const resolved = success + failed;
    const rateEl = document.getElementById('m-success-rate');
    const subEl = document.getElementById('m-success-sub');
    if (resolved === 0) {
      rateEl.textContent = '—';
      rateEl.className = 'value';
      subEl.textContent = 'No resolved charges yet';
    } else {
      const rate = (success / resolved) * 100;
      rateEl.textContent = rate.toFixed(1) + '%';
      rateEl.className = 'value ' + (rate >= 95 ? 'good' : 'warn');
      subEl.textContent = success + ' settled / ' + failed + ' failed';
    }

    document.getElementById('m-pending').textContent = pending;
    document.getElementById('m-throughput').innerHTML =
      m.throughput_last_hour + ' <span style="font-size:14px;color:var(--text-dim);">veh/hr</span>';
  }

  // Marks every live-data element as stale/failed at once, rather than
  // just one tile, so a fetch failure is never mistaken for "nothing's
  // happened yet" while other panels keep showing minutes-old numbers.
  function markStale(message){
    for (const id of ['m-total', 'm-success-rate', 'm-pending', 'm-throughput']) {
      document.getElementById(id).textContent = '—';
    }
    document.getElementById('m-success-sub').textContent = message;
    document.getElementById('feed').innerHTML =
      '<tr><td colspan="5" style="color:var(--bad);">' + escapeHtml(message) + '</td></tr>';
    document.getElementById('method-mix').innerHTML =
      '<div style="color:var(--bad); font-size:12px;">' + escapeHtml(message) + '</div>';
    document.getElementById('audit-events').innerHTML =
      '<div style="color:var(--bad); font-size:12px;">' + escapeHtml(message) + '</div>';
  }

  async function refreshDashboard(){
    if (document.visibilityState === 'hidden') return;
    try {
      const res = await fetch('/api/dashboard');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      renderFeed(data.transactions);
      renderMetricTiles(data);
      renderMethodMix(data.method_counts);
      renderAuditEvents(data.recent_events);
    } catch (e) {
      markStale('Failed to load: ' + e);
    }
  }

  refreshDashboard();
  setInterval(refreshDashboard, 5000);
</script>

</body>
</html>
`;

const LOGS_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Toll Ops — Logs</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
${BASE_CSS}
  .log-toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }
  .log-search {
    flex: 1;
    min-width: 220px;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--text);
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    padding: 9px 12px;
  }
  .log-search:focus {
    outline: none;
    border-color: var(--gold-dim);
    box-shadow: 0 0 0 3px rgba(184,121,26,0.12);
  }
  .log-count {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    white-space: nowrap;
  }

  .log-panel {
    padding: 0;
    overflow: hidden;
  }
  .log-list {
    max-height: 72vh;
    overflow-y: auto;
    font-family: var(--mono);
    font-size: 12.5px;
  }
  .log-row {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 9px 16px;
    border-bottom: 1px solid var(--panel-border);
    border-left: 3px solid transparent;
  }
  .log-row:last-child { border-bottom: none; }
  .log-row:hover { background: rgba(26,34,51,0.025); }
  .log-row.sev-good { border-left-color: var(--good); }
  .log-row.sev-warn { border-left-color: var(--gold); }
  .log-row.sev-bad { border-left-color: var(--bad); }
  .log-row.sev-neutral { border-left-color: var(--panel-border); }

  .log-time { color: var(--text-dim); flex-shrink: 0; width: 68px; }
  .log-badge {
    flex-shrink: 0;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.03em;
    padding: 2px 7px;
    border-radius: 3px;
    white-space: nowrap;
  }
  .log-badge.sev-good { background: rgba(30,143,95,0.12); color: var(--good); }
  .log-badge.sev-warn { background: rgba(184,121,26,0.12); color: var(--gold); }
  .log-badge.sev-bad { background: rgba(199,57,47,0.12); color: var(--bad); }
  .log-badge.sev-neutral { background: rgba(102,112,133,0.12); color: var(--text-dim); }
  .log-txn { flex-shrink: 0; color: var(--text-dim); font-size: 11px; width: 44px; }
  .log-detail {
    color: var(--text);
    overflow-wrap: anywhere;
  }
  .log-empty { padding: 40px 16px; text-align: center; color: var(--text-dim); font-family: var(--sans); font-size: 13px; }
</style>
</head>
<body>
<div class="page">

  <div class="header">
    <h1>Smart Cashless Tolling — Audit Console</h1>
    <span class="clock" id="clock">--:--:--</span>
  </div>
  <div class="nav">
    <a href="/">Dashboard</a>
    <a href="/logs" class="active">Logs</a>
  </div>

  <div class="log-toolbar">
    <input class="log-search" id="log-search" type="text" placeholder="Filter by event type or detail…" autocomplete="off">
    <span class="log-count" id="log-count">Loading…</span>
  </div>

  <div class="panel log-panel">
    <div class="log-list" id="log-list">
      <div class="log-empty">Loading…</div>
    </div>
  </div>

</div>
<script>
  function pad(n){ return n.toString().padStart(2,'0'); }
  function tick(){
    const d = new Date();
    document.getElementById('clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  setInterval(tick, 1000); tick();

  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function formatTime(sqliteDatetime){
    const d = new Date(sqliteDatetime.replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return escapeHtml(sqliteDatetime);
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  // Classifies every event_type this system actually emits (core/db.py's log_audit_event
  // calls and workers/charge's audit inserts) into one of four severities for the left-border
  // accent + badge color. Falls back to 'neutral' for anything not listed here, rather than
  // guessing -- an unrecognized event type showing as neutral is honest; showing it as
  // good/bad based on a naming heuristic would not be.
  const SEVERITY = {
    PAYMENT_VERIFIED_SUCCESS: 'good',
    PAYMENT_LINK_SENT: 'good',
    PAYMENT_REMINDER_SENT: 'good',
    TURSO_SYNC_RESTORED: 'good',
    TURSO_SYNC_GAP_RESOLVED: 'good',

    UNKNOWN_RFID_UID: 'warn',
    UNKNOWN_ANPR_PLATE: 'warn',
    DUPLICATE_TOLL_SKIPPED: 'warn',
    PAYMENT_LINK_REISSUED: 'warn',
    TURSO_SYNC_GAP: 'warn',
    TURSO_SYNC_FAILED: 'warn',
    TURSO_REPLICA_RESET: 'warn',
    IDENTIFICATION_FAILED: 'warn',

    PAYMENT_INIT_FAILED: 'bad',
    SMS_NOTIFY_FAILED: 'bad',
    PAYMENT_LINK_EXHAUSTED: 'bad',

    // Deliberately neutral, not 'good' -- confirmed live 2026-09-17 that Arkesel reporting
    // success only means the platform accepted the send, not that it reached the handset.
    SMS_SENT: 'neutral',
  };
  function severityOf(eventType){
    return SEVERITY[eventType] || 'neutral';
  }

  let latestEvents = [];

  function render(){
    const list = document.getElementById('log-list');
    const countEl = document.getElementById('log-count');
    const filter = document.getElementById('log-search').value.trim().toLowerCase();

    const filtered = !filter ? latestEvents : latestEvents.filter((e) =>
      e.event_type.toLowerCase().includes(filter) ||
      (e.event_detail || '').toLowerCase().includes(filter) ||
      String(e.transaction_id || '').includes(filter)
    );

    countEl.textContent = filter
      ? filtered.length + ' of ' + latestEvents.length + ' events'
      : latestEvents.length + ' events (latest first)';

    if (filtered.length === 0) {
      list.innerHTML = '<div class="log-empty">' + (latestEvents.length === 0 ? 'No events yet.' : 'No events match “' + escapeHtml(filter) + '”.') + '</div>';
      return;
    }

    let html = '';
    for (const e of filtered) {
      const sev = severityOf(e.event_type);
      html +=
        '<div class="log-row sev-' + sev + '">' +
        '<span class="log-time">' + formatTime(e.created_at) + '</span>' +
        '<span class="log-badge sev-' + sev + '">' + escapeHtml(e.event_type) + '</span>' +
        '<span class="log-txn">' + (e.transaction_id ? '#' + e.transaction_id : '') + '</span>' +
        '<span class="log-detail">' + (e.event_detail ? escapeHtml(e.event_detail) : '') + '</span>' +
        '</div>';
    }
    list.innerHTML = html;
  }

  async function refreshLogs(){
    if (document.visibilityState === 'hidden') return;
    try {
      const res = await fetch('/api/logs?limit=200');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      latestEvents = data.events || [];
      render();
    } catch (e) {
      document.getElementById('log-count').textContent = 'Failed to load: ' + e;
    }
  }

  document.getElementById('log-search').addEventListener('input', render);

  refreshLogs();
  setInterval(refreshLogs, 5000);
</script>

</body>
</html>
`;
