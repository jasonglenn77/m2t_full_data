# daily_correlate.py
"""
Compute per-pair daily Pearson correlations from a single daily parquet
and apply them to the v2 pair ledger.

For each badge in the parquet, build a 96-point 15-minute p.u. signature
for the day. Use spatial_neighbors (the same haversine BallTree the v1
model uses) to find candidate pairs within RADIUS_METERS. For each
candidate pair with sufficient overlapping non-NaN samples, compute a
single daily Pearson r. The ledger appends today's r to each pair's
rolling window of recent values.

Used both standalone (for a single new day) and as a worker module by
backfill_accumulator.py.

Usage:
    python daily_correlate.py <parquet_path>
"""

import gc
import os
import sys

import numpy as np
import pandas as pd

from config import INTERVALS_PER_DAY
from pair_accumulator import (
    canonical_pair,
    dict_to_ledger,
    ledger_to_dict,
    load_ledger,
    save_ledger,
    update_pair,
)
from spatial_neighbors import build_tree, find_neighbors

# Minimum non-NaN overlapping samples needed to compute a daily Pearson.
# 60 of 96 slots = 62.5% data presence; below that the daily r is too
# noisy to be useful.
MIN_DAILY_OVERLAP = 60


def day_label_from_path(parquet_path):
    """Extract 'YYYY-MM-DD' from a parquet filename like 'data/raw/daily/2026-04-25.parquet'."""
    base = os.path.basename(parquet_path)
    return base.replace(".parquet", "")


def read_day_signatures(parquet_path):
    """
    Read one daily parquet and return per-badge 96-point signatures plus
    lat/lon. Badges without enough non-NaN samples to clear
    MIN_DAILY_OVERLAP are dropped.

    Returns:
        badges: np.ndarray[str], shape (n,)
        signatures: np.ndarray[float32], shape (n, INTERVALS_PER_DAY)
        lats: np.ndarray[float64], shape (n,)
        lons: np.ndarray[float64], shape (n,)
    """
    df = pd.read_parquet(
        parquet_path,
        columns=["BADGE", "MSRMTDTTM", "PUVALUE", "BADGE_LAT", "BADGE_LONG"],
    )
    df["BADGE"] = df["BADGE"].astype("category")
    df["MSRMTDTTM"] = pd.to_datetime(df["MSRMTDTTM"], utc=True)

    day_start = df["MSRMTDTTM"].min().floor("D")
    times = pd.date_range(
        start=day_start, periods=INTERVALS_PER_DAY, freq="15min", tz="UTC"
    )

    badges = []
    signatures = []
    lats = []
    lons = []
    for badge, g in df.groupby("BADGE", sort=False, observed=True):
        g = g.sort_values("MSRMTDTTM").drop_duplicates(
            subset=["MSRMTDTTM"], keep="last"
        )
        ts = g.set_index("MSRMTDTTM")["PUVALUE"].reindex(times)
        sig = ts.to_numpy(dtype=np.float32)
        if np.isfinite(sig).sum() < MIN_DAILY_OVERLAP:
            continue
        first = g.iloc[0]
        lat = pd.to_numeric(first["BADGE_LAT"], errors="coerce")
        lon = pd.to_numeric(first["BADGE_LONG"], errors="coerce")
        if not (np.isfinite(lat) and np.isfinite(lon)):
            continue
        badges.append(str(badge))
        signatures.append(sig)
        lats.append(float(lat))
        lons.append(float(lon))

    del df
    gc.collect()

    return (
        np.array(badges, dtype=object),
        np.vstack(signatures) if signatures else np.empty((0, INTERVALS_PER_DAY), dtype=np.float32),
        np.array(lats, dtype=np.float64),
        np.array(lons, dtype=np.float64),
    )


def compute_daily_correlations(badges, signatures, lats, lons):
    """
    For each within-radius candidate pair, compute the daily Pearson.
    Returns a list of (badge_a, badge_b, r) tuples in canonical order.

    Skips pairs where overlap < MIN_DAILY_OVERLAP or one signature is
    constant (zero variance, NaN correlation).
    """
    if len(badges) == 0:
        return []

    tree, coords = build_tree(lats, lons)
    neighbors = find_neighbors(tree, coords)

    pair_updates = []
    finite_mask = np.isfinite(signatures)  # (n, 96) bool array, computed once

    for i in range(len(badges)):
        sig_i = signatures[i]
        finite_i = finite_mask[i]
        badge_i = badges[i]
        for j in neighbors[i]:
            if j <= i:
                continue
            sig_j = signatures[j]
            mask = finite_i & finite_mask[j]
            overlap = int(mask.sum())
            if overlap < MIN_DAILY_OVERLAP:
                continue
            a = sig_i[mask]
            b = sig_j[mask]
            # Skip degenerate constant signals (no variance => undefined Pearson)
            if a.std() == 0 or b.std() == 0:
                continue
            r = float(np.corrcoef(a, b)[0, 1])
            if not np.isfinite(r):
                continue
            a_id, b_id = canonical_pair(badge_i, badges[j])
            pair_updates.append((a_id, b_id, r))

    return pair_updates


def apply_updates_to_dict(ledger_dict, pair_updates, day_label):
    """Mutate ledger_dict in place by applying today's pair correlations."""
    for badge_a, badge_b, r in pair_updates:
        update_pair(ledger_dict, badge_a, badge_b, r, day_label)


def main(parquet_path):
    day_label = day_label_from_path(parquet_path)
    print(f"Computing daily correlations for {day_label} ...")

    badges, signatures, lats, lons = read_day_signatures(parquet_path)
    print(f"  {len(badges):,} badges with sufficient data")

    pair_updates = compute_daily_correlations(badges, signatures, lats, lons)
    print(f"  {len(pair_updates):,} pair correlations computed")

    print("  Loading ledger ...")
    ledger_df = load_ledger()
    ledger_dict = ledger_to_dict(ledger_df)
    del ledger_df
    gc.collect()

    apply_updates_to_dict(ledger_dict, pair_updates, day_label)

    print(f"  Writing ledger ({len(ledger_dict):,} unique pairs) ...")
    save_ledger(dict_to_ledger(ledger_dict))
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python daily_correlate.py <parquet_path>")
    main(sys.argv[1])
