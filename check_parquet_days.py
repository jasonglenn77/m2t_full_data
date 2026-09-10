# check_parquet_days.py
"""
Catch daily parquets that were extracted before the source data was ready.

save_daily_data marks a day "done" as soon as the write succeeds, whatever
came back. A day pulled too early therefore lands short, is never
re-extracted, and gets folded into the pair ledger permanently -- where it
quietly weakens every correlation involving it. Nothing downstream notices.

This compares each day's row count against the median of its neighbors.
Row counts come from parquet metadata, so checking a range costs no data
read; only days that look short are opened to count the hours actually
present.

Exits non-zero if any day in the range looks incomplete, so a pipeline can
stop before the bad day reaches the ledger.

Usage:
    python check_parquet_days.py                              # newest 14 days
    python check_parquet_days.py --from 2026-08-31 --to 2026-09-06
    python check_parquet_days.py --reset 2026-09-06           # re-extract it
"""

import argparse
import glob
import json
import os

import pyarrow.parquet as pq

from config import DAILY_RAW_DIR, INTERVALS_PER_DAY, STATE_DIR

STATE_FILE = os.path.join(STATE_DIR, "daily_extract_state.json")

# A day below this share of the local median is treated as suspect. Real
# day-to-day variation sits within a couple of percent; anything approaching
# 10% down is a truncated extract, not a quiet day.
SHORT_THRESHOLD = 0.90


def day_of(path):
    return os.path.basename(path).replace(".parquet", "")


def median(values):
    s = sorted(values)
    n = len(s)
    if not n:
        return 0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def count_hours(path):
    """Distinct clock hours present. Only called for suspect days."""
    try:
        tbl = pq.read_table(path, columns=["MSRMTDTTM"])
        col = tbl.column("MSRMTDTTM").to_pylist()
        return len({t.hour for t in col if t is not None})
    except Exception as exc:
        print(f"      (could not read timestamps: {exc})")
        return None


def cmd_reset(day):
    if not os.path.exists(STATE_FILE):
        raise SystemExit(f"{STATE_FILE} not found.")
    with open(STATE_FILE) as fh:
        state = json.load(fh)
    if day not in state:
        raise SystemExit(f"{day} is not in the extract state; nothing to reset.")
    del state[day]
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)
    print(f"Cleared {day} from {STATE_FILE}.")
    print("The next save_daily_data run covering that date will re-extract it")
    print("and overwrite the parquet.")
    print()
    print("If the short day was already applied to the pair ledger, re-extracting")
    print("alone will NOT correct it -- the ledger skips days it has already seen.")
    print("Say so before re-running and the ledger can be rebuilt from that date.")


def main():
    parser = argparse.ArgumentParser(
        description="Flag daily parquets that look truncated."
    )
    parser.add_argument("--from", dest="from_date", default=None,
                        help="First day to check, YYYY-MM-DD.")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="Last day to check, YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=14,
                        help="How many recent days to check when no range is "
                             "given (default 14).")
    parser.add_argument("--reset", default=None,
                        help="Clear one day from the extract state so it is "
                             "pulled again, then exit.")
    args = parser.parse_args()

    if args.reset:
        cmd_reset(args.reset)
        return

    files = sorted(glob.glob(os.path.join(DAILY_RAW_DIR, "*.parquet")))
    if not files:
        raise SystemExit(f"No parquets in {DAILY_RAW_DIR}/")

    # Always measure against a wider baseline than the range being checked,
    # or a whole short week would define its own normal.
    baseline = files[-max(args.days, 21):]
    counts = {}
    for f in baseline:
        try:
            counts[day_of(f)] = pq.ParquetFile(f).metadata.num_rows
        except Exception as exc:
            print(f"  {day_of(f)}: unreadable ({exc})")

    if not counts:
        raise SystemExit("Could not read row counts from any parquet.")

    med = median(list(counts.values()))

    if args.from_date or args.to_date:
        lo = args.from_date or "0000-00-00"
        hi = args.to_date or "9999-99-99"
        checking = [d for d in sorted(counts) if lo <= d <= hi]
    else:
        checking = sorted(counts)[-args.days:]

    print(f"Checking {len(checking)} day(s) against a median of {med:,.0f} rows")
    print(f"(baseline: {len(counts)} days, flag below "
          f"{SHORT_THRESHOLD:.0%} = {med * SHORT_THRESHOLD:,.0f} rows)")
    print()

    short = []
    for d in checking:
        n = counts[d]
        share = n / med if med else 0
        flag = share < SHORT_THRESHOLD
        mark = "SHORT" if flag else "ok"
        print(f"  {d}  {n:>12,} rows  {share:6.1%}  {mark}")
        if flag:
            hours = count_hours(os.path.join(DAILY_RAW_DIR, f"{d}.parquet"))
            if hours is not None:
                print(f"      {hours} of 24 hours present")
            short.append((d, n, share, hours))

    print()
    if not short:
        print("All days look complete.")
        return

    print(f"{len(short)} day(s) look truncated:")
    for d, n, share, hours in short:
        detail = f"{hours}/24 hours" if hours is not None else "hours unknown"
        print(f"  {d}  {n:,} rows ({share:.1%} of normal, {detail})")
    print()
    print("Most likely the day was extracted before its reads finished")
    print("processing. To pull it again:")
    print(f"  python check_parquet_days.py --reset {short[0][0]}")
    print("  then re-run the extract for that date.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
