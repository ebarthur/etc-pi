# Smart Toll — Build Plan

Status: everything buildable without real credentials/hardware is done — Phases 0-4 (core
loop, tests, Paystack webhook, Turso sync) and Phase 8 (Pi deployment script + log capping).
Phase 5 (ANPR) stays blocked on trained models that don't exist yet; Phases 6-7 (identity
verification, SMS approval) are explicitly out of scope per this plan's own locked decisions,
not started. Everything else that's left needs something only you can provide — see Phase 9.

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

## Phase 3 — Paystack webhook (`workers/charge`) — DONE

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
dev machine (they weren't when this was first built) — typecheck is DONE:
- [x] `cd workers/charge && npm install && npm run typecheck` — passes clean, no errors.
- [ ] `wrangler secret put PAYSTACK_SECRET_KEY` / `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`,
      then `wrangler deploy`.
- [ ] Point Paystack's webhook URL (dashboard → Settings → API Keys & Webhooks) at the
      deployed Worker URL, trigger a real mobile money test charge, confirm the transaction
      resolves from `PENDING` to `SUCCESS`/`FAILED` and a `WEBHOOK_CHARGE_RESOLVED` audit_log
      row appears.

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

## Ops dashboard (`workers/dashboard`) — 2026-08-15 — started

Wasn't part of the original phased plan (`workers/dashboard/` was just an empty `.gitkeep`
placeholder from the initial skeleton) — started now from a layout mockup the user provided.
Scaffolded as a Cloudflare Worker, same shape as `workers/charge`: `package.json`/`tsconfig.json`
(identical dependency pins), `wrangler.toml`, `src/index.ts` — typechecks clean.

- [x] Gated behind HTTP Basic Auth (`DASHBOARD_PASSWORD` secret, constant-time compared) on
      every request, decided deliberately before wiring any real data: unlike `workers/charge`
      (only ever receives Paystack's own signed webhook calls), this Worker serves real customer
      data — phone numbers via `vehicles`, plate numbers — to whoever can reach the URL, so it
      must never be servable without a shared secret.
- [x] **Live Transaction Feed** panel wired to real data: `GET /api/transactions` queries Turso
      (`transactions` LEFT JOIN `vehicles` on `vehicle_id`, ordered by `created_at` DESC, limit
      20), the page polls it every 5s. Rows are built as explicit plain objects (not the raw
      libsql `Row` array-like value) before `JSON.stringify`, so the response has real field
      names regardless of how `Row` serializes by default.
- [x] Every other panel from the mockup (Detection Accuracy, Revenue Leakage, Mean Latency,
      Throughput, Detection Method Mix, Payment Gateway Health) kept as illustrative mock data
      **on purpose, per the user's explicit choice** — this system doesn't compute or track any
      of those anywhere. A real "method mix" today would just be 100% RFID (ANPR doesn't exist
      yet), and accuracy/leakage/latency aren't measured anywhere in the codebase. The page's
      footer note and a "LIVE — TURSO" tag on the feed panel's heading say explicitly which part
      is real, so this can't be misread as a fully-live dashboard.
- [ ] Not yet done (needs Node/a real deploy target, same as `workers/charge`'s remaining
      checklist): `npm install && npm run typecheck` — done, passes clean. Still needed:
      `wrangler secret put DASHBOARD_PASSWORD` / `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`, then
      `wrangler deploy`, then a real browser check that Basic Auth actually gates access and the
      feed populates against live Turso data.
- [ ] Future "add more if needed" candidates, not started: computing Method Mix for real once
      ANPR exists (trivial — `transactions.identification_method` is already tracked per row);
      an actual Detection Accuracy/Revenue Leakage definition would need new tracking this
      system doesn't have today, not just a dashboard query.
