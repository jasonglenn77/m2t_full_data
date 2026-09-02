# build_deliverable.py
"""
Turn the consensus report into the files you actually hand to coworkers.

consensus_report.csv contains every (badge, recommended transformer) pair
either model surfaced, tiered by how strongly the two agree. Only the
`both_high_confidence` tier is defensible as an action list: two
methodologically independent models, each passing four gates including
two cross-run stability gates, converging on the same recommendation.

Every row is in scope for field verification. The end goal is full
reconciliation of the meter-to-transformer population, so that when an
outage occurs the affected meters are known with confidence. The model
does not replace the field visit -- it decides where the visit goes.

Writes into data/outputs/deliverables/:

    field_verification_list.csv     the work list, tiered by priority
    action_cross_feeder.csv         priority 1, split out for convenience
    action_new_assignment.csv       priority 2, split out for convenience
    review_same_feeder.csv          priority 3, split out for convenience
    README.txt                      what each file is and how to work it

Usage:
    python build_deliverable.py
    python build_deliverable.py --tier v2_high_confidence_only
"""

import argparse
import os

import pandas as pd

CONSENSUS = "data/outputs/consensus_report.csv"
OUT_DIR = "data/outputs/deliverables"

# RECOMMENDATION_TYPE -> (priority, field action, what the model established)
#
# Every row here is in scope for field verification -- the end goal is full
# reconciliation of the meter-to-transformer population, so that outage impact
# is known with confidence. Priority orders the work by how much the model can
# establish on its own, NOT by whether a row is worth looking at:
#
#   1-2  model resolves it; the field visit confirms before re-mapping
#   3    model detects the mismatch but cannot say which transformer is right,
#        because voltage alone cannot separate transformers on one feeder --
#        the field visit decides it
#   4    blocked on missing GIS data, not on the model
PRIORITY = {
    "cross_feeder_likely_gis_error": (
        1,
        "Verify, then re-map if confirmed",
        "Meter's voltage tracks a transformer on a DIFFERENT feeder. Voltage "
        "should not correlate across feeders, so the GIS record is the likely "
        "error. Strongest evidence the model produces.",
    ),
    "new_assignment": (
        2,
        "Verify, then assign",
        "No transformer recorded in GIS for this meter. Both model versions "
        "agree which transformer it belongs to.",
    ),
    "same_feeder_ambiguous": (
        3,
        "Verify on site - model cannot resolve this one",
        "Both versions agree the record looks wrong, but the current and "
        "recommended transformers share a feeder, where voltage patterns are "
        "similar by nature. The model cannot tell which is correct; the field "
        "check decides. Sorted by strength of evidence.",
    ),
    "unknown_feeder": (
        4,
        "Hold - populate GIS feeder data first",
        "Feeder information is missing for one of the transformers involved, "
        "so this finding cannot be classified until GIS is updated.",
    ),
}
UNKNOWN_PRIORITY = (9, "Review", "Unclassified recommendation type.")

# Left blank for the field crew to complete and return, so the same file
# carries the work out and the reconciliation result back.
FIELD_TRACKING_COLS = [
    "FIELD_VERIFIED_Y_N",
    "FIELD_CONFIRMED_TRANSFORMER",
    "VERIFIED_BY",
    "VERIFIED_DATE",
    "FIELD_NOTES",
]

