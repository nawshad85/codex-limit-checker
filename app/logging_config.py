from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional


def default_log_path(env: Optional[dict[str, str]] = None) -> Path:
    values = os.environ if env is None else env
    appdata = values.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "CodexUsageMonitor" / "monitor.log"


def configure_logging(debug: bool = False, path: Optional[Path] = None) -> Path:
    log_path = path or default_log_path()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=256 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        root.addHandler(file_handler)
    except OSError:
        root.addHandler(logging.NullHandler())

    if debug and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        console.setLevel(logging.DEBUG)
        root.addHandler(console)

    return log_path
