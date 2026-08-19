# check_state.py
"""
One-command answer to "where did I leave off, and do I have everything?"

Reports, for whichever machine you run it on:

  1. Which large local-only files are present (the ones git cannot carry)
  2. The v2 pair ledger's last applied day  -> your v2 resume point
  3. The v1 signature store's window end    -> your v1 resume point
     (optional; costs a few parquet reads because nothing records it)
  4. Daily parquet inventory + any missing calendar days
  5. Which GIS files the model will actually pick up
  6. The last recorded v1 and v2 model runs

Usage:
    python check_state.py
    python check_state.py --detect-v1-day
    python check_state.py --detect-v1-day --max-candidates 40
"""

import argparse
import glob
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

from config import DAILY_RAW_DIR, INTERVALS_PER_DAY, SIGNATURE_DIR, WINDOW_DAYS

GIS_DIR = "GIS_mapping"
LEDGER_PATH = "data/state/pair_ledger.parquet"
SIG_PATH = os.path.join(SIGNATURE_DIR, "signatures.npy")
BADGE_IDS_PATH = os.path.join(SIGNATURE_DIR, "badge_ids.npy")

# Files git never carries: either .gitignore'd or too big to commit.
# If any of these are missing, they have to arrive by OneDrive/copy.
LOCAL_ONLY = [
    (LEDGER_PATH, "v2 pair ledger", "REQUIRED for v2"),
    (SIG_PATH, "v1 signature store", "REQUIRED for v1 only"),
    (DAILY_RAW_DIR, "daily parquet folder", "REQUIRED for both"),
    (GIS_DIR, "GIS source CSVs", "REQUIRED for both"),
]


