/**
 * workers/charge — Paystack webhook receiver (plan.md Phase 3).
 *
 * Paystack calls this on charge status changes. Mobile money charges are
 * frequently asynchronous (core/main.py leaves them `PENDING` and records
 * the Paystack reference via core.db.set_transaction_reference), so this
 * is what actually resolves a transaction to SUCCESS/FAILED.
 *
 * Every request is verified against `x-paystack-signature` before its body
 * is trusted (HMAC-SHA512 of the raw request body, keyed with the Paystack
 * secret key — see https://paystack.com/docs/payments/webhooks/). Requests
 * that fail verification are rejected outright; the DB is never touched.
 */

import { createClient } from "@libsql/client";

export interface Env {
  PAYSTACK_SECRET_KEY: string;
  TURSO_DATABASE_URL: string;
  TURSO_AUTH_TOKEN: string;
}

// Paystack events this worker acts on. Everything else (transfer.*,
// subscription.*, etc.) is acknowledged with 200 and ignored, so Paystack
// doesn't keep retrying deliveries we were never going to handle.
const RESOLVED_STATUS: Record<string, "SUCCESS" | "FAILED"> = {
  "charge.success": "SUCCESS",
  "charge.failed": "FAILED",
};

async function verifySignature(rawBody: string, signature: string | null, secretKey: string): Promise<boolean> {
  if (!signature) return false;

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secretKey),
    { name: "HMAC", hash: "SHA-512" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(rawBody));
  const expected = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");

  // Constant-time compare — signature and expected are both fixed-length
  // (128 hex chars for SHA-512) hex strings, so a length check up front
  // doesn't leak timing information about the content.
  if (expected.length !== signature.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= expected.charCodeAt(i) ^ signature.charCodeAt(i);
  }
  return diff === 0;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const rawBody = await request.text();
    const signature = request.headers.get("x-paystack-signature");
    if (!(await verifySignature(rawBody, signature, env.PAYSTACK_SECRET_KEY))) {
      return new Response("Invalid signature", { status: 401 });
    }

    let event: { event?: string; data?: { reference?: string; status?: string } };
    try {
      event = JSON.parse(rawBody);
    } catch {
      return new Response("Invalid JSON", { status: 400 });
    }

    const newStatus = event.event ? RESOLVED_STATUS[event.event] : undefined;
    const reference = event.data?.reference;
    if (!newStatus || !reference) {
      // Not an event we act on (or malformed payload for one we do) —
      // 200 so Paystack doesn't retry something that will never change.
      return new Response("Ignored", { status: 200 });
    }

    const turso = createClient({
      url: env.TURSO_DATABASE_URL,
      authToken: env.TURSO_AUTH_TOKEN,
    });

    // Only resolve a transaction still PENDING: makes redelivery of the
    // same webhook (Paystack retries on non-2xx, or can double-send) a
    // no-op the second time round, rather than re-triggering audit_log
    // writes for an already-resolved transaction.
    const found = await turso.execute({
      sql: "SELECT transaction_id FROM transactions WHERE momo_reference = ? AND payment_status = 'PENDING'",
      args: [reference],
    });

    if (found.rows.length === 0) {
      // Genuinely-unknown reference, or (more likely on a Pi with
      // intermittent connectivity) core.db's background sync hasn't yet
      // pushed this transaction's row up to Turso. Respond non-2xx so
      // Paystack's own retry schedule gives that sync time to catch up,
      // rather than silently dropping the resolution.
      return new Response("Transaction not found or already resolved", { status: 409 });
    }

    const transactionId = found.rows[0].transaction_id;

    // Re-check payment_status = 'PENDING' in the UPDATE itself, not just the
    // earlier SELECT: two redeliveries of the same webhook (or a genuine
    // charge.success/charge.failed race) can both pass that SELECT before
    // either write commits. Only the request whose UPDATE actually matches a
    // row proceeds to the audit_log insert, so redelivery is a true no-op
    // (one resolution, one audit row) instead of a race that can double-log
    // or let a late FAILED clobber an already-committed SUCCESS.
    const updateResult = await turso.execute({
      sql: "UPDATE transactions SET payment_status = ? WHERE transaction_id = ? AND payment_status = 'PENDING'",
      args: [newStatus, transactionId],
    });

    if (updateResult.rowsAffected === 0) {
      return new Response("Already resolved", { status: 200 });
    }

    await turso.execute({
      sql: "INSERT INTO audit_log (transaction_id, event_type, event_detail) VALUES (?, ?, ?)",
      args: [transactionId, "WEBHOOK_CHARGE_RESOLVED", `${event.event} -> ${newStatus} (ref=${reference})`],
    });

    return new Response("OK", { status: 200 });
  },
};
