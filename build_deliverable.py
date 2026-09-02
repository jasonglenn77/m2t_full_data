# build_deliverable.py
"""
Turn the consensus report into the files handed to the GIS and field teams.

Two audiences, two files:

  field_verification_list.csv   meters with evidence strong enough to send a
                                crew to this week. Tiered by priority.
  full_difference_list.csv      EVERY meter where either model disagrees with
                                GIS, categorised and ranked. The complete
                                record for GIS to load and work down over time.

Every row in both files is in scope for verification eventually. The end goal
is full reconciliation of the meter-to-transformer population, so that when an
outage occurs the affected meters are known with confidence. The model does not
replace the field check -- it decides where the check goes first.

Usage:
    python build_deliverable.py
    python build_deliverable.py --no-full
    python build_deliverable.py --tier v2_high_confidence_only
"""

import argparse
import os

import pandas as pd

CONSENSUS = "data/outputs/consensus_report.csv"
OUT_DIR = "data/outputs/deliverables"

# RECOMMENDATION_TYPE -> (priority, field action, what the model established)
#
# Priority orders the work by how much the model can establish on its own,
# NOT by whether a row is worth looking at:
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
        "check decides.",
    ),
    "unknown_feeder": (
        4,
        "Hold - populate GIS feeder data first",
        "Feeder information is missing for one of the transformers involved, "
        "so this finding cannot be classified until GIS is updated.",
    ),
}
UNKNOWN_PRIORITY = (9, "Review", "Unclassified recommendation type.")

# CONSENSUS_TIER -> (rank, plain-language strength of evidence)
EVIDENCE = {
    "both_high_confidence": (1, "Both models agree, high confidence"),
    "v1_high_confidence_only": (2, "One model high confidence, other silent"),
    "v2_high_confidence_only": (2, "One model high confidence, other silent"),
    "v1_high_confidence_v2_seen": (2, "One model high confidence, other agrees weakly"),
    "v2_high_confidence_v1_seen": (2, "One model high confidence, other agrees weakly"),
    "both_seen": (3, "Both models flag it, neither at high confidence"),
    "v1_only": (4, "One model only, below confidence threshold"),
    "v2_only": (4, "One model only, below confidence threshold"),
}
UNKNOWN_EVIDENCE = (9, "Unclassified")

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
        "so voltage cannot say which is correct - the field check decides.",
    ),
]


def agreed_min(df, v1_col, v2_col, out_col):
    """Take the LOWER of the two versions' numbers, so the figure is the one
    both will stand behind rather than the more optimistic of the pair."""
    cols = []
    for c in (v1_col, v2_col):
        if c in df.columns:
            df["_n_" + c] = pd.to_numeric(df[c], errors="coerce")
            cols.append("_n_" + c)
    if cols:
        df[out_col] = df[cols].min(axis=1).round(4)
        df.drop(columns=cols, inplace=True)
    else:
        df[out_col] = pd.NA


def annotate(df):
    """Add the routing columns both deliverables share."""
    agreed_min(df, "V1_CONFIDENCE_GAP", "V2_CONFIDENCE_GAP", "AGREED_CONFIDENCE_GAP")
    # Share of recorded weekly runs producing this same recommendation. Where
    # the model cannot resolve the answer itself, this is the best available
    # guide to which meters to send a crew to first.
    agreed_min(
        df,
        "V1_RECOMMENDATION_STABILITY",
        "V2_RECOMMENDATION_STABILITY",
        "WEEKS_REPEATED_SHARE",
    )

    if "RECOMMENDATION_TYPE" in df.columns:
        for i, name in enumerate(("FIELD_PRIORITY", "FIELD_ACTION", "MODEL_BASIS")):
            df[name] = df["RECOMMENDATION_TYPE"].map(
                lambda t, i=i: PRIORITY.get(t, UNKNOWN_PRIORITY)[i]
            )
    else:
        df["FIELD_PRIORITY"] = UNKNOWN_PRIORITY[0]
        df["FIELD_ACTION"] = UNKNOWN_PRIORITY[1]
        df["MODEL_BASIS"] = UNKNOWN_PRIORITY[2]

    if "CONSENSUS_TIER" in df.columns:
        df["EVIDENCE_RANK"] = df["CONSENSUS_TIER"].map(
            lambda t: EVIDENCE.get(t, UNKNOWN_EVIDENCE)[0]
        )
        df["EVIDENCE"] = df["CONSENSUS_TIER"].map(
            lambda t: EVIDENCE.get(t, UNKNOWN_EVIDENCE)[1]
        )
    return df


