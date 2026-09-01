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

# RECOMMENDATION_TYPE -> (priority, label shown in the combined file)
# Priority is what a recipient should work first, not model confidence:
# same_feeder rows can carry a perfect confidence gap and still be wrong,
# because meters on a shared feeder look alike from voltage alone.
PRIORITY = {
    "cross_feeder_likely_gis_error": (
        1,
        "ACT FIRST - wrong feeder, likely GIS error",
    ),
    "new_assignment": (
        2,
        "ACT - meter unmapped in GIS, both models agree where it belongs",
    ),
    "unknown_feeder": (
        3,
        "GIS DATA GAP - feeder missing, cannot classify until GIS is fixed",
    ),
    "same_feeder_ambiguous": (
        4,
        "DO NOT ACT - same feeder, unverifiable from voltage; field-check only",
    ),
}
UNKNOWN_PRIORITY = (9, "unclassified")

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

    # AGREED_CONFIDENCE_GAP: the LOWER of the two models' gaps, so the number
    # reflects what both models will stand behind rather than the more
    # optimistic one.
    for col in ("V1_CONFIDENCE_GAP", "V2_CONFIDENCE_GAP"):
        if col in tier.columns:
            tier[col + "_NUM"] = pd.to_numeric(tier[col], errors="coerce")
    gap_cols = [c for c in tier.columns if c.endswith("_CONFIDENCE_GAP_NUM")]
    if gap_cols:
        tier["AGREED_CONFIDENCE_GAP"] = tier[gap_cols].min(axis=1).round(4)
        tier = tier.drop(columns=gap_cols)
    else:
        tier["AGREED_CONFIDENCE_GAP"] = pd.NA

    if "RECOMMENDATION_TYPE" in tier.columns:
        tier["ACTION_PRIORITY"] = tier["RECOMMENDATION_TYPE"].map(
            lambda t: PRIORITY.get(t, UNKNOWN_PRIORITY)[0]
        )
        tier["ACTION"] = tier["RECOMMENDATION_TYPE"].map(
            lambda t: PRIORITY.get(t, UNKNOWN_PRIORITY)[1]
        )
    else:
        tier["ACTION_PRIORITY"] = UNKNOWN_PRIORITY[0]
        tier["ACTION"] = UNKNOWN_PRIORITY[1]

    tier = tier.sort_values(
        ["ACTION_PRIORITY", "AGREED_CONFIDENCE_GAP"],
        ascending=[True, False],
        kind="stable",
    )

    # Put the routing columns first so the file explains itself on open.
    lead = ["ACTION_PRIORITY", "ACTION", "RECOMMENDATION_TYPE",
            "AGREED_CONFIDENCE_GAP", "BADGE", "CURRENT_TRANSFORMER",
            "RECOMMENDED_TRANSFORMER"]
    ordered = [c for c in lead if c in tier.columns]
    tier = tier[ordered + [c for c in tier.columns if c not in ordered]]

    main_path = os.path.join(args.out_dir, "both_high_confidence.csv")
    tier.to_csv(main_path, index=False)
    print()
    print(f"Wrote {main_path}: {len(tier):,} rows")
    print("  (single-file deliverable: sorted by ACTION_PRIORITY, then confidence)")

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
        print("By action priority:")
        summary = (
            tier.groupby(["ACTION_PRIORITY", "ACTION"])
            .size()
            .reset_index(name="ROWS")
            .sort_values("ACTION_PRIORITY")
        )
        for _, r in summary.iterrows():
            print(f"  {r['ACTION_PRIORITY']}  {r['ROWS']:>6,}  {r['ACTION']}")

        # How many same-feeder rows survive various confidence cutoffs. Useful
        # when deciding whether to include any of them as a secondary tier.
        sf = tier[tier["RECOMMENDATION_TYPE"] == "same_feeder_ambiguous"]
        if not sf.empty and sf["AGREED_CONFIDENCE_GAP"].notna().any():
            print()
            print("same_feeder_ambiguous rows by AGREED_CONFIDENCE_GAP cutoff:")
            for cut in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
                n = int((sf["AGREED_CONFIDENCE_GAP"] >= cut).sum())
                print(f"  >= {cut:.1f} : {n:>6,}")
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
