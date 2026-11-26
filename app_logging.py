"""Log file + friendly crash handling, so a failure at a customer's shop leaves evidence to debug."""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "nextgen_erp.log"


def log_dir(app_dir: Path) -> Path:
    path = Path(app_dir) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging(app_dir: Path, version: str = "") -> Path:
    path = log_dir(app_dir) / LOG_NAME
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    logging.getLogger("nextgen").info("=== NextGen ERP starting %s (Python %s) ===", version, sys.version.split()[0])
    return path


def install_crash_handler(app_dir: Path, show_dialog=None):
    """Log every uncaught exception (main thread and worker threads); optionally tell the user once per run."""
    state = {"shown": 0}
    log = logging.getLogger("nextgen.crash")

    def handle(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Uncaught exception:\n%s", text)
        if show_dialog is not None and state["shown"] < 3:
            state["shown"] += 1
            try:
                show_dialog(f"{exc_type.__name__}: {exc}", str(log_dir(app_dir) / LOG_NAME))
            except Exception:
                pass

    sys.excepthook = handle
    threading.excepthook = lambda args: handle(args.exc_type, args.exc_value, args.exc_traceback)
