#run_subset.py
from datetime import datetime, timezone
import pandas as pd
 
from extract_data import extract_date_range
from pipeline import run_pipeline
from config import OUTPUT_DIR
 
start = datetime(2026, 2, 1, tzinfo=timezone.utc)
end = datetime(2026, 3, 2, tzinfo=timezone.utc)
 
df = extract_date_range(start, end)
 
print(f"Extracted rows: {len(df)}")
 
badges, clusters = run_pipeline(df)
 
cluster_rows = []
 
for cid, cluster in enumerate(clusters):
    for idx in cluster:
        cluster_rows.append({
            "BADGE": badges[idx],
            "CLUSTER": cid
        })
 
cluster_df = pd.DataFrame(cluster_rows)
cluster_df.to_csv(f"{OUTPUT_DIR}/subset_clusters.csv", index=False)
 
cluster_sizes = cluster_df.groupby("CLUSTER").size().reset_index(name="CLUSTER_SIZE")
cluster_sizes.to_csv(f"{OUTPUT_DIR}/subset_cluster_sizes.csv", index=False)
 
print("Cluster size summary:")
print(cluster_sizes["CLUSTER_SIZE"].describe())
print("\nLargest clusters:")
print(cluster_sizes.sort_values("CLUSTER_SIZE", ascending=False).head(20))
 
print("clusters written to outputs folder")