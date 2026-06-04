#badge_change_report.py
import os
import pandas as pd
 
from config import SIGNATURE_DIR, OUTPUT_DIR
 
def main(new_day_parquet):
    meta = pd.read_parquet(os.path.join(SIGNATURE_DIR, "badge_metadata.parquet"))
    meta["BADGE"] = meta["BADGE"].astype(str)
 
    new_df = pd.read_parquet(new_day_parquet)
    new_df["BADGE"] = new_df["BADGE"].astype(str)
 
    new_meta = (
        new_df.sort_values("MSRMTDTTM")
        .groupby("BADGE", as_index=False)
        .first()[["BADGE", "BADGE_LAT", "BADGE_LONG", "DEVICE_TYPE_CD", "NOMINAL"]]
    )
 
    old_badges = set(meta["BADGE"])
    new_badges = set(new_meta["BADGE"])
 
    added = sorted(new_badges - old_badges)
    removed = sorted(old_badges - new_badges)
 
    merged = meta.merge(new_meta, on="BADGE", how="inner", suffixes=("_OLD", "_NEW"))
 
    changed = merged[
        ~(merged["BADGE_LAT_OLD"].fillna("__NA__").astype(str) == merged["BADGE_LAT_NEW"].fillna("__NA__").astype(str)) |
        ~(merged["BADGE_LONG_OLD"].fillna("__NA__").astype(str) == merged["BADGE_LONG_NEW"].fillna("__NA__").astype(str)) |
        ~(merged["DEVICE_TYPE_CD_OLD"].fillna("__NA__").astype(str) == merged["DEVICE_TYPE_CD_NEW"].fillna("__NA__").astype(str)) |
        ~(merged["NOMINAL_OLD"].fillna("__NA__").astype(str) == merged["NOMINAL_NEW"].fillna("__NA__").astype(str))
    ].copy()
 
    os.makedirs(OUTPUT_DIR, exist_ok=True)
 
    pd.DataFrame({"BADGE": added}).to_csv(os.path.join(OUTPUT_DIR, "added_badges.csv"), index=False)
    pd.DataFrame({"BADGE": removed}).to_csv(os.path.join(OUTPUT_DIR, "removed_badges.csv"), index=False)
    changed.to_csv(os.path.join(OUTPUT_DIR, "changed_badge_metadata.csv"), index=False)
 
    print(f"Added badges: {len(added)}")
    print(f"Removed badges: {len(removed)}")
    print(f"Badges with metadata changes: {len(changed)}")

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("Usage: python badge_change_report.py <new_day_parquet>")

    main(sys.argv[1])