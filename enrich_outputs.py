# enrich_outputs.py
"""
Add GIS context columns to the correction reports without changing any
model logic. Reads the latest ServicePoints*.csv and Transformers*.csv in
GIS_mapping/ and enriches:

    data/outputs/transformer_corrections.csv
        -> data/outputs/transformer_corrections_enriched.csv

    data/outputs/corrections_ranked.csv (if present)
        -> data/outputs/corrections_ranked_enriched.csv

    data/outputs/corrections_with_stability.csv (if present)
        -> data/outputs/corrections_with_stability_enriched.csv

Added columns:
    BADGE_ADDRESS, BADGE_CITY, BADGE_LAT, BADGE_LON, BADGE_FEEDERID
    CURRENT_TX_STRUCTNO, CURRENT_TX_FEEDERID, CURRENT_TX_VAULTCD,
        CURRENT_TX_LAT, CURRENT_TX_LON, CURRENT_TX_KVA
    RECOMMENDED_TX_STRUCTNO, RECOMMENDED_TX_FEEDERID, RECOMMENDED_TX_VAULTCD,
        RECOMMENDED_TX_LAT, RECOMMENDED_TX_LON, RECOMMENDED_TX_KVA
    DISTANCE_BADGE_TO_CURRENT_M, DISTANCE_BADGE_TO_RECOMMENDED_M
    FEEDER_MISMATCH  (True if current and recommended TXs are on different feeders)

This script never touches signatures, clusters, or correlation logic.
"""

import glob
import os

import numpy as np
import pandas as pd

GIS_DIR = "GIS_mapping"
OUT_DIR = "data/outputs"
EARTH_R = 6371000.0


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
    """
    Build a (TRANSFORMERBANKOBJECTID -> transformer-info-row) lookup.

    ServicePoints carries both TRANSFORMERBANKOBJECTID (the join key used
    by known_mapping.csv) and TRANSBANKTAG (the join key to the Transformers
    file). We use SP as the bridge.
    """
    bridge = (
        sp[["TRANSFORMERBANKOBJECTID", "TRANSBANKTAG"]]
        .dropna(subset=["TRANSFORMERBANKOBJECTID", "TRANSBANKTAG"])
        .drop_duplicates(subset=["TRANSFORMERBANKOBJECTID"], keep="first")
    )
    tx_with_id = bridge.merge(tx, left_on="TRANSBANKTAG", right_on="TAG", how="left")
    tx_with_id = tx_with_id.drop(columns=["TAG"]).copy()
    return tx_with_id


def enrich_file(input_path, output_path, sp, tx_lookup):
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
        "POINT_Y": "BADGE_LAT",
        "POINT_X": "BADGE_LON",
        "FEEDERID": "BADGE_FEEDERID_RAW",
        "d_FEEDERID": "BADGE_FEEDERID",
    }
    sp_badge = sp[list(sp_badge_cols.keys())].rename(columns=sp_badge_cols)
    df = df.merge(sp_badge, on="BADGE", how="left")

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
        df["BADGE_LAT"], df["BADGE_LON"], df["CURRENT_TX_LAT"], df["CURRENT_TX_LON"]
    ).round(1)
    df["DISTANCE_BADGE_TO_RECOMMENDED_M"] = haversine_m(
        df["BADGE_LAT"],
        df["BADGE_LON"],
        df["RECOMMENDED_TX_LAT"],
        df["RECOMMENDED_TX_LON"],
    ).round(1)

    df["FEEDER_MISMATCH"] = (
        df["CURRENT_TX_FEEDERID_RAW"].fillna("")
        != df["RECOMMENDED_TX_FEEDERID_RAW"].fillna("")
    )

    df = df.drop(
        columns=[
            c
            for c in ["BADGE_FEEDERID_RAW", "CURRENT_TX_FEEDERID_RAW", "RECOMMENDED_TX_FEEDERID_RAW"]
            if c in df.columns
        ]
    )

    df.to_csv(output_path, index=False)
    print(
        f"    Wrote {output_path} ({len(df):,} rows, "
        f"feeder mismatches: {df['FEEDER_MISMATCH'].sum():,})"
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
    print(
        f"  {len(sp):,} unique ServicePoints | "
        f"{len(tx):,} unique Transformers | "
        f"{len(tx_lookup):,} TRANSFORMERBANKOBJECTIDs joinable to Transformer rows"
    )

    inputs = [
        ("transformer_corrections.csv", "transformer_corrections_enriched.csv"),
        ("corrections_ranked.csv", "corrections_ranked_enriched.csv"),
        ("corrections_with_stability.csv", "corrections_with_stability_enriched.csv"),
    ]
    print("Enriching outputs:")
    for inp, outp in inputs:
        enrich_file(
            os.path.join(OUT_DIR, inp), os.path.join(OUT_DIR, outp), sp, tx_lookup
        )


if __name__ == "__main__":
    main()
