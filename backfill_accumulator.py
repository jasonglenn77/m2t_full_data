# backfill_accumulator.py
"""
Replay daily parquets in chronological order through the v2 daily
correlator to build up the pair ledger from scratch.

Unlike running daily_correlate.py per file, this keeps the ledger in
memory across days and only saves periodically — much faster for the
initial backfill because we avoid re-parsing the full ledger between
days.

By default starts from the most recent WINDOW_DAYS parquets, matching
the v1 model's window. Use --all to process every parquet in
data/raw/daily/ instead.

Usage:
    python backfill_accumulator.py [--save-every N] [--all] [--from-date YYYY-MM-DD]
"""

import argparse
import gc
import glob
import os
import sys

from config import DAILY_RAW_DIR, WINDOW_DAYS
from daily_correlate import (
    apply_updates_to_dict,
    compute_daily_correlations,
    day_label_from_path,
    read_day_signatures,
)
from pair_accumulator import (
    LEDGER_PATH,
    dict_to_ledger,
    ledger_to_dict,
    load_ledger,
    save_ledger,
)


def main():
    parser = argparse.ArgumentParser(
        description="Backfill the v2 pair ledger from daily parquets."
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=5,
        help="Save the ledger to disk every N days during backfill (default 5).",
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
        "--resume",
        action="store_true",
        help="Continue from an existing ledger rather than starting fresh.",
    )
    args = parser.parse_args()

    files = sorted(glob.glob(os.path.join(DAILY_RAW_DIR, "*.parquet")))
    if not files:
        raise SystemExit(f"No parquets found in {DAILY_RAW_DIR}/")

    if args.from_date:
        files = [f for f in files if day_label_from_path(f) >= args.from_date]
    elif not args.all:
        files = files[-WINDOW_DAYS:]

    if not files:
        raise SystemExit("No parquets match the requested range.")

    print(f"Backfill plan: {len(files)} parquets")
    print(f"  Range: {day_label_from_path(files[0])} -> {day_label_from_path(files[-1])}")
    print(f"  Save every {args.save_every} day(s)")

    if args.resume and os.path.exists(LEDGER_PATH):
        print(f"  Resuming from existing ledger at {LEDGER_PATH}")
        ledger_dict = ledger_to_dict(load_ledger())
        # Filter out already-processed days so update_pair's idempotence
        # short-circuits the work cleanly.
        already_done = {e["LAST_DAY"] for e in ledger_dict.values()}
        if already_done:
            most_recent = max(already_done)
            print(f"  Ledger's most recent day: {most_recent}")
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
        ledger_dict = {}

    print()

    for i, f in enumerate(files, 1):
        day = day_label_from_path(f)
        print(f"[{i}/{len(files)}] {day}")

        badges, signatures, lats, lons = read_day_signatures(f)
        print(f"  {len(badges):,} badges with sufficient data")

        pair_updates = compute_daily_correlations(badges, signatures, lats, lons)
        print(f"  {len(pair_updates):,} pair correlations")

        apply_updates_to_dict(ledger_dict, pair_updates, day)
        print(f"  Ledger now {len(ledger_dict):,} unique pairs")

        del badges, signatures, lats, lons, pair_updates
        gc.collect()

        if i % args.save_every == 0 or i == len(files):
            print(f"  Saving ledger ...")
            save_ledger(dict_to_ledger(ledger_dict))

    print()
    print(f"Backfill complete. Final ledger: {len(ledger_dict):,} unique pairs.")
    print(f"Saved to: {LEDGER_PATH}")


if __name__ == "__main__":
    main()