def order_columns(df, lead):
    lead = [c for c in lead if c in df.columns]
    return df[lead + [c for c in df.columns if c not in lead]]


def write_field_list(tier, out_dir):
    tier = tier.sort_values(
        ["FIELD_PRIORITY", "WEEKS_REPEATED_SHARE", "AGREED_CONFIDENCE_GAP"],
        ascending=[True, False, False],
        kind="stable",
    ).copy()
    for c in FIELD_TRACKING_COLS:
        tier[c] = ""

    lead = ["FIELD_PRIORITY", "FIELD_ACTION", "RECOMMENDATION_TYPE",
            "WEEKS_REPEATED_SHARE", "AGREED_CONFIDENCE_GAP",
            "BADGE", "CURRENT_TRANSFORMER", "RECOMMENDED_TRANSFORMER"]
    lead += FIELD_TRACKING_COLS + ["MODEL_BASIS"]
    tier = order_columns(tier, lead)

    path = os.path.join(out_dir, "field_verification_list.csv")
    tier.to_csv(path, index=False)
    print()
    print(f"Wrote {path}: {len(tier):,} rows")
    print("  This week's field scope. Sorted by FIELD_PRIORITY, then how many")
    print("  weeks the recommendation has repeated, then confidence.")
    return tier


def write_full_list(df, out_dir):
    """Every disagreement either model produced -- the complete record for GIS
    to hold and work down over time, not just this week's field scope."""
    full = df.sort_values(
        ["EVIDENCE_RANK", "FIELD_PRIORITY", "WEEKS_REPEATED_SHARE",
         "AGREED_CONFIDENCE_GAP"],
        ascending=[True, True, False, False],
        kind="stable",
    ).copy()

    lead = ["EVIDENCE_RANK", "EVIDENCE", "FIELD_PRIORITY", "FIELD_ACTION",
            "RECOMMENDATION_TYPE", "WEEKS_REPEATED_SHARE",
            "AGREED_CONFIDENCE_GAP", "BADGE", "CURRENT_TRANSFORMER",
            "RECOMMENDED_TRANSFORMER", "CONSENSUS_TIER"]
    full = order_columns(full, lead)

    path = os.path.join(out_dir, "full_difference_list.csv")
    full.to_csv(path, index=False)
    print()
    print(f"Wrote {path}: {len(full):,} rows")
    print("  Complete record of every model/GIS disagreement, ranked by")
    print("  strength of evidence then field priority.")

    print()
    print("Full population by strength of evidence:")
    summary = (
        full.groupby(["EVIDENCE_RANK", "EVIDENCE"])
        .size()
        .reset_index(name="ROWS")
        .sort_values("EVIDENCE_RANK")
    )
    for _, r in summary.iterrows():
        print(f"  {r['EVIDENCE_RANK']}  {r['ROWS']:>6,}  {r['EVIDENCE']}")
    print(f"     {len(full):>6,}  TOTAL disagreements on record")
    return full


