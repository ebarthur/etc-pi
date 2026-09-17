#!/usr/bin/env bash
#
# scripts/force_resync.sh
#
# One-button fix for the recurring "local replica silently stuck, sync() reports success but
# nothing new reaches Turso" bug this session tracked down repeatedly (see core/db.py's
# _repair_replica_conflict). The automatic drift-detector (core/db.py's _check_sync_gap) already
# self-heals this on its own within roughly a minute of confirming it -- this script exists for
# when you don't want to wait, e.g. mid-demo and something looks stale on the dashboard.
#
# Safe to run any time: salvages any local-only rows (transactions/audit_log) to Turso first,
# then discards and rebuilds the local replica fresh. Stops the service first and restarts it
# after, because running the repair (which briefly moves db/tolling.db aside mid-rebuild) while
# the live service is also touching that same file is a real cross-process race -- see
# core/db.py's _db_path_lock docstring for why that lock only protects one process, not two.
#
# Usage:
#   ./scripts/force_resync.sh

set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "Stopping smart-toll.service..."
sudo systemctl stop smart-toll.service

echo "Repairing the local Turso replica (salvage + rebuild)..."
venv/bin/python3 -c "import core.db as db; db._repair_replica_conflict(RuntimeError('manual force_resync.sh run'))"

echo "Restarting smart-toll.service..."
sudo systemctl start smart-toll.service
sleep 2
systemctl is-active --quiet smart-toll.service && echo "Done -- service is back up." || echo "WARNING: service did not come back up -- check with: systemctl status smart-toll.service"