def human(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def header(text):
    print()
    print("=" * 68)
    print(text)
    print("=" * 68)


def report_local_only():
    header("1. Local-only files (git cannot carry these)")
    for path, label, note in LOCAL_ONLY:
        if not os.path.exists(path):
            print(f"  MISSING  {label:<24} {path}   <-- {note}")
            continue
        if os.path.isdir(path):
            n = len(os.listdir(path))
            print(f"  ok       {label:<24} {path}  ({n:,} files)")
        else:
            size = os.path.getsize(path)
            mtime = pd.Timestamp(os.path.getmtime(path), unit="s")
            print(
                f"  ok       {label:<24} {path}  "
                f"({human(size)}, modified {mtime:%Y-%m-%d})"
            )


def report_v2_ledger():
    header("2. v2 pair ledger — your v2 resume point")
    if not os.path.exists(LEDGER_PATH):
        print("  No ledger found. v2 would build one from scratch.")
        return None
    from streaming_update import get_most_recent_day

    print("  Scanning LAST_DAY column (this takes a minute on a 3 GB ledger) ...")
    day = get_most_recent_day()
    if day is None:
        print("  Ledger exists but is empty.")
        return None
    resume = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    print(f"  Last day applied to ledger : {day}")
    print(f"  v2 resumes at              : {resume}")
    return day


def _read_parquet_day(path, badges, badge_is_int):
    """Read only the sample badges' rows from one daily parquet."""
    want = [int(b) for b in badges] if badge_is_int else list(badges)
    cols = ["BADGE", "MSRMTDTTM", "PUVALUE"]
    try:
        df = pd.read_parquet(path, columns=cols, filters=[("BADGE", "in", want)])
    except Exception:
        df = pd.read_parquet(path, columns=cols)
        df = df[df["BADGE"].isin(want)]
    return df


def _day_vectors(df, badges):
    """Rebuild each sample badge's 96-point vector the same way
    rolling_update.py does, so the comparison is apples to apples."""
    if df.empty:
        return {}
    df = df.copy()
    df["MSRMTDTTM"] = pd.to_datetime(df["MSRMTDTTM"], utc=True)
    day_start = df["MSRMTDTTM"].min().floor("D")
    times = pd.date_range(
        start=day_start, periods=INTERVALS_PER_DAY, freq="15min", tz="UTC"
    )
    out = {}
    df["BADGE"] = df["BADGE"].astype(str)
    for badge, g in df.groupby("BADGE", sort=False):
        if badge not in badges:
            continue
        g = g.sort_values("MSRMTDTTM").drop_duplicates(
            subset=["MSRMTDTTM"], keep="last"
        )
        ts = g.set_index("MSRMTDTTM")["PUVALUE"].reindex(times)
        out[badge] = ts.to_numpy(dtype=np.float32)
    return out


def _vectors_match(a, b):
    """NaN-aware float32 comparison of two 96-point day vectors."""
    finite_a = np.isfinite(a)
    finite_b = np.isfinite(b)
    if finite_a.sum() < INTERVALS_PER_DAY * 0.5:
        return False
    if (finite_a != finite_b).sum() > INTERVALS_PER_DAY * 0.05:
        return False
    both = finite_a & finite_b
    if both.sum() == 0:
        return False
    return bool(np.allclose(a[both], b[both], atol=1e-4, rtol=0))


def detect_v1_window_end(max_candidates, n_sample=4):
    """Nothing records which day the v1 signature store ends on, so
    identify it by matching the store's newest 96 columns against the
    daily parquets, newest first."""
    header("3. v1 signature store — detecting window end")
    if not os.path.exists(SIG_PATH) or not os.path.exists(BADGE_IDS_PATH):
        print("  Signature store not present; skipping (v1 unavailable).")
        return None

    sig = np.load(SIG_PATH, mmap_mode="r")
    badge_ids = np.array(
        [str(b) for b in np.load(BADGE_IDS_PATH, allow_pickle=True)], dtype=object
    )
    print(f"  Store shape: {sig.shape[0]:,} badges x {sig.shape[1]:,} intervals "
          f"({sig.shape[1] // INTERVALS_PER_DAY} days)")
    if sig.shape[1] != WINDOW_DAYS * INTERVALS_PER_DAY:
        print(
            f"  NOTE: store holds {sig.shape[1] // INTERVALS_PER_DAY} days but "
            f"config.WINDOW_DAYS is {WINDOW_DAYS}. Rebuild before running v1."
        )

    last_day = sig[:, -INTERVALS_PER_DAY:]
    # Pick sample badges spread through the store that have a full day of data.
    sample = []
    step = max(1, sig.shape[0] // 500)
    for i in range(0, sig.shape[0], step):
        row = np.asarray(last_day[i])
        if np.isfinite(row).sum() >= INTERVALS_PER_DAY * 0.95:
            sample.append((badge_ids[i], row.copy()))
        if len(sample) >= n_sample:
            break
    if not sample:
        print("  Could not find sample badges with a full final day; aborting.")
        return None

    sample_badges = {b for b, _ in sample}
    print(f"  Comparing {len(sample)} sample badges against daily parquets ...")

    files = sorted(glob.glob(os.path.join(DAILY_RAW_DIR, "*.parquet")))[::-1]
    files = files[:max_candidates]
    if not files:
        print("  No daily parquets to compare against.")
        return None

    badge_is_int = False
    try:
        import pyarrow.parquet as pq

        field = pq.ParquetFile(files[0]).schema_arrow.field("BADGE")
        badge_is_int = "int" in str(field.type).lower()
    except Exception:
        pass

    for path in files:
        day = os.path.basename(path).replace(".parquet", "")
        try:
            df = _read_parquet_day(path, sample_badges, badge_is_int)
            vectors = _day_vectors(df, sample_badges)
        except Exception as exc:
            print(f"    {day}: read failed ({exc})")
            continue
        hits = sum(
            1
            for badge, stored in sample
            if badge in vectors and _vectors_match(stored, vectors[badge])
        )
        print(f"    {day}: {hits}/{len(sample)} sample badges match")
        if hits >= max(2, len(sample) // 2 + 1):
            resume = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            print()
            print(f"  v1 window ends on : {day}")
            print(f"  v1 resumes at     : {resume}")
            return day

    print()
    print("  No match found in the candidate range. The v1 store is probably")
    print("  older than the parquets you have, or was built with different")
    print("  config settings. Rebuilding with build_signature_store.py is the")
    print("  clean fix (it builds from the newest WINDOW_DAYS parquets).")
    return None


def report_parquets():
    header("4. Daily parquet inventory")
    files = sorted(glob.glob(os.path.join(DAILY_RAW_DIR, "*.parquet")))
    if not files:
        print(f"  No parquets in {DAILY_RAW_DIR}/")
        return
    days = [os.path.basename(f).replace(".parquet", "") for f in files]
    first, last = days[0], days[-1]
    print(f"  {len(days):,} files, {first} -> {last}")

    have = set(days)
    d0 = date.fromisoformat(first)
    d1 = date.fromisoformat(last)
    missing = []
    d = d0
    while d <= d1:
        if d.isoformat() not in have:
            missing.append(d.isoformat())
        d += timedelta(days=1)

    if not missing:
        print("  No gaps: every calendar day in that range is present.")
    else:
        print(f"  {len(missing)} MISSING day(s) inside the range:")
        # Collapse consecutive runs for readability.
        runs = []
        start = prev = missing[0]
        for m in missing[1:]:
            if date.fromisoformat(m) - date.fromisoformat(prev) == timedelta(days=1):
                prev = m
                continue
            runs.append((start, prev))
            start = prev = m
        runs.append((start, prev))
        for a, b in runs:
            print(f"    {a}" if a == b else f"    {a} .. {b}")


def report_gis():
    header("5. GIS files the model will use")
    if not os.path.isdir(GIS_DIR):
        print(f"  {GIS_DIR}/ does not exist.")
        return
    for pattern in ["ServicePoints*.csv", "Transformers*.csv"]:
        files = glob.glob(os.path.join(GIS_DIR, pattern))
        print(f"  {pattern}")
        if not files:
            print("    (none found)  <-- required")
            continue
        chosen = max(files, key=os.path.getmtime)
        for f in sorted(files, key=os.path.getmtime, reverse=True):
            mark = "-->" if f == chosen else "   "
            mtime = pd.Timestamp(os.path.getmtime(f), unit="s")
            print(
                f"    {mark} {os.path.basename(f):<40} "
                f"{human(os.path.getsize(f)):>10}  modified {mtime:%Y-%m-%d %H:%M}"
            )
    km = "data/outputs/known_mapping.csv"
    if os.path.exists(km):
        mtime = pd.Timestamp(os.path.getmtime(km), unit="s")
        print(f"\n  known_mapping.csv last rebuilt: {mtime:%Y-%m-%d %H:%M}")
    else:
        print("\n  known_mapping.csv missing — run build_known_mapping.py")


def report_runs():
    header("6. Recorded model runs")
    for label, path in [
        ("v1", "data/state/history/runs.parquet"),
        ("v2", "data/state/history_v2/runs.parquet"),
    ]:
        if not os.path.exists(path):
            print(f"  {label}: no run history yet ({path})")
            continue
        runs = pd.read_parquet(path)
        print(f"  {label}: {len(runs)} recorded run(s). Most recent:")
        for _, r in runs.tail(5).iterrows():
            print(f"      {r['RUN_ID']:<32} {str(r['RUN_DATE'])[:19]}")


def main():
    parser = argparse.ArgumentParser(
        description="Report where this machine's model state stands."
    )
    parser.add_argument(
        "--detect-v1-day",
        action="store_true",
        help="Identify the v1 signature store's window end (reads parquets; slow).",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=30,
        help="How many recent parquets to test when detecting the v1 day.",
    )
    parser.add_argument(
        "--skip-ledger",
        action="store_true",
        help="Skip the v2 ledger scan (the slowest default step).",
    )
    args = parser.parse_args()

    report_local_only()
    if not args.skip_ledger:
        report_v2_ledger()
    if args.detect_v1_day:
        detect_v1_window_end(args.max_candidates)
    report_parquets()
    report_gis()
    report_runs()
    print()


if __name__ == "__main__":
    main()
