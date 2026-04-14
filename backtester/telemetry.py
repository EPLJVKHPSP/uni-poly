"""Structured JSONL telemetry for visualization.

This is intentionally append-only (one JSON object per line) to make it easy to
stream, tail, and ingest into pandas/duckdb later.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


SCHEMA_VERSION = 1


def _iso_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def new_run_id() -> str:
    return uuid.uuid4().hex


@dataclass
class TelemetrySink:
    path: Optional[str]
    run_id: str
    enabled: bool = True
    schema_version: int = SCHEMA_VERSION

    def emit(self, event: str, ts: int, payload: Optional[Dict[str, Any]] = None) -> None:
        if not self.enabled or not self.path:
            return

        record = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "event": event,
            "ts": int(ts),
            "iso": _iso_from_ts(ts),
            "payload": payload or {},
        }

        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")

