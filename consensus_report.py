# consensus_report.py
"""
Combine v1 and v2 model outputs into a single consensus report.

For each unique (BADGE, RECOMMENDED_TRANSFORMER) pair surfaced by either
model, classify into a tier reflecting how strongly the two models agree.
The "both_high_confidence" tier is the highest-signal subset of all —
two methodologically-independent models converging on the same
recommendation.

Reads:
    data/outputs/corrections_ranked.csv                 (v1)
    data/outputs/corrections_with_stability_enriched.csv (v1)
    data/outputs/corrections_high_confidence_enriched.csv (v1)
    data/outputs_v2/corrections_ranked.csv               (v2)
    data/outputs_v2/corrections_with_stability_enriched.csv (v2)
    data/outputs_v2/corrections_high_confidence_enriched.csv (v2)

Writes:
    data/outputs/consensus_report.csv

Tiers (rank order — top is highest priority for field action):
    both_high_confidence        — in both v1 and v2 high-confidence lists
    v1_high_confidence_only     — strong in v1, weak/absent in v2
    v2_high_confidence_only     — strong in v2, weak/absent in v1
    both_seen                   — in both full lists but neither high-conf
    v1_only                     — recommended only by v1
    v2_only                     — recommended only by v2
"""

import os

import pandas as pd

V1_RANKED = "data/outputs/corrections_ranked.csv"
V1_STABILITY_ENRICHED = "data/outputs/corrections_with_stability_enriched.csv"
V1_HIGH_CONF_ENRICHED = "data/outputs/corrections_high_confidence_enriched.csv"

V2_RANKED = "data/outputs_v2/corrections_ranked.csv"
V2_STABILITY_ENRICHED = "data/outputs_v2/corrections_with_stability_enriched.csv"
V2_HIGH_CONF_ENRICHED = "data/outputs_v2/corrections_high_confidence_enriched.csv"

OUT = "data/outputs/consensus_report.csv"


def normalize_id(series):
    return (
        series.astype(str)
        .str.replace(r"\.0+$", "", regex=True)
        .replace({"nan": pd.NA, "<NA>": pd.NA, "None": pd.NA})
    )


def load_model(ranked_path, enriched_path, high_conf_path, model_name):
    """
    Load one model's outputs. Returns a DataFrame indexed by (BADGE,
    RECOMMENDED_TRANSFORMER) with the relevant columns prefixed with
    the model name (V1_ or V2_).
    """
    cols_needed = [
        "BADGE",
        "CURRENT_TRANSFORMER",
        "RECOMMENDED_TRANSFORMER",
        "CLUSTER_SIZE",
        "MAPPED_PEERS",
        "CONFIDENCE_GAP",
        "MAJORITY_SHARE",
    ]

    if not os.path.exists(ranked_path):
        print(f"  WARNING: {ranked_path} not found; {model_name} treated as empty")
        return pd.DataFrame()

    df = pd.read_csv(ranked_path, dtype=str)
    df["BADGE"] = df["BADGE"].astype(str)
    df["RECOMMENDED_TRANSFORMER"] = normalize_id(df["RECOMMENDED_TRANSFORMER"])
    df["CURRENT_TRANSFORMER"] = normalize_id(df["CURRENT_TRANSFORMER"])
    keep = [c for c in cols_needed if c in df.columns]
    df = df[keep].copy()

    # Stability scores from corrections_with_stability_enriched (if present)
    if os.path.exists(enriched_path):
        enr = pd.read_csv(enriched_path, dtype=str)
        enr["BADGE"] = enr["BADGE"].astype(str)
        enr["RECOMMENDED_TRANSFORMER"] = normalize_id(enr["RECOMMENDED_TRANSFORMER"])
        stab_cols = ["BADGE", "RECOMMENDED_TRANSFORMER"]
        for c in [
            "PEER_STABILITY",
            "RECOMMENDATION_STABILITY",
            "RECOMMENDATION_TYPE",
            "BADGE_ADDRESS",
            "BADGE_CITY",
            "BADGE_FEEDERID",
            "CURRENT_TX_FEEDERID",
            "RECOMMENDED_TX_FEEDERID",
            "DISTANCE_BADGE_TO_CURRENT_M",
            "DISTANCE_BADGE_TO_RECOMMENDED_M",
        ]:
            if c in enr.columns:
                stab_cols.append(c)
        df = df.merge(
            enr[stab_cols], on=["BADGE", "RECOMMENDED_TRANSFORMER"], how="left"
        )

    # High-confidence membership flag
    hc_keys = set()
    if os.path.exists(high_conf_path):
        hc = pd.read_csv(high_conf_path, dtype=str)
        hc["BADGE"] = hc["BADGE"].astype(str)
        hc["RECOMMENDED_TRANSFORMER"] = normalize_id(hc["RECOMMENDED_TRANSFORMER"])
        hc_keys = set(zip(hc["BADGE"], hc["RECOMMENDED_TRANSFORMER"]))
    df["__HIGH_CONF__"] = df.apply(
        lambda r: (r["BADGE"], r["RECOMMENDED_TRANSFORMER"]) in hc_keys, axis=1
    )

    # Rename to model-prefixed columns
    rename_map = {}
    for c in df.columns:
        if c in ("BADGE", "RECOMMENDED_TRANSFORMER"):
            continue
        if c == "__HIGH_CONF__":
            rename_map[c] = f"IN_{model_name}_HIGH_CONF"
        elif c in (
            "BADGE_ADDRESS",
            "BADGE_CITY",
            "BADGE_FEEDERID",
            "RECOMMENDATION_TYPE",
            "CURRENT_TX_FEEDERID",
            "RECOMMENDED_TX_FEEDERID",
            "DISTANCE_BADGE_TO_CURRENT_M",
            "DISTANCE_BADGE_TO_RECOMMENDED_M",
        ):
            # GIS context — shared between both models (same source); we'll keep one copy
            rename_map[c] = c
        else:
            rename_map[c] = f"{model_name}_{c}"
    df = df.rename(columns=rename_map)
    return df