# (RECOMMENDATION_TYPE, output filename, note for the README)
SPLITS = [
    (
        "cross_feeder_likely_gis_error",
        "action_cross_feeder.csv",
        "Priority 1. The meter correlates with a transformer on a DIFFERENT "
        "feeder, which is hard to explain except as a GIS error. Verify, then "
        "re-map if confirmed.",
    ),
    (
        "new_assignment",
        "action_new_assignment.csv",
        "Priority 2. Meters with no transformer assigned in GIS that the model "
        "grouped with mapped peers. Verify, then assign.",
    ),
    (
        "same_feeder_ambiguous",
        "review_same_feeder.csv",
        "Priority 3, and the bulk of the field program. Both versions agree "
        "the record looks wrong, but the transformers involved share a feeder, "
        "so voltage cannot say which is correct - the field check decides. "
        "Sorted by how many weeks the recommendation has repeated; work from "
        "the top.",
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

    # Take the LOWER of the two versions' numbers, so each figure is the one
    # both will stand behind rather than the more optimistic of the pair.
    def agreed(v1_col, v2_col, out_col):
        cols = []
        for c in (v1_col, v2_col):
            if c in tier.columns:
                tier["_n_" + c] = pd.to_numeric(tier[c], errors="coerce")
                cols.append("_n_" + c)
        if cols:
            tier[out_col] = tier[cols].min(axis=1).round(4)
            tier.drop(columns=cols, inplace=True)
        else:
            tier[out_col] = pd.NA

    agreed("V1_CONFIDENCE_GAP", "V2_CONFIDENCE_GAP", "AGREED_CONFIDENCE_GAP")
    # Share of recorded weekly runs that produced this same recommendation.
    # For priority 3, where the model cannot resolve the answer itself, this
    # is the best available guide to which meters to send a crew to first.
    agreed(
        "V1_RECOMMENDATION_STABILITY",
        "V2_RECOMMENDATION_STABILITY",
        "WEEKS_REPEATED_SHARE",
    )

    if "RECOMMENDATION_TYPE" in tier.columns:
        for i, name in enumerate(("FIELD_PRIORITY", "FIELD_ACTION", "MODEL_BASIS")):
            tier[name] = tier["RECOMMENDATION_TYPE"].map(
                lambda t, i=i: PRIORITY.get(t, UNKNOWN_PRIORITY)[i]
            )
    else:
        tier["FIELD_PRIORITY"] = UNKNOWN_PRIORITY[0]
        tier["FIELD_ACTION"] = UNKNOWN_PRIORITY[1]
        tier["MODEL_BASIS"] = UNKNOWN_PRIORITY[2]

    tier = tier.sort_values(
        ["FIELD_PRIORITY", "WEEKS_REPEATED_SHARE", "AGREED_CONFIDENCE_GAP"],
        ascending=[True, False, False],
        kind="stable",
    )

    for c in FIELD_TRACKING_COLS:
        tier[c] = ""

    # Routing and identity first, then the blank columns the crew fills in,
    # then everything else.
    lead = ["FIELD_PRIORITY", "FIELD_ACTION", "RECOMMENDATION_TYPE",
            "WEEKS_REPEATED_SHARE", "AGREED_CONFIDENCE_GAP",
            "BADGE", "CURRENT_TRANSFORMER", "RECOMMENDED_TRANSFORMER"]
    lead = [c for c in lead if c in tier.columns]
    lead += FIELD_TRACKING_COLS + ["MODEL_BASIS"]
    tier = tier[lead + [c for c in tier.columns if c not in lead]]

    main_path = os.path.join(args.out_dir, "field_verification_list.csv")
    tier.to_csv(main_path, index=False)
    print()
    print(f"Wrote {main_path}: {len(tier):,} rows")
    print("  Sorted by FIELD_PRIORITY, then how many weeks the recommendation")
    print("  has repeated, then confidence. Blank columns at FIELD_VERIFIED_Y_N")
    print("  onward are for the crew to complete and return.")

    readme = [
        "M2T model - field verification work list",
        "=" * 62,
        "",
        f"Source:  {CONSENSUS}",
        f"Rows:    {len(tier):,} meters in field scope",
        "",
        "WHAT THIS IS",
        "  Meters where two independent models both disagree with the GIS",
        "  transformer assignment, with enough evidence across weekly runs to",
        "  be worth a field visit. The model does not replace the field check;",
        "  it decides where the check goes.",
        "",
        "  The goal is full reconciliation of the meter-to-transformer",
        "  population, so that when an outage occurs on a feeder or a",
        "  transformer, the affected meters are known with confidence.",
        "",
        "HOW TO WORK IT",
        "  field_verification_list.csv is the single work list. Sort is already",
        "  applied: FIELD_PRIORITY first, then how many weeks the same",
        "  recommendation has repeated, then confidence. Work from the top.",
        "",
        "  FIELD_PRIORITY reflects how much the model can establish on its own,",
        "  not whether a row is worth looking at. Priority 3 is the largest",
        "  group and still in scope - the model detects the mismatch but cannot",
        "  say which transformer is correct, so the field visit decides.",
        "",
        "  WEEKS_REPEATED_SHARE is the share of weekly runs that produced this",
        "  same recommendation. 1.0 means every week since the meter first",
        "  appeared. Within a priority, higher is a better field candidate.",
        "",
        "  The blank columns from FIELD_VERIFIED_Y_N onward are for the crew to",
        "  complete and return, so the same file carries the work out and the",
        "  result back.",
        "",
    ]

    has_type = "RECOMMENDATION_TYPE" in tier.columns
    if has_type and not tier.empty:
        print()
        print("Field scope by priority:")
        summary = (
            tier.groupby(["FIELD_PRIORITY", "FIELD_ACTION"])
            .size()
            .reset_index(name="ROWS")
            .sort_values("FIELD_PRIORITY")
        )
        for _, r in summary.iterrows():
            print(f"  {r['FIELD_PRIORITY']}  {r['ROWS']:>6,}  {r['FIELD_ACTION']}")
        print(f"     {len(tier):>6,}  TOTAL in field scope")

        # Priority 3 is the bulk of the field program and the model cannot rank
        # it by its own resolution, so show how it splits by repeat-rate. This
        # is what a crew supervisor needs to size and sequence the work.
        sf = tier[tier["RECOMMENDATION_TYPE"] == "same_feeder_ambiguous"]
        if not sf.empty and sf["WEEKS_REPEATED_SHARE"].notna().any():
            print()
            print("Priority 3 by share of weeks the recommendation repeated:")
            for cut in (1.0, 0.95, 0.9, 0.8, 0.7):
                n = int((sf["WEEKS_REPEATED_SHARE"] >= cut).sum())
                label = "every week" if cut == 1.0 else f">= {cut:.0%} of weeks"
                print(f"  {label:>18} : {n:>6,}")
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
            "SPLIT FILES",
            "  The three files above are subsets of field_verification_list.csv,",
            "  provided only if it is easier to assign them separately. Working",
            "  from the single list is equivalent.",
            "",
            "ALSO FOR THE GIS TEAM (from data/outputs_v2/)",
            "  badges_missing_from_gis.csv   meters reporting data with no GIS row",
            "  latlon_discrepancies.csv      GIS coordinates > 100 m from the data",
            "",
            "NOT FOR DISTRIBUTION",
            "  full_clusters_enriched.csv (218K rows) is an internal join asset.",
            "",
        ]
    )

    readme_path = os.path.join(args.out_dir, "README.txt")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(readme))
    print(f"Wrote {readme_path}")


if __name__ == "__main__":
    main()
