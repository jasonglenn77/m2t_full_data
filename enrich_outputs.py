# enrich_outputs.py
"""
Add GIS context columns to the correction reports without changing any
model logic. Reads the latest ServicePoints*.csv and Transformers*.csv in
GIS_mapping/ and produces enriched outputs that classify the signal
quality of each recommendation from the perspective that the model is
the source of truth and GIS may be incorrect.

Outputs:
    data/outputs/transformer_corrections_enriched.csv
    data/outputs/corrections_ranked_enriched.csv
    data/outputs/corrections_with_stability_enriched.csv  (if input exists)
    data/outputs/badges_missing_from_gis.csv
        Badges in the signature store that don't appear in ServicePoints.
        Each row shows the cluster's majority recommendation as a starting
        point for first-time GIS entry.
    data/outputs/latlon_discrepancies.csv
        Badges where the model's lat/lon (from CIS via Oracle SQL) differs
        from the GIS POINT_X/POINT_Y by more than the threshold (default
        100 m). These are GIS-side data quality candidates.

Each enriched correction row carries a RECOMMENDATION_TYPE label:

    cross_feeder_likely_gis_error
        Current and recommended transformers are on different feeders.
        Voltage signatures shouldn't cross feeders; the parsimonious
        explanation is that GIS has the badge's feeder/transformer wrong.
        Treat as HIGH-signal.

    same_feeder_ambiguous
        Current and recommended transformers are on the same feeder.
        Same-feeder voltage signatures can look very similar even on
        different transformers; LOWER-signal on its own. Combine with
        CONFIDENCE_GAP and stability scores to triage.

    new_assignment
        Badge has no current transformer in GIS. The model is suggesting
        a first-time assignment from cluster majority.

    unknown_feeder
        Feeder info is missing for one or both transformers; can't classify.

This script never feeds back into correlation or clustering.
"""

import glob
import os

import numpy as np
import pandas as pd

GIS_DIR = "GIS_mapping"
OUT_DIR = "data/outputs"
SIG_DIR = "data/processed/signatures"

SIG_BADGE_IDS = os.path.join(SIG_DIR, "badge_ids.npy")
SIG_LAT = os.path.join(SIG_DIR, "lat.npy")
SIG_LON = os.path.join(SIG_DIR, "lon.npy")

DAILY_RAW_DIR = "data/raw/daily"
# Number of most-recent daily parquets used to define "currently active"
# badges. badges_missing_from_gis.csv is filtered to badges that appeared
# in at least one of these days so stale meters (e.g., decommissioned
# weeks ago but still in the signature store's badge list) don't pollute
# the report. A 7-day window tolerates occasional missed reads.
RECENT_DAYS = 7

CLUSTERS = os.path.join(OUT_DIR, "full_clusters.csv")
KNOWN_MAPPING = os.path.join(OUT_DIR, "known_mapping.csv")

EARTH_R = 6371000.0
LATLON_DISCREPANCY_THRESHOLD_M = 100.0


def find_latest(pattern):
    files = glob.glob(os.path.join(GIS_DIR, pattern))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def normalize_id(series):
    return (
        series.astype(str)
        .str.replace(r"\.0+$", "", regex=True)
        .replace({"nan": pd.NA, "<NA>": pd.NA, "None": pd.NA})
    )


def haversine_m(lat1, lon1, lat2, lon2):
    lat1 = pd.to_numeric(lat1, errors="coerce")
    lon1 = pd.to_numeric(lon1, errors="coerce")
    lat2 = pd.to_numeric(lat2, errors="coerce")
    lon2 = pd.to_numeric(lon2, errors="coerce")
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlam = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