def classify_tier(in_v1, in_v1_hc, in_v2, in_v2_hc):
    if in_v1_hc and in_v2_hc:
        return "both_high_confidence"
    if in_v1_hc and not in_v2:
        return "v1_high_confidence_only"
    if in_v2_hc and not in_v1:
        return "v2_high_confidence_only"
    if in_v1_hc:
        return "v1_high_confidence_v2_seen"
    if in_v2_hc:
        return "v2_high_confidence_v1_seen"
    if in_v1 and in_v2:
        return "both_seen"
    if in_v1:
        return "v1_only"
    return "v2_only"


TIER_RANK = {
    "both_high_confidence": 0,
    "v1_high_confidence_only": 1,
    "v2_high_confidence_only": 1,
    "v1_high_confidence_v2_seen": 2,
    "v2_high_confidence_v1_seen": 2,
    "both_seen": 3,
    "v1_only": 4,
    "v2_only": 4,
}


def main():
    print("Loading v1 ...")
    v1 = load_model(V1_RANKED, V1_STABILITY_ENRICHED, V1_HIGH_CONF_ENRICHED, "V1")
    print(f"  v1 rows: {len(v1):,}")

    print("Loading v2 ...")
    v2 = load_model(V2_RANKED, V2_STABILITY_ENRICHED, V2_HIGH_CONF_ENRICHED, "V2")
    print(f"  v2 rows: {len(v2):,}")

    # Outer-join on (BADGE, RECOMMENDED_TRANSFORMER)
    # Drop the duplicate GIS-context columns from v2 before merging
    shared_gis_cols = {
        "BADGE_ADDRESS",
        "BADGE_CITY",
        "BADGE_FEEDERID",
        "RECOMMENDATION_TYPE",
        "CURRENT_TX_FEEDERID",
        "RECOMMENDED_TX_FEEDERID",
        "DISTANCE_BADGE_TO_CURRENT_M",
        "DISTANCE_BADGE_TO_RECOMMENDED_M",
    }
    v2_dedup = v2.drop(columns=[c for c in shared_gis_cols if c in v2.columns])

    print("Merging ...")
    merged = v1.merge(
        v2_dedup,
        on=["BADGE", "RECOMMENDED_TRANSFORMER"],
        how="outer",
        indicator=True,
    )
    merged["IN_V1"] = merged["_merge"].isin(["left_only", "both"])
    merged["IN_V2"] = merged["_merge"].isin(["right_only", "both"])
    merged = merged.drop(columns=["_merge"])

    for col in ["IN_V1_HIGH_CONF", "IN_V2_HIGH_CONF"]:
        if col not in merged.columns:
            merged[col] = False
        merged[col] = merged[col].fillna(False).astype(bool)

    merged["CONSENSUS_TIER"] = merged.apply(
        lambda r: classify_tier(
            r["IN_V1"], r["IN_V1_HIGH_CONF"], r["IN_V2"], r["IN_V2_HIGH_CONF"]
        ),
        axis=1,
    )

    merged["_TIER_RANK"] = merged["CONSENSUS_TIER"].map(TIER_RANK)
    merged = merged.sort_values(
        ["_TIER_RANK", "V1_CONFIDENCE_GAP", "V2_CONFIDENCE_GAP"],
        ascending=[True, False, False],
    ).drop(columns=["_TIER_RANK"])

    # Preferred column order
    preferred = [
        "BADGE",
        "CURRENT_TRANSFORMER",
        "RECOMMENDED_TRANSFORMER",
        "CONSENSUS_TIER",
        "IN_V1",
        "IN_V1_HIGH_CONF",
        "IN_V2",
        "IN_V2_HIGH_CONF",
        "RECOMMENDATION_TYPE",
        "BADGE_ADDRESS",
        "BADGE_CITY",
        "BADGE_FEEDERID",
        "CURRENT_TX_FEEDERID",
        "RECOMMENDED_TX_FEEDERID",
        "DISTANCE_BADGE_TO_CURRENT_M",
        "DISTANCE_BADGE_TO_RECOMMENDED_M",
        "V1_CLUSTER_SIZE",
        "V1_MAPPED_PEERS",
        "V1_CONFIDENCE_GAP",
        "V1_MAJORITY_SHARE",
        "V1_PEER_STABILITY",
        "V1_RECOMMENDATION_STABILITY",
        "V2_CLUSTER_SIZE",
        "V2_MAPPED_PEERS",
        "V2_CONFIDENCE_GAP",
        "V2_MAJORITY_SHARE",
        "V2_PEER_STABILITY",
        "V2_RECOMMENDATION_STABILITY",
    ]
    # Combine CURRENT_TRANSFORMER from v1 and v2 (they should agree; v1 wins
    # ties). This MUST happen before cols_final is built: CURRENT_TRANSFORMER
    # is in `preferred`, but if it does not exist yet when cols_final is
    # computed it gets filtered straight back out, and the report ships
    # telling the field team where to move a meter without saying what it is
    # currently assigned to.
    drop_cols = []
    if "V1_CURRENT_TRANSFORMER" in merged.columns and "V2_CURRENT_TRANSFORMER" in merged.columns:
        merged["CURRENT_TRANSFORMER"] = merged["V1_CURRENT_TRANSFORMER"].combine_first(
            merged["V2_CURRENT_TRANSFORMER"]
        )
        drop_cols = ["V1_CURRENT_TRANSFORMER", "V2_CURRENT_TRANSFORMER"]
    elif "V1_CURRENT_TRANSFORMER" in merged.columns:
        merged["CURRENT_TRANSFORMER"] = merged["V1_CURRENT_TRANSFORMER"]
        drop_cols = ["V1_CURRENT_TRANSFORMER"]
    elif "V2_CURRENT_TRANSFORMER" in merged.columns:
        merged["CURRENT_TRANSFORMER"] = merged["V2_CURRENT_TRANSFORMER"]
        drop_cols = ["V2_CURRENT_TRANSFORMER"]

    cols_final = [c for c in preferred if c in merged.columns] + [
        c for c in merged.columns if c not in preferred
    ]
    cols_final = [c for c in cols_final if c not in drop_cols]

    merged = merged[cols_final]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    merged.to_csv(OUT, index=False)

    print()
    print("Consensus tier breakdown:")
    counts = merged["CONSENSUS_TIER"].value_counts()
    for tier in TIER_RANK:
        n = int(counts.get(tier, 0))
        print(f"  {tier:>30}: {n:,}")

    print()
    if "RECOMMENDATION_TYPE" in merged.columns:
        print("Both high-confidence by RECOMMENDATION_TYPE:")
        bhc = merged[merged["CONSENSUS_TIER"] == "both_high_confidence"]
        for rtype, n in bhc["RECOMMENDATION_TYPE"].value_counts().items():
            print(f"  {rtype:>30}: {int(n):,}")

    print()
    print(f"Wrote {OUT}: {len(merged):,} rows")


if __name__ == "__main__":
    main()
