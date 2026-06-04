#rolling_update.py
import os
import numpy as np
import pandas as pd
 
from config import SIGNATURE_DIR, INTERVALS_PER_DAY
 
def main(new_day_parquet):
    badges = np.load(os.path.join(SIGNATURE_DIR, "badge_ids.npy"), allow_pickle=True)
    lat = np.load(os.path.join(SIGNATURE_DIR, "lat.npy"))
    lon = np.load(os.path.join(SIGNATURE_DIR, "lon.npy"))
    signatures = np.load(os.path.join(SIGNATURE_DIR, "signatures.npy"), mmap_mode="r+")
    meta = pd.read_parquet(os.path.join(SIGNATURE_DIR, "badge_metadata.parquet"))
 
    badges = np.array([str(b) for b in badges], dtype=object)
    old_badges = set(badges)
 
    new_df = pd.read_parquet(new_day_parquet)
    new_df["BADGE"] = new_df["BADGE"].astype(str)
    new_df["MSRMTDTTM"] = pd.to_datetime(new_df["MSRMTDTTM"], utc=True)
 
    new_badges = set(new_df["BADGE"].unique())
 
    added = sorted(new_badges - old_badges)
    removed = sorted(old_badges - new_badges)
 
    print(f"Added badges: {len(added)}")
    print(f"Removed badges: {len(removed)}")
 
    # Drop oldest day
    signatures = signatures[:, INTERVALS_PER_DAY:]
 
    # Add newly appearing badges
    if added:
        add_count = len(added)
        badges = np.concatenate([badges, np.array(added, dtype=object)])
        lat = np.concatenate([lat, np.full(add_count, np.nan)])
        lon = np.concatenate([lon, np.full(add_count, np.nan)])
        signatures = np.concatenate(
            [signatures, np.full((add_count, signatures.shape[1]), np.nan, dtype=np.float32)],
            axis=0
        )
 
    badge_index = {b: i for i, b in enumerate(badges)}
 
    day_start = new_df["MSRMTDTTM"].min().floor("D")
    times = pd.date_range(start=day_start, periods=INTERVALS_PER_DAY, freq="15min", tz="UTC")
 
    new_day_matrix = np.full((len(badges), INTERVALS_PER_DAY), np.nan, dtype=np.float32)
 
    for badge, g in new_df.groupby("BADGE"):
        i = badge_index[badge]
        g = g.sort_values("MSRMTDTTM").drop_duplicates(subset=["MSRMTDTTM"], keep="last")
 
        ts = g.set_index("MSRMTDTTM")["PUVALUE"].reindex(times)
        new_day_matrix[i, :] = ts.to_numpy(dtype=np.float32)
 
        lat[i] = pd.to_numeric(g["BADGE_LAT"].iloc[0], errors="coerce")
        lon[i] = pd.to_numeric(g["BADGE_LONG"].iloc[0], errors="coerce")
 
    signatures = np.concatenate([signatures, new_day_matrix], axis=1)
 
    meta_existing = meta.copy()
    meta_existing["BADGE"] = meta_existing["BADGE"].astype(str)
 
    new_meta_rows = (
        new_df.sort_values("MSRMTDTTM")
        .groupby("BADGE", as_index=False)
        .first()[["BADGE", "BADGE_LAT", "BADGE_LONG", "DEVICE_TYPE_CD", "NOMINAL"]]
    )
    new_meta_rows["BADGE_LAT"] = pd.to_numeric(new_meta_rows["BADGE_LAT"], errors="coerce")
    new_meta_rows["BADGE_LONG"] = pd.to_numeric(new_meta_rows["BADGE_LONG"], errors="coerce")
 
    meta_updated = (
        pd.concat([meta_existing, new_meta_rows], ignore_index=True)
        .sort_values("BADGE")
        .drop_duplicates(subset=["BADGE"], keep="last")
        .reset_index(drop=True)
    )
 
    np.save(os.path.join(SIGNATURE_DIR, "badge_ids.npy"), badges)
    np.save(os.path.join(SIGNATURE_DIR, "lat.npy"), lat)
    np.save(os.path.join(SIGNATURE_DIR, "lon.npy"), lon)
    np.save(os.path.join(SIGNATURE_DIR, "signatures.npy"), signatures)
    meta_updated.to_parquet(os.path.join(SIGNATURE_DIR, "badge_metadata.parquet"), index=False)
 
    report = pd.DataFrame({
        "ADDED_BADGE": pd.Series(added, dtype="object"),
        "REMOVED_BADGE": pd.Series(removed, dtype="object")
    })
    report.to_csv(os.path.join(SIGNATURE_DIR, "rolling_add_remove_report.csv"), index=False)
 
    print("Rolling update complete.")

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("Usage: python rolling_update.py <new_day_parquet>")

    main(sys.argv[1])