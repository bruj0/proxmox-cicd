"""StructuredLogger — dual console + JSON-line audit log.

Mirrors proxmox-k3s/provisioner/lib/log.py so the three repos
(proxmox-vms, proxmox-k3s, proxmox-cicd) share the same audit
log shape and redactor semantics.

Implements M4 (no silent failures): every event emits one JSON
object per line to the audit log with timestamp, level, step,
trace_id, message, data.

Implements M7 (secrets never logged): keys whose names contain
"secret" / "token" / "password" / "ssh_key" / "sshkey"
(case-insensitive) are dropped recursively in any log dict
before it is written to disk. Dropped (not replaced with
[REDACTED]) so a log query never accidentally surfaces the
key path.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Key names that always get redacted when they appear as a JSON
# key in a log dict. Match by substring, case-insensitively.
_REDACT_KEYS = {"secret", "token", "password", "ssh_key", "sshkey"}


def _scrub(value: Any) -> Any:
    """Recursively redact any dict key whose name contains a
    redact substring. Lists are walked element-wise. Scalars
    pass through unchanged. Redacted keys are dropped entirely.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            k_lower = k.lower()
            if any(token in k_lower for token in _REDACT_KEYS):
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


@dataclass
class StructuredLogger:
    """Dual console + JSON-line audit logger.

    The audit log file path is fixed at construction time; the
    console handler is attached to stderr at INFO and above.
    Each log call writes one line to the file and optionally a
    human-readable line to stderr.
    """

    audit_path: Path
    component: str = "proxmox-cicd"
    _trace_id: str = ""
    _lock: threading.Lock | None = None

    def __post_init__(self) -> None:
        self._trace_id = uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._console = logging.getLogger(f"{self.component}.console")
        self._console.setLevel(logging.INFO)
        if not self._console.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            self._console.addHandler(handler)

    def _emit(self, level: str, step: str, message: str, **data: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "component": self.component,
            "host": socket.gethostname(),
            "trace_id": self._trace_id,
            "level": level,
            "step": step,
            "message": message,
        }
        if data:
            record["data"] = _scrub(data)
        lock = self._lock
        assert lock is not None  # invariant: __post_init__ set it
        with lock:
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
        if level in ("warn", "error"):
            console_msg = f"[{level.upper()}] {step}: {message}"
            getattr(self._console, level if level != "warn" else "warning")(
                console_msg
            )
        elif level == "info":
            self._console.info(f"{step}: {message}")

    def info(self, step: str, message: str = "", **data: Any) -> None:
        self._emit("info", step, message, **data)

    def warn(self, step: str, message: str = "", **data: Any) -> None:
        self._emit("warn", step, message, **data)

    def error(
        self, step: str, message: str = "", error: str = "", **data: Any
    ) -> None:
        record: dict[str, Any] = {"error": error} if error else {}
        record.update(data)
        self._emit("error", step, message, **record)


# Re-export the scrubber for tests.
__all__ = ["StructuredLogger", "_scrub"]


# `re` is imported for parity with the sibling repos' log.py; the
# runtime scrubber doesn't need regex today, but the API surface
# is intentionally identical.
_ = re
