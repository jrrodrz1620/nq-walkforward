"""
Critical-error logging  (validation gate 6).

When the broker is unreachable after every retry, we gracefully persist the
context + a full state snapshot to ``error_log.json`` so nothing is lost and an
operator can reconcile by hand.
"""
from __future__ import annotations

import json
import os
import tempfile
import traceback
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_error_state(
    path: str,
    *,
    error: BaseException,
    context: dict[str, Any],
    state_snapshot: Optional[dict] = None,
    severity: str = "critical",
) -> dict:
    """Append a structured critical-alert entry to ``path`` (a JSON array).

    Writes are atomic (temp file + ``os.replace``) so a crash mid-write can't
    corrupt the log. Returns the entry that was appended.
    """
    entry = {
        "timestamp": _utc_now_iso(),
        "severity": severity,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
        "context": context,
        "state_snapshot": state_snapshot or {},
    }

    existing: list = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, list):
                existing = loaded
        except (json.JSONDecodeError, OSError):
            # Corrupt/unreadable log: preserve it, start fresh array.
            existing = []

    existing.append(entry)

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2, default=str)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    return entry
