# extract_week.py
"""
Extract a range of days, validating each one before marking it done.

save_daily_data marks a day "done" as soon as the write succeeds, whatever
came back -- and extract_date_range only raises when a query errors, not when
it returns zero rows. A date whose reads have not been processed yet
therefore produces a short or empty parquet, gets marked done, and is never
retried. Nothing downstream notices until the correlations for that day come
out weak.

This wraps the same extraction but checks each day against the size of known
good days first, and only records it as done if it passes. A day that comes
back short is left unrecorded, so simply running again later re-extracts it.

That makes it safe to pull part of a week early -- say Friday, for Monday
through Thursday -- and collect the rest after the weekend, which spreads the
slowest part of the weekly run across two sittings.

Usage:
    python extract_week.py --from 2026-09-21 --to 2026-09-27
    python extract_week.py --from 2026-09-21 --to 2026-09-27 --partial
    python extract_week.py --from 2026-09-21 --to 2026-09-27 --dry-run

--partial exits 0 when days are merely not ready yet, for a mid-week pull.
Without it, any missing or short day is an error, which is what the weekly
pipeline wants.
"""

import argparse
import glob
import json
import os
from datetime import datetime, timedelta, timezone

import pyarrow.parquet as pq

from config import DAILY_RAW_DIR, STATE_DIR

# save_daily_data imports extract_data at module scope, which pulls in the
# Oracle driver -- present only on the machine that talks to C2M. Reading the
# extract state should not require it, so the three small state helpers are
# mirrored here rather than imported, and extract_data is imported at the
# point of use. That keeps --dry-run and the assessment logic runnable
# anywhere, and leaves save_daily_data.py untouched.
STATE_FILE = os.path.join(STATE_DIR, "daily_extract_state.json")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def save_day(df, day):
    os.makedirs(DAILY_RAW_DIR, exist_ok=True)
    df.to_parquet(os.path.join(DAILY_RAW_DIR, f"{day:%Y-%m-%d}.parquet"), index=False)

# A day below this share of the median of known good days is treated as
# incomplete. Real day-to-day variation is under 1%, so this has wide margin.
SHORT_THRESHOLD = 0.90
# Used only when there is no baseline yet (a brand new install).
MIN_HOURS = 24


def day_str(d):
    return d.strftime("%Y-%m-%d")


def parquet_path(day):
    return os.path.join(DAILY_RAW_DIR, f"{day}.parquet")


def row_count(path):
    try:
        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return 0


def count_hours(path):
    try:
        col = pq.read_table(path, columns=["MSRMTDTTM"]).column("MSRMTDTTM").to_pylist()
        return len({t.hour for t in col if t is not None})
    except Exception:
        return None


def baseline_median(exclude_days):
    """Median rows across known days OUTSIDE the range being worked on, so a
    short week cannot quietly define its own idea of normal."""
    counts = []
    for f in sorted(glob.glob(os.path.join(DAILY_RAW_DIR, "*.parquet")))[-45:]:
        day = os.path.basename(f).replace(".parquet", "")
        if day in exclude_days:
            continue
        n = row_count(f)
        if n:
            counts.append(n)
    if not counts:
        return 0
    counts.sort()
    m = len(counts)
    return counts[m // 2] if m % 2 else (counts[m // 2 - 1] + counts[m // 2]) / 2


def assess(day, median):
    """Return (state, rows, hours) where state is complete | short | missing."""
    path = parquet_path(day)
    if not os.path.exists(path):
        return "missing", 0, None
    n = row_count(path)
    if median and n >= median * SHORT_THRESHOLD:
        return "complete", n, None
    hours = count_hours(path)
    if not median and hours == MIN_HOURS:
        return "complete", n, hours
    return "short", n, hours


def main():
    parser = argparse.ArgumentParser(
        description="Extract days, validating each before marking it done."
    )
    parser.add_argument("--from", dest="from_date", required=True,
                        help="First day, YYYY-MM-DD.")
    parser.add_argument("--to", dest="to_date", required=True,
                        help="Last day, YYYY-MM-DD.")
    parser.add_argument("--partial", action="store_true",
                        help="Exit 0 when days are simply not ready yet.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be extracted, change nothing.")
    args = parser.parse_args()

    start = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if start > end:
        raise SystemExit("--from is after --to")

    days = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    day_keys = {day_str(d) for d in days}

    median = baseline_median(day_keys)
    print(f"Extracting {day_str(start)} .. {day_str(end)}")
    if median:
        print(f"  A complete day is >= {median * SHORT_THRESHOLD:,.0f} rows "
              f"(median of known days: {median:,.0f})")
    else:
        print(f"  No baseline yet; requiring {MIN_HOURS} hours of data per day.")
    print()

    state = load_state()
    ready, pending = [], []

    for d in days:
        key = day_str(d)
        status, rows, hours = assess(key, median)

        if status == "complete":
            if state.get(key) != "done":
                state[key] = "done"
                if not args.dry_run:
                    save_state(state)
            print(f"  {key}  already complete ({rows:,} rows) - skipping")
            ready.append(key)
            continue

        if status == "short":
            detail = f"{rows:,} rows" + (f", {hours}/24 hours" if hours is not None else "")
            print(f"  {key}  on disk but INCOMPLETE ({detail}) - re-extracting")
            # Drop the done marker first: if this attempt also comes back
            # short, the day must stay eligible for a later retry.
            if key in state and not args.dry_run:
                del state[key]
                save_state(state)
        else:
            print(f"  {key}  not extracted yet")

        if args.dry_run:
            pending.append(key)
            continue

        try:
            from extract_data import extract_date_range

            df = extract_date_range(d, d)
        except Exception as exc:
            print(f"      extraction failed: {exc}")
            pending.append(key)
            continue

        save_day(df, d)
        status, rows, hours = assess(key, median)
        if status == "complete":
            state[key] = "done"
            save_state(state)
            print(f"      complete ({rows:,} rows)")
            ready.append(key)
        else:
            detail = f"{rows:,} rows" + (f", {hours}/24 hours" if hours is not None else "")
            print(f"      STILL INCOMPLETE ({detail}) - left unrecorded, "
                  f"will retry on the next run")
            pending.append(key)

    print()
    print(f"Complete: {len(ready)} of {len(days)} day(s)")
    if not pending:
        print("Every day in the range is present and full.")
        return

    print(f"Not yet usable: {', '.join(pending)}")
    if args.partial:
        print()
        print("--partial: treating these as not ready yet rather than an error.")
        print("Run again once their reads have been processed; the days already")
        print("collected are recorded and will be skipped.")
        return
    raise SystemExit(1)


if __name__ == "__main__":
    main()
