"""Probalytics REST + ClickHouse clients with retries and rate-limit awareness."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import requests
from clickhouse_driver import Client as ClickHouseClient

logger = logging.getLogger(__name__)


PROBALYTICS_REST = "https://api.probalytics.io"
PROBALYTICS_CLICKHOUSE_HOST = "clickhouse.probalytics.io"
PROBALYTICS_CLICKHOUSE_PORT = 9440

# Probalytics' published hard ceiling is 3000 req / 10s. We stay well below it
# so we don't trip throttles (and to leave room for parallel workers).
DEFAULT_MAX_REQ_PER_SECOND = 60.0


@dataclass(frozen=True)
class ProbalyticsCreds:
    api_key: str
    api_secret: str
    ch_host: str
    ch_port: int
    ch_user: str
    ch_password: str
    ch_database: str

    @property
    def bearer(self) -> str:
        return f"{self.api_key}:{self.api_secret}"


def load_creds_from_env() -> ProbalyticsCreds:
    """Read Probalytics credentials from .env-style environment variables.

    Expected keys (case-sensitive, matching what the dashboard hands out):
      api_key, api_secret, clickhouse_host, database, username, password
    """
    missing = [k for k in ("api_key", "api_secret", "username", "password", "database")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing Probalytics env vars: {missing}")

    host_spec = os.environ.get("clickhouse_host", f"{PROBALYTICS_CLICKHOUSE_HOST}:{PROBALYTICS_CLICKHOUSE_PORT}")
    if ":" in host_spec:
        host, port_s = host_spec.split(":", 1)
        port = int(port_s)
    else:
        host, port = host_spec, PROBALYTICS_CLICKHOUSE_PORT

    return ProbalyticsCreds(
        api_key=os.environ["api_key"],
        api_secret=os.environ["api_secret"],
        ch_host=host,
        ch_port=port,
        ch_user=os.environ["username"],
        ch_password=os.environ["password"],
        ch_database=os.environ["database"],
    )


class _RateLimiter:
    """Token-bucket rate limiter shared across worker threads."""

    def __init__(self, max_per_second: float):
        self.max_per_second = max(max_per_second, 1.0)
        self._next_at = 0.0

    def wait(self) -> None:
        import threading
        # Keep the lock attribute lazy so the limiter pickles trivially.
        lock = getattr(self, "_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._lock = lock
        with lock:
            now = time.monotonic()
            interval = 1.0 / self.max_per_second
            if now < self._next_at:
                sleep_for = self._next_at - now
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next_at = now + interval


class ProbalyticsRest:
    """Thin REST wrapper with retries on 429/5xx."""

    def __init__(self, creds: ProbalyticsCreds, *, max_req_per_second: float = DEFAULT_MAX_REQ_PER_SECOND):
        self._creds = creds
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {creds.bearer}"
        self._limiter = _RateLimiter(max_req_per_second)

    def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        stream: bool = False,
        timeout: float = 60.0,
        max_retries: int = 5,
    ) -> requests.Response:
        url = f"{PROBALYTICS_REST.rstrip('/')}{path}"
        delay = 1.0
        for attempt in range(1, max_retries + 1):
            self._limiter.wait()
            try:
                resp = self._session.get(url, params=params, stream=stream, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == max_retries:
                    raise
                logger.warning("GET %s failed (%s); retry %d/%d in %.1fs",
                               path, exc, attempt, max_retries, delay)
                time.sleep(delay)
                delay = min(delay * 2.0, 30.0)
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt == max_retries:
                    return resp
                ra = resp.headers.get("Retry-After")
                wait_s = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
                logger.warning("GET %s -> %d; retry %d/%d in %.1fs",
                               path, resp.status_code, attempt, max_retries, wait_s)
                time.sleep(wait_s)
                delay = min(delay * 2.0, 30.0)
                continue
            return resp
        raise RuntimeError(f"unreachable: max_retries exhausted for {path}")

    def download_orderbook(
        self,
        market_platform_id: str,
        start_time: str,
        end_time: str,
        out_path: str,
        *,
        timeout: float = 90.0,
    ) -> Optional[int]:
        """Download an orderbook-snapshot Parquet to ``out_path``.

        Returns the byte count written, or None on a non-200 response (caller
        decides what to do — typically log + skip).
        """
        params = {
            "market_platform_id": market_platform_id,
            "start_time": start_time,
            "end_time": end_time,
        }
        resp = self.get("/api/v1/orderbook-snapshots/download", params=params, stream=True, timeout=timeout)
        if resp.status_code != 200:
            logger.warning("orderbook download %s [%s..%s] -> %d %s",
                           market_platform_id, start_time, end_time,
                           resp.status_code, resp.text[:200])
            return None
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        total = 0
        with open(out_path, "wb") as fp:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fp.write(chunk)
                    total += len(chunk)
        return total


def open_clickhouse(creds: ProbalyticsCreds) -> ClickHouseClient:
    return ClickHouseClient(
        host=creds.ch_host,
        port=creds.ch_port,
        secure=True,
        user=creds.ch_user,
        password=creds.ch_password,
        database=creds.ch_database,
    )


def stream_clickhouse(
    client: ClickHouseClient,
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    chunk_size: int = 50_000,
) -> Iterator[List[tuple]]:
    """Yield ``chunk_size``-sized batches of rows so we don't OOM on big pulls.

    We can't tune ``max_block_size`` against Probalytics' read-only ClickHouse,
    so we just rebatch client-side.
    """
    rows_buf: List[tuple] = []
    for row in client.execute_iter(sql, params or {}):
        rows_buf.append(row)
        if len(rows_buf) >= chunk_size:
            yield rows_buf
            rows_buf = []
    if rows_buf:
        yield rows_buf
