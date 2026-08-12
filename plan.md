# Smart Toll — Build Plan

Status: decisions locked in below — ready to build, starting Phase 0.

## Context / decisions this plan assumes

- Primary identification = RFID. ANPR is a fallback, **not being built yet** (models not
  trained). The orchestrator should leave a clean seam for it, not build around it.
- Payment = Paystack Charge API, mobile_money channel (`api_clients/momo.py`, already written).
- MoMo charges are often asynchronous (`pending` -> resolved later) — we need a webhook to
  get the final status, not just trust the synchronous response.
- SMS approval / speed-warning SMS = explicitly future work. Not in scope until the core
  loop (identify -> charge -> log) is proven.
- Identity verification (`api_clients/ghana_card.py` / `workers/verify`) is **deferred
  entirely** — not even the local mock is wired in for now. Core loop is RFID -> vehicle
  lookup -> charge, nothing else. Revisit as its own phase later.
- DB: Turso (libSQL) **embedded replica** mode, confirmed — local-speed reads/writes on the
  Pi with background sync to the cloud. See Phase 4.
- Vehicle registration: a manual script (`scripts/register_vehicle.py`) — no dashboard/UI
  dependency for now.
- Paystack test credentials: confirmed available, already expected in local `.env`.
- Hardware target is a Raspberry Pi — every phase should favor low CPU/RAM/SD-card-write
  footprint over cleverness. (We already found real SD card corruption on this box — see
  Phase 0. Whatever we do here, minimize gratuitous disk writes, especially logging.)

---

## Phase 0 — Housekeeping (small, do first) — DONE

- [x] Regenerate Ghana Card mock fixtures. The old `api_clients/mock_data/` directory turned
      out to be corrupted deeper than expected — unreadable *and* undeletable (`e2fsck`
      required, not attempted on a live root fs). Routed around it instead: fixtures now live
      at `api_clients/fixtures/ghana_cards.json`, `ghana_card.py`'s `DATA_PATH` updated, dead
      directory gitignored so it stays out of the way.
- [x] Pinned `requirements.txt` to exact current PyPI versions (verified live, not guessed).
      Added `RPi.GPIO==0.7.1` explicitly (was an implicit transitive dep before, but
      `rfid/reader.py` imports it directly). Noted `picamera2` is best installed via apt
      (`python3-picamera2`) on real Pi OS, not pip, since it binds against system libcamera.
- [x] `.env` created from `.env.example`, confirmed gitignored. **Still needs your real
      Paystack test secret key pasted in locally** — placeholder is in there now.
- [x] `scripts/register_vehicle.py` written and smoke-tested (insert + duplicate-rejection).
      Surfaced a real schema gap along the way: `vehicles` had no `phone_number` column, but
      `momo.charge_toll()` needs one and we deferred the Ghana Card lookup (the only other
      phone source) — added `phone_number TEXT NOT NULL` to `vehicles` in `core/db.py` plus a
      new `register_vehicle()` helper (no DB file existed yet, so this was a clean schema
      edit, not a migration).

## Manual verification checklist (yours to run later)

Not run as part of an automated suite yet — Phase 2 and a full end-to-end pass are being
deferred until more of the system exists, then tested together in one go. These are the
commands to hand-verify Phase 0/1 individually whenever you get to it, from the repo root:

- [ ] Put your real Paystack test secret key in `.env` (`PAYSTACK_SECRET_KEY=sk_test_...`).
- [ ] Register a test vehicle (use a real Paystack MoMo test number for `--phone`):
      `python3 -m scripts.register_vehicle --phone <momo-test-number> --rfid-uid TESTUID01 --plate GT-0000-26 --owner "Test Owner" --type car`
- [ ] Charge it: `python3 -m core.main --uid TESTUID01` — expect `charge success`.
- [ ] Unknown tag: `python3 -m core.main --uid NOTREGISTERED` — expect `Unknown tag: ...`.
- [ ] Inspect what landed in the DB:
      `sqlite3 db/tolling.db "SELECT * FROM vehicles; SELECT * FROM transactions; SELECT * FROM audit_log;"`
- [ ] On the real Pi, with the RC522 wired up and deps installed
      (`pip install -r requirements.txt`): `python3 -m core.main` (no `--uid`) — physically tap
      a registered tag, confirm it charges; tap an unregistered tag, confirm the RFID-timeout
      path logs cleanly after ~2s; Ctrl+C, confirm it exits without a GPIO cleanup error.
- [ ] Wipe test data when done: `rm -rf db/` (gitignored, safe to delete anytime).

## Phase 1 — Core orchestrator (`core/main.py`) — DONE

