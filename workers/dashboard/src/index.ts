/**
 * workers/dashboard — Toll Ops audit dashboard.
 *
 * Serves a single-page HTML dashboard, laid out from a mockup the user
 * provided. Only the "Live Transaction Feed" panel (GET /api/transactions)
 * is wired to real data — it queries Turso directly for recent
 * transactions/vehicles. Every other panel (Detection Accuracy, Revenue
 * Leakage, Mean Latency, Throughput, Method Mix, Payment Gateway Health)
 * stays on the mockup's illustrative mock data on purpose: this system
 * doesn't compute or track any of those anywhere (ANPR itself doesn't
 * exist yet, so a real "method mix" would just be 100% RFID), so wiring
 * them to "real" data would mean inventing metrics, not just plumbing.
 * The page's footer note says so explicitly, so this is never misread as
 * live.
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
  const header = request.headers.get("Authorization");
  if (!header || !header.startsWith("Basic ")) return false;
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

async function fetchRecentTransactions(env: Env): Promise<TransactionRow[]> {
  const turso = createClient({ url: env.TURSO_DATABASE_URL, authToken: env.TURSO_AUTH_TOKEN });
  const result = await turso.execute({
    sql: `SELECT t.created_at, v.plate_number, t.identification_method, t.toll_amount, t.payment_status
          FROM transactions t
          LEFT JOIN vehicles v ON v.vehicle_id = t.vehicle_id
          ORDER BY t.created_at DESC
          LIMIT 20`,
    args: [],
  });
  // Built explicitly as plain objects (not the raw Row array-like values)
  // so the JSON response has real field names regardless of how the
  // libsql client's Row type serializes by default.
  return result.rows.map((row) => ({
    created_at: row.created_at as string,
    plate_number: (row.plate_number as string | null) ?? null,
    identification_method: row.identification_method as string,
    toll_amount: row.toll_amount as number,
    payment_status: row.payment_status as string,
  }));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!isAuthorized(request, env)) return unauthorized();

    const url = new URL(request.url);

    if (url.pathname === "/api/transactions") {
      if (!env.TURSO_DATABASE_URL || !env.TURSO_AUTH_TOKEN) {
        return new Response(JSON.stringify({ error: "Turso not configured" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      try {
        const rows = await fetchRecentTransactions(env);
        return new Response(JSON.stringify(rows), {
          headers: { "Content-Type": "application/json" },
        });
      } catch (e) {
        return new Response(JSON.stringify({ error: String(e) }), {
          status: 502,
          headers: { "Content-Type": "application/json" },
        });
      }
    }

    if (url.pathname === "/") {
      return new Response(DASHBOARD_HTML, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    return new Response("Not found", { status: 404 });
  },
};

const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Toll Ops — Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap');

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

  .header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 20px;
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

  .panel {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 6px;
    padding: 16px 18px;
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

  .footer-note {
    margin-top: 20px;
    font-size: 11px;
    color: var(--text-dim);
    font-family: var(--mono);
  }
</style>
</head>
<body>

  <div class="header">
    <h1>Smart Cashless Tolling — Audit Console</h1>
    <span class="clock" id="clock">--:--:--</span>
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
      <div class="label">Detection Accuracy</div>
      <div class="value good">99.12%</div>
      <div class="sub">Target ≥ 98% (mock)</div>
    </div>
    <div class="metric">
      <div class="label">Revenue Leakage</div>
      <div class="value good">0.41%</div>
      <div class="sub">Target &lt; 0.5% (mock)</div>
    </div>
    <div class="metric">
      <div class="label">Mean Latency</div>
      <div class="value good">312 ms</div>
      <div class="sub">Target &lt; 500 ms (mock)</div>
    </div>
    <div class="metric">
      <div class="label">Throughput</div>
      <div class="value warn">1,240 <span style="font-size:14px;color:var(--text-dim);">veh/hr</span></div>
      <div class="sub">Current lane load (mock)</div>
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
      <h2>Detection Method Mix (mock)</h2>
      <div class="bar-row">
        <div class="bar-label">RFID</div>
        <div class="bar-track"><div class="bar-fill" style="width:87%; background: var(--good);"></div></div>
        <div style="color:var(--text-dim); font-family: var(--mono);">87%</div>
      </div>
      <div class="bar-row">
        <div class="bar-label">ANPR fallback</div>
        <div class="bar-track"><div class="bar-fill" style="width:13%; background: var(--gold);"></div></div>
        <div style="color:var(--text-dim); font-family: var(--mono);">13%</div>
      </div>

      <h2 style="margin-top:20px;">Payment Gateway Health (mock)</h2>
      <div class="bar-row">
        <div class="bar-label">MoMo API</div>
        <div class="bar-track"><div class="bar-fill" style="width:98%; background: var(--good);"></div></div>
        <div style="color:var(--text-dim); font-family: var(--mono);">98%</div>
      </div>
      <div class="bar-row">
        <div class="bar-label">Ghana Card API</div>
        <div class="bar-track"><div class="bar-fill" style="width:95%; background: var(--good);"></div></div>
        <div style="color:var(--text-dim); font-family: var(--mono);">95%</div>
      </div>
    </div>
  </div>

  <div class="footer-note">
    Live Transaction Feed reads real data from Turso (transactions joined with vehicles).
    Every other panel (Detection Accuracy, Revenue Leakage, Mean Latency, Throughput, Method
    Mix, Payment Gateway Health) is illustrative mock data — this system doesn't compute or
    track those anywhere yet.
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

  function formatTime(sqliteDatetime){
    // core/db.py stores created_at via SQLite's datetime('now'), which is
    // UTC in "YYYY-MM-DD HH:MM:SS" form — append Z so Date parses it as UTC
    // instead of local time.
    const d = new Date(sqliteDatetime.replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return sqliteDatetime;
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  async function refreshFeed(){
    const tbody = document.getElementById('feed');
    try {
      const res = await fetch('/api/transactions');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const rows = await res.json();
      if (!Array.isArray(rows) || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-dim);">No transactions yet.</td></tr>';
        return;
      }
      tbody.innerHTML = '';
      for (const r of rows) {
        const methodClass = (r.identification_method || '').toLowerCase();
        const statusClass = STATUS_CLASS[r.payment_status] || 'pending';
        const statusLabel = STATUS_LABEL[r.payment_status] || r.payment_status;
        const tr = document.createElement('tr');
        tr.innerHTML =
          '<td>' + formatTime(r.created_at) + '</td>' +
          '<td>' + (r.plate_number || '—') + '</td>' +
          '<td><span class="method-tag ' + methodClass + '">' + r.identification_method + '</span></td>' +
          '<td>GHS ' + Number(r.toll_amount).toFixed(2) + '</td>' +
          '<td><span class="status-pill ' + statusClass + '">' + statusLabel + '</span></td>';
        tbody.appendChild(tr);
      }
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--bad);">Failed to load: ' + e + '</td></tr>';
    }
  }

  refreshFeed();
  setInterval(refreshFeed, 5000);
</script>

</body>
</html>
`;
