# build_known_mapping.py
"""
Generate data/outputs/known_mapping.csv from the latest ServicePoints CSV
in GIS_mapping/.

Picks the most-recently-modified file matching ServicePoints*.csv, filters
to badges present in the current signature store (badge_ids.npy), and
writes out the badge -> transf_id mapping in the format the rest of the
pipeline expects.

Run this whenever GIS sends a new ServicePoints file. Drop it in
GIS_mapping/ (either replacing the existing file or alongside it with a
new date suffix) and re-run this script.

Usage:
    python build_known_mapping.py
"""

import glob
import os

import numpy as np
import pandas as pd

GIS_DIR = "GIS_mapping"
SIGNATURE_BADGES = "data/processed/signatures/badge_ids.npy"
OUT = "data/outputs/known_mapping.csv"


def find_latest(pattern):
    files = glob.glob(os.path.join(GIS_DIR, pattern))
    if not files:
        raise SystemExit(f"No files matching '{pattern}' found in {GIS_DIR}/")
    return max(files, key=os.path.getmtime)


def normalize_id(series):
    return (
        series.astype(str)
        .str.replace(r"\.0+$", "", regex=True)
        .replace({"nan": pd.NA, "<NA>": pd.NA, "None": pd.NA})
    )


def main():
    sp_path = find_latest("ServicePoints*.csv")
    print(f"Reading: {sp_path}")

    sp = pd.read_csv(
        sp_path,
        dtype=str,
        usecols=["BADGENUMBER", "TRANSFORMERBANKOBJECTID"],
        low_memory=False,
    )
    print(f"  Raw rows: {len(sp):,}")

    sp = sp.dropna(subset=["BADGENUMBER"]).copy()
    sp["BADGENUMBER"] = sp["BADGENUMBER"].astype(str).str.strip()
    sp["TRANSFORMERBANKOBJECTID"] = normalize_id(sp["TRANSFORMERBANKOBJECTID"])

    # Dedupe: one mapping per badge (keep first occurrence)
    sp = sp.sort_values("BADGENUMBER").drop_duplicates(
        subset=["BADGENUMBER"], keep="first"
    )

    if os.path.exists(SIGNATURE_BADGES):
        badge_ids = np.load(SIGNATURE_BADGES, allow_pickle=True)
        model_badges = {str(b) for b in badge_ids}
        before = len(sp)
        sp = sp[sp["BADGENUMBER"].isin(model_badges)].copy()
        print(
            f"  Filtered to badges in signature store: {len(sp):,} of {before:,}"
        )
    else:
        print(
            f"  WARNING: {SIGNATURE_BADGES} not found — writing all "
            f"ServicePoints rows with no model-universe filter."
        )

    out = sp.rename(
        columns={"BADGENUMBER": "badge", "TRANSFORMERBANKOBJECTID": "transf_id"}
    )[["transf_id", "badge"]]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.to_csv(OUT, index=False)
    print(f"Wrote {OUT}: {len(out):,} rows")

    n_mapped = out["transf_id"].notna().sum()
    print(
        f"  Mapped (transf_id present): {n_mapped:,}  |  "
        f"Unmapped: {len(out) - n_mapped:,}"
    )


if __name__ == "__main__":
    main()