def write_readme(out_dir, n_field, n_full, tier_name):
    lines = [
        "M2T model - meter-to-transformer verification",
        "=" * 62,
        "",
        f"Source:  {CONSENSUS}",
        "",
        "WHAT THIS IS",
        "  Two independently built models group meters by the voltage they",
        "  already report, without reference to GIS, then compare those groups",
        "  against the GIS transformer assignments. Where they disagree, that",
        "  disagreement is the finding.",
        "",
        "  The model does not replace the field check; it decides where the",
        "  check goes first. The goal is full reconciliation of the",
        "  meter-to-transformer population, so that when an outage occurs on a",
        "  feeder or a transformer, the affected meters are known with",
        "  confidence.",
        "",
        "THE TWO LISTS",
        "",
        f"  field_verification_list.csv   ({n_field:,} rows)",
        "    This week's field scope: disagreements with evidence strong",
        "    enough now to justify sending a crew. Work top down.",
        "",
        f"  full_difference_list.csv      ({n_full:,} rows)",
        "    The complete record - every meter either model disagrees with GIS",
        "    on, including those not yet well enough evidenced for the field",
        "    list. Ranked so it can be worked down over time. EVIDENCE_RANK 1",
        "    is the field list above; ranks 2-4 are progressively weaker and",
        "    will move up as more weekly runs accumulate.",
        "",
        "COLUMNS THAT MATTER",
        "  FIELD_PRIORITY          How much the model could establish on its",
        "                          own - NOT whether a row is worth looking at.",
        "                          Priority 3 is the largest group and still in",
        "                          scope: the model detects the mismatch but",
        "                          cannot say which transformer is correct, so",
        "                          the field visit decides.",
        "  WEEKS_REPEATED_SHARE    Share of weekly runs producing this same",
        "                          recommendation. 1.0 means every week. Within",
        "                          a priority, higher is a better candidate.",
        "  AGREED_CONFIDENCE_GAP   The more conservative of the two models'",
        "                          confidence scores.",
        "  MODEL_BASIS             What the model did and did not establish.",
        "",
        "RETURNING RESULTS",
        "  The blank columns from FIELD_VERIFIED_Y_N onward are for the crew.",
        "  Record what was found on site:",
        "",
        "    FIELD_CONFIRMED_TRANSFORMER = the RECOMMENDED_TRANSFORMER",
        "      -> the model was right. Update GIS. The model reads the updated",
        "         GIS extract next Monday and reports the meter as resolved.",
        "",
        "    FIELD_CONFIRMED_TRANSFORMER = the CURRENT_TRANSFORMER",
        "      -> GIS was already right. Return the file and the meter is",
        "         suppressed from future lists.",
        "",
        "  Either way, return the completed file. Both outcomes are progress;",
        "  a confirmed GIS record is as useful as a corrected one.",
        "",
    ]

    for rec_type, filename, note in SPLITS:
        lines.extend([f"  {filename}", f"    {note}", ""])

    lines.extend(
        [
            "SPLIT FILES",
            "  The three files above are subsets of field_verification_list.csv,",
            "  provided only if it is easier to assign them separately.",
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

    path = os.path.join(out_dir, "README.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"Wrote {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Build the field and full-record deliverables."
    )
    parser.add_argument(
        "--tier",
        default="both_high_confidence",
        help="Consensus tier for the field list (default: both_high_confidence).",
    )
    parser.add_argument(
        "--no-full",
        action="store_true",
        help="Skip full_difference_list.csv (the complete GIS record).",
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

    df = annotate(df)
    os.makedirs(args.out_dir, exist_ok=True)

    tier = df[df["CONSENSUS_TIER"] == args.tier].copy()
    if tier.empty:
        print()
        print(f"WARNING: no rows in tier '{args.tier}'.")
        print("The usual cause is a stale v1 model - the two models have to be")
        print("built from the same era of data for them to agree on anything.")

    tier = write_field_list(tier, args.out_dir)

    if "RECOMMENDATION_TYPE" in tier.columns and not tier.empty:
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

        sf = tier[tier["RECOMMENDATION_TYPE"] == "same_feeder_ambiguous"]
        if not sf.empty and sf["WEEKS_REPEATED_SHARE"].notna().any():
            print()
            print("Priority 3 by share of weeks the recommendation repeated:")
            for cut in (1.0, 0.9, 0.8, 0.7):
                n = int((sf["WEEKS_REPEATED_SHARE"] >= cut).sum())
                label = "every week" if cut == 1.0 else f">= {cut:.0%} of weeks"
                print(f"  {label:>18} : {n:>6,}")

    n_full = 0
    if not args.no_full:
        n_full = len(write_full_list(df, args.out_dir))

    print()
    for rec_type, filename, _ in SPLITS:
        if "RECOMMENDATION_TYPE" not in tier.columns:
            continue
        subset = tier[tier["RECOMMENDATION_TYPE"] == rec_type]
        path = os.path.join(args.out_dir, filename)
        subset.to_csv(path, index=False)
        print(f"Wrote {path}: {len(subset):,} rows")

    write_readme(args.out_dir, len(tier), n_full, args.tier)


if __name__ == "__main__":
    main()
