---
name: restore-smart-toll
description: Restore the Smart Toll project on this Raspberry Pi after a fresh SD card flash (following the 2026-08 SD card hardware corruption). Use after re-flashing the OS, or any time this repo needs rebuilding from scratch on a clean disk.
---

# Restore Smart Toll after SD card reflash

Context: this Pi's SD card (`/dev/mmcblk0p2`) developed real hardware-level ext4
corruption in August 2026 — confirmed via `dmesg`/`journalctl -k -p err`
(`bad block bitmap checksum`, `Corrupt inode bitmap`, `Directory block failed
checksum`) that **recurred across multiple reboots and got worse over time**,
including silent bit-rot of file *content* with zero filesystem warning (ext4
only checksums metadata/directories, not file data, so corrupted file bytes
can look totally fine to `git status`/`ls` while being wrong). A reflash + new
card (or a fully repaired one, `e2fsck` run offline from a rescue context) is
the fix — not another reboot.

Everything **tracked in git** is safe regardless of what happens to this SD
card — it lives at `git@github.com:ebarthur/etc-pi.git` on GitHub. The things
actually at risk are local-only files that were never committed. This skill
covers both halves: what to preserve *before* wiping, and how to rebuild
*after* reflashing.

## Before wiping — back these up somewhere OTHER than this SD card

Do not trust "I copied it to another folder on this same card" as a backup —
that's exactly what silently rotted this session (a fresh copy of the models
made minutes earlier no longer matched its own checksum). Get these off the
Pi entirely (another machine, cloud storage, a USB drive) before wiping:

1. **`anpr/crop.pt`** (~5.4MB) — trained YOLOv11n license-plate detector.
2. **`anpr/ocr.ckpt`** (~353MB) — trained PARSeq scene-text-recognition
   checkpoint. Originates from a Kaggle training run (`/kaggle/working/
   parseq_repo/outputs/parseq/...`, checkpoint name shows
   `val_accuracy=23.77`) — if no clean local copy survives, check whether the
   Kaggle output/notebook is still available as a source of truth.
3. **`.env`** — real secrets (Paystack, Arkesel, Turso). Not committed by
   design (`.gitignore`). If you can't export the file itself safely, at
   minimum record which values are set (the key *names* are in
   `.env.example`, already in git) so they can be re-fetched from each
   service's dashboard.
