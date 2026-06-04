# compare_runs.py
"""
Compare two M2T pipeline output folders and produce a polished diff report.

Usage:
    python compare_runs.py <baseline_dir> [current_dir]

If <current_dir> is omitted, it defaults to data/outputs.
Both folders must contain full_clusters.csv. transformer_corrections.csv
is compared if both folders have it.

Output: a `comparison_<timestamp>` folder created inside <current_dir>,
containing a printable summary.txt plus detail CSVs.
"""

import argparse
import os
from datetime import datetime

import pandas as pd


def load_clusters(path):
    return pd.read_csv(path, dtype={"BADGE": str, "CLUSTER": str})


def cluster_membership(df):
    out = {}
    for _, g in df.groupby("CLUSTER"):
        members = frozenset(g["BADGE"].tolist())
        for b in members:
            out[b] = members - {b}
    return out


def cluster_signatures(df):
    return {cid: frozenset(g["BADGE"]) for cid, g in df.groupby("CLUSTER")}


def diff_corrections(base_path, cur_path):
    bc = pd.read_csv(base_path, dtype={"BADGE": str})
    nc = pd.read_csv(cur_path, dtype={"BADGE": str})
    merged = bc.merge(
        nc, on="BADGE", how="outer", suffixes=("_OLD", "_NEW"), indicator=True
    )

    def status(row):
        if row["_merge"] == "left_only":
            return "no_longer_flagged"
        if row["_merge"] == "right_only":
            return "newly_flagged"
        old = row.get("RECOMMENDED_TRANSFORMER_OLD")
        new = row.get("RECOMMENDED_TRANSFORMER_NEW")
        if pd.isna(old) and pd.isna(new):
            return "unchanged"
        if old != new:
            return "recommendation_changed"
        return "unchanged"

    merged["STATUS"] = merged.apply(status, axis=1)
    return merged.drop(columns=["_merge"])


