#build_signature_store.py
import os
import glob
import numpy as np
import pandas as pd
from numpy.lib.format import open_memmap
 
from config import DAILY_RAW_DIR, SIGNATURE_DIR, WINDOW_DAYS, INTERVALS_PER_DAY, SIGNATURE_LENGTH
 
 
def build_from_daily_files():
    files = sorted(glob.glob(os.path.join(DAILY_RAW_DIR, "*.parquet")))
    if not files:
        raise ValueError("No daily parquet files found.")
 
    expected_files = WINDOW_DAYS
    if len(files) != expected_files:
        raise ValueError(f"Expected {expected_files} daily parquet files, found {len(files)}.")
 
    # -------- Pass 1: discover all badges and first-seen metadata --------
    all_badges = set()
    meta_rows = []
 
    for f in files:
        day_df = pd.read_parquet(
            f,
            columns=["BADGE", "BADGE_LAT", "BADGE_LONG", "DEVICE_TYPE_CD", "NOMINAL"]
        )
 
        day_df["BADGE"] = day_df["BADGE"].astype(str)
        all_badges.update(day_df["BADGE"].unique().tolist())
 
        meta_day = (
            day_df.sort_values("BADGE")
            .drop_duplicates(subset=["BADGE"], keep="first")
            .copy()
        )
        meta_rows.append(meta_day)
 
    badges = np.array(sorted(all_badges), dtype=object)
    badge_index = {b: i for i, b in enumerate(badges)}
 
    meta_df = pd.concat(meta_rows, ignore_index=True)
    meta_df["BADGE"] = meta_df["BADGE"].astype(str)
    meta_df = (
        meta_df.sort_values("BADGE")
        .drop_duplicates(subset=["BADGE"], keep="first")
        .reset_index(drop=True)
    )
 
    meta_df["BADGE_LAT"] = pd.to_numeric(meta_df["BADGE_LAT"], errors="coerce")
    meta_df["BADGE_LONG"] = pd.to_numeric(meta_df["BADGE_LONG"], errors="coerce")
    meta_df["NOMINAL"] = pd.to_numeric(meta_df["NOMINAL"], errors="coerce")
 
    lat_map = dict(zip(meta_df["BADGE"], meta_df["BADGE_LAT"]))
    lon_map = dict(zip(meta_df["BADGE"], meta_df["BADGE_LONG"]))
    dev_map = dict(zip(meta_df["BADGE"], meta_df["DEVICE_TYPE_CD"]))
    nom_map = dict(zip(meta_df["BADGE"], meta_df["NOMINAL"]))
 
    lat = np.array([lat_map.get(b, np.nan) for b in badges], dtype=np.float64)
    lon = np.array([lon_map.get(b, np.nan) for b in badges], dtype=np.float64)
    device_type = np.array([dev_map.get(b, None) for b in badges], dtype=object)
    nominal = np.array([nom_map.get(b, np.nan) for b in badges], dtype=np.float64)
 
    # -------- Build canonical 30-day timeline --------
    first_day_df = pd.read_parquet(files[0], columns=["MSRMTDTTM"])
    first_day_df["MSRMTDTTM"] = pd.to_datetime(first_day_df["MSRMTDTTM"], utc=True)
    start = first_day_df["MSRMTDTTM"].min().floor("15min")
 
    timeline = pd.date_range(
        start=start,
        periods=SIGNATURE_LENGTH,
        freq="15min",
        tz="UTC"
    )
 
    # -------- Allocate memory-mapped .npy on disk --------
    os.makedirs(SIGNATURE_DIR, exist_ok=True)
    signatures_path = os.path.join(SIGNATURE_DIR, "signatures.npy")
 
    signatures = open_memmap(
        signatures_path,
        mode="w+",
        dtype=np.float32,
        shape=(len(badges), SIGNATURE_LENGTH)
    )
    signatures[:] = np.nan
 
    # -------- Pass 2: fill 96 intervals per day --------
    for day_idx, f in enumerate(files):
        print(f"Processing {os.path.basename(f)} ({day_idx + 1}/{len(files)})")
 
        day_df = pd.read_parquet(f, columns=["BADGE", "MSRMTDTTM", "PUVALUE"])
        day_df["BADGE"] = day_df["BADGE"].astype(str)
        day_df["MSRMTDTTM"] = pd.to_datetime(day_df["MSRMTDTTM"], utc=True)
 
        day_start = start + pd.Timedelta(days=day_idx)
        day_times = pd.date_range(
            start=day_start,
            periods=INTERVALS_PER_DAY,
            freq="15min",
            tz="UTC"
        )
 
        col_start = day_idx * INTERVALS_PER_DAY
        col_end = col_start + INTERVALS_PER_DAY
 
        for badge, g in day_df.groupby("BADGE"):
            i = badge_index.get(badge)
            if i is None:
                continue
 
            g = g.sort_values("MSRMTDTTM").drop_duplicates(subset=["MSRMTDTTM"], keep="last")
            ts = g.set_index("MSRMTDTTM")["PUVALUE"].reindex(day_times)
            signatures[i, col_start:col_end] = ts.to_numpy(dtype=np.float32)
 
        signatures.flush()
 
    # -------- Save sidecar arrays / metadata --------
    np.save(os.path.join(SIGNATURE_DIR, "badge_ids.npy"), badges)
    np.save(os.path.join(SIGNATURE_DIR, "lat.npy"), lat)
    np.save(os.path.join(SIGNATURE_DIR, "lon.npy"), lon)
 
    meta_out = pd.DataFrame({
        "BADGE": badges,
        "BADGE_LAT": lat,
        "BADGE_LONG": lon,
        "DEVICE_TYPE_CD": device_type,
        "NOMINAL": nominal,
    })
    meta_out.to_parquet(os.path.join(SIGNATURE_DIR, "badge_metadata.parquet"), index=False)
 
    print(f"Saved signature store for {len(badges)} badges and {len(timeline)} intervals.")
    print(f"Expected intervals: {SIGNATURE_LENGTH}")
    print(f"Actual intervals saved: {signatures.shape[1]}")
 
 
if __name__ == "__main__":
    build_from_daily_files()