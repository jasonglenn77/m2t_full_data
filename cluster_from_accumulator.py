# cluster_from_accumulator.py
"""
Cluster meters using the v2 pair-correlation ledger.

For each pair in data/state/pair_ledger.parquet:
  1. Compute statistics across its rolling daily-correlation window:
       mean_r   - mean of recent daily Pearsons
       std_r    - standard deviation of those values
       trend    - mean(last third) - mean(first third)
  2. Apply three gates simultaneously:
       n_recent >= V2_MIN_DAYS         (enough history to trust the stats)
       mean_r   >= V2_THRESHOLD         (relationship is consistently strong)
       std_r    <= V2_STD_MAX           (relationship is stable, not noisy)
       trend    >= V2_TREND_MIN         (relationship is not deteriorating)
  3. Edge weight = mean_r for surviving pairs.

Then apply the same mutual top-K filter v1 uses, run connected
components, and write data/outputs_v2/full_clusters.csv.

The v1 model is untouched; this writes only into data/outputs_v2/.
"""

import gc
import os

import networkx as nx
import numpy as np
import pandas as pd

from config import TOP_K_NEIGHBORS
from pair_accumulator import PairLedger

OUT_DIR = "data/outputs_v2"
SIG_BADGE_IDS = "data/processed/signatures/badge_ids.npy"
CLUSTERS_OUT = os.path.join(OUT_DIR, "full_clusters.csv")
EXCLUDED_OUT = os.path.join(OUT_DIR, "excluded_missing_coords.csv")

# v2 gate thresholds. Mean threshold parallels v1's CORRELATION_THRESHOLD.
# Std and trend caps are the new structural defenses against false
# positives in same-feeder cases.
V2_THRESHOLD = 0.96
V2_STD_MAX = 0.05
V2_TREND_MIN = -0.05
V2_MIN_DAYS = 15


def compute_per_pair_stats(ledger):
    """
    Compute mean_r, std_r, trend, and n_recent for every pair in the
    ledger using vectorized numpy where possible.

    Returns:
        mean_r:   (n,) float32 — per-pair mean over valid window slots
        std_r:    (n,) float32 — per-pair standard deviation
        trend:    (n,) float32 — mean(last third) - mean(first third), 0 if n<6
        n_recent: (n,) int16   — number of valid window slots per pair
    """
    n = ledger.n
    daily = ledger.daily_values[:n]
    n_recent = ledger.n_recent[:n].copy()

    n_days_max = daily.shape[1]
    cols = np.arange(n_days_max)
    valid_mask = cols[None, :] < n_recent[:, None]

    n_recent_f = n_recent.astype(np.float32)
    n_recent_safe = np.maximum(n_recent_f, 1.0)

    daily_for_mean = np.where(valid_mask, daily, 0.0)
    sums = daily_for_mean.sum(axis=1)
    mean_r = (sums / n_recent_safe).astype(np.float32)

    daily_sq = np.where(valid_mask, daily * daily, 0.0)
    sums_sq = daily_sq.sum(axis=1)
    mean_sq = sums_sq / n_recent_safe
    var_r = np.maximum(mean_sq - mean_r.astype(np.float64) ** 2, 0.0)
    std_r = np.sqrt(var_r).astype(np.float32)

    # Trend requires a per-pair loop because each pair's slice depends on n_recent[i].
    # Only compute for pairs with n_recent >= 6; others get 0.
    trend = np.zeros(n, dtype=np.float32)
    enough = n_recent >= 6
    indices = np.where(enough)[0]
    for i in indices:
        ni = int(n_recent[i])
        k = ni // 3
        first_mean = float(daily[i, :k].mean())
        last_mean = float(daily[i, ni - k : ni].mean())
        trend[i] = last_mean - first_mean

    return mean_r, std_r, trend, n_recent


def apply_gates(mean_r, std_r, trend, n_recent):
    """Boolean mask of pairs passing all four gates."""
    return (
        (n_recent >= V2_MIN_DAYS)
        & (mean_r >= V2_THRESHOLD)
        & (std_r <= V2_STD_MAX)
        & (trend > V2_TREND_MIN)
    )