def load_service_points(path):
    cols = [
        "BADGENUMBER",
        "TRANSFORMERBANKOBJECTID",
        "TRANSBANKTAG",
        "CCBADDRESS1",
        "CCBCITY",
        "POINT_X",
        "POINT_Y",
        "FEEDERID",
        "d_FEEDERID",
    ]
    sp = pd.read_csv(path, dtype=str, usecols=cols, low_memory=False)
    sp = sp.dropna(subset=["BADGENUMBER"]).copy()
    sp["BADGENUMBER"] = sp["BADGENUMBER"].astype(str).str.strip()
    sp["TRANSFORMERBANKOBJECTID"] = normalize_id(sp["TRANSFORMERBANKOBJECTID"])
    sp["TRANSBANKTAG"] = sp["TRANSBANKTAG"].astype(str).str.strip()
    sp.loc[sp["TRANSBANKTAG"].isin(["nan", "", "None"]), "TRANSBANKTAG"] = pd.NA
    sp = sp.sort_values("BADGENUMBER").drop_duplicates(
        subset=["BADGENUMBER"], keep="first"
    )
    return sp


def load_transformers(path):
    cols = [
        "TAG",
        "STRUCTNO",
        "FEEDERID",
        "d_FEEDERID",
        "d_SUBTYPECD",
        "VAULTCD",
        "POINT_X",
        "POINT_Y",
        "TOTALKVA",
    ]
    tx = pd.read_csv(path, dtype=str, usecols=cols, low_memory=False)
    tx["TAG"] = tx["TAG"].astype(str).str.strip()
    tx = tx.dropna(subset=["TAG"]).copy()
    tx = tx.sort_values("TAG").drop_duplicates(subset=["TAG"], keep="first")
    return tx


def build_tx_id_lookup(sp, tx):
    bridge = (
        sp[["TRANSFORMERBANKOBJECTID", "TRANSBANKTAG"]]
        .dropna(subset=["TRANSFORMERBANKOBJECTID", "TRANSBANKTAG"])
        .drop_duplicates(subset=["TRANSFORMERBANKOBJECTID"], keep="first")
    )
    tx_with_id = bridge.merge(tx, left_on="TRANSBANKTAG", right_on="TAG", how="left")
    tx_with_id = tx_with_id.drop(columns=["TAG"]).copy()
    return tx_with_id


def load_model_latlon():
    if not (
        os.path.exists(SIG_BADGE_IDS)
        and os.path.exists(SIG_LAT)
        and os.path.exists(SIG_LON)
    ):
        return None
    badges = np.load(SIG_BADGE_IDS, allow_pickle=True)
    lats = np.load(SIG_LAT)
    lons = np.load(SIG_LON)
    return pd.DataFrame(
        {
            "BADGE": [str(b) for b in badges],
            "MODEL_BADGE_LAT": lats,
            "MODEL_BADGE_LON": lons,
        }
    )


def classify_recommendation(row):
    cur_feeder = row.get("CURRENT_TX_FEEDERID_RAW")
    rec_feeder = row.get("RECOMMENDED_TX_FEEDERID_RAW")
    cur_tx = row.get("CURRENT_TRANSFORMER")

    if pd.isna(cur_tx) or cur_tx == "":
        return "new_assignment"
    if pd.isna(cur_feeder) or pd.isna(rec_feeder) or cur_feeder == "" or rec_feeder == "":
        return "unknown_feeder"
    if cur_feeder == rec_feeder:
        return "same_feeder_ambiguous"
    return "cross_feeder_likely_gis_error"


