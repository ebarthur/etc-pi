# Smart Toll — Build Plan

Status (2026-08-15): code-side is done and now running on the actual target hardware (this
Pi). RFID + camera + Turso sync + both Cloudflare Workers are live; a vehicle-presence
trigger (camera-based, since no dedicated sensor exists) gates the whole flow. One real
hardware fault is open (RC522 antenna, see below) and blocks RFID from actually charging
anyone right now. Phase 5 (ANPR) stays blocked on trained models; Phases 6-7 (identity
verification, SMS approval) are explicitly out of scope per this plan's own locked decisions.

Update (2026-08-25): Phase 5 (ANPR) is now wired in — see its section below. The RC522
antenna fault from 2026-08-15 was never a code problem, so this doesn't fix it, but it does
mean the orchestrator is no longer dead in the water while that fault is open: every arrival
that RFID doesn't catch now actually gets read via camera + `anpr/yolov11.py` instead of just
being logged and dropped. Given that fault, ANPR is — in practice, not in the code's
structure — carrying most real identifications right now; `core/main.py`'s docstring covers
why the code still reads RFID-first. Also added in this pass: a per-vehicle repeat-toll
cooldown (`TOLL_REPEAT_COOLDOWN_SECONDS`, `core/db.py`'s `has_recent_transaction()`) since
*nothing* previously stopped the same vehicle being charged twice for one lingering pass, and
env-overridable presence/ANPR tuning knobs so a scaled-down test rig (small RC cars with
printed plates instead of real vehicles) can be retuned without code changes.

Update (2026-08-26): Phase 3's direct-charge design is replaced — see "Phase 3" below,
superseded, not deleted. Live-testing against a real (not sandbox) Paystack account this
session surfaced two problems: a live mobile money charge (`POST /charge`) comes back
`status: "send_otp"`, requiring a customer-side OTP step this codebase never handled, so the
transaction was getting mislabeled `FAILED` when it was really just stuck; and this account
has no webhook access, so `workers/charge`'s webhook receiver could never resolve anything
regardless. Fix: `api_clients/momo.py`'s `initialize_transaction()` now creates a Paystack
hosted-checkout link (`POST /transaction/initialize`) instead of charging directly;
`core/main.py` SMSes that link to the owner to pay on their own time/device/channel
(`api_clients/arkesel.py`'s `send_payment_link_sms()`, replacing the old pre-charge
notification); and `workers/charge` is now a Cloudflare Cron Trigger (every minute, not an
HTTP route) that polls Paystack's verify endpoint for every `PENDING` transaction and
resolves it directly against Turso — one automatic link reissue on failure/abandonment, then
terminal `FAILED`; a reminder SMS (same link) if still genuinely pending after 2 hours.
`core/db.py`'s `transactions` table gained `checkout_url`/`link_issued_at`/
`reminder_sent_at`/`reissue_count` via a new idempotent migration step in `init_db()` (the
first schema change after real data already existed locally and in Turso).

## Next session — pick up here

1. **RFID antenna fault (blocking)** — `TxControlReg` refuses to enable no matter what's
   written to it; every other register works. This is the chip's own antenna
   overcurrent/short protection, not a code or general-wiring issue. Full register-level
   evidence is in "Pi hardware bring-up" below. **Needs physical action from you**: inspect
   the antenna coil trace/solder joints on the RC522 board for damage, or swap in a second
   RC522 module if you can get one, then ask to re-run the same isolated diagnostic script
   (was throwaway code this session — ask to have it promoted to
   `scripts/test_rfid_hardware.py` if it's needed again). Until this is fixed, every real
   vehicle arrival falls through to the ANPR-capture path (which works) — no tag will ever
   be read.
2. **SD card is actively corrupting data (blocking, ongoing risk)** — `dmesg` shows real
   ext4 errors on `mmcblk0p2` (bad block bitmap checksums, a corrupt inode bitmap, failed
   CRC checks). Already caused one full-file garbage-overwrite of an uncommitted `plan.md`
   this session. **Back up `.env` and `db/tolling.db` off this card now if you haven't**,
   and plan to reflash/replace the card — expect more corruption until then. Check
   `git status`/`file <path>` on anything uncommitted before trusting it's intact.
3. **Point Paystack's webhook** (dashboard → Settings → API Keys & Webhooks) at
   `https://smart-toll-charge-webhook.steam67.workers.dev` — dashboard-only, no API for it.
   Then trigger one real mobile money charge and confirm it resolves PENDING -> SUCCESS/FAILED
   with a `WEBHOOK_CHARGE_RESOLVED` audit row.
4. **Rotate the Cloudflare API token** — a real `CLOUDFLARE_API_TOKEN` is sitting in
   plaintext in `~/.bashrc` (and bash history) from an earlier session. Flagged, not
   touched — user said they'd handle it themselves.
5. Once RFID is physically fixed: re-test the full trigger -> RFID -> charge -> SMS chain
   for real (motion trigger + presence debounce are already verified live; only the RFID
   leg of it is currently blocked by the hardware fault above).
6. ~~Phase 5 (ANPR) whenever trained `.pt` models exist~~ — **done 2026-08-25**, see Phase 5
   below. `sensors/presence.py` now runs a two-stream capture (mirroring
   `anpr/live_test.py`'s `RoadsideCamera`): full-res `ANPR_CAPTURE_RESOLUTION` for the models,
   rotated upright via the shared `ANPR_CAPTURE_ROTATION`, separate from the still-low-res
   motion stream.

Everything else that's left needs something only you can provide — see Phase 9 below for
the fuller per-system breakdown.

## Context / decisions this plan assumes

- Primary identification = RFID, structurally: the code checks RFID first because a tag read
  is unambiguous where a plate read is a probabilistic guess. ANPR (`anpr/yolov11.py`) is
  wired in as of 2026-08-25 as the fallback on an RFID timeout — see Phase 5. With the RC522
  antenna fault open (see "Next session" above), it's ANPR that ends up doing most real
  identifications in practice; that's a hardware-availability fact, not a reason to restructure
  the code's priority.
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

## Phase 1 — Core orchestrator (`core/main.py`) — DONE

- [x] Read RFID UID / look up vehicle / compute toll / charge via Paystack / log transaction +
      audit event, all wired up in `core/main.py`.
- [x] Unknown tag and RFID-timeout (no-tag) cases both log a distinct audit event rather than
      crashing or silently dropping (`UNKNOWN_RFID_UID`; an RFID timeout now runs the ANPR
      fallback — see Phase 5, done 2026-08-25 — logging `UNKNOWN_ANPR_PLATE` on an unmatched
      plate read or `IDENTIFICATION_FAILED` if ANPR found nothing usable either).
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

## Phase 2 — Tests — DONE

- [x] Filled in `tests/test_rfid.py` — mocks `MFRC522` (via `tests/conftest.py` stubbing
      `RPi.GPIO`/`mfrc522` in `sys.modules`, since neither is installed on this dev machine);
      covers a successful scan, a timeout, BCC-checksum-byte stripping in `_format_uid`, and
      GPIO cleanup.
- [x] Filled in `tests/test_integration.py` — end-to-end against a throwaway SQLite file
      (`tmp_path`, not `db/tolling.db`) via `handle_uid()`, with Paystack's and Arkesel's HTTP
      calls mocked (both route through the same `requests` module object, so it's one patch
      keyed by URL, not two — a second separate patch would silently clobber the first).
      Covers: known-UID success, unknown UID (audit log only, no transaction row), pending
      charge (left `PENDING` for the Phase 3 webhook), and failed charge.
- [x] Added `requirements-dev.txt` (`-r requirements.txt` + `pytest==9.1.1`, verified live)
      since pytest is a dev-only dependency, not something the Pi needs at runtime.
- [x] Ran the suite locally: `python3 -m pytest tests/test_rfid.py tests/test_integration.py -v`
      — 9 passed.

## Phase 3 — Paystack webhook (`workers/charge`) — SUPERSEDED (2026-08-26)

**See the 2026-08-26 update above.** `workers/charge` no longer receives a webhook at all —
this account has no webhook access, and a live mobile money charge needs a customer-side OTP
step this design never handled. It's now a Cron Trigger polling Paystack's verify endpoint
instead. Left below as a historical record of the original (working, in sandbox) design, not
because any of it still runs.

- [x] Cloudflare Worker (`workers/charge/src/index.ts`) receives Paystack's webhook POST,
      verifies `x-paystack-signature` (HMAC-SHA512 of the *raw* request body, keyed with
      `PAYSTACK_SECRET_KEY`, constant-time compared) before touching anything else. Verified
      live against Paystack's docs that this is HMAC-SHA512, not SHA256, and that
      `charge.failed` is a real webhook event for non-bulk charges (mobile money charges
      qualify) — Paystack does send it, contrary to a common assumption that only
      `charge.success` fires.
- [x] On `charge.success` / `charge.failed`, resolves the matching transaction
      (`WHERE momo_reference = ? AND payment_status = 'PENDING'`) to `SUCCESS`/`FAILED` and
      writes a `WEBHOOK_CHARGE_RESOLVED` audit_log row, via `@libsql/client` talking to Turso
      directly (a Worker has no persistent disk, so this is a plain remote client — no
      embedded replica — matching the plan below). The `PENDING` guard makes webhook
      redelivery a no-op the second time.
- [x] Found and fixed a real gap while wiring this up: `core.main.handle_uid`'s pending-charge
      branch (Phase 1) logged the Paystack reference into `audit_log.event_detail` but never
      wrote it onto the transaction row itself — so the webhook had no `momo_reference` to
      match against. Added `core.db.set_transaction_reference()` and call it from
      `handle_uid()` before the `CHARGE_PENDING` audit log.
- [x] Unmatched/not-yet-synced reference: responds `409` (not `200`) rather than silently
      dropping the resolution — the Pi's background sync (Phase 4) runs on an interval, so a
      webhook can legitimately arrive at Turso before the corresponding local `PENDING` row has
      synced up. A `409` makes Paystack's own webhook retry schedule effectively wait for that
      sync, instead of us needing to build a reconciliation queue.
- [x] `workers/charge/package.json` / `tsconfig.json` / `wrangler.toml` scaffolded (none
      existed before — Phase 0's skeleton only had empty placeholder files). Dependency
      versions verified live against the npm registry, same discipline as `requirements.txt`:
      `@libsql/client@0.17.4`, `wrangler@4.123.0`, `typescript@7.0.2`,
      `@cloudflare/workers-types@5.20260814.1`.
- [x] No Node/npm on this dev machine, so `npm install` / `wrangler dev` / a real deploy
      couldn't be run here — the TypeScript is correct by careful reading and by matching the
      libsql-client-ts source/examples, not by a local build or type-check. Worth doing a real
      `npm install && npm run typecheck` once you're on a machine with Node before deploying.

## Phase 4 — Turso sync (confirmed) — DONE

- [x] `core/db.py` now runs on `libsql` (`pip install libsql==0.1.11`) instead of raw
      `sqlite3`. Public function signatures are unchanged — `get_vehicle_by_rfid`,
      `log_transaction`, `register_vehicle`, etc. all still return the same dict-like rows and
      raise `sqlite3.IntegrityError` on a duplicate `rfid_uid`/`plate_number`, via a thin
      `Row`/`_Cursor`/`_Connection` adapter layer (libsql returns plain tuples and raises plain
      `ValueError` for constraint violations, not `sqlite3.Row`/`sqlite3.IntegrityError` — the
      adapter translates both so nothing above `core/db.py` had to change). Verified against a
      real local `libsql` build: schema creation, inserts, selects, `executemany`,
      `executescript`, `lastrowid`, and the constraint-error translation all behave correctly;
      the full existing pytest suite (9 tests) plus 5 new sync tests (14 total) pass.
- [x] **Real architecture correction found during implementation, not just assumed from
      docs:** `libsql.connect(path, sync_url=..., auth_token=...)` does a *blocking* network
      round trip **at connect time** (confirmed empirically: ~2.8s to fail against an
      unreachable host) and raises if it fails — it does not lazily defer all network activity
      to a later explicit `.sync()` call the way the upstream examples imply. Passing
      `sync_url`/`auth_token` on every `get_connection()` call (the original plan) would have
      meant every single toll transaction did a network call and hard-failed when the Pi was
      offline — exactly backwards from "local-speed, offline-tolerant." Confirmed empirically
      that a file written via plain local connections stays fully intact and readable after a
      synced-connect attempt against it fails. **Superseded — see the Phase 9 update below**:
      the original fix here (`get_connection()` never passes Turso credentials to
      `libsql.connect()` at all; only `_open_synced_connection()`, used by the background sync
      thread, does) turned out to be an overcorrection once tested against a real, reachable
      Turso DB — it avoided the network-blocking problem, but it also meant local writes were
      never visible to a later push sync at all, regardless of how often the background thread
      ran. The real fix keeps `get_connection()` network-safe using `offline=True`, not by
      omitting Turso credentials from it entirely.
- [x] Local writes (`transactions`, `audit_log`) stay fast/offline-tolerant: `get_connection()`
      is 100% local-file, no network, always. `_sync_once()` / the background thread
      (`start_background_sync()`, interval from `TURSO_SYNC_INTERVAL_SECONDS`, default 30s) is
      the only thing that ever touches Turso, and its failures are caught and never propagate —
      confirmed a local write still succeeds and returns instantly even with
      `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` pointed at an unreachable host.
      `init_db()` also does one best-effort synchronous sync attempt at startup (pulls
      canonical `vehicles`/`toll_rates`, pushes anything a prior short-lived process like
      `scripts/register_vehicle.py` left unsynced) before handing off to the background thread.
- [x] Source of truth: `vehicles`/`toll_rates` are managed centrally in Turso (Pi is
      read-mostly); `transactions`/`audit_log` are written locally first and synced up. In
      practice this is a *who-normally-writes-each-table* distinction, not separate sync
      mechanisms — libsql's embedded-replica sync is a whole-file operation, so everything
      syncs together on the same schedule.
- [x] Sync-failure logging is deliberately throttled to protect the SD card (see plan.md's
      hardware note): only the onset and recovery of an outage get an `audit_log` row
      (`TURSO_SYNC_FAILED` / `TURSO_SYNC_RESTORED`), not one row per failed tick — otherwise a
      Pi offline for hours would write one audit row every `TURSO_SYNC_INTERVAL_SECONDS` for
      the whole outage. Verified via `tests/test_db_sync.py`.
- [x] Offline case tested via `tests/test_db_sync.py` (5 new tests) rather than by physically
      pulling network on real hardware (no Pi / no real Turso credentials available in this
      environment): local writes succeed with Turso unconfigured, local writes succeed with
      Turso configured but pointed at an unreachable host, sync failures are tolerated and
      throttled, and the background thread starts only when Turso is actually configured.
      **Not verified here** (no real Turso database/credentials to test against): the actual
      happy-path sync — pulling real remote changes down, or pushing local writes up and
      confirming they land in Turso. Worth a real end-to-end pass once you have a live Turso DB
      — see Phase 9.
- [x] **New build-time gotcha for the Pi, documented in `requirements.txt`:** `libsql` has no
      prebuilt wheel for Linux aarch64 on PyPI (only macOS arm64, Windows, Linux x86_64) —
      confirmed live against PyPI's file listing. `pip install` falls back to a from-source
      build, which needs `cmake` (`sudo apt install cmake`) and a C/C++ toolchain, but does
      *not* need a manually-installed Rust toolchain — `maturin` (libsql's build backend)
      auto-bootstraps one via `puccinialin` into `~/.cache/puccinialin` on first build.
      Confirmed this actually works end-to-end on this machine (itself aarch64 Linux): the
      first build compiles a real dependency tree (axum/tower/prost-style gRPC stack) and
      takes several minutes of real CPU/RAM — budget for that during Pi setup (Phase 8), it's a
      one-time cost.

## Phase 5 — ANPR — DONE (2026-08-25)

**Scope decision, 2026-08-15**: both the plate detector *and* the OCR step will be
custom-trained `.pt` models supplied by the user — not a generic pretrained OCR engine. The
original assumption (YOLOv11 detector + `easyocr` for reading the cropped plate) is dropped;
`easyocr` was removed from `requirements.txt`. `ultralytics` alone covers the detector; the OCR
model turned out to be PARSeq (PyTorch Lightning checkpoint, not YOLO-format) — see
`anpr/parseq_infer.py` and this skill's `.claude/skills/restore-smart-toll` notes on the
2026-08-19 model inspection.

- [x] `anpr/yolov11.py`: `ANPRPipeline` loads the trained YOLOv11n detector (`crop.pt`) via
      `ultralytics` and the PARSeq OCR checkpoint (`ocr.ckpt`) via `anpr/parseq_infer.py`,
      crops each detection, batches the OCR pass. `PlateRead.confidence` combines both models'
      scores; `best_plate()` gates on `ANPR_OCR_MIN_CONFIDENCE` and rejects empty reads.
- [x] Wired into `core/main.py`'s fallback seam from Phase 1: `run_hardware_loop()` captures a
      full-res frame off `sensors.presence.PresenceSensor.capture_frame()` on an RFID timeout,
      runs it through `ANPRPipeline.best_plate()`, and hands a hit to the new `handle_plate()`
      (parallel to `handle_uid()`, both routed through a shared `_charge_vehicle()` so the
      charge/notify/log/cooldown logic can't drift between the two paths). A genuine miss on
      both RFID and ANPR now logs `IDENTIFICATION_FAILED` (was `RFID_TIMEOUT`).
- [x] `ANPR_DETECTOR_CONFIDENCE` / `ANPR_OCR_MIN_CONFIDENCE` (`core/config.py`) — both
      env-overridable now (`ANPR_DETECTOR_CONFIDENCE`, `ANPR_OCR_MIN_CONFIDENCE`) since a
      printed miniature test-rig plate is expected to need different thresholds than whatever
      the real-vehicle models were trained/validated against.
- [x] Captured frames still go to `anpr/captures/` (gitignored) — retention/cleanup policy
      remains an open TODO, not addressed in this pass.
- [x] The known ~90-degree capture rotation is now corrected before frames reach either model
      (`sensors/presence.py`'s `capture_frame()`, `ANPR_CAPTURE_ROTATION` in `core/config.py`,
      `anpr.yolov11.rotate_frame()` shared with `anpr/live_test.py`) — re-run
      `python3 -m anpr.live_test --calibrate` and update `ANPR_CAPTURE_ROTATION` if the camera
      is ever remounted differently.
- [x] New in this pass, not originally scoped here but exposed by wiring ANPR in as the
      fallback that's functionally primary: neither RFID nor ANPR previously had *any*
      protection against charging the same vehicle twice for one lingering pass (a real risk
      once a vehicle can be re-identified by camera alone, and an even more immediate one on a
      test rig where the same RC car laps past the gate repeatedly). Added
      `TOLL_REPEAT_COOLDOWN_SECONDS` (`core/config.py`, default 60s, env-overridable) and
      `core/db.py`'s `has_recent_transaction()` — `core/main.py`'s `_charge_vehicle()` checks
      it before every charge and logs `DUPLICATE_TOLL_SKIPPED` rather than charging again.
      Needed millisecond-resolution `created_at` (`strftime(...,'now')` instead of
      `datetime('now')`) so a short/zero cooldown doesn't misfire on two passes landing in the
      same wall-clock second.
- [ ] Still open: retention/cleanup policy for `anpr/captures/`; real-world confidence-
      threshold tuning against actual roadside (or rig) data via `anpr/live_test.py`; PARSeq's
      trained accuracy is still low (`val_accuracy ~23.8%`) so expect a real miss rate until/
      unless the model gets more training.

## Phase 6 — Identity verification (deferred, out of scope for now)

- [ ] Placeholder only. Decide then whether `workers/verify` replaces the local
      `api_clients/ghana_card.py` mock or the mock stays for dev/test. Wire into `core/main.py`
      as an optional pre-charge check once built.

## Phase 7 — SMS approval (future, explicitly out of scope for now)

- [ ] Placeholder only. Revisit after Phase 1–4 are proven. Likely another Worker + an SMS
      gateway (e.g. Africa's Talking, common for Ghana numbers).

## Phase 8 — Pi deployment hardening — DONE

- [x] `scripts/setup_pi.sh`: apt deps (`cmake`/`build-essential` for libsql's from-source
      build, `python3-picamera2`), enables SPI (`raspi-config nonint do_spi 0`) since the
      RC522 needs it, adds the run user to `gpio`/`spi` groups, creates a
      `--system-site-packages` venv (so it can see apt's `picamera2` while still pip-installing
      everything else — matches the existing comment in `requirements.txt`), and installs +
      enables a `smart-toll.service` systemd unit (`Restart=on-failure`, `RestartSec=5`) for
      `core/main.py`. Idempotent (safe to re-run after a `git pull`). Root-only, syntax-checked
      (`bash -n`) but **not executed** — it does real system-level things (apt, usermod,
      systemctl, a reboot-requiring group change) that only belong on the actual target
      hardware, not this dev machine.
- [x] Logging: no app-level log files by design — `core/main.py` only ever `print()`s, systemd
      captures that into journald, and `setup_pi.sh` caps journald itself
      (`SystemMaxUse=50M`, `SystemMaxFileSize=10M` via a journald.conf.d drop-in) so it can't
      grow unbounded. Avoids hand-rolling log rotation and avoids adding a second thing that
      writes to the SD card — directly the concern this plan's hardware note raised after the
      Phase 0 corruption.

## Phase 9 — Modular external-system testing (yours to run — needs real creds/hardware/Node)

Everything code-side is done (Phases 0-4 and 8). Nothing left in this list can be finished
without something only you can provide: a real secret, a real cloud account, Node/npm, or
physical hardware. Grouped by system so you can tackle (and unblock) them independently —
none of these block each other except where noted.

**Paystack** (mobile money charges) — DONE, tested live 2026-08-15 via the isolated
`scripts/test_paystack_charge.py` (fires one real `charge_toll()` call, no DB/SMS involved;
refuses to run unless `PAYSTACK_SECRET_KEY` looks like `sk_test_...`, so it can't accidentally
fire against a live key).
- [x] Real test key confirmed working in `.env`.
- [x] **Found and fixed a real bug in `api_clients/momo.py`, not just a test-script issue**: the
      required `email` field was built as `f"{phone_number}@smarttoll.local"` — confirmed
      empirically Paystack's email validator rejects `.local` specifically (an IANA-reserved
      special-use TLD, RFC 2606/6761/6762) with `"Invalid Email Address Passed"`, regardless of
      the rest of the address. This would have failed **every single real charge**, not just
      testing. Confirmed it's the reserved-TLD list being checked, not real DNS resolution —
      `.test`/`.invalid`/`localhost` all fail the same way, but an unregistered `.io` domain
      passes fine. Fixed to `@smarttoll.com`.
- [x] Paystack's mobile money sandbox only resolves for its own designated test number, not an
      arbitrary real one — confirmed empirically your real number gets rejected
      (`"Declined. Please use the test mobile money number..."`, HTTP 400,
      `code: unprocessed_transaction`) regardless of provider/amount. Paystack's own docs use
      `0551234987` as the MTN Ghana example; confirmed empirically that number resolves
      immediately to `status: success` (HTTP 200) for every provider code
      (`mtn`/`vodafone`/`airteltigo`) and every amount tried — this sandbox path doesn't
      simulate the OTP/on-device-approval step a real charge goes through, it just resolves
      the designated number straight to success.
- [x] `success` path: confirmed live, real reference returned, `ChargeResult` parsed correctly.
- [x] `failed` path: confirmed live (via the wrong-test-number rejection above) that a
      real HTTP 400/`status: false` response gets parsed into `ChargeResult(success=False,
      status="failed", reference=None, ...)` without crashing. Caveat worth remembering: that
      specific response is a sandbox-only artifact (`type: api_error`,
      `code: unprocessed_transaction`) — Paystack's normal convention for a genuine production
      decline (e.g. insufficient funds) is HTTP 200 with top-level `status: true` and
      `data.status: "failed"` plus a real reference (matching `tests/test_integration.py`'s
      mocked `failed`/`pending` fixtures), which `momo.py` also parses correctly via its normal
      (non-early-return) path — just not something reachable live from this sandbox to confirm
      against a real response.
- [ ] `pending` path: **not confirmed live** — the designated test number always resolves
      instantly to `success` (see above) regardless of provider or amount; found no documented
      way to force a `pending` outcome from Paystack's mobile money sandbox (their test-payments
      docs page 403s to automated fetches; no community source documents one either). Real
      mobile money charges are commonly asynchronous per Paystack's own docs ("the value of
      `data.status` is `pending`" describes live-mode behavior), which is the entire reason
      Phase 3's webhook exists — but the sandbox's one designated test number appears to skip
      that intermediate state entirely, likely because it has no real device to send an
      OTP/approval prompt to. Coverage for this path stays at the mocked-HTTP level
      (`tests/test_integration.py::test_pending_charge_leaves_transaction_pending`), which is
      the standard way to test something a sandbox doesn't expose a live trigger for.
- [ ] Full end-to-end via `core.main` (register a vehicle, then `python3 -m core.main --uid
      TESTUID01`) not yet run — only the isolated `charge_toll()` call has been tested live so
      far, per this session's "isolate each component, permission before each" approach.

**Arkesel** (SMS toll-passage notification) — DONE, tested live 2026-08-15 via the isolated
`scripts/test_arkesel_sms.py` (sends one real SMS, no DB/Paystack involved).
- [x] Real key confirmed working in `.env`.
- [x] First attempt used the code default `ARKESEL_SENDER_ID=SmartToll` — API returned
      `success` but no SMS ever arrived. **Unapproved Arkesel sender IDs fail silently**
      (accepted by the API, never actually delivered) rather than erroring — worth remembering
      if a future sender ID swap seems to "work" per the API response but nothing shows up.
- [x] Retried with `ARKESEL_SENDER_ID=Adeton` (borrowed from another account) — delivered
      successfully, confirmed received. **Follow-up for you:** register your own sender ID with
      Arkesel before this goes further than testing; `Adeton` isn't yours.
- [x] `send_sms()`'s response-shape assumptions (`data.get("status") == "success"`) confirmed
      correct against a real response — no code fix needed here.

**Turso** (background DB sync) — DONE, tested live 2026-08-15 via the isolated
`scripts/test_turso_sync.py` (round-trips a throwaway vehicle through a real push sync and an
independent replica, then deletes it). Found and fixed two real bugs along the way that no
prior testing (offline-only, against unreachable/bogus hosts) could have caught:
- [x] **Bug 1 — fresh DB files could never be synced at all.** `init_db()` created the local
      file via a plain connection (`get_connection()`) before ever attempting a synced
      connection. libsql's embedded-replica sync only writes its metadata sidecar
      (`<DB_PATH>-info`) on a file's *first-ever* connection if that connection is a synced
      one — retrofitting it onto an already-plain-created file fails permanently with
      `invalid local state: db file exists but metadata file does not`. Fixed: `init_db()` now
      opens one synced (non-offline) bootstrap connection and calls `.sync()` on it *before*
      any plain connection touches a brand-new `DB_PATH` — see `core.db._has_replica_metadata`
      and the comment block above `init_db()`.
- [x] **Bug 2 — local writes were never actually pushed to Turso, ever, even when reachable.**
      The Phase 4 design (`get_connection()` **never** passes Turso credentials to
      `libsql.connect()`, on the theory that a later synced connection's `.sync()` would pick
      up whatever the plain connection had written to the shared file) doesn't hold: confirmed
      empirically that `.sync()` on a connection that was never itself Turso-aware for its
      writes does not push those writes — a fresh, independent replica confirmed the remote
      genuinely never received them. libsql-python's `connect()` has an `offline=True` option
      (undocumented in the upstream examples this project was originally built from) that is
      the actual mechanism for "write locally at local speed, push later": confirmed
      empirically it's local-speed and network-safe at both connect time and write time, even
      against an unreachable host — but *only* once replica metadata already exists (Bug 1) —
      and that a *separate* `offline=True` connection's `.sync()` **does** pick up and push
      writes made by a different `offline=True` connection on the same file. Fixed:
      `get_connection()` now opens with `offline=True` (plus the Turso credentials) whenever
      Turso is configured and replica metadata exists, and the background sync thread's
      `_open_synced_connection()` was split into a non-offline variant (bootstrap only) and a
      new `_open_offline_connection()` (the periodic push/pull). A non-offline synced
      connection was ruled out for the hot path on its own separate grounds too — confirmed
      empirically it proxies every write to the remote primary at ~3-4s per write, even when
      reachable.
- [x] Verified end-to-end against the real database (`etc-ebarthur.aws-us-west-2.turso.io`):
      register a vehicle locally, force a push sync, confirm it lands via a totally independent
      replica file, delete it, sync the delete. All 14 pytest tests still pass afterward
      (`tests/test_integration.py`/`tests/test_db_sync.py` now explicitly blank Turso config
      before calling `init_db()`, since real credentials in `.env` would otherwise make this
      "offline, deterministic" suite silently reach out to the real Turso DB on every run).
- [ ] Not yet done: pulling the network (or blocking Turso's hostname) mid-run on real hardware
      to confirm the offline-then-reconnect story end-to-end outside of the mocked-host pytest
      coverage — lower priority now that the actual push mechanism is confirmed correct.

**Cloudflare Worker** (`workers/charge` — Paystack webhook). Node/npm are now available on this
dev machine (they weren't when this was first built) — DEPLOYED 2026-08-15:
- [x] `cd workers/charge && npm install && npm run typecheck` — passes clean, no errors.
- [x] Secrets set (`PAYSTACK_SECRET_KEY`/`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`) and
      `wrangler deploy` run — live at `https://smart-toll-charge-webhook.steam67.workers.dev`.
      **Real gotcha hit during this deploy**: `.env`'s values are double-quoted
      (`KEY="value"`); Python's `python-dotenv` strips the quotes automatically, but a naive
      shell `cut -d= -f2-` extraction (used to pipe values into `wrangler secret put` without
      ever printing them) does not — the first deploy attempt set `TURSO_DATABASE_URL` to the
      literal string `"libsql://...io"`, quote characters included, and every Turso query
      failed with `URL_INVALID`. Fixed by stripping surrounding quotes before piping into
      `wrangler secret put`; re-set all three secrets, confirmed clean afterward.
- [x] Live sanity checks: `GET` → `405`; `POST` with no/bad signature → `401` (signature
      verification confirmed working against the deployed instance, not just locally).
- [ ] Point Paystack's webhook URL (dashboard → Settings → API Keys & Webhooks) at
      `https://smart-toll-charge-webhook.steam67.workers.dev` — **your step, dashboard-only,
      no public API for it**. Then trigger a real mobile money test charge, confirm the
      transaction resolves from `PENDING` to `SUCCESS`/`FAILED` and a `WEBHOOK_CHARGE_RESOLVED`
      audit_log row appears.

**Cloudflare Worker** (`workers/dashboard` — ops dashboard) — DEPLOYED 2026-08-15:
- [x] Secrets set (`DASHBOARD_PASSWORD`/`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`, same
      quote-stripping fix as above) and deployed — live at
      `https://smart-toll-dashboard.steam67.workers.dev`.
- [x] Live sanity checks: no auth → `401`; wrong password → `401`; correct password → `200`
      and `/api/transactions` returns real (currently empty — no live transactions yet) JSON
      from Turso, not an error.
- [x] Code review pass on `workers/dashboard/src/index.ts` (the one file from this session that
      hadn't gone through one yet) found and fixed a real **stored XSS**: `plate_number` (no DB
      CHECK constraint, operator-suppliable via `scripts/register_vehicle.py --plate`) was
      concatenated unescaped into the feed table's `innerHTML` on every 5s poll — a malicious
      plate string would execute JS in every authenticated viewer's browser. Fixed with an
      `escapeHtml()` helper applied to all DB-sourced fields rendered into the DOM. Also fixed:
      a missing-secret crash (undefined `DASHBOARD_PASSWORD` threw instead of a clean `401`),
      a case-sensitive Basic-auth scheme check (RFC 7235 allows lowercase `basic`), a new Turso
      client being built on every single poll instead of reused, a render-blocking Google Fonts
      `@import`, and unconditional polling when the browser tab isn't visible.

## Code review pass — 2026-08-15 — DONE

Ran a full review of everything touched this session (the Turso/libSQL persistence layer, the
Worker, config, tests) before moving on to Paystack live testing. Found and fixed 5 real issues,
verified against the pytest suite (still 14/14) and a real Turso round-trip afterward (still
works):
- [x] `core/config.py`: `TURSO_SYNC_INTERVAL_SECONDS` crashed the whole app at import time if
      the env var was set-but-blank (`float("")`) — the adjacent Turso vars treat blank as
      "disabled," so this was an inconsistent, surprising trap. Fixed with `... or "30"`.
- [x] `workers/charge/src/index.ts`: the `PENDING`-only idempotency guard was checked in a
      `SELECT` but not atomically with the later `UPDATE` — two concurrent webhook redeliveries
      could both pass the check and both write, double-logging `WEBHOOK_CHARGE_RESOLVED` (or
      worse, letting a late `FAILED` clobber an already-committed `SUCCESS`). Fixed by moving
      the `PENDING` check into the `UPDATE`'s `WHERE` clause and only inserting the audit row
      if `rowsAffected` confirms this request's write actually won.
- [x] `.gitignore`: `db/*.db` didn't match libsql's `-info`/`-wal`/`-shm` sidecar files, so
      local replica sync state could get accidentally `git add -A`'d. Fixed with `db/*.db-*`.
- [x] `core/db.py`: `_Connection.executemany()` didn't translate constraint-violation
      `ValueError`s to `sqlite3.IntegrityError` the way `execute()` does, breaking the class's
      own documented contract (latent — nothing hits this path today, but would silently break
      error handling for any future `executemany()` caller expecting the same behavior every
      other write path gets). Fixed to match `execute()`.
- [x] `tests/test_integration.py`: `test_pending_charge_leaves_transaction_pending` never
      asserted `momo_reference` actually got written — the one thing `core.main`'s pending
      branch added specifically so the webhook could match on it later was untested. Added the
      assertion.

**RC522 + Raspberry Pi** (physical hardware — nothing above this line needs it; dev-mode
`--uid` covers everything else)
- [ ] `sudo ./scripts/setup_pi.sh` on the actual Pi (not this dev machine) — installs deps,
      enables SPI, sets up the `smart-toll` systemd service. Then `sudo reboot` (group
      membership needs a fresh login).
- [ ] With the RC522 wired up: `sudo systemctl start smart-toll`, then
      `journalctl -u smart-toll -f` — physically tap a registered tag, confirm it charges; tap
      an unregistered tag, confirm the RFID-timeout path logs cleanly after ~2s; `sudo systemctl
      stop smart-toll`, confirm no GPIO cleanup error in the log.

## Ops dashboard (`workers/dashboard`) — 2026-08-15 — DEPLOYED, fully live

Wasn't part of the original phased plan (`workers/dashboard/` was just an empty `.gitkeep`
placeholder from the initial skeleton) — started from a layout mockup the user provided,
initially with only the transaction feed live and everything else mock (per an explicit choice
at the time), then the user asked for every mock value removed. Live at
`https://smart-toll-dashboard.steam67.workers.dev`.

- [x] Gated behind HTTP Basic Auth (`DASHBOARD_PASSWORD` secret, constant-time compared) on
      every request — this Worker serves real customer data (phone numbers via `vehicles`, plate
      numbers) to whoever can reach the URL, unlike `workers/charge` which only ever receives
      Paystack's own signed webhook calls.
- [x] **Everything live now, single `GET /api/dashboard` call** (merged from what were
      originally two separate endpoints, `/api/transactions` + `/api/metrics`, to halve
      per-poll request volume — a code-review finding): transaction feed (`transactions` LEFT
      JOIN `vehicles`, last 20), payment status breakdown → Total Transactions / Payment Success
      Rate / Pending Charges tiles, Detection Method Mix (real `GROUP BY identification_method`
      counts — currently 100% RFID since ANPR doesn't exist, which is now an honest live number
      instead of a mocked-in 87/13 split), Throughput (real count of transactions in the last
      hour), and a new **Recent Audit Events** panel (`audit_log`, last 8) that replaced the old
      "Payment Gateway Health" panel — that one had no real signal to show (no uptime monitoring
      of Paystack/Arkesel exists) and would have just been a second mock panel wearing a
      different label.
- [x] **Two tiles removed entirely, not faked as empty**: Detection Accuracy (no ground-truth
      data exists anywhere to compare a detection against — there's nothing to compute, not even
      in principle, without adding a whole separate verification mechanism) and Mean Latency
      (`core/main.py` doesn't record per-transaction timing anywhere). The footer note explains
      both, so their absence reads as "not tracked," not as a bug.
- [x] Verified against the real empty database: `GET /api/dashboard` (authed) returns
      `{"transactions":[],"total_transactions":0,"status_counts":{},"method_counts":{},"throughput_last_hour":0,"recent_events":[]}`
      — clean, no errors — confirming every query handles the zero-rows case correctly, matching
      "everything live even if it's empty."
- [x] Code review before this second deploy (separate from the first pass) found and fixed:
      a **stored XSS** in the very first live-data version (unescaped `plate_number` in the feed,
      caught before that version ever shipped); after the metrics rewrite, a second pass found
      two more spots where escaping had been missed on new code (`refreshFeed`'s catch-block
      error text, `formatTime`'s invalid-date fallback) and fixed both; a `renderMethodMix` bug
      where a hardcoded `['RFID','ANPR','NONE']` list would've silently under-represented the mix
      if `identification_method` ever held any other value (fixed to iterate whatever keys Turso
      actually returns); a `refreshMetrics` failure path that only reset one of six live elements
      on fetch failure, leaving the rest showing stale numbers with no indication (fixed with a
      single `markStale()` that clears every live element together); a stale top-of-file doc
      comment claiming panels were still mock after they'd been wired live (updated); and added a
      guard inside `getTursoClient()` itself so a future call site can't skip the
      Turso-configured check and permanently poison a Worker isolate's cached client.
- [x] The Turso-credential-rotation trade-off noted right after the first deploy got a real fix,
      not just a documented workaround: `getTursoClient()` now keys its cached client on the
      actual `url+token` pair (not just "have we built one yet"), so a rotated
      `TURSO_AUTH_TOKEN` takes effect on the very next request instead of requiring a manual
      `wrangler deploy` to force isolate recycling. Redeployed and reverified live afterward
      (auth still gates correctly, `/api/dashboard` still returns clean empty JSON).
- [ ] Future, not started: once ANPR (Phase 5) exists, Method Mix will show real non-zero ANPR
      percentages with no code changes needed — the query is already correct for that case.

## Real end-to-end pass + `core.main --uid` sync gap — 2026-08-15 — DONE

Full chain run for real for the first time (previously only the isolated `charge_toll()` call
had been live-tested): registered a real test vehicle
(`--phone 0551234987 --rfid-uid TESTUID01 --plate GT-9999-26`, Paystack's designated test
number), ran `python3 -m core.main --uid TESTUID01`, got a real `SUCCESS` charge. Both the
`vehicles` row and the resulting `transactions` row were independently confirmed present on the
real Turso database via a separate replica connection — not just "no error was printed."

- [x] **Real gap found**: the vehicle synced to Turso, but the transaction initially didn't —
      `python3 -m core.main --uid <UID>` is a one-shot process; `init_db()`'s startup sync runs
      *before* the transaction is created, and the process exits well before the background
      thread's first `TURSO_SYNC_INTERVAL_SECONDS` tick (default 30s — daemon threads are
      killed outright on interpreter exit, not given a chance to finish). The transaction sat
      local-only until a later process's own startup sync happened to sweep it up. This only
      affects the `--uid` dev-mode testing path, not the real hardware loop (`run_hardware_loop`
      stays alive, so the background thread runs normally there) — but it meant every dev-mode
      test run needed a manual follow-up sync to actually show up on Turso/the dashboard.
- [x] **Fixed** in `core/main.py`: one explicit best-effort `_sync_once()` call added in two
      places — right after `--uid` mode's `handle_uid()` call before the process exits, and in
      `run_hardware_loop()`'s `finally` block after `reader.cleanup()` (so a graceful
      `systemctl stop`/restart on the Pi doesn't leave up to ~30s of the most recent activity
      unsynced either, even though the background thread already covers the common case there).
      Safe unconditionally — `_sync_once()` no-ops without Turso configured and never raises if
      unreachable. All 14 tests still pass.
- [x] Confirmed both sync directions work, not just the push side already exercised by
      `scripts/test_turso_sync.py`: wrote a row directly to remote Turso (bypassing local
      entirely, same effect as editing it in Turso's web console) and confirmed a local
      `get_vehicle_by_rfid()` call found it after a sync — i.e. registering a vehicle via
      Turso's own console instead of `scripts/register_vehicle.py` is a legitimate second path,
      as long as `vehicle_type` is kept to one of `car`/`suv`/`bus`/`truck` (the `toll_rates`
      foreign key enforces this locally, but a console-side insert won't be caught until the
      toll-rate lookup runs).
- [x] **A file corruption incident happened and was resolved** during this work: a background
      agent's edit to `workers/dashboard/src/index.ts` introduced a single literal NUL byte
      into the `getTursoClient()` cache-key line (`${env.TURSO_DATABASE_URL}\0${env.TURSO_AUTH_TOKEN}`
      instead of a space-separated string), which made `git`/`file` classify the whole file as
      binary. Fixed by replacing the NUL byte with the originally-intended space and
      reverifying: valid UTF-8 again, `tsc --noEmit` clean, redeployed, live endpoints
      reverified. Worth remembering if this ever recurs: check for embedded NUL bytes
      specifically (`data.count(b'\x00')` in Python) before assuming a "binary" git diff on a
      source file means something more exotic.

## Pi hardware bring-up — 2026-08-15 — service running, real corruption diagnosed

Continuing Phase 8/9 on the actual target hardware (this box is now the Pi itself, per
`git clone git@github.com:ebarthur/etc-pi.git etc` in shell history — `setup_pi.sh` had
already been run: SPI enabled, `gpio`/`spi` groups present, `smart-toll.service` installed
and enabled).

- [x] **Root-caused a second, more severe corruption**: this file's uncommitted working copy
      (and, separately, an unrelated `~/.claude` memory file on the same disk) had been fully
      overwritten with high-entropy random bytes — not a single stray NUL byte like the
      `index.ts` incident above, the *entire* file. `dmesg` shows why: the root filesystem
      (`mmcblk0p2`, the SD card) has active `EXT4-fs error`s — bad block bitmap checksums, a
      corrupt inode bitmap, and failed CRC checks — a genuine hardware/storage fault, not
      anything an edit did. This matches the Phase 0 note above about prior SD corruption on
      this same box. Fixed by restoring `plan.md` from the last clean commit (`9af7968`); the
      corrupted portion was unreadable garbage, nothing recoverable was lost. **Not fixed**:
      the underlying SD card fault itself — `fsck` on a live root fs was ruled out (same reason
      as the Phase 0 note), so it's still throwing CRC errors on writes. Recommend backing up
      `.env`/`db/tolling.db`/anything uncommitted and planning to reflash/replace the card;
      until then, expect more corruption like this.
- [x] **`smart-toll.service` was crash-looping** (~99 restarts, `ModuleNotFoundError: No
      module named 'libsql'`): the systemd unit's venv (`venv/`, `--system-site-packages`,
      created by `setup_pi.sh`) never actually had `pip install -r requirements.txt` completed
      — missing `mfrc522`, `libsql`, `opencv-python-headless` was shadowed by ultralytics'
      newer `opencv-python` pull, etc. (A separate, older `.venv/` from earlier dev-machine
      testing did have everything, but isn't what the systemd unit points at.) Stopped the
      service first to stop hammering the already-faulting card with a restart every ~5s, then
      ran `venv/bin/pip install -r requirements.txt` (a cached `libsql` wheel meant no
      multi-minute source build was actually needed this time) and restarted the service —
      confirmed `active (running)`, no further crashes.
- [ ] **RFID hardware fault found, not yet resolved**: an actual physical RFID tag scan against
      the running hardware loop was attempted with both tags from the kit (the card and the blue
      keyfob) — neither was ever detected, 0 hits across 500+ `MFRC522_Request(PICC_REQIDL)`
      polls with a tag held directly against the reader. Root-caused via direct register access
      (bypassing `rfid/reader.py`, talking to the `mfrc522` library directly):
        - Basic SPI communication is fine — `VersionReg` (0x37) reads a consistent `0xB2` across
          15 back-to-back reads, and an arbitrary scratch register (`ModeWidthReg`, 0x24)
          correctly writes and reads back a test pattern (`0x55`). So this isn't a general
          MISO/MOSI/SCK/CS wiring problem, and reads aren't the issue.
        - `TxControlReg` (0x14, the antenna-driver-enable register) is the one exception: it
          reads `0x80` (antenna drivers off) both before and *after* an explicit direct write of
          `0x83` (the value `AntennaOn()` is supposed to set) — the write to this one specific
          register silently doesn't take, while every other register write does. This is the
          RC522's own overcurrent/short-circuit protection on the antenna output (TX1/TX2)
          refusing to enable — not a code bug (`rfid/reader.py`/`mfrc522`'s `AntennaOn()` logic
          is correct; it's the chip refusing at the hardware level) and not a general wiring
          bug (everything else on the SPI bus works).
        - The board's red LED being lit is not evidence against this — on this class of cheap
          RC522 breakout it's just a power-present indicator, not an antenna/RF-status light,
          and doesn't change the register-level finding above.
      **Likely cause**: a fault in the antenna itself. On this board style the antenna is a
      printed copper coil on the PCB (not a separate/detachable module), so the next physical
      checks are: visible damage/crack/burn on the coil trace, a cold solder joint near it, or
      — most likely for a low-cost clone board — a dead unit. **Parked pending physical
      inspection or a second RC522 module to swap in and re-test against** (same isolated
      register-level script, not yet turned into a permanent `scripts/` file since it was
      throwaway diagnostic code — worth promoting to `scripts/test_rfid_hardware.py` if this
      kind of check is needed again).
- [x] **Camera checked — works.** `Picamera2.global_camera_info()` detects an IMX708 (Camera
      Module 3) sensor cleanly, and a real still capture (`create_still_configuration()` +
      `capture_file()`) succeeded at full native resolution (4608x2592), verified as a valid,
      openable JPEG (`PIL.Image.verify()`), not a stub/corrupt file. Unrelated to the RFID
      fault above — this is a separate camera on a separate interface (CSI, not SPI), and it's
      fine. **One thing to fix before Phase 5 (ANPR) starts**: the captured image comes out
      rotated ~90° from upright based on how the module is physically mounted, even though the
      sensor's own `Rotation: 180` metadata doesn't reflect that — the capture config will need
      an explicit rotation/transform (or a physical remount) once real plate-detection frames
      matter; not a blocker today since Phase 5 is still waiting on the user's trained models.
- [ ] Not yet done: pointing Paystack's webhook at the deployed Worker (Phase 9, dashboard-only
      step) and the SD card replacement/reflash above.

## Vehicle-presence trigger (`sensors/presence.py`) — 2026-08-15 — DONE, verified live

Gap identified while testing RFID above: `run_hardware_loop()` free-polled the RC522 every
`RFID_TIMEOUT_SECONDS` (2s) forever, with no concept of "a vehicle actually arrived" — if
ANPR fallback were wired into the old `handle_no_tag()` as-is, it would've fired on every
idle poll, camera running/inferencing around the clock for no reason. No dedicated presence
hardware (IR break-beam, ultrasonic, inductive loop) is available and there wasn't time to
source one, so the already-working camera doubles as the trigger instead, via frame-differencing.

- [x] **`core/config.py`**: added `PRESENCE_*` constants. Threshold picked from a real
      measured baseline on this hardware, not guessed — 20 consecutive frames of a static
      scene at (640, 480) gave a mean-abs-pixel-diff noise floor of ~2.2-3.3 (0-255 scale);
      `PRESENCE_MOTION_THRESHOLD=10.0` sits ~3x above that. `PRESENCE_SUSTAIN_FRAMES=3` /
      `PRESENCE_CLEAR_FRAMES=5` debounce single-frame noise on arrival/departure.
- [x] **`sensors/presence.py`** (new): `PresenceSensor` with a hardware-agnostic interface
      (`wait_for_vehicle()` / `wait_until_clear()` / `capture_fallback_frame()` /
      `cleanup()`) so a real presence sensor could swap in later without touching
      `core/main.py`. Diffs consecutive low-res (640x480) grayscale frames against each
      other, not against a fixed baseline — deliberately: a vehicle that arrives and then
      stops should still register via the *arrival* motion (multiple consecutive changing
      frames while it's still moving into position), not via a persistent large diff from
      some fixed reference, which would never settle back to "clear" while the vehicle
      just sits there. Documented limitation: this only works if the vehicle is still
      moving across at least `PRESENCE_SUSTAIN_FRAMES` poll cycles (~0.45s at defaults)
      during approach — true for realistic vehicle speeds, but an extremely slow creep
      could miss it. A real presence sensor wouldn't have this limitation if it's ever
      worth swapping in.
- [x] **`core/main.py`** restructured: `run_hardware_loop()` now blocks on
      `presence.wait_for_vehicle()` before opening the RFID window, and on a timeout
      (no tag) captures a real fallback frame to `anpr/captures/<timestamp>.jpg` and passes
      the path into `handle_no_tag()` for the audit log — actual ANPR inference still isn't
      wired in (Phase 5 still blocked on the user's trained models), but the capture point
      now exists and is exercised for real. `presence.wait_until_clear()` debounces re-arming
      after each vehicle.
- [x] **Tests**: `tests/test_presence.py` (5 tests) against a mocked `Picamera2` — frame-diff
      math runs for real (real numpy), only the camera hardware is faked, matching the
      existing `tests/test_rfid.py` pattern. `tests/conftest.py` extended to stub
      `picamera2` the same way `RPi.GPIO`/`mfrc522` already were. `numpy==2.2.4` added to
      `requirements.txt` as a direct dependency (was only ever transitive before, via
      opencv/ultralytics) and installed into both venvs. All 19 tests pass (14 previous + 5
      new).
- [x] **Verified live end-to-end**, not just unit-tested: measured real frame-diff noise on
      this exact camera over 15s idle (stayed 2.6-3.65, well under the 10.0 threshold — no
      false triggers at rest), then ran the actual `smart-toll.service` with the new code,
      waved a hand in front of the camera, and confirmed via `journalctl` that it triggered,
      opened the RFID window, timed out (RFID hardware still faulty, see above), and saved a
      real fallback capture — independently reverified that file with `PIL.Image.verify()`
      (`640x480`, valid JPEG, not a stub). Idle behavior changed too: the service no longer
      logs anything at all while genuinely idle (no vehicle present), versus the old
      every-2-seconds `RFID_TIMEOUT` spam.