def build_mutual_top_k_edges(badge_a, badge_b, weights, top_k):
    """
    Mutual top-K edge filter.

    For each badge, keep its top-K strongest edges by weight. Then keep
    only pairs where both meters chose each other in their top-K.

    Returns: list of (BADGE_A, BADGE_B) tuples in canonical (A < B) order.
    """
    forward = pd.DataFrame({"FROM": badge_a, "TO": badge_b, "W": weights})
    backward = pd.DataFrame({"FROM": badge_b, "TO": badge_a, "W": weights})
    long_df = pd.concat([forward, backward], ignore_index=True)
    del forward, backward
    gc.collect()

    long_df = long_df.sort_values("W", ascending=False, kind="stable")
    top_k_df = long_df.groupby("FROM", sort=False).head(top_k).copy()
    del long_df
    gc.collect()

    lo = np.where(
        top_k_df["FROM"].values < top_k_df["TO"].values,
        top_k_df["FROM"].values,
        top_k_df["TO"].values,
    )
    hi = np.where(
        top_k_df["FROM"].values < top_k_df["TO"].values,
        top_k_df["TO"].values,
        top_k_df["FROM"].values,
    )
    top_k_df = top_k_df.assign(LO=lo, HI=hi)

    pair_counts = top_k_df.groupby(["LO", "HI"]).size()
    mutual_pairs = pair_counts[pair_counts == 2].index.tolist()
    return mutual_pairs


def load_full_badge_universe():
    """All badges to potentially cluster: prefer the signature store's
    badge_ids.npy so v2 clusters cover the same meter universe as v1.
    Falls back to ledger membership if the file is absent."""
    if os.path.exists(SIG_BADGE_IDS):
        ids = np.load(SIG_BADGE_IDS, allow_pickle=True)
        return [str(b) for b in ids]
    return None


def main():
    print("Loading pair ledger ...")
    ledger = PairLedger.load()
    n = len(ledger)
    print(f"  {n:,} pairs loaded; most recent day: {ledger.most_recent_day()}")

    print("Computing per-pair statistics ...")
    mean_r, std_r, trend, n_recent = compute_per_pair_stats(ledger)

    # Detach the per-pair badge IDs from the ledger so we can free its big arrays
    badge_a_all = ledger.badge_a[:n].copy()
    badge_b_all = ledger.badge_b[:n].copy()
    del ledger
    gc.collect()

    print(
        f"Applying gates: n >= {V2_MIN_DAYS}, mean >= {V2_THRESHOLD}, "
        f"std <= {V2_STD_MAX}, trend > {V2_TREND_MIN}"
    )
    keep = apply_gates(mean_r, std_r, trend, n_recent)
    n_pass = int(keep.sum())
    print(f"  {n_pass:,} of {n:,} pairs pass ({100 * n_pass / n:.1f}%)")

    badge_a = badge_a_all[keep]
    badge_b = badge_b_all[keep]
    weights = mean_r[keep].astype(np.float32)

    del mean_r, std_r, trend, n_recent
    del badge_a_all, badge_b_all
    del keep
    gc.collect()

    print(f"Building mutual top-{TOP_K_NEIGHBORS} edges ...")
    mutual_edges = build_mutual_top_k_edges(
        badge_a, badge_b, weights, TOP_K_NEIGHBORS
    )
    print(f"  {len(mutual_edges):,} mutual edges retained")

    del badge_a, badge_b, weights
    gc.collect()

    all_badges = load_full_badge_universe()
    if all_badges is None:
        ledger_badges = set()
        for a, b in mutual_edges:
            ledger_badges.add(a)
            ledger_badges.add(b)
        all_badges = sorted(ledger_badges)
        print(
            f"  Signature store badge_ids.npy not found; using "
            f"{len(all_badges):,} badges from the mutual-edge set"
        )
    else:
        print(f"  Using {len(all_badges):,} badges from signature store")

    print("Building connected components ...")
    g = nx.Graph()
    g.add_nodes_from(all_badges)
    g.add_edges_from(mutual_edges)
    components = list(nx.connected_components(g))
    print(f"  {len(components):,} clusters")

    print(f"Writing {CLUSTERS_OUT} ...")
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    for cid, comp in enumerate(components):
        for badge in comp:
            rows.append((badge, cid))
    out = pd.DataFrame(rows, columns=["BADGE", "CLUSTER"])
    out.to_csv(CLUSTERS_OUT, index=False)
    print(f"  {len(out):,} badges in {len(components):,} clusters")

    # Mirror v1's empty excluded_missing_coords.csv so downstream wrappers
    # can find what they expect.
    pd.DataFrame({"BADGE": []}).to_csv(EXCLUDED_OUT, index=False)


if __name__ == "__main__":
    main()
