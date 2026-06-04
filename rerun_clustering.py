#rerun_clustering.py
import os
import numpy as np
import pandas as pd
 
from config import SIGNATURE_DIR, OUTPUT_DIR
from spatial_neighbors import build_tree, find_neighbors
from correlation_engine import compute_corr
from edge_builder import build_candidate_edges, mutual_edges
from clustering_engine import build_clusters
 
def main():
    badges = np.load(os.path.join(SIGNATURE_DIR, "badge_ids.npy"), allow_pickle=True)
    lat = np.load(os.path.join(SIGNATURE_DIR, "lat.npy"))
    lon = np.load(os.path.join(SIGNATURE_DIR, "lon.npy"))
    signatures = np.load(os.path.join(SIGNATURE_DIR, "signatures.npy"), mmap_mode="r")
 
    valid = ~np.isnan(lat) & ~np.isnan(lon)
 
    invalid = ~valid
    print(f"Excluded badges with missing lat/lon: {invalid.sum()}")
 
    badges_v = badges[valid]
    lat_v = lat[valid]
    lon_v = lon[valid]
    sig_v = signatures[valid]
 
    tree, coords = build_tree(lat_v, lon_v)
    neighbors = find_neighbors(tree, coords)
 
    candidate_edges = build_candidate_edges(
        badges_v,
        sig_v,
        neighbors,
        compute_corr
    )
 
    edges = mutual_edges(candidate_edges)
    clusters = build_clusters(edges, len(badges_v))
 
    rows = []
    for cid, cluster in enumerate(clusters):
        for idx in cluster:
            rows.append({"BADGE": badges_v[idx], "CLUSTER": cid})
 
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(OUTPUT_DIR, "full_clusters.csv"), index=False)
 
    pd.DataFrame({"BADGE": badges[invalid]}).to_csv(
        os.path.join(OUTPUT_DIR, "excluded_missing_coords.csv"),
        index=False
    )
 
    print("Wrote full_clusters.csv")
 
if __name__ == "__main__":
    main()