# record_run.py
"""
Append today's run outputs to the persistent history ledger so we can
track cluster stability and recommendation confidence over time.

Run this after each main pipeline (rerun_clustering.py /
find_mapping_errors.py / evaluate_results.py).

Usage:
    python record_run.py
        # Records current data/outputs/ under an auto-generated run_id
        # (UTC timestamp).

    python record_run.py 2026-05-11_baseline
        # Records under an explicit run_id.

    python record_run.py 2026-03-10_baseline --from-folder data/outputs_baseline_2026-03-10
        # Records a different folder under an explicit run_id (backfill).

The history is stored in data/state/history/ as four parquet files:
    runs.parquet                  — one row per recorded run
    badge_presence.parquet        — per-badge appearance counts
    pair_co_membership.parquet    — pairs of badges, with co-cluster counts
    recommendation_history.parquet — (badge, recommended_transformer) counts
"""

import argparse
import itertools
import os
from datetime import datetime, timezone

import pandas as pd

HISTORY_DIR = "data/state/history"
RUNS_FILE = os.path.join(HISTORY_DIR, "runs.parquet")
BADGE_PRESENCE_FILE = os.path.join(HISTORY_DIR, "badge_presence.parquet")
PAIR_COMEM_FILE = os.path.join(HISTORY_DIR, "pair_co_membership.parquet")
REC_HISTORY_FILE = os.path.join(HISTORY_DIR, "recommendation_history.parquet")


def load_or_empty(path, columns):
    if os.path.exists(path):
        return pd.read_parquet(path)
    return pd.DataFrame(columns=columns)


def normalize_id(series):
    return (
        series.astype(str)
        .str.replace(r"\.0+$", "", regex=True)
        .replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
    )