def enrich_file(input_path, output_path, sp, tx_lookup, model_latlon):
    if not os.path.exists(input_path):
        print(f"  Skipping (not present): {input_path}")
        return

    print(f"  Enriching {input_path} ...")
    df = pd.read_csv(input_path, dtype=str)
    if df.empty:
        df.to_csv(output_path, index=False)
        print(f"    (empty input) wrote {output_path}")
        return

    df["BADGE"] = df["BADGE"].astype(str)
    if "CURRENT_TRANSFORMER" in df.columns:
        df["CURRENT_TRANSFORMER"] = normalize_id(df["CURRENT_TRANSFORMER"])
    if "RECOMMENDED_TRANSFORMER" in df.columns:
        df["RECOMMENDED_TRANSFORMER"] = normalize_id(df["RECOMMENDED_TRANSFORMER"])

    sp_badge_cols = {
        "BADGENUMBER": "BADGE",
        "CCBADDRESS1": "BADGE_ADDRESS",
        "CCBCITY": "BADGE_CITY",
        "POINT_Y": "GIS_BADGE_LAT",
        "POINT_X": "GIS_BADGE_LON",
        "FEEDERID": "BADGE_FEEDERID_RAW",
        "d_FEEDERID": "BADGE_FEEDERID",
    }
    sp_badge = sp[list(sp_badge_cols.keys())].rename(columns=sp_badge_cols)
    df = df.merge(sp_badge, on="BADGE", how="left")
    # Mirror ServicePoints column names so downstream tools that expect
    # POINT_X / POINT_Y have them verbatim.
    df["BADGE_POINT_X"] = df["GIS_BADGE_LON"]
    df["BADGE_POINT_Y"] = df["GIS_BADGE_LAT"]

    if model_latlon is not None:
        df = df.merge(model_latlon, on="BADGE", how="left")
        df["MODEL_VS_GIS_LATLON_DISTANCE_M"] = haversine_m(
            df["MODEL_BADGE_LAT"],
            df["MODEL_BADGE_LON"],
            df["GIS_BADGE_LAT"],
            df["GIS_BADGE_LON"],
        ).round(1)
        df["LATLON_DISCREPANCY"] = (
            df["MODEL_VS_GIS_LATLON_DISTANCE_M"] > LATLON_DISCREPANCY_THRESHOLD_M
        )

    cur_cols = {
        "TRANSFORMERBANKOBJECTID": "CURRENT_TRANSFORMER",
        "STRUCTNO": "CURRENT_TX_STRUCTNO",
        "FEEDERID": "CURRENT_TX_FEEDERID_RAW",
        "d_FEEDERID": "CURRENT_TX_FEEDERID",
        "d_SUBTYPECD": "CURRENT_TX_SUBTYPE",
        "VAULTCD": "CURRENT_TX_VAULTCD",
        "POINT_Y": "CURRENT_TX_LAT",
        "POINT_X": "CURRENT_TX_LON",
        "TOTALKVA": "CURRENT_TX_KVA",
    }
    cur = tx_lookup[list(cur_cols.keys())].rename(columns=cur_cols)
    df = df.merge(cur, on="CURRENT_TRANSFORMER", how="left")

    rec_cols = {
        "TRANSFORMERBANKOBJECTID": "RECOMMENDED_TRANSFORMER",
        "STRUCTNO": "RECOMMENDED_TX_STRUCTNO",
        "FEEDERID": "RECOMMENDED_TX_FEEDERID_RAW",
        "d_FEEDERID": "RECOMMENDED_TX_FEEDERID",
        "d_SUBTYPECD": "RECOMMENDED_TX_SUBTYPE",
        "VAULTCD": "RECOMMENDED_TX_VAULTCD",
        "POINT_Y": "RECOMMENDED_TX_LAT",
        "POINT_X": "RECOMMENDED_TX_LON",
        "TOTALKVA": "RECOMMENDED_TX_KVA",
    }
    rec = tx_lookup[list(rec_cols.keys())].rename(columns=rec_cols)
    df = df.merge(rec, on="RECOMMENDED_TRANSFORMER", how="left")

    df["DISTANCE_BADGE_TO_CURRENT_M"] = haversine_m(
        df["GIS_BADGE_LAT"],
        df["GIS_BADGE_LON"],
        df["CURRENT_TX_LAT"],
        df["CURRENT_TX_LON"],
    ).round(1)
    df["DISTANCE_BADGE_TO_RECOMMENDED_M"] = haversine_m(
        df["GIS_BADGE_LAT"],
        df["GIS_BADGE_LON"],
        df["RECOMMENDED_TX_LAT"],
        df["RECOMMENDED_TX_LON"],
    ).round(1)

    df["RECOMMENDATION_TYPE"] = df.apply(classify_recommendation, axis=1)

    df = df.drop(
        columns=[
            c
            for c in [
                "BADGE_FEEDERID_RAW",
                "CURRENT_TX_FEEDERID_RAW",
                "RECOMMENDED_TX_FEEDERID_RAW",
            ]
            if c in df.columns
        ]
    )

    df.to_csv(output_path, index=False)
    type_counts = df["RECOMMENDATION_TYPE"].value_counts().to_dict()
    print(f"    Wrote {output_path} ({len(df):,} rows)")
    for k, v in type_counts.items():
        print(f"      {k}: {v:,}")


