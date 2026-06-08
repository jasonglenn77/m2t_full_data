#rolling_update.py
"""
Slide the rolling signature window forward by one day.

Memory-efficient rewrite: streams the existing signatures.npy via a
read-only memmap, writes the shifted-and-extended store to a write-only
memmap on disk, and atomically swaps the file when done. Peak RAM stays
in the hundreds of MB regardless of WINDOW_DAYS.

Usage:
    python rolling_update.py <new_day_parquet>
"""

import gc
import os
import sys

import numpy as np
import pandas as pd
from numpy.lib.format import open_memmap

from config import INTERVALS_PER_DAY, SIGNATURE_DIR

BADGE_IDS = os.path.join(SIGNATURE_DIR, "badge_ids.npy")
LAT = os.path.join(SIGNATURE_DIR, "lat.npy")
LON = os.path.join(SIGNATURE_DIR, "lon.npy")
SIG = os.path.join(SIGNATURE_DIR, "signatures.npy")
SIG_TMP = os.path.join(SIGNATURE_DIR, "signatures.npy.tmp")
META = os.path.join(SIGNATURE_DIR, "badge_metadata.parquet")
REPORT = os.path.join(SIGNATURE_DIR, "rolling_add_remove_report.csv")

COPY_CHUNK_ROWS = 5000


def main(new_day_parquet):
    if os.path.exists(SIG_TMP):
        os.remove(SIG_TMP)

    old_badges = np.load(BADGE_IDS, allow_pickle=True)
    old_badges = np.array([str(b) for b in old_badges], dtype=object)
    old_lat = np.load(LAT)
    old_lon = np.load(LON)
    old_meta = pd.read_parquet(META)
    old_meta["BADGE"] = old_meta["BADGE"].astype(str)
    old_badges_set = set(old_badges)

    new_df = pd.read_parquet(
        new_day_parquet,
        columns=[
            "BADGE",
            "MSRMTDTTM",
            "PUVALUE",
            "BADGE_LAT",
            "BADGE_LONG",
            "DEVICE_TYPE_CD",
            "NOMINAL",
        ],
    )
    new_df["BADGE"] = new_df["BADGE"].astype(str)
    new_df["MSRMTDTTM"] = pd.to_datetime(new_df["MSRMTDTTM"], utc=True)

    new_badges_set = set(new_df["BADGE"].unique())
    added = sorted(new_badges_set - old_badges_set)
    removed = sorted(old_badges_set - new_badges_set)
    print(f"Added badges: {len(added)}")
    print(f"Removed badges: {len(removed)}")

    if added:
        all_badges = np.concatenate([old_badges, np.array(added, dtype=object)])
        new_lat = np.concatenate([old_lat, np.full(len(added), np.nan)])
        new_lon = np.concatenate([old_lon, np.full(len(added), np.nan)])
    else:
        all_badges = old_badges.copy()
        new_lat = old_lat.copy()
        new_lon = old_lon.copy()

    n_badges = len(all_badges)
    n_old = len(old_badges)
    badge_index = {b: i for i, b in enumerate(all_badges)}

    old_signatures = np.load(SIG, mmap_mode="r")
    n_cols = old_signatures.shape[1]

    new_signatures = open_memmap(
        SIG_TMP, mode="w+", dtype=np.float32, shape=(n_badges, n_cols)
    )
    new_signatures[:] = np.nan

    print(f"Copying old days (drop oldest), {n_old:,} badges in chunks of {COPY_CHUNK_ROWS:,} ...")
    for start in range(0, n_old, COPY_CHUNK_ROWS):
        end = min(start + COPY_CHUNK_ROWS, n_old)
        new_signatures[start:end, :-INTERVALS_PER_DAY] = old_signatures[
            start:end, INTERVALS_PER_DAY:
        ]
        new_signatures.flush()

    del old_signatures
    gc.collect()

    print("Writing new day columns ...")
    day_start = new_df["MSRMTDTTM"].min().floor("D")
    times = pd.date_range(
        start=day_start, periods=INTERVALS_PER_DAY, freq="15min", tz="UTC"
    )

    for badge, g in new_df.groupby("BADGE", sort=False):
        i = badge_index.get(badge)
        if i is None:
            continue
        g = g.sort_values("MSRMTDTTM").drop_duplicates(
            subset=["MSRMTDTTM"], keep="last"
        )
        ts = g.set_index("MSRMTDTTM")["PUVALUE"].reindex(times)
        new_signatures[i, -INTERVALS_PER_DAY:] = ts.to_numpy(dtype=np.float32)
        new_lat[i] = pd.to_numeric(g["BADGE_LAT"].iloc[0], errors="coerce")
        new_lon[i] = pd.to_numeric(g["BADGE_LONG"].iloc[0], errors="coerce")

    new_signatures.flush()
    del new_signatures
    gc.collect()

    print("Updating metadata ...")
    new_meta_rows = (
        new_df.sort_values("MSRMTDTTM")
        .groupby("BADGE", as_index=False)
        .first()[["BADGE", "BADGE_LAT", "BADGE_LONG", "DEVICE_TYPE_CD", "NOMINAL"]]
    )
    new_meta_rows["BADGE_LAT"] = pd.to_numeric(
        new_meta_rows["BADGE_LAT"], errors="coerce"
    )
    new_meta_rows["BADGE_LONG"] = pd.to_numeric(
        new_meta_rows["BADGE_LONG"], errors="coerce"
    )
    meta_updated = (
        pd.concat([old_meta, new_meta_rows], ignore_index=True)
        .sort_values("BADGE")
        .drop_duplicates(subset=["BADGE"], keep="last")
        .reset_index(drop=True)
    )

    del new_df, old_meta, new_meta_rows
    gc.collect()

    # Atomic swap. Windows can't rename over an existing file, so unlink first.
    os.remove(SIG)
    os.rename(SIG_TMP, SIG)

    np.save(BADGE_IDS, all_badges)
    np.save(LAT, new_lat)
    np.save(LON, new_lon)
    meta_updated.to_parquet(META, index=False)

    report = pd.DataFrame(
        {
            "ADDED_BADGE": pd.Series(added, dtype="object"),
            "REMOVED_BADGE": pd.Series(removed, dtype="object"),
        }
    )
    report.to_csv(REPORT, index=False)

    print("Rolling update complete.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python rolling_update.py <new_day_parquet>")
    main(sys.argv[1])
