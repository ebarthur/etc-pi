#!/usr/bin/env bash
#
# scripts/tail_all_logs.sh
#
# Single merged live view of everything the Smart Toll system does at runtime, across
# every process that isn't the one you're sitting at:
#   - smart-toll.service (the Pi orchestrator)      -- via journalctl
#   - smart-toll-charge-webhook (Cloudflare Worker)  -- via `wrangler tail`
#   - smart-toll-dashboard (Cloudflare Worker)       -- via `wrangler tail`
#
# Each source runs in the background, piped through `sed` to prefix every line with a
# colored tag, and all three interleave into this one terminal in real time. Built for the
# 2026-09-18 defense demo, where a silent failure in any one of these (the stuck-replica sync
# bug, a dropped SMS, a Paystack verify error) needs to be visible immediately instead of
# discovered after the fact -- see the SMS_NOTIFY_FAILED gap this same session found in
# workers/charge/src/index.ts's sendSms().
#
# Requires: `wrangler` reachable via npx (already authenticated -- same login used for every
# `wrangler deploy` this session), and journalctl access to smart-toll.service (passwordless
# sudo already in place on this Pi, used throughout this session for systemctl).
#
# Usage:
#   ./scripts/tail_all_logs.sh
#   Ctrl+C to stop everything cleanly.

set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PIDS=()

cleanup() {
    echo
    echo "Stopping all log streams..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" >/dev/null 2>&1
    done
    wait >/dev/null 2>&1
    exit 0
}
trap cleanup INT TERM

echo "Tailing smart-toll.service (Pi) + smart-toll-charge-webhook + smart-toll-dashboard (Cloudflare)."
echo "Ctrl+C to stop."
echo

# --- Pi orchestrator (journald) ---
# picamera2's own DEBUG lines (one per preview frame, several per second whenever DEV_MODE's
# MJPEG preview is running) are filtered out here -- confirmed live they'd otherwise drown out
# every real event (RFID/ANPR reads, charges, sync/repair messages) in a wall of frame-job
# noise. This only affects what this viewer shows, not what journald itself retains.
(
    sudo journalctl -u smart-toll.service -f -n 20 --no-pager |
        grep --line-buffered -v "picamera2" |
        sed -u $'s/^/\033[36m[PI]\033[0m         /'
) &
PIDS+=($!)

# --- Cloudflare Worker: charge (cron poller) ---
(
    cd workers/charge &&
        npx --yes wrangler tail --format pretty 2>&1 |
        sed -u $'s/^/\033[35m[CHARGE]\033[0m     /'
) &
PIDS+=($!)

# --- Cloudflare Worker: dashboard (HTTP) ---
(
    cd workers/dashboard &&
        npx --yes wrangler tail --format pretty 2>&1 |
        sed -u $'s/^/\033[33m[DASHBOARD]\033[0m  /'
) &
PIDS+=($!)

wait