def main():
    parser = argparse.ArgumentParser(
        description="Compare two M2T output folders (cluster-aware diff)."
    )
    parser.add_argument("baseline", help="Path to the baseline outputs folder.")
    parser.add_argument(
        "current",
        nargs="?",
        default="data/outputs",
        help="Path to the current outputs folder (default: data/outputs).",
    )
    args = parser.parse_args()

    base_dir = args.baseline
    cur_dir = args.current

    base_clusters = load_clusters(os.path.join(base_dir, "full_clusters.csv"))
    cur_clusters = load_clusters(os.path.join(cur_dir, "full_clusters.csv"))

    base_mem = cluster_membership(base_clusters)
    cur_mem = cluster_membership(cur_clusters)
    base_sigs = cluster_signatures(base_clusters)
    cur_sigs = cluster_signatures(cur_clusters)

    base_badges = set(base_mem)
    cur_badges = set(cur_mem)
    common = base_badges & cur_badges
    added = cur_badges - base_badges
    dropped = base_badges - cur_badges

    badge_rows = []
    for b in sorted(common):
        old_peers = base_mem[b]
        new_peers = cur_mem[b]
        if old_peers == new_peers:
            continue
        gained = new_peers - old_peers
        lost = old_peers - new_peers
        union = old_peers | new_peers
        jaccard = (len(old_peers & new_peers) / len(union)) if union else 1.0
        badge_rows.append(
            {
                "BADGE": b,
                "OLD_CLUSTER_SIZE": len(old_peers) + 1,
                "NEW_CLUSTER_SIZE": len(new_peers) + 1,
                "GAINED_PEERS": len(gained),
                "LOST_PEERS": len(lost),
                "JACCARD_PEERS": round(jaccard, 4),
            }
        )
    badge_changes = pd.DataFrame(
        badge_rows,
        columns=[
            "BADGE",
            "OLD_CLUSTER_SIZE",
            "NEW_CLUSTER_SIZE",
            "GAINED_PEERS",
            "LOST_PEERS",
            "JACCARD_PEERS",
        ],
    )

    base_b2c = dict(zip(base_clusters["BADGE"], base_clusters["CLUSTER"]))
    cur_b2c = dict(zip(cur_clusters["BADGE"], cur_clusters["CLUSTER"]))

    split_rows = []
    for old_cid, badges in base_sigs.items():
        bs = badges & common
        if len(bs) < 2:
            continue
        new_cids = {cur_b2c[b] for b in bs}
        if len(new_cids) > 1:
            split_rows.append(
                {
                    "OLD_CLUSTER": old_cid,
                    "OLD_SIZE": len(badges),
                    "BADGES_IN_COMMON": len(bs),
                    "SPLIT_INTO": len(new_cids),
                }
            )

    merge_rows = []
    for new_cid, badges in cur_sigs.items():
        bs = badges & common
        if len(bs) < 2:
            continue
        old_cids = {base_b2c[b] for b in bs}
        if len(old_cids) > 1:
            merge_rows.append(
                {
                    "NEW_CLUSTER": new_cid,
                    "NEW_SIZE": len(badges),
                    "BADGES_IN_COMMON": len(bs),
                    "MERGED_FROM": len(old_cids),
                }
            )

    base_sig_set = set(base_sigs.values())
    cur_sig_set = set(cur_sigs.values())
    identical = base_sig_set & cur_sig_set

    corr_diff = None
    base_corr_path = os.path.join(base_dir, "transformer_corrections.csv")
    cur_corr_path = os.path.join(cur_dir, "transformer_corrections.csv")
    if os.path.exists(base_corr_path) and os.path.exists(cur_corr_path):
        corr_diff = diff_corrections(base_corr_path, cur_corr_path)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = os.path.join(cur_dir, f"comparison_{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    lines = []

    def line(s=""):
        print(s)
        lines.append(s)

    line("=" * 60)
    line("M2T run comparison")
    line(f"  baseline: {base_dir}")
    line(f"  current:  {cur_dir}")
    line(f"  generated: {stamp}")
    line("=" * 60)
    line()
    line("--- Badges ---")
    line(f"  Baseline badges:     {len(base_badges):>8d}")
    line(f"  Current  badges:     {len(cur_badges):>8d}")
    line(f"  Added (new only):    {len(added):>8d}")
    line(f"  Dropped (gone):      {len(dropped):>8d}")
    line(f"  Common to both runs: {len(common):>8d}")
    line()
    line("--- Clusters ---")
    line(f"  Baseline clusters:        {len(base_sigs):>5d}")
    line(f"  Current  clusters:        {len(cur_sigs):>5d}")
    line(f"  Identical clusters:       {len(identical):>5d}")
    line(f"  Old clusters that split:  {len(split_rows):>5d}")
    line(f"  New clusters merged:      {len(merge_rows):>5d}")
    line()
    line("--- Membership churn (badges in common) ---")
    if not badge_changes.empty:
        moved = len(badge_changes)
        avg_jac = badge_changes["JACCARD_PEERS"].mean()
        line(f"  Badges with peer-set changes:    {moved:>5d}")
        line(f"  Mean peer-set Jaccard (movers):  {avg_jac:.3f}")
    else:
        line("  Badges with peer-set changes:        0")
    line()
    if corr_diff is not None:
        counts = corr_diff["STATUS"].value_counts()
        line("--- transformer_corrections.csv diff ---")
        for k in (
            "newly_flagged",
            "no_longer_flagged",
            "recommendation_changed",
            "unchanged",
        ):
            line(f"  {k:>26}: {int(counts.get(k, 0)):>5d}")
        line()
    line(f"Detail CSVs written to: {out_dir}")

    badge_changes.to_csv(os.path.join(out_dir, "badge_cluster_changes.csv"), index=False)
    pd.DataFrame(
        split_rows,
        columns=["OLD_CLUSTER", "OLD_SIZE", "BADGES_IN_COMMON", "SPLIT_INTO"],
    ).to_csv(os.path.join(out_dir, "old_clusters_that_split.csv"), index=False)
    pd.DataFrame(
        merge_rows,
        columns=["NEW_CLUSTER", "NEW_SIZE", "BADGES_IN_COMMON", "MERGED_FROM"],
    ).to_csv(os.path.join(out_dir, "new_clusters_that_merged.csv"), index=False)
    if corr_diff is not None:
        corr_diff.to_csv(
            os.path.join(out_dir, "transformer_corrections_diff.csv"), index=False
        )
    with open(os.path.join(out_dir, "summary.txt"), "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
