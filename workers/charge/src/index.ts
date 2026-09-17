/**
 * workers/charge — Paystack checkout-status poller (replaces the old webhook receiver).
 *
 * core/main.py no longer charges a vehicle owner directly: it creates a Paystack
 * hosted-checkout link (POST /transaction/initialize) and texts it to them to pay whenever
 * they're ready, on whatever device/channel they pick. Nothing on the Pi resolves that
 * payment — this account has no webhook access, and a live mobile money charge needs a
 * customer-side OTP step this codebase never handled anyway. So instead, this Worker runs on
 * a Cloudflare Cron Trigger once a minute: pull every transaction still PENDING with a
 * checkout reference, verify each against Paystack (GET /transaction/verify/:reference), and
 * act on the result --
 *
 *   success            -> mark the transaction SUCCESS and text the customer a payment-received
 *                         confirmation.
 *   failed / abandoned -> Paystack can report this transiently mid-flow -- e.g. a momo charge
 *                         that's still waiting on the customer's approval prompt -- and it can
 *                         still resolve to success on the *same* reference a minute later. So
 *                         this is NOT acted on immediately: only once the current link
 *                         (link_issued_at) is >= 2 hours old does it count as truly dead --
 *                         at that point, reissue ONE fresh checkout link (a new
 *                         /transaction/initialize call) and text it; a second
 *                         failure/abandonment (still >= 2 hours old) marks the transaction
 *                         terminally FAILED instead of reissuing again. Younger than 2 hours,
 *                         just keep polling the same reference next tick -- no action, no text.
 *   anything else       -> still genuinely pending. If the *current* link
 *   (pending/processing)  (link_issued_at) is >= 2 hours old and no reminder has gone out
 *                         yet for it, text a reminder with the same link.
 *
 * All three outcomes are written straight to Turso (same as the old webhook did) — this
 * Worker never depends on the Pi being online. `link_issued_at`/`reminder_sent_at`/
 * `reissue_count` all key off the *current* link, and reset together on a reissue, so a
 * fresh link gets its own full 2-hour reminder window rather than the reminder firing the
 * instant it goes out.
 */

import { createClient, type Client } from "@libsql/client";

export interface Env {
  PAYSTACK_SECRET_KEY: string;
  ARKESEL_API_KEY: string;
  ARKESEL_SENDER_ID: string;
  TOLL_GATE_NAME: string;
  TURSO_DATABASE_URL: string;
  TURSO_AUTH_TOKEN: string;
}

interface PendingRow {
  transaction_id: number;
  momo_reference: string;
  // Nullable in the schema, but always set together with momo_reference (both
  // core.db.set_transaction_reference and this file's own reissue path write them in the
  // same UPDATE) -- the WHERE momo_reference IS NOT NULL filter below means checkout_url is
  // never actually null on a row this query returns.
  checkout_url: string;
  toll_amount: number;
  reissue_count: number;
  link_issued_at: string;
  reminder_sent_at: string | null;
  phone_number: string;
  plate_number: string | null;
}

// DEMO OVERRIDE (2026-09-17): 2 minutes instead of the production value of 2 hours, so the
// reminder/reissue path can be shown live at tomorrow's defense instead of waiting 2 hours.
// reminder_sent_at/reissue_count already guard both paths to fire exactly once per link, so
// this doesn't cause repeats -- it only changes how soon that one firing happens. REVERT TO
// `2 * 60 * 60 * 1000` AFTER THE DEMO -- a real customer should not get a reminder text 2
// minutes after the original payment link.
const REMINDER_AFTER_MS = 2 * 60 * 1000; // 2 minutes -- DEMO ONLY, see comment above
const MAX_REISSUES = 1;

// --- Paystack ---

async function verifyTransaction(
  env: Env,
  reference: string,
): Promise<{ status: string } | null> {
  const res = await fetch(
    `https://api.paystack.co/transaction/verify/${encodeURIComponent(reference)}`,
    { headers: { Authorization: `Bearer ${env.PAYSTACK_SECRET_KEY}` } },
  );
  const body = await res.json<{ status: boolean; data?: { status?: string } }>();
  if (!body.status || !body.data?.status) return null;
  return { status: body.data.status };
}

interface InitializeResult {
  authorization_url: string;
  reference: string;
}