def get_recently_active_badges(n_days=RECENT_DAYS):
    """
    Return the union of badges that appeared in the latest n_days daily
    parquets. Used as a "currently active" filter so the missing-from-GIS
    report excludes meters that stopped reporting weeks ago but are still
    carried in the signature store's badge list.

    Returns None if data/raw/daily/ is not present (e.g., running on a
    machine without raw data); caller should fall back to no filter.
    """
    if not os.path.isdir(DAILY_RAW_DIR):
        return None
    files = sorted(glob.glob(os.path.join(DAILY_RAW_DIR, "*.parquet")))[-n_days:]
    if not files:
        return None
    active = set()
    for f in files:
        df = pd.read_parquet(f, columns=["BADGE"])
        active.update(df["BADGE"].astype(str).unique())
    return active


def write_missing_from_gis(sp, tx_lookup):
    if not os.path.exists(CLUSTERS):
        print(f"  Skipping missing-from-GIS report: {CLUSTERS} not found")
        return

    print("  Building badges_missing_from_gis.csv ...")
    clusters = pd.read_csv(CLUSTERS, dtype=str)
    clusters["BADGE"] = clusters["BADGE"].astype(str)
    sp_badges = set(sp["BADGENUMBER"])

    missing = clusters[~clusters["BADGE"].isin(sp_badges)].copy()

    # Filter to badges that have appeared in the most recent daily parquets
    # so this report reflects currently-active meters rather than stale
    # entries left in the signature store's badge list. Falls back to no
    # filter if raw daily parquets aren't on this machine.
    active = get_recently_active_badges()
    if active is not None:
        before = len(missing)
        missing = missing[missing["BADGE"].isin(active)].copy()
        print(
            f"    Recency filter: {len(missing):,} of {before:,} badges "
            f"appeared in the last {RECENT_DAYS} daily parquets"
        )
    else:
        print(
            f"    NOTE: {DAILY_RAW_DIR} not found — skipping recency filter; "
            f"output may include stale badges no longer reporting."
        )

    if missing.empty:
        print("    No badges in clusters are missing from GIS.")
        empty = pd.DataFrame(
            columns=[
                "BADGE",
                "CLUSTER",
                "CLUSTER_SIZE",
                "MAPPED_PEERS",
                "MAJORITY_TRANSFORMER",
                "MAJORITY_VOTES",
                "MAJORITY_SHARE",
            ]
        )
        empty.to_csv(os.path.join(OUT_DIR, "badges_missing_from_gis.csv"), index=False)
        return

    truth = pd.read_csv(KNOWN_MAPPING, dtype=str).rename(
        columns={"badge": "BADGE", "transf_id": "TRANSFORMER"}
    )
    truth["TRANSFORMER"] = normalize_id(truth["TRANSFORMER"])
    joined = clusters.merge(truth, on="BADGE", how="left")
    mapped = joined.dropna(subset=["TRANSFORMER"])

    cluster_size = (
        clusters.groupby("CLUSTER").size().rename("CLUSTER_SIZE").reset_index()
    )
    mapped_size = (
        mapped.groupby("CLUSTER").size().rename("MAPPED_PEERS").reset_index()
    )
    votes_per_tx = (
        mapped.groupby(["CLUSTER", "TRANSFORMER"]).size().rename("VOTES").reset_index()
    )

    majority = (
        votes_per_tx.sort_values("VOTES", ascending=False)
        .drop_duplicates(subset=["CLUSTER"], keep="first")
        .rename(
            columns={
                "TRANSFORMER": "MAJORITY_TRANSFORMER",
                "VOTES": "MAJORITY_VOTES",
            }
        )
    )

    out = missing[["BADGE", "CLUSTER"]].merge(cluster_size, on="CLUSTER", how="left")
    out = out.merge(mapped_size, on="CLUSTER", how="left")
    out = out.merge(majority, on="CLUSTER", how="left")
    out["MAPPED_PEERS"] = out["MAPPED_PEERS"].fillna(0).astype(int)
    out["MAJORITY_VOTES"] = out["MAJORITY_VOTES"].fillna(0).astype(int)
    denom = out["MAPPED_PEERS"].astype(float).replace(0, np.nan)
    out["MAJORITY_SHARE"] = (out["MAJORITY_VOTES"].astype(float) / denom).round(3)

    rec_cols = {
        "TRANSFORMERBANKOBJECTID": "MAJORITY_TRANSFORMER",
        "STRUCTNO": "RECOMMENDED_TX_STRUCTNO",
        "d_FEEDERID": "RECOMMENDED_TX_FEEDERID",
        "VAULTCD": "RECOMMENDED_TX_VAULTCD",
        "POINT_Y": "RECOMMENDED_TX_LAT",
        "POINT_X": "RECOMMENDED_TX_LON",
        "TOTALKVA": "RECOMMENDED_TX_KVA",
    }
    rec = tx_lookup[list(rec_cols.keys())].rename(columns=rec_cols)
    out = out.merge(rec, on="MAJORITY_TRANSFORMER", how="left")

    out = out.sort_values(
        ["MAJORITY_SHARE", "MAPPED_PEERS"], ascending=[False, False]
    )
    out.to_csv(os.path.join(OUT_DIR, "badges_missing_from_gis.csv"), index=False)
    print(f"    Wrote {len(out):,} badges missing from GIS")