- [x] Read RFID UID / look up vehicle / compute toll / charge via Paystack / log transaction +
      audit event, all wired up in `core/main.py`.
- [x] Unknown tag and RFID-timeout (no-tag) cases both log a distinct audit event
      (`UNKNOWN_RFID_UID`, `RFID_TIMEOUT`) rather than crashing or silently dropping. The
      ANPR fallback call site is a comment + audit log in `handle_no_tag()`, not a speculative
      stub function — nothing to call yet.
- [x] `pending` Paystack responses leave the transaction as `PENDING` (DB default) rather than
      calling `update_transaction_status` — Phase 3's webhook is what resolves those.
- [x] Dev-mode `--uid` flag added so this runs without RC522 hardware; the hardware import
      (`rfid.reader.RFIDReader`) is deferred inside `run_hardware_loop()` so `--uid` mode works
      even without `RPi.GPIO`/`mfrc522` installed (neither is on this machine yet).
- [x] Found and fixed two real gaps while wiring this up:
      1. `python-dotenv` was a dependency but nothing called `load_dotenv()` — `.env` was
         silently ignored. Fixed in `core/config.py`.
      2. Smoke-tested end to end: registered a test vehicle, scanned its UID, confirmed a
         `FAILED` transaction + matching audit log row when charging with the still-placeholder
         Paystack key (got a real "Invalid key" response back — confirms the request path
         works, just needs your real key in `.env`). Also verified the unknown-tag and
         RFID-timeout paths log correctly. All smoke-test DB rows were then wiped
         (`db/tolling.db` is local/gitignored scratch data, not meant to persist).

## Phase 2 — Tests

- [ ] Fill in `tests/test_rfid.py` — mock `MFRC522`, test timeout behavior and UID formatting.
- [ ] Fill in `tests/test_integration.py` — end-to-end against a throwaway SQLite file (not the
      real `db/tolling.db`), mocking Paystack's HTTP call.

## Phase 3 — Paystack webhook (`workers/charge`)

- [ ] Cloudflare Worker receives Paystack's webhook POST, verifies the signature
      (`x-paystack-signature` HMAC against the secret key — do not skip signature verification).
- [ ] On `charge.success` / `charge.failed`, update the matching transaction's status.
- [ ] Depends on Phase 4's decision: if Turso is in, the Worker writes directly to Turso and
      the Pi picks it up on next sync; if not, we need a different path for the Worker (which
      has no route to a Pi behind NAT) to reach the local DB — flag this if you skip Phase 4.

## Phase 4 — Turso sync (confirmed)

- [ ] Create Turso DB, set up embedded-replica sync (`libsql` client) in `core/db.py`,
      replacing the raw `sqlite3` connection — keep the existing schema/functions API so
      nothing above this layer has to change.
- [ ] Local writes (transactions, audit log) stay fast/offline-tolerant; sync happens in the
      background.
- [ ] Decide source of truth per table: `vehicles`/`toll_rates` likely canonical in Turso
      (managed centrally, Pi is a read-mostly replica); `transactions`/`audit_log` written
      locally first, synced up.
- [ ] Test the offline case explicitly: pull the Pi's network, confirm a charge still gets
      logged locally and syncs once connectivity returns.

## Phase 5 — ANPR (once models exist)

- [ ] `anpr/yolov11.py`: load the trained `.pt` (YOLOv11 plate detector) via `ultralytics`,
      crop plate region, feed to the OCR model.
- [ ] Wire into `core/main.py`'s existing fallback seam from Phase 1 — triggered when RFID
      read times out.
- [ ] Confidence threshold below which we don't trust the plate match (config value, tune
      later against real data).
- [ ] Captured frames go to `anpr/captures/` (already gitignored) — decide a retention/cleanup
      policy so this doesn't slowly fill the SD card.

## Phase 6 — Identity verification (deferred, out of scope for now)

- [ ] Placeholder only. Decide then whether `workers/verify` replaces the local
      `api_clients/ghana_card.py` mock or the mock stays for dev/test. Wire into `core/main.py`
      as an optional pre-charge check once built.

## Phase 7 — SMS approval (future, explicitly out of scope for now)

- [ ] Placeholder only. Revisit after Phase 1–4 are proven. Likely another Worker + an SMS
      gateway (e.g. Africa's Talking, common for Ghana numbers).

## Phase 8 — Pi deployment hardening

- [ ] `scripts/setup_pi.sh`: install deps, GPIO permissions for the RC522, systemd service
      for `main.py` with auto-restart.
- [ ] Logging: rotate/cap log files — the SD card corruption we found on this box is a good
      reminder that unbounded local logging is a real risk, not a hypothetical.