// Mirrors api_clients/momo.py's initialize_transaction -- same endpoint, same body shape
// (amount as a String, metadata as a JSON.stringify()'d string per Paystack's docs), run
// independently here since this Worker never depends on the Pi being reachable.
async function initializeTransaction(
  env: Env,
  phoneNumber: string,
  amountGhs: number,
): Promise<InitializeResult | null> {
  const amountPesewas = Math.round(amountGhs * 100);
  const res = await fetch("https://api.paystack.co/transaction/initialize", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.PAYSTACK_SECRET_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      email: `${phoneNumber}@smarttoll.com`,
      amount: String(amountPesewas),
      currency: "GHS",
      metadata: JSON.stringify({ phone_number: phoneNumber, reissued: true }),
    }),
  });
  const body = await res.json<{
    status: boolean;
    data?: { authorization_url?: string; reference?: string };
  }>();
  if (!body.status || !body.data?.authorization_url || !body.data?.reference) return null;
  return { authorization_url: body.data.authorization_url, reference: body.data.reference };
}

// --- Arkesel ---

// Mirrors api_clients/arkesel.py's _to_international -- same normalization, reimplemented
// here since this Worker runs independently of the Pi's Python process.
function toInternational(phoneNumber: string): string {
  const digits = phoneNumber.trim().replace(/^\+/, "");
  return digits.startsWith("0") ? `233${digits.slice(1)}` : digits;
}

interface SmsResult {
  success: boolean;
  detail: string;
  messageId?: string;
}

// Previously fire-and-forget (no return value, nothing checked at any call site) -- confirmed
// live that a "payment received" SMS can silently never reach the customer with zero trace
// anywhere (not console, not audit_log) once Arkesel's own response isn't inspected. Mirrors
// api_clients/arkesel.py's SmsResult shape so a failure is at least as visible here as it
// already is on the Pi side.
//
// Note the real limitation this doesn't close: Arkesel returning status:"success" only means
// the platform accepted/queued the message (confirmed live 2026-09-17 -- a "payment received"
// text got a clean success response, sms_balance decremented normally, and still never reached
// the handset). It is not a delivery receipt. Capturing `messageId` here is what makes that
// gap diagnosable after the fact -- see sendSmsAndLog below.
async function sendSms(env: Env, phoneNumber: string, message: string): Promise<SmsResult> {
  try {
    const res = await fetch("https://sms.arkesel.com/api/v2/sms/send", {
      method: "POST",
      headers: { "api-key": env.ARKESEL_API_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({
        sender: env.ARKESEL_SENDER_ID,
        message,
        recipients: [toInternational(phoneNumber)],
      }),
    });
    const body = await res
      .json<{ status?: string; message?: string; data?: { id?: string }[] }>()
      .catch(() => ({}) as { status?: string; message?: string; data?: { id?: string }[] });
    if (body.status !== "success") {
      return { success: false, detail: `HTTP ${res.status}: ${JSON.stringify(body)}` };
    }
    return { success: true, detail: body.message ?? "success", messageId: body.data?.[0]?.id };
  } catch (e) {
    return { success: false, detail: String(e) };
  }
}

// Logs on BOTH outcomes now, not just failure -- a successful send is still only "Arkesel
// accepted it," not proof of arrival (see sendSms's docstring above), so the message ID is
// worth having in audit_log/the web /logs page for every send, to look up against Arkesel's
// own delivery report when a "didn't arrive" question comes up again.
async function sendSmsAndLog(
  turso: Client,
  env: Env,
  transactionId: number,
  phoneNumber: string,
  message: string,
): Promise<void> {
  const result = await sendSms(env, phoneNumber, message);
  if (result.success) {
    await turso.execute({
      sql: "INSERT INTO audit_log (transaction_id, event_type, event_detail) VALUES (?, 'SMS_SENT', ?)",
      args: [transactionId, `sms_id=${result.messageId ?? "unknown"}`],
    });
    return;
  }
  console.error(`SMS failed for transaction_id=${transactionId}: ${result.detail}`);
  await turso.execute({
    sql: "INSERT INTO audit_log (transaction_id, event_type, event_detail) VALUES (?, 'SMS_NOTIFY_FAILED', ?)",
    args: [transactionId, result.detail.slice(0, 500)],
  });
}

function vehicleRef(plateNumber: string | null): string {
  return plateNumber ?? "Your vehicle";
}

// --- Turso helpers ---

// Same idempotent-guard pattern the old webhook used: the UPDATE's own WHERE
// payment_status = 'PENDING' means a redelivered/overlapping run that already lost the race
// just gets rowsAffected = 0 and skips the audit write, rather than double-logging. Returns
// whether this call actually applied the change, so callers can gate a one-shot side effect
// (e.g. the payment-received SMS) on having genuinely won that race.
async function markResolved(
  turso: Client,
  transactionId: number,
  status: "SUCCESS" | "FAILED",
  eventType: string,
  detail: string,
): Promise<boolean> {
  const result = await turso.execute({
    sql: "UPDATE transactions SET payment_status = ? WHERE transaction_id = ? AND payment_status = 'PENDING'",
    args: [status, transactionId],
  });
  if (result.rowsAffected === 0) return false;
  await turso.execute({
    sql: "INSERT INTO audit_log (transaction_id, event_type, event_detail) VALUES (?, ?, ?)",
    args: [transactionId, eventType, detail],
  });
  return true;
}

function isOlderThan(sqliteDatetimeUtc: string, ms: number): boolean {
  // core/db.py's strftime('%Y-%m-%d %H:%M:%f', 'now') produces "YYYY-MM-DD HH:MM:SS.mmm" in
  // UTC with no timezone marker -- same format the dashboard Worker already parses this way.
  const then = new Date(sqliteDatetimeUtc.replace(" ", "T") + "Z").getTime();
  return Date.now() - then >= ms;
}

async function handleTerminalFailure(
  turso: Client,
  env: Env,
  row: PendingRow,
  failureStatus: string,
): Promise<void> {
  if (row.reissue_count < MAX_REISSUES) {
    const fresh = await initializeTransaction(env, row.phone_number, row.toll_amount);
    if (!fresh) return; // couldn't get a new link this tick -- retry next minute, nothing to update yet

    const result = await turso.execute({
      sql: `UPDATE transactions
            SET momo_reference = ?, checkout_url = ?,
                link_issued_at = strftime('%Y-%m-%d %H:%M:%f', 'now'),
                reminder_sent_at = NULL, reissue_count = reissue_count + 1
            WHERE transaction_id = ? AND payment_status = 'PENDING'`,
      args: [fresh.reference, fresh.authorization_url, row.transaction_id],
    });
    if (result.rowsAffected === 0) return; // resolved by a concurrent run in the meantime

    await sendSmsAndLog(
      turso,
      env,
      row.transaction_id,
      row.phone_number,
      `Your toll payment link for ${vehicleRef(row.plate_number)} expired or failed. ` +
        `Here's a new link to pay your GHS ${row.toll_amount.toFixed(2)} toll for ` +
        `${env.TOLL_GATE_NAME}: ${fresh.authorization_url}`,
    );
    await turso.execute({
      sql: "INSERT INTO audit_log (transaction_id, event_type, event_detail) VALUES (?, ?, ?)",
      args: [
        row.transaction_id,
        "PAYMENT_LINK_REISSUED",
        `previous ${failureStatus} (ref=${row.momo_reference}) -> new ref=${fresh.reference}`,
      ],
    });
    return;
  }

  await markResolved(
    turso,
    row.transaction_id,
    "FAILED",
    "PAYMENT_LINK_EXHAUSTED",
    `second link also ${failureStatus} (ref=${row.momo_reference}) -- giving up`,
  );
}

async function maybeSendReminder(turso: Client, env: Env, row: PendingRow): Promise<void> {
  if (row.reminder_sent_at) return; // already reminded for this link
  if (!isOlderThan(row.link_issued_at, REMINDER_AFTER_MS)) return;

  // Claim the reminder before sending it: guards the same overlapping-run race as
  // markResolved above, just on reminder_sent_at instead of payment_status.
  const result = await turso.execute({
    sql: `UPDATE transactions SET reminder_sent_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
          WHERE transaction_id = ? AND payment_status = 'PENDING' AND reminder_sent_at IS NULL`,
    args: [row.transaction_id],
  });
  if (result.rowsAffected === 0) return;

  await sendSmsAndLog(
    turso,
    env,
    row.transaction_id,
    row.phone_number,
    `Reminder: ${vehicleRef(row.plate_number)}'s GHS ${row.toll_amount.toFixed(2)} toll for ` +
      `${env.TOLL_GATE_NAME} is still unpaid. Pay here: ${row.checkout_url}`,
  );
  await turso.execute({
    sql: "INSERT INTO audit_log (transaction_id, event_type, event_detail) VALUES (?, ?, ?)",
    args: [row.transaction_id, "PAYMENT_REMINDER_SENT", `ref=${row.momo_reference}`],
  });
}

// Sequential, not Promise.all: keeps this friendly to Paystack's rate limits and easy to
// reason about at prototype volume -- a few dozen pending transactions a minute is not a
// throughput problem worth the added complexity of a concurrency limit.
async function resolveAll(turso: Client, env: Env, rows: PendingRow[]): Promise<void> {
  for (const row of rows) {
    await resolveOne(turso, env, row);
  }
}

async function resolveOne(turso: Client, env: Env, row: PendingRow): Promise<void> {
  let verified: { status: string } | null;
  try {
    verified = await verifyTransaction(env, row.momo_reference);
  } catch (e) {
    console.error(`verify failed for transaction_id=${row.transaction_id}: ${e}`);
    return; // never mark anything on a failed *check* -- just retry next tick
  }
  if (!verified) return;

  if (verified.status === "success") {
    const applied = await markResolved(
      turso,  
      row.transaction_id,
      "SUCCESS",
      "PAYMENT_VERIFIED_SUCCESS",
      `ref=${row.momo_reference}`,
    );
    if (applied) {
      await sendSmsAndLog(
        turso,
        env,
        row.transaction_id,
        row.phone_number,
        `Payment received for ${vehicleRef(row.plate_number)}'s GHS ${row.toll_amount.toFixed(2)} toll ` +
          `at ${env.TOLL_GATE_NAME}. Safe travels!`,
      );
    }
    return;
  }

  if (verified.status === "failed" || verified.status === "abandoned") {
    // Not necessarily really over -- e.g. a momo charge can sit "abandoned" while the
    // customer is still completing an approval prompt, then flip to "success" on this same
    // reference a minute later. Only escalate once the link's genuinely been outstanding for
    // 2 hours; otherwise just leave it alone and re-verify the same reference next tick.
    if (!isOlderThan(row.link_issued_at, REMINDER_AFTER_MS)) return;
    await handleTerminalFailure(turso, env, row, verified.status);
    return;
  }

  // pending / processing / anything else Paystack might return -- still genuinely waiting.
  await maybeSendReminder(turso, env, row);
}

// Cloudflare Cron Triggers are minute-granularity by platform design -- `[triggers] crons` in
// wrangler.toml is standard 5-field cron syntax with no seconds field, so "every 15 seconds"
// isn't expressible as a trigger directly. Instead the trigger stays at the platform floor (once
// a minute) and this polls internally every POLL_INTERVAL_MS, TICKS_PER_MINUTE times, so the
// effective resolution cadence is 15s without needing a Durable Object alarm (a real fix for
// production, but more infra than a demo needs). Each tick is a handful of fetch() calls plus a
// couple of small Turso queries -- negligible CPU time, so the wall-clock sleeps between ticks
// don't threaten the Worker's CPU-time budget on any plan.
const POLL_INTERVAL_MS = 15_000;
const TICKS_PER_MINUTE = 4;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function tick(env: Env): Promise<void> {
  const turso = createClient({ url: env.TURSO_DATABASE_URL, authToken: env.TURSO_AUTH_TOKEN });

  const pending = await turso.execute({
    sql: `SELECT t.transaction_id, t.momo_reference, t.checkout_url, t.toll_amount,
                 t.reissue_count, t.link_issued_at, t.reminder_sent_at,
                 v.phone_number, v.plate_number
          FROM transactions t
          JOIN vehicles v ON v.vehicle_id = t.vehicle_id
          WHERE t.payment_status = 'PENDING' AND t.momo_reference IS NOT NULL`,
    args: [],
  });

  await resolveAll(turso, env, pending.rows as unknown as PendingRow[]);
}

async function pollForOneMinute(env: Env): Promise<void> {
  for (let i = 0; i < TICKS_PER_MINUTE; i++) {
    try {
      await tick(env);
    } catch (e) {
      console.error(`tick failed: ${e}`); // one bad tick shouldn't stop the remaining ticks
    }
    if (i < TICKS_PER_MINUTE - 1) await sleep(POLL_INTERVAL_MS);
  }
}

export default {
  // No HTTP surface any more -- this Worker is cron-only now. Kept only so a stray request
  // (e.g. someone hitting the old webhook URL) gets a clean 404 instead of an error.
  async fetch(): Promise<Response> {
    return new Response("Not found", { status: 404 });
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(pollForOneMinute(env));
  },
};