def write_full_clusters_enriched(sp, model_latlon):
    if not os.path.exists(CLUSTERS):
        print(f"  Skipping full_clusters_enriched.csv: {CLUSTERS} not found")
        return

    print("  Building full_clusters_enriched.csv ...")
    clusters = pd.read_csv(CLUSTERS, dtype=str)
    clusters["BADGE"] = clusters["BADGE"].astype(str)

    sp_cols = {
        "BADGENUMBER": "BADGE",
        "CCBADDRESS1": "BADGE_ADDRESS",
        "CCBCITY": "BADGE_CITY",
        "POINT_X": "BADGE_POINT_X",
        "POINT_Y": "BADGE_POINT_Y",
        "d_FEEDERID": "BADGE_FEEDERID",
        "TRANSFORMERBANKOBJECTID": "GIS_TRANSFORMER",
        "TRANSBANKTAG": "GIS_TRANSBANKTAG",
    }
    sp_subset = sp[list(sp_cols.keys())].rename(columns=sp_cols).copy()
    sp_subset["GIS_TRANSFORMER"] = normalize_id(sp_subset["GIS_TRANSFORMER"])

    out = clusters.merge(sp_subset, on="BADGE", how="left")

    if model_latlon is not None:
        out = out.merge(model_latlon, on="BADGE", how="left")

    cluster_size = (
        clusters.groupby("CLUSTER").size().rename("CLUSTER_SIZE").reset_index()
    )
    out = out.merge(cluster_size, on="CLUSTER", how="left")

    cols = [
        "BADGE",
        "CLUSTER",
        "CLUSTER_SIZE",
        "GIS_TRANSFORMER",
        "GIS_TRANSBANKTAG",
        "BADGE_ADDRESS",
        "BADGE_CITY",
        "BADGE_POINT_X",
        "BADGE_POINT_Y",
        "BADGE_FEEDERID",
        "MODEL_BADGE_LAT",
        "MODEL_BADGE_LON",
    ]
    cols = [c for c in cols if c in out.columns]
    out = out[cols]
    out.to_csv(os.path.join(OUT_DIR, "full_clusters_enriched.csv"), index=False)
    print(
        f"    Wrote {len(out):,} rows (every badge with cluster + GIS context)"
    )


