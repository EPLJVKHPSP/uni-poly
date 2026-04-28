"""Print a one-screen summary of the Probalytics data we have on disk."""

from __future__ import annotations

import glob
import json
import os
import sys

import pandas as pd
from dotenv import load_dotenv

ROOT_DEFAULT = "data/probalytics"


def main(root: str = ROOT_DEFAULT) -> int:
    load_dotenv(".env", override=True)
    print(f"== Probalytics data summary ({root}) ==")
    if not os.path.isdir(root):
        print("  (no data directory)")
        return 1

    markets_path = os.path.join(root, "markets.parquet")
    if os.path.exists(markets_path):
        markets = pd.read_parquet(markets_path)
        print(f"\n[markets]  {len(markets):,} markets")
        print(markets.groupby(["asset", "kind"], dropna=False).size().to_string())
        print(f"opened_at: {markets['opened_at'].min()}  ->  {markets['opened_at'].max()}")
    else:
        print("\n[markets]  (missing)")

    fills_files = sorted(glob.glob(os.path.join(root, "fills", "*.parquet")))
    print(f"\n[fills]    {len(fills_files)} day-files")
    total_rows = 0
    total_bytes = 0
    for f in fills_files:
        n = len(pd.read_parquet(f, columns=["timestamp"]))
        sz = os.path.getsize(f)
        total_rows += n
        total_bytes += sz
        print(f"  {os.path.basename(f)}   rows={n:>6,}  size={sz/1024:>6.1f}KB")
    print(f"  TOTAL: {total_rows:,} fills, {total_bytes/1e6:.2f} MB")

    book_files = sorted(glob.glob(os.path.join(root, "orderbooks", "**", "*.parquet"), recursive=True))
    book_files = [b for b in book_files if os.path.basename(b).startswith("0x")]
    by_day: dict[str, list[str]] = {}
    for b in book_files:
        day = os.path.basename(os.path.dirname(b))
        by_day.setdefault(day, []).append(b)
    print(f"\n[orderbooks]  {len(book_files)} (market, day) parquets across {len(by_day)} days")
    book_bytes = sum(os.path.getsize(b) for b in book_files)
    print(f"  total bytes: {book_bytes/1e6:.2f} MB")
    for day in sorted(by_day):
        print(f"  {day}  files={len(by_day[day]):>4}  bytes={sum(os.path.getsize(b) for b in by_day[day])/1e6:>5.2f}MB")

    meta_path = os.path.join(root, "_meta", "sync_state.json")
    if os.path.exists(meta_path):
        with open(meta_path) as fp:
            meta = json.load(fp)
        print("\n[meta]  sync_state.json:")
        print(json.dumps({k: v for k, v in meta.items() if k != "fills_files"}, indent=2, default=str))

    empty_path = os.path.join(root, "_meta", "orderbook_empty.json")
    if os.path.exists(empty_path):
        with open(empty_path) as fp:
            e = json.load(fp)
        print(f"\n[meta]  {len(e)} (market, day) pairs flagged as known-empty (skipped on resync)")

    return 0


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else ROOT_DEFAULT
    sys.exit(main(root))
