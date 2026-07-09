# backfill_accumulator.py
"""
Replay daily parquets in chronological order through the streaming
ledger update path to build up (or catch up) the v2 pair ledger.

Uses streaming_update.stream_update_ledger for each day: never loads
the full ~12M-pair ledger into memory. Peak memory is ~500 MB per day
regardless of ledger size, which lets the backfill run on a memory-
constrained machine alongside other work.

Trade-off vs the old in-memory design: each day pays a full sequential
re-read of the ledger from disk (~1-2 min). For a 7-day weekly batch
that's under 15 min of extra I/O.

By default starts from the most recent WINDOW_DAYS parquets, matching
the v1 model's window. Use --all to process every parquet in
data/raw/daily/ instead.

Usage:
    python backfill_accumulator.py [--all] [--from-date YYYY-MM-DD]
                                    [--to-date YYYY-MM-DD] [--resume]
"""

import argparse
import gc
import glob
import os

from config import DAILY_RAW_DIR, WINDOW_DAYS
from daily_correlate import (
    compute_daily_correlations,
    day_label_from_path,
    read_day_signatures,
)
from pair_accumulator import LEDGER_PATH
from streaming_update import get_most_recent_day, stream_update_ledger


def main():
    parser = argparse.ArgumentParser(
        description="Backfill the v2 pair ledger from daily parquets (streaming)."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process every parquet in data/raw/daily/, not just the latest WINDOW_DAYS.",
    )
    parser.add_argument(
        "--from-date",
        default=None,
        help="Only process parquets with date >= this YYYY-MM-DD. Overrides --all.",
    )
    parser.add_argument(
        "--to-date",
        default=None,
        help="Only process parquets with date <= this YYYY-MM-DD.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue from an existing ledger (skip days already applied) rather than starting fresh.",
    )
    args = parser.parse_args()

    files = sorted(glob.glob(os.path.join(DAILY_RAW_DIR, "*.parquet")))
    if not files:
        raise SystemExit(f"No parquets found in {DAILY_RAW_DIR}/")

    if args.from_date:
        files = [f for f in files if day_label_from_path(f) >= args.from_date]
    elif not args.all:
        files = files[-WINDOW_DAYS:]

    if args.to_date:
        files = [f for f in files if day_label_from_path(f) <= args.to_date]

    if not files:
        raise SystemExit("No parquets match the requested range.")

    print(f"Backfill plan: {len(files)} parquets")
    print(f"  Range: {day_label_from_path(files[0])} -> {day_label_from_path(files[-1])}")
    print(f"  Update mode: streaming per-day (peak memory ~500 MB)")

    if args.resume and os.path.exists(LEDGER_PATH):
        print(f"  Resuming from existing ledger at {LEDGER_PATH}")
        most_recent = get_most_recent_day()
        if most_recent:
            print(f"  Ledger's most recent day: {most_recent}")
            before = len(files)
            files = [f for f in files if day_label_from_path(f) > most_recent]
            print(
                f"  Skipping {before - len(files)} already-processed day(s); "
                f"{len(files)} parquets remaining"
            )
            if not files:
                print("Nothing to do; ledger is already up to date.")
                return
    else:
        if os.path.exists(LEDGER_PATH):
            confirm = (
                input(
                    f"  Existing ledger at {LEDGER_PATH} will be OVERWRITTEN. "
                    f"Type 'yes' to continue: "
                )
                .strip()
                .lower()
            )
            if confirm != "yes":
                print("Aborted.")
                return
            os.remove(LEDGER_PATH)

    print()

    for i, f in enumerate(files, 1):
        day = day_label_from_path(f)
        print(f"[{i}/{len(files)}] {day}")

        badges, signatures, lats, lons = read_day_signatures(f)
        print(f"  {len(badges):,} badges with sufficient data")

        pair_updates = compute_daily_correlations(badges, signatures, lats, lons)
        print(f"  {len(pair_updates):,} pair correlations")

        del badges, signatures, lats, lons
        gc.collect()

        stream_update_ledger(day, pair_updates)

        del pair_updates
        gc.collect()

    print()
    print(f"Backfill complete. Ledger saved to: {LEDGER_PATH}")


if __name__ == "__main__":
    main()