def main():
    parser = argparse.ArgumentParser(
        description="Record one M2T run into the historical stability ledger."
    )
    parser.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Optional run identifier (default: UTC timestamp).",
    )
    parser.add_argument(
        "--from-folder",
        default="data/outputs",
        help="Folder containing full_clusters.csv and transformer_corrections.csv (default: data/outputs).",
    )
    args = parser.parse_args()

    folder = args.from_folder
    clusters_path = os.path.join(folder, "full_clusters.csv")
    corrections_path = os.path.join(folder, "transformer_corrections.csv")

    if not os.path.exists(clusters_path):
        raise SystemExit(f"Could not find {clusters_path}")

    os.makedirs(HISTORY_DIR, exist_ok=True)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    print(f"Recording run '{run_id}' from {folder}")

    clusters = pd.read_csv(clusters_path, dtype=str)
    clusters["CLUSTER"] = normalize_id(clusters["CLUSTER"])
    clusters = clusters.dropna(subset=["BADGE"]).copy()

    corrections = None
    if os.path.exists(corrections_path):
        corrections = pd.read_csv(corrections_path, dtype=str)
        corrections["RECOMMENDED_TRANSFORMER"] = normalize_id(
            corrections["RECOMMENDED_TRANSFORMER"]
        )

    # 1. Append run metadata
    runs = load_or_empty(
        RUNS_FILE, ["RUN_ID", "RUN_DATE", "N_BADGES", "N_CLUSTERS", "SOURCE_FOLDER"]
    )
    if not runs.empty and run_id in runs["RUN_ID"].values:
        raise SystemExit(
            f"Run '{run_id}' already recorded. Delete it manually if you want to re-record."
        )

    new_run = pd.DataFrame(
        [
            {
                "RUN_ID": run_id,
                "RUN_DATE": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "N_BADGES": len(clusters),
                "N_CLUSTERS": clusters["CLUSTER"].nunique(),
                "SOURCE_FOLDER": folder,
            }
        ]
    )
    runs = pd.concat([runs, new_run], ignore_index=True)
    runs.to_parquet(RUNS_FILE, index=False)
    print(f"  Run metadata appended ({len(runs)} runs total).")

    # 2. Update badge presence
    presence = load_or_empty(
        BADGE_PRESENCE_FILE, ["BADGE", "N_RUNS_SEEN", "FIRST_RUN", "LAST_RUN"]
    )
    if not presence.empty:
        presence["N_RUNS_SEEN"] = presence["N_RUNS_SEEN"].astype(int)

    current_badges = pd.DataFrame({"BADGE": clusters["BADGE"].unique()})

    merged = current_badges.merge(presence, on="BADGE", how="left")
    merged["N_RUNS_SEEN"] = merged["N_RUNS_SEEN"].fillna(0).astype(int) + 1
    merged["FIRST_RUN"] = merged["FIRST_RUN"].fillna(run_id)
    merged["LAST_RUN"] = run_id

    old_only = presence[~presence["BADGE"].isin(current_badges["BADGE"])]
    presence = pd.concat([merged, old_only], ignore_index=True)
    presence.to_parquet(BADGE_PRESENCE_FILE, index=False)
    print(f"  Badge presence updated ({len(presence):,} unique badges tracked).")

    # 3. Update pair co-membership
    print("  Generating peer pairs from clusters ...")
    pair_records = []
    for cluster_id, group in clusters.groupby("CLUSTER"):
        members = sorted(group["BADGE"].tolist())
        if len(members) < 2:
            continue
        for a, b in itertools.combinations(members, 2):
            pair_records.append((a, b))

    if pair_records:
        new_pairs = pd.DataFrame(pair_records, columns=["BADGE_A", "BADGE_B"])
        new_pairs["INCREMENT"] = 1
        # Collapse duplicates within this run (shouldn't happen — same pair appears once per cluster — but be safe)
        new_pairs = new_pairs.groupby(["BADGE_A", "BADGE_B"], as_index=False)["INCREMENT"].sum()

        existing = load_or_empty(
            PAIR_COMEM_FILE,
            ["BADGE_A", "BADGE_B", "TIMES_TOGETHER", "FIRST_TOGETHER", "LAST_TOGETHER"],
        )
        if not existing.empty:
            existing["TIMES_TOGETHER"] = existing["TIMES_TOGETHER"].astype(int)

        combined = new_pairs.merge(
            existing, on=["BADGE_A", "BADGE_B"], how="outer", suffixes=("", "_old")
        )
        combined["TIMES_TOGETHER"] = combined["TIMES_TOGETHER"].fillna(0).astype(int) + combined["INCREMENT"].fillna(0).astype(int)
        combined["FIRST_TOGETHER"] = combined["FIRST_TOGETHER"].fillna(run_id)
        combined["LAST_TOGETHER"] = combined.apply(
            lambda r: run_id if pd.notna(r["INCREMENT"]) else r["LAST_TOGETHER"], axis=1
        )

        pairs_updated = combined[
            ["BADGE_A", "BADGE_B", "TIMES_TOGETHER", "FIRST_TOGETHER", "LAST_TOGETHER"]
        ]
        pairs_updated.to_parquet(PAIR_COMEM_FILE, index=False)
        print(
            f"  {len(new_pairs):,} pairs this run; "
            f"{len(pairs_updated):,} unique pairs in ledger."
        )

    # 4. Update recommendation history
    if corrections is not None and "RECOMMENDED_TRANSFORMER" in corrections.columns:
        new_recs = (
            corrections[["BADGE", "RECOMMENDED_TRANSFORMER"]]
            .dropna(subset=["RECOMMENDED_TRANSFORMER"])
            .drop_duplicates()
            .copy()
        )
        new_recs["INCREMENT"] = 1

        existing = load_or_empty(
            REC_HISTORY_FILE,
            [
                "BADGE",
                "RECOMMENDED_TRANSFORMER",
                "TIMES_RECOMMENDED",
                "FIRST_RUN",
                "LAST_RUN",
            ],
        )
        if not existing.empty:
            existing["TIMES_RECOMMENDED"] = existing["TIMES_RECOMMENDED"].astype(int)

        combined = new_recs.merge(
            existing,
            on=["BADGE", "RECOMMENDED_TRANSFORMER"],
            how="outer",
            suffixes=("", "_old"),
        )
        combined["TIMES_RECOMMENDED"] = combined["TIMES_RECOMMENDED"].fillna(0).astype(int) + combined["INCREMENT"].fillna(0).astype(int)
        combined["FIRST_RUN"] = combined["FIRST_RUN"].fillna(run_id)
        combined["LAST_RUN"] = combined.apply(
            lambda r: run_id if pd.notna(r["INCREMENT"]) else r["LAST_RUN"], axis=1
        )

        rec_updated = combined[
            [
                "BADGE",
                "RECOMMENDED_TRANSFORMER",
                "TIMES_RECOMMENDED",
                "FIRST_RUN",
                "LAST_RUN",
            ]
        ]
        rec_updated.to_parquet(REC_HISTORY_FILE, index=False)
        print(
            f"  {len(new_recs):,} (badge, transformer) recommendations this run; "
            f"{len(rec_updated):,} unique pairs in ledger."
        )

    print(f"Done recording run '{run_id}'.")


if __name__ == "__main__":
    main()
