"""Sync Probalytics data into local Parquet files.

Three independent sub-commands:

  python3 -m scripts.probalytics_sync markets       # universe -> markets.parquet
  python3 -m scripts.probalytics_sync fills         # day-partitioned fills/
  python3 -m scripts.probalytics_sync orderbooks    # per-market orderbooks/
  python3 -m scripts.probalytics_sync all           # all of the above

Universe is restricted to BTC/ETH strike markets by default; flip
``--include-hourly false`` / ``--include-daily false`` to narrow further.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, date, timezone, timedelta
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# Import-friendly path setup so the script runs both as a module and a file.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probalytics_pkg.client import ProbalyticsRest, load_creds_from_env  # noqa: E402
from probalytics_pkg.markets_sync import (  # noqa: E402
    DEFAULT_DATA_ROOT,
    fetch_market_universe,
    fills_date_window,
    sync_fills_for_universe,
    write_markets_parquet,
)
from probalytics_pkg.books_sync import (  # noqa: E402
    OrderBookSyncStats,
    sync_orderbooks,
    write_sync_state,
)


def _parse_iso_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return date.fromisoformat(s)


def _parse_iso_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _filter_to_markets_with_fills(df: pd.DataFrame, root: str) -> pd.DataFrame:
    """Return only the universe rows whose ``market_platform_id`` shows up
    in any local fills parquet under ``<root>/fills/*.parquet``."""
    import glob
    files = sorted(glob.glob(os.path.join(root, "fills", "*.parquet")))
    if not files:
        logging.warning("no local fills parquets found; skipping --only-traded filter")
        return df
    seen: set[str] = set()
    for f in files:
        try:
            seen.update(pd.read_parquet(f, columns=["market_platform_id"])["market_platform_id"].astype(str).tolist())
        except Exception as exc:  # noqa: BLE001
            logging.warning("could not read %s: %s", f, exc)
    return df[df["market_platform_id"].astype(str).isin(seen)].reset_index(drop=True)


def _load_or_fetch_universe(args) -> pd.DataFrame:
    creds = load_creds_from_env()
    universe_path = os.path.join(args.root, "markets.parquet")
    if os.path.exists(universe_path) and not args.refresh_universe:
        df = pd.read_parquet(universe_path)
        logging.info("loaded universe from %s (%d markets)", universe_path, len(df))
        return df
    df = fetch_market_universe(
        creds,
        assets=tuple(a.lower() for a in args.assets),
        include_hourly=args.include_hourly,
        include_daily=args.include_daily,
    )
    write_markets_parquet(df, root=args.root)
    return df


def cmd_markets(args) -> int:
    creds = load_creds_from_env()
    df = fetch_market_universe(
        creds,
        assets=tuple(a.lower() for a in args.assets),
        include_hourly=args.include_hourly,
        include_daily=args.include_daily,
    )
    write_markets_parquet(df, root=args.root)
    print(f"OK: {len(df):,} markets -> {args.root}/markets.parquet")
    by_kind = df.groupby(["asset", "kind"], dropna=False).size().reset_index(name="n")
    print(by_kind.to_string(index=False))
    return 0


def cmd_fills(args) -> int:
    creds = load_creds_from_env()
    # Universe is selected server-side via slug predicate, so we don't pass IDs.
    start = _parse_iso_date(args.start)
    end = _parse_iso_date(args.end)
    assets = tuple(a.lower() for a in args.assets)
    if start is None or end is None:
        win = fills_date_window(
            creds,
            assets=assets,
            include_hourly=args.include_hourly,
            include_daily=args.include_daily,
        )
        if win[0] is None:
            print("no fills available for this universe", file=sys.stderr)
            return 1
        start = start or win[0].date()
        end = end or win[1].date()
        logging.info("fills window auto-detected: %s -> %s", start, end)

    paths = sync_fills_for_universe(
        creds,
        assets=assets,
        include_hourly=args.include_hourly,
        include_daily=args.include_daily,
        start_day=start,
        end_day=end,
        root=args.root,
    )
    print(f"OK: wrote {len(paths)} fills parquet files in {args.root}/fills/")
    return 0


def cmd_orderbooks(args) -> int:
    creds = load_creds_from_env()
    df = _load_or_fetch_universe(args)

    if args.only_traded:
        df = _filter_to_markets_with_fills(df, args.root)
        logging.info("only-traded filter: %d markets remain", len(df))

    if args.limit:
        df = df.head(args.limit)

    rest = ProbalyticsRest(creds, max_req_per_second=args.rate)
    stats = sync_orderbooks(
        rest, df,
        root=args.root,
        start_floor=_parse_iso_dt(args.start),
        end_ceiling=_parse_iso_dt(args.end),
        workers=args.workers,
        force=args.force,
        request_timeout=args.timeout,
    )
    print(
        f"OK: attempted={stats.attempted} ok={stats.succeeded} empty={stats.empty} "
        f"err={stats.errored} skipped_existing={stats.skipped_existing} "
        f"skipped_known_empty={stats.skipped_known_empty} "
        f"bytes={stats.bytes_total/1e6:.1f}MB"
    )
    if stats.errors:
        print("first errors:")
        for e in stats.errors[:10]:
            print(f"  - {e}")
    return 0


def cmd_all(args) -> int:
    creds = load_creds_from_env()
    assets = tuple(a.lower() for a in args.assets)
    df = fetch_market_universe(
        creds,
        assets=assets,
        include_hourly=args.include_hourly,
        include_daily=args.include_daily,
    )
    write_markets_parquet(df, root=args.root)
    if args.limit:
        df = df.head(args.limit)

    win = fills_date_window(
        creds, assets=assets,
        include_hourly=args.include_hourly, include_daily=args.include_daily,
    )
    start = _parse_iso_date(args.start) or (win[0].date() if win[0] else None)
    end = _parse_iso_date(args.end) or (win[1].date() if win[1] else None)

    fills_paths = []
    if start and end:
        fills_paths = sync_fills_for_universe(
            creds, assets=assets,
            include_hourly=args.include_hourly, include_daily=args.include_daily,
            start_day=start, end_day=end, root=args.root,
        )

    rest = ProbalyticsRest(creds, max_req_per_second=args.rate)
    stats = sync_orderbooks(
        rest, df,
        root=args.root,
        start_floor=_parse_iso_dt(args.start),
        end_ceiling=_parse_iso_dt(args.end),
        workers=args.workers,
        force=args.force,
    )
    write_sync_state(args.root, universe_size=len(df), fills_paths=fills_paths, book_stats=stats)
    print(f"OK: universe={len(df)} fills_files={len(fills_paths)} books_ok={stats.succeeded}")
    return 0


def _attach_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=DEFAULT_DATA_ROOT,
                        help=f"Output root (default: {DEFAULT_DATA_ROOT})")
    parser.add_argument("--assets", nargs="+", default=["bitcoin", "ethereum"])
    parser.add_argument("--include-hourly", type=lambda s: s.lower() != "false", default=True)
    parser.add_argument("--include-daily", type=lambda s: s.lower() != "false", default=True)
    parser.add_argument("--start", default=None,
                        help="ISO date or datetime; clamps fills/books windows")
    parser.add_argument("--end", default=None,
                        help="ISO date or datetime; clamps fills/books windows")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit number of markets (sanity testing)")
    parser.add_argument("--refresh-universe", action="store_true",
                        help="Force re-fetch of markets.parquet even if it exists")


def main(argv=None) -> int:
    load_dotenv(".env", override=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    p = argparse.ArgumentParser(description="Probalytics -> local Parquet sync")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_markets = sub.add_parser("markets", help="Sync market metadata only")
    _attach_common(p_markets)
    p_markets.set_defaults(func=cmd_markets)

    p_fills = sub.add_parser("fills", help="Sync historical fills (day partitions)")
    _attach_common(p_fills)
    p_fills.set_defaults(func=cmd_fills)

    p_books = sub.add_parser("orderbooks", help="Sync per-market orderbook snapshots")
    _attach_common(p_books)
    p_books.add_argument("--workers", type=int, default=4)
    p_books.add_argument("--rate", type=float, default=30.0,
                        help="Max REST requests per second (cap=300)")
    p_books.add_argument("--timeout", type=float, default=120.0,
                        help="Per-request timeout in seconds")
    p_books.add_argument("--force", action="store_true",
                        help="Overwrite existing per-market files")
    p_books.add_argument("--only-traded", action="store_true",
                        help="Only fetch books for markets that show up in local fills/*.parquet (~85% reduction)")
    p_books.set_defaults(func=cmd_orderbooks)

    p_all = sub.add_parser("all", help="markets + fills + orderbooks in one go")
    _attach_common(p_all)
    p_all.add_argument("--workers", type=int, default=4)
    p_all.add_argument("--rate", type=float, default=30.0)
    p_all.add_argument("--timeout", type=float, default=120.0)
    p_all.add_argument("--force", action="store_true")
    p_all.set_defaults(func=cmd_all)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
