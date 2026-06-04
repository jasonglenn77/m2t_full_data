# rank_corrections.py
"""
Rank flagged transformer corrections by signal strength so you can
prioritize field follow-up.

For each flagged BADGE this script asks: of the badges in this cluster
that have a known transformer, how many vote for the RECOMMENDED_TRANSFORMER
versus the CURRENT_TRANSFORMER? Larger gap, larger evidence.

Usage:
    python rank_corrections.py
        # Rank every row in data/outputs/transformer_corrections.csv

    python rank_corrections.py "data/outputs/comparison_<stamp>/transformer_corrections_diff.csv"
        # Rank only rows with STATUS == "newly_flagged" in the diff CSV

Output: data/outputs/corrections_ranked.csv
"""

import argparse
import pandas as pd

CLUSTERS = "data/outputs/full_clusters.csv"
CORRECTIONS = "data/outputs/transformer_corrections.csv"
KNOWN_MAPPING = "data/outputs/known_mapping.csv"
OUT = "data/outputs/corrections_ranked.csv"


def normalize_id(series):
    # known_mapping.csv stores transformer IDs as ints ("31358"); pipeline outputs
    # write them as floats ("31358.0"). Strip the trailing .0 so they merge.
    return (
        series.astype(str)
        .str.replace(r"\.0+$", "", regex=True)
        .replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
    )


def main():
    parser = argparse.ArgumentParser(
        description="Rank flagged transformer corrections by confidence."
    )
    parser.add_argument(
        "diff_csv",
        nargs="?",
        default=None,
        help="Optional transformer_corrections_diff.csv path; filters to newly_flagged.",
    )
    args = parser.parse_args()

    clusters = pd.read_csv(CLUSTERS, dtype=str)
    clusters["CLUSTER"] = normalize_id(clusters["CLUSTER"])

    truth = pd.read_csv(KNOWN_MAPPING, dtype=str).rename(
        columns={"badge": "BADGE", "transf_id": "TRANSFORMER"}
    )
    truth["TRANSFORMER"] = normalize_id(truth["TRANSFORMER"])

    cluster_size = (
        clusters.groupby("CLUSTER").size().rename("CLUSTER_SIZE").reset_index()
    )

    df = clusters.merge(truth, on="BADGE", how="left")
    mapped = df.dropna(subset=["TRANSFORMER"])
    mapped_size = (
        mapped.groupby("CLUSTER").size().rename("MAPPED_PEERS").reset_index()
    )
    votes = (
        mapped.groupby(["CLUSTER", "TRANSFORMER"])
        .size()
        .rename("VOTES")
        .reset_index()
    )

    if args.diff_csv:
        diff = pd.read_csv(args.diff_csv, dtype=str)
        candidates = (
            diff[diff["STATUS"] == "newly_flagged"][
                [
                    "BADGE",
                    "CURRENT_TRANSFORMER_NEW",
                    "RECOMMENDED_TRANSFORMER_NEW",
                    "CLUSTER_NEW",
                ]
            ]
            .rename(
                columns={
                    "CURRENT_TRANSFORMER_NEW": "CURRENT_TRANSFORMER",
                    "RECOMMENDED_TRANSFORMER_NEW": "RECOMMENDED_TRANSFORMER",
                    "CLUSTER_NEW": "CLUSTER",
                }
            )
            .copy()
        )
        source_label = f"newly_flagged in {args.diff_csv}"
    else:
        candidates = pd.read_csv(CORRECTIONS, dtype=str)
        source_label = CORRECTIONS

    candidates["CLUSTER"] = normalize_id(candidates["CLUSTER"])
    candidates["CURRENT_TRANSFORMER"] = normalize_id(candidates["CURRENT_TRANSFORMER"])
    candidates["RECOMMENDED_TRANSFORMER"] = normalize_id(candidates["RECOMMENDED_TRANSFORMER"])

    candidates = candidates.merge(
        votes.rename(
            columns={"TRANSFORMER": "CURRENT_TRANSFORMER", "VOTES": "CURRENT_VOTES"}
        ),
        on=["CLUSTER", "CURRENT_TRANSFORMER"],
        how="left",
    )
    candidates["CURRENT_VOTES"] = candidates["CURRENT_VOTES"].fillna(0).astype(int)

    candidates = candidates.merge(
        votes.rename(
            columns={
                "TRANSFORMER": "RECOMMENDED_TRANSFORMER",
                "VOTES": "MAJORITY_VOTES",
            }
        ),
        on=["CLUSTER", "RECOMMENDED_TRANSFORMER"],
        how="left",
    )
    candidates["MAJORITY_VOTES"] = candidates["MAJORITY_VOTES"].fillna(0).astype(int)

    candidates = candidates.merge(cluster_size, on="CLUSTER", how="left")
    candidates["CLUSTER_SIZE"] = candidates["CLUSTER_SIZE"].fillna(0).astype(int)
    candidates = candidates.merge(mapped_size, on="CLUSTER", how="left")
    candidates["MAPPED_PEERS"] = candidates["MAPPED_PEERS"].fillna(0).astype(int)

    denom = candidates["MAPPED_PEERS"].replace(0, pd.NA)
    candidates["MAJORITY_SHARE"] = (
        (candidates["MAJORITY_VOTES"] / denom).astype(float).round(4)
    )
    candidates["CONFIDENCE_GAP"] = (
        ((candidates["MAJORITY_VOTES"] - candidates["CURRENT_VOTES"]) / denom)
        .astype(float)
        .round(4)
    )

    ranked = candidates.sort_values(
        ["CONFIDENCE_GAP", "MAPPED_PEERS"], ascending=[False, False]
    ).reset_index(drop=True)

    cols = [
        "BADGE",
        "CURRENT_TRANSFORMER",
        "RECOMMENDED_TRANSFORMER",
        "CLUSTER",
        "CLUSTER_SIZE",
        "MAPPED_PEERS",
        "CURRENT_VOTES",
        "MAJORITY_VOTES",
        "MAJORITY_SHARE",
        "CONFIDENCE_GAP",
    ]
    ranked = ranked[cols]
    ranked.to_csv(OUT, index=False)

    print(f"Source:  {source_label}")
    print(f"Ranked:  {len(ranked):,} flagged corrections")
    if len(ranked):
        gap = ranked["CONFIDENCE_GAP"].dropna()
        print(f"  Median CONFIDENCE_GAP:           {gap.median():.3f}")
        print(f"  75th-percentile CONFIDENCE_GAP:  {gap.quantile(0.75):.3f}")
        strong = (
            (ranked["CONFIDENCE_GAP"] >= 0.5) & (ranked["MAPPED_PEERS"] >= 4)
        ).sum()
        print(f"  Strong-signal rows (gap >= 0.5 AND MAPPED_PEERS >= 4): {strong:,}")
        print("\nTop 10:")
        print(ranked.head(10).to_string(index=False))
    print(f"\nWrote: {OUT}")


if __name__ == "__main__":
    main()
