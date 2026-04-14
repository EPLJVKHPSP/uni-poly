"""Polymarket CLOB API client — fetch historical bet prices."""

import sys
import logging

import requests as _requests_mod

CLOB_BASE_URL = "https://clob.polymarket.com"

logger = logging.getLogger(__name__)


def _requests():
    """Resolve ``requests`` through the shim module so that
    @patch("polymarket_history.requests.get") intercepts calls correctly."""
    shim = sys.modules.get("polymarket_history")
    if shim and hasattr(shim, "requests"):
        return getattr(shim, "requests")
    return _requests_mod


def fetch_price_history(clob_token_id, start_ts=None, end_ts=None, fidelity=60):
    """
    Fetch historical prices for a single CLOB token from Polymarket.

    Args:
        clob_token_id: The CLOB asset/token ID
        start_ts: Unix timestamp for range start (optional)
        end_ts: Unix timestamp for range end (optional)
        fidelity: Granularity in minutes (default 60 = hourly)

    Returns:
        List of {t: unix_ts, p: price} dicts
    """
    params = {
        "market": clob_token_id,
        "interval": "all",
        "fidelity": fidelity,
    }
    if start_ts:
        params["startTs"] = int(start_ts)
    if end_ts:
        params["endTs"] = int(end_ts)

    try:
        resp = _requests().get(
            f"{CLOB_BASE_URL}/prices-history",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("history", [])
    except Exception as e:
        logger.error(f"Failed to fetch history for {clob_token_id[:16]}...: {e}")
        return []
