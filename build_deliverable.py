# build_deliverable.py
"""
Turn the consensus report into the files you actually hand to coworkers.

consensus_report.csv contains every (badge, recommended transformer) pair
either model surfaced, tiered by how strongly the two agree. Only the
`both_high_confidence` tier is defensible as an action list: two
methodologically independent models, each passing four gates including
two cross-run stability gates, converging on the same recommendation.

Writes into data/outputs/deliverables/:

    both_high_confidence.csv        the full agreed set
    action_cross_feeder.csv         field team  - strongest signal
    action_new_assignment.csv       GIS team    - first-time mappings
    review_same_feeder.csv          validation candidates, NOT actions
    README.txt                      what each file is and who gets it

Usage:
    python build_deliverable.py
    python build_deliverable.py --tier v2_high_confidence_only
"""

import argparse
import os

import pandas as pd

CONSENSUS = "data/outputs/consensus_report.csv"
OUT_DIR = "data/outputs/deliverables"

# (RECOMMENDATION_TYPE, output filename, who it goes to)
SPLITS = [
    (
        "cross_feeder_likely_gis_error",
        "action_cross_feeder.csv",
        "Field team - highest signal. Both models agree the meter correlates "
        "with a transformer on a DIFFERENT feeder, which is hard to explain "
        "except as a GIS error.",
    ),
    (
        "new_assignment",
        "action_new_assignment.csv",
        "GIS team - meters with no transformer assigned in GIS that the model "
        "clustered with mapped peers. First-time mappings, not disputes.",
    ),
    (
        "same_feeder_ambiguous",
        "review_same_feeder.csv",
        "NOT an action list. Meters on a shared feeder produce near-identical "
        "voltage signatures regardless of transformer; field validation has "
        "confirmed false positives here. Send only as validation candidates.",
    ),
]


def main():
    parser = argparse.ArgumentParser(
        description="Build customer deliverables from the consensus report."
    )
    parser.add_argument(
        "--tier",
        default="both_high_confidence",
        help="Consensus tier to extract (default: both_high_confidence).",
    )
    parser.add_argument(
        "--out-dir", default=OUT_DIR, help=f"Output folder (default: {OUT_DIR})."
    )
    args = parser.parse_args()

    if not os.path.exists(CONSENSUS):
        raise SystemExit(
            f"{CONSENSUS} not found. Run the v1 pipeline and then "
            f"consensus_report.py first - the consensus report needs both "
            f"models to be current."
        )

    df = pd.read_csv(CONSENSUS, dtype=str)
    print(f"Read {CONSENSUS}: {len(df):,} rows")

    if "CONSENSUS_TIER" not in df.columns:
        raise SystemExit("consensus_report.csv has no CONSENSUS_TIER column.")

    print()
    print("Tier breakdown:")
    print(df["CONSENSUS_TIER"].value_counts().to_string())

    tier = df[df["CONSENSUS_TIER"] == args.tier].copy()
    if tier.empty:
        print()
        print(f"WARNING: no rows in tier '{args.tier}'.")
        print("If this is unexpected, the usual cause is a stale v1 model -")
        print("the two models have to be built from the same era of data for")
        print("them to agree on anything.")

    os.makedirs(args.out_dir, exist_ok=True)

    main_path = os.path.join(args.out_dir, "both_high_confidence.csv")
    tier.to_csv(main_path, index=False)
    print()
    print(f"Wrote {main_path}: {len(tier):,} rows")

    readme = [
        "M2T model deliverables",
        "=" * 60,
        "",
        f"Source:        {CONSENSUS}",
        f"Tier selected: {args.tier}",
        f"Rows:          {len(tier):,}",
        "",
        "both_high_confidence.csv",
        "  Every recommendation both models agree on at high confidence.",
        "  Use the split files below to route work; this is the superset.",
        "",
    ]

    has_type = "RECOMMENDATION_TYPE" in tier.columns
    if has_type and not tier.empty:
        print()
        print("Recommendation type breakdown:")
        print(tier["RECOMMENDATION_TYPE"].value_counts().to_string())
        print()

    for rec_type, filename, note in SPLITS:
        if not has_type:
            continue
        subset = tier[tier["RECOMMENDATION_TYPE"] == rec_type]
        path = os.path.join(args.out_dir, filename)
        subset.to_csv(path, index=False)
        print(f"Wrote {path}: {len(subset):,} rows")
        readme.extend([filename, f"  ({len(subset):,} rows) {note}", ""])

    readme.extend(
        [
            "Also worth sending to the GIS team (from data/outputs_v2/):",
            "  badges_missing_from_gis.csv   meters GIS has no row for",
            "  latlon_discrepancies.csv      model vs GIS coordinates > 100 m apart",
            "",
            "Do NOT send full_clusters_enriched.csv (218K rows) - that is an",
            "internal join asset, not a deliverable.",
            "",
        ]
    )

    readme_path = os.path.join(args.out_dir, "README.txt")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(readme))
    print(f"Wrote {readme_path}")


if __name__ == "__main__":
    main()