def write_latlon_discrepancies(sp, model_latlon):
    if model_latlon is None:
        print(
            "  Skipping latlon_discrepancies.csv: model lat/lon arrays not found"
        )
        return

    print("  Building latlon_discrepancies.csv ...")
    gis = sp[["BADGENUMBER", "POINT_X", "POINT_Y", "CCBADDRESS1", "CCBCITY"]].rename(
        columns={
            "BADGENUMBER": "BADGE",
            "POINT_Y": "GIS_BADGE_LAT",
            "POINT_X": "GIS_BADGE_LON",
            "CCBADDRESS1": "BADGE_ADDRESS",
            "CCBCITY": "BADGE_CITY",
        }
    )
    merged = model_latlon.merge(gis, on="BADGE", how="inner")
    merged["MODEL_VS_GIS_LATLON_DISTANCE_M"] = haversine_m(
        merged["MODEL_BADGE_LAT"],
        merged["MODEL_BADGE_LON"],
        merged["GIS_BADGE_LAT"],
        merged["GIS_BADGE_LON"],
    ).round(1)

    discrepant = merged[
        merged["MODEL_VS_GIS_LATLON_DISTANCE_M"] > LATLON_DISCREPANCY_THRESHOLD_M
    ].copy()
    discrepant = discrepant.sort_values(
        "MODEL_VS_GIS_LATLON_DISTANCE_M", ascending=False
    )

    cols = [
        "BADGE",
        "BADGE_ADDRESS",
        "BADGE_CITY",
        "MODEL_BADGE_LAT",
        "MODEL_BADGE_LON",
        "GIS_BADGE_LAT",
        "GIS_BADGE_LON",
        "MODEL_VS_GIS_LATLON_DISTANCE_M",
    ]
    discrepant[cols].to_csv(
        os.path.join(OUT_DIR, "latlon_discrepancies.csv"), index=False
    )
    print(
        f"    Wrote {len(discrepant):,} badges with model vs GIS distance > "
        f"{LATLON_DISCREPANCY_THRESHOLD_M:.0f} m"
    )


def main():
    sp_path = find_latest("ServicePoints*.csv")
    tx_path = find_latest("Transformers*.csv")
    if sp_path is None:
        raise SystemExit(f"No ServicePoints*.csv found in {GIS_DIR}/")
    if tx_path is None:
        raise SystemExit(f"No Transformers*.csv found in {GIS_DIR}/")

    print(f"Using ServicePoints: {os.path.basename(sp_path)}")
    print(f"Using Transformers:  {os.path.basename(tx_path)}")

    sp = load_service_points(sp_path)
    tx = load_transformers(tx_path)
    tx_lookup = build_tx_id_lookup(sp, tx)
    model_latlon = load_model_latlon()
    print(
        f"  {len(sp):,} unique ServicePoints | "
        f"{len(tx):,} unique Transformers | "
        f"{len(tx_lookup):,} TRANSFORMERBANKOBJECTIDs joinable to Transformer rows"
    )
    if model_latlon is not None:
        print(f"  {len(model_latlon):,} badges with model lat/lon loaded")

    inputs = [
        ("transformer_corrections.csv", "transformer_corrections_enriched.csv"),
        ("corrections_ranked.csv", "corrections_ranked_enriched.csv"),
        ("corrections_with_stability.csv", "corrections_with_stability_enriched.csv"),
        ("corrections_high_confidence.csv", "corrections_high_confidence_enriched.csv"),
    ]
    print("Enriching correction outputs:")
    for inp, outp in inputs:
        enrich_file(
            os.path.join(OUT_DIR, inp),
            os.path.join(OUT_DIR, outp),
            sp,
            tx_lookup,
            model_latlon,
        )

    print("Producing supplementary reports:")
    write_missing_from_gis(sp, tx_lookup)
    write_latlon_discrepancies(sp, model_latlon)
    write_full_clusters_enriched(sp, model_latlon)


if __name__ == "__main__":
    main()
