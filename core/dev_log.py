"""
core/dev_log.py

Verbose logging for field-test sessions. Console-only by default --
deliberately never writes to disk, since this Pi's SD card has already shown
real corruption (plan.md Phase 0) and dev-mode verbosity shouldn't add write
volume on top of console noise. Passing an explicit `log_file` opts into a
FileHandler too, for the rare case a specific test run needs a saved
transcript (e.g. reviewing a single test pass later) -- that's on the caller
to ask for, never the default.

Off by default, so a real/production run stays quiet. Turn it on for a
field-test session with either:

    DEV_MODE=1 python3 -m core.main
    python3 -m core.main --dev
    python3 -m core.main --dev --log-file logs/test_run.log

Once active, every module's logger.debug()/info() calls start printing --
presence/motion (sensors/presence.py), ANPR detection+OCR
(anpr/yolov11.py), DB lookups/writes (core/db.py), and full HTTP
request/response logging for SMS/charge calls (api_clients/). With it off,
the root logger still stays at WARNING rather than dropping to nothing --
core/main.py's NOT-REGISTERED-IN-DATABASE flag on an unmatched RFID/ANPR
read logs at WARNING specifically so that always surfaces, dev mode or not.

Usage:
    from core.dev_log import setup_dev_logging
    setup_dev_logging()   # call once, as early as possible in main()
"""

import logging
import sys
from pathlib import Path
from typing import Optional

from core.config import DEV_MODE

_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-24s %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_dev_logging(force: bool = False, log_file: Optional[str] = None) -> bool:
    """Configure root logging for this process. Returns whether dev-mode
    verbosity activated (DEV_MODE env var, `force=True` from --dev, or a
    `log_file` being given at all -- asking for a saved transcript implies
    wanting the verbosity that makes it worth saving).

    `log_file`, when given, appends to that path in addition to stdout --
    the one opt-in exception to this module's console-only rule.
    """
    active = DEV_MODE or force or bool(log_file)

    root = logging.getLogger()
    root.handlers.clear()
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(logging.DEBUG if active else logging.WARNING)

    if active:
        logging.getLogger(__name__).info(
            "Dev-mode logging active%s",
            f" -- also writing to {log_file}" if log_file else " -- console only, nothing written to disk",
        )
    return active
