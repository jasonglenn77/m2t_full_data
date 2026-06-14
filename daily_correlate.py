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

    Vectorized: for each badge i, computes Pearson with all of its upper-
    triangle neighbors in one batched numpy operation. Much faster than
    a per-pair np.corrcoef call.

    Handles per-pair NaN masks correctly: positions where either signal
    is NaN are excluded from that pair's correlation (each pair has its
    own effective sample size).

    Skips pairs where overlap < MIN_DAILY_OVERLAP or either signal is
    constant within its mask (denominator zero, correlation undefined).
    """
    if len(badges) == 0:
        return []

    tree, coords = build_tree(lats, lons)
    neighbors = find_neighbors(tree, coords)

    pair_updates = []
    finite_mask = np.isfinite(signatures)  # (n_badges, 96) bool
    # Replace NaN with 0 once; pair_mask gates which positions contribute,
    # so the 0s are harmless for the Pearson math.
    sig_clean = np.where(finite_mask, signatures, 0.0).astype(np.float64)

    for i in range(len(badges)):
        all_js = neighbors[i]
        # Upper-triangle: skip self and already-processed pairs
        js = all_js[all_js > i]
        if len(js) == 0:
            continue

        sig_i = sig_clean[i]              # (96,)
        finite_i = finite_mask[i]         # (96,) bool
        sig_js = sig_clean[js]            # (n_j, 96)
        valid_js = finite_mask[js]        # (n_j, 96) bool

        pair_mask = finite_i[None, :] & valid_js  # (n_j, 96) bool
        n_per_pair = pair_mask.sum(axis=1)        # (n_j,)
        keep = n_per_pair >= MIN_DAILY_OVERLAP
        if not keep.any():
            continue

        js = js[keep]
        sig_js = sig_js[keep]
        pair_mask = pair_mask[keep]
        n_per_pair = n_per_pair[keep].astype(np.float64)

        # Per-pair means over each pair's own mask
        sum_i = (sig_i[None, :] * pair_mask).sum(axis=1)
        sum_j = (sig_js * pair_mask).sum(axis=1)
        mean_i = sum_i / n_per_pair
        mean_j = sum_j / n_per_pair

        # Centered values within the mask, zero outside
        centered_i = np.where(pair_mask, sig_i[None, :] - mean_i[:, None], 0.0)
        centered_j = np.where(pair_mask, sig_js - mean_j[:, None], 0.0)

        numerator = (centered_i * centered_j).sum(axis=1)
        norm_i_sq = (centered_i ** 2).sum(axis=1)
        norm_j_sq = (centered_j ** 2).sum(axis=1)
        denominator_sq = norm_i_sq * norm_j_sq

        valid_r = denominator_sq > 0
        if not valid_r.any():
            continue

        # Use a safe denominator for the division to avoid runtime warnings;
        # we mask out the invalid entries immediately after.
        rs = numerator / np.sqrt(np.where(valid_r, denominator_sq, 1.0))
        finite_r = valid_r & np.isfinite(rs)
        if not finite_r.any():
            continue

        badge_i_id = badges[i]
        for k in np.where(finite_r)[0]:
            j_idx = int(js[k])
            badge_j_id = badges[j_idx]
            r = float(rs[k])
            if badge_i_id < badge_j_id:
                pair_updates.append((badge_i_id, badge_j_id, r))
            else:
                pair_updates.append((badge_j_id, badge_i_id, r))

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
