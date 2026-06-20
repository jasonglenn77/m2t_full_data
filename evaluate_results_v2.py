# evaluate_results_v2.py
"""
v2 wrapper around evaluate_results.

Same logic as v1: report cluster purity and transformer completeness
metrics on the v2 clusters.

Reads:
    data/outputs_v2/full_clusters.csv
    data/outputs/known_mapping.csv   (shared with v1)

Writes:
    data/outputs_v2/cluster_to_transformer_counts.csv
    data/outputs_v2/transformer_to_cluster_counts.csv
"""

import os

import pandas as pd

CLUSTER_FILE = "data/outputs_v2/full_clusters.csv"
TRUTH_FILE = "data/outputs/known_mapping.csv"
OUT_DIR = "data/outputs_v2"

clusters = pd.read_csv(CLUSTER_FILE, dtype={"BADGE": str})
truth = pd.read_csv(TRUTH_FILE, dtype={"badge": str})
truth = truth.rename(columns={"badge": "BADGE", "transf_id": "TRANSFORMER"})

df = clusters.merge(truth, on="BADGE", how="inner")

print("Mapped badges evaluated:", len(df))
print("Unique badges in clusters file:", clusters["BADGE"].nunique())
print("Unique badges in known mapping:", truth["BADGE"].nunique())

cluster_summary = df.groupby("CLUSTER")["TRANSFORMER"].nunique()
print("Clusters containing >1 transformer:")
print(cluster_summary[cluster_summary > 1])

transformer_summary = df.groupby("TRANSFORMER")["CLUSTER"].nunique()
print("\nTransformers split across clusters:")
print(transformer_summary[transformer_summary > 1])

print("\nTotal clusters:", df["CLUSTER"].nunique())
print("Total transformers:", df["TRANSFORMER"].nunique())

os.makedirs(OUT_DIR, exist_ok=True)
cluster_summary.to_csv(os.path.join(OUT_DIR, "cluster_to_transformer_counts.csv"))
transformer_summary.to_csv(
    os.path.join(OUT_DIR, "transformer_to_cluster_counts.csv")
)
