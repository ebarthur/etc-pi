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
 *   success            -> mark the transaction SUCCESS.
 *   failed / abandoned -> reissue ONE fresh checkout link (a new /transaction/initialize
 *                         call) and text it; a second failure/abandonment marks the
 *                         transaction terminally FAILED instead of reissuing again.
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

const REMINDER_AFTER_MS = 2 * 60 * 60 * 1000; // 2 hours
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

async function sendSms(env: Env, phoneNumber: string, message: string): Promise<void> {
  await fetch("https://sms.arkesel.com/api/v2/sms/send", {
    method: "POST",
    headers: { "api-key": env.ARKESEL_API_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({
      sender: env.ARKESEL_SENDER_ID,
      message,
      recipients: [toInternational(phoneNumber)],
    }),
  });
}

function vehicleRef(plateNumber: string | null): string {
  return plateNumber ?? "Your vehicle";
}

// --- Turso helpers ---

// Same idempotent-guard pattern the old webhook used: the UPDATE's own WHERE
// payment_status = 'PENDING' means a redelivered/overlapping run that already lost the race
// just gets rowsAffected = 0 and skips the audit write, rather than double-logging.
async function markResolved(
  turso: Client,
  transactionId: number,
  status: "SUCCESS" | "FAILED",
  eventType: string,
  detail: string,
): Promise<void> {
  const result = await turso.execute({
    sql: "UPDATE transactions SET payment_status = ? WHERE transaction_id = ? AND payment_status = 'PENDING'",
    args: [status, transactionId],
  });
  if (result.rowsAffected === 0) return;
  await turso.execute({
    sql: "INSERT INTO audit_log (transaction_id, event_type, event_detail) VALUES (?, ?, ?)",
    args: [transactionId, eventType, detail],
  });
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

    await sendSms(
      env,
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

  await sendSms(
    env,
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
    await markResolved(
      turso,
      row.transaction_id,
      "SUCCESS",
      "PAYMENT_VERIFIED_SUCCESS",
      `ref=${row.momo_reference}`,
    );
    return;
  }

  if (verified.status === "failed" || verified.status === "abandoned") {
    await handleTerminalFailure(turso, env, row, verified.status);
    return;
  }

  // pending / processing / anything else Paystack might return -- still genuinely waiting.
  await maybeSendReminder(turso, env, row);
}

export default {
  // No HTTP surface any more -- this Worker is cron-only now. Kept only so a stray request
  // (e.g. someone hitting the old webhook URL) gets a clean 404 instead of an error.
  async fetch(): Promise<Response> {
    return new Response("Not found", { status: 404 });
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
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

    ctx.waitUntil(resolveAll(turso, env, pending.rows as unknown as PendingRow[]));
  },
};