4. `db/tolling.db*` — **not worth backing up**. This is disposable local
   scratch data by design (see `core/db.py`'s module docstring) — either
   synced to Turso already (if configured) or safe to lose and reinit.

Everything else (`core/`, `api_clients/`, `workers/`, `tests/`, `scripts/`,
`rfid/`, `sensors/`, `plan.md`, this skill file once committed) is tracked in
git — nothing to do for it here except `git push` before wiping if there are
any uncommitted changes (`git status` first).

## After reflashing — rebuild from scratch

1. **Sanity-check the new card before trusting it with real work**: write a
   throwaway file, re-read/checksum it two or three times a few seconds
   apart, and check `dmesg | grep -iE "ext4|error|crc|corrupt"` for anything
   at all. Zero hits expected on a healthy fresh card — if you see EXT4
   errors already, the reflash/new card didn't fix it, stop and investigate
   before proceeding.

2. **Base OS deps + repo**:
   ```
   sudo apt-get update
   sudo apt-get install -y git python3-venv python3-pip python3-picamera2 cmake build-essential
   git clone git@github.com:ebarthur/etc-pi.git ~/Documents/etc
   cd ~/Documents/etc
   ```

3. **Restore the untracked files** from wherever you backed them up in the
   step above: `.env`, `anpr/crop.pt`, `anpr/ocr.ckpt`. Checksum them against
   your backup copy immediately after copying (`md5sum`) — don't assume the
   copy succeeded cleanly.

4. **Create the venv exactly like `scripts/setup_pi.sh` does** —
   `--system-site-packages` so it can see apt's `picamera2`:
   ```
   python3 -m venv --system-site-packages venv
   venv/bin/pip install --upgrade pip
   ```

5. **Install PyTorch CPU-only FIRST, explicitly** — this is one of two real
   pitfalls hit during the 2026-08-15 session. `ultralytics` (in
   `requirements.txt`) pulls in `torch` transitively, and letting pip resolve
   that on its own on this box pulled the full **CUDA** build
   (`torch-2.13.0` + `nvidia_cudnn_cu13` + `cuda_toolkit`, ~900MB) — useless
   on a Pi with no NVIDIA GPU, and a huge waste of time/SD-card writes. Force
   the CPU wheel first so it's already satisfied when `ultralytics` resolves —
   **install `torchvision` in this same command, not just `torch`**: doing
   `torch` alone here (as an earlier version of this doc said) leaves
   `torchvision` to be resolved later from a generic PyPI index, which built
   against a different ABI than the `+cpu` torch wheel and broke
   `torchvision::nms` at import time (`RuntimeError: operator
   torchvision::nms does not exist`, surfaced as 3 failing tests in
   `tests/test_anpr.py` during the 2026-08-25 restore) — `scripts/setup_pi.sh`
   already gets this right by installing both together:
   ```
   venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
   venv/bin/pip install -r requirements.txt -r requirements-dev.txt
   ```
   If you do end up with a mismatched `torchvision`, fix forward rather than
   reinstalling everything: `venv/bin/pip install --index-url
   https://download.pytorch.org/whl/cpu --force-reinstall --no-deps
   torchvision`.

6. **Run `scripts/setup_pi.sh`** (idempotent, root) for the rest: SPI enable
   for the RC522, `gpio`/`spi` group membership, the `smart-toll.service`
   systemd unit, journald caps.

7. **Verify**: `git status` clean, `venv/bin/python -m pytest` passes (use
   `python -m pytest`, not the bare `pytest` binary — the project has no
   `conftest.py`/`pyproject.toml` adding the repo root to `sys.path`, so the
   bare binary fails every test module with `ModuleNotFoundError: No module
   named 'core'` etc.), `venv/bin/python -m core.main --uid <test-uid>` runs
   without error (dev mode, no hardware needed).

## Where Phase 5 (ANPR) integration actually stood, 2026-08-15

Useful so whoever resumes doesn't have to re-derive this from the model files
again — inspecting both `.pt`/`.ckpt` files (they're `torch.save` zip
archives; `unzip -p <file> data.pkl | strings` shows the pickled class refs
without needing to load them) found:

- **`anpr/crop.pt`** = a real `ultralytics` YOLOv11n detection model
  (`DetectionModel`, run name `yolo11n_lpr_run1`). Straightforward:
  `ultralytics.YOLO("anpr/crop.pt")`.
- **`anpr/ocr.ckpt`** = **not** a YOLO/ultralytics model — it's a
  [PARSeq](https://github.com/baudm/parseq) (permuted autoregressive scene
  text recognition) model saved as a full **PyTorch Lightning** training
  checkpoint (contains `state_dict` plus optimizer/scheduler/epoch state).
  Loading it for inference needs the PARSeq model class + `pytorch_lightning`
  (`PARSeq.load_from_checkpoint(...)`) — not a plain `torch.load()`, and not
  covered by `ultralytics` alone as `plan.md`'s Phase 5 note had left open as
  a possibility. This means the dependency footprint is bigger than
  `requirements.txt`'s current comment anticipates (`torch`/`torchvision`
  alone isn't enough — need `pytorch-lightning` + the PARSeq repo's model
  code + likely `timm`). Checkpoint name shows `val_accuracy=23.7705` at
  epoch ~43-45 — quite low; may need more training regardless of integration
  work.
- `anpr/yolov11.py` and `tests/test_anpr.py` were both still empty stubs — no
  integration code was written before this session stopped for the disk
  issue.
- Two config mismatches to fix when integration resumes:
  - `.gitignore` lists `anpr/models/*.pt`, but the real files are directly at
    `anpr/crop.pt` / `anpr/ocr.ckpt` — the pattern doesn't actually match, so
    double-check `git status` doesn't try to stage the 353MB checkpoint.
  - `core/config.py`'s `ANPR_MODEL_PATH` points at
    `anpr/models/plate_detector.pt`, which doesn't match either actual
    filename — needs updating to the real paths (and a second constant added
    for the OCR checkpoint path) before `anpr/yolov11.py` can use it.
- Captured frames come out ~90° rotated from upright (physical camera mount)
  — `sensors/presence.py`'s docstring flags this; account for it before
  feeding frames to the detector.
