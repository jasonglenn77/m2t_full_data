# signature_builder.py
 
import pandas as pd
import numpy as np
from config import WINDOW_DAYS, INTERVALS_PER_DAY
 
 
def build_signatures(df):
 
    df["MSRMTDTTM"] = pd.to_datetime(df["MSRMTDTTM"])
 
    df = df.sort_values(["BADGE", "MSRMTDTTM"])
 
    start = df["MSRMTDTTM"].min()
    end = df["MSRMTDTTM"].max()
 
    timeline = pd.date_range(
        start=start,
        end=end,
        freq="15min"
    )
 
    badges = df["BADGE"].unique()
 
    signature_matrix = np.full((len(badges), len(timeline)), np.nan)
 
    lat = []
    lon = []
 
    badge_index = {b: i for i, b in enumerate(badges)}
 
    for badge, g in df.groupby("BADGE"):
 
        idx = badge_index[badge]
 
        ts = g.set_index("MSRMTDTTM")["PUVALUE"]
 
        ts = ts.reindex(timeline)
 
        signature_matrix[idx] = ts.values
 
        lat.append(g["BADGE_LAT"].iloc[0])
        lon.append(g["BADGE_LONG"].iloc[0])
 
    lat = np.array(lat, dtype=float)
    lon = np.array(lon, dtype=float)
 
    return badges, signature_matrix, lat, lon