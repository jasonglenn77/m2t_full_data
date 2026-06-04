#find_mapping_errors.py
import pandas as pd
 
CLUSTER_FILE = "data/outputs/full_clusters.csv"
TRUTH_FILE = "data/outputs/known_mapping.csv"
 
clusters = pd.read_csv(CLUSTER_FILE, dtype={"BADGE": str})
truth = pd.read_csv(TRUTH_FILE, dtype={"badge": str})
 
truth = truth.rename(columns={
    "badge": "BADGE",
    "transf_id": "TRANSFORMER"
})
 
df = clusters.merge(truth, on="BADGE", how="inner")
 
recommendations = []
 
for cluster, g in df.groupby("CLUSTER"):
    modes = g["TRANSFORMER"].mode()

    if modes.empty:
        continue

    majority = modes.iloc[0]
 
    for _, row in g.iterrows():
        if row["TRANSFORMER"] != majority:
            recommendations.append({
                "BADGE": row["BADGE"],
                "CURRENT_TRANSFORMER": row["TRANSFORMER"],
                "RECOMMENDED_TRANSFORMER": majority,
                "CLUSTER": cluster
            })
 
rec_df = pd.DataFrame(recommendations)
rec_df.to_csv("data/outputs/transformer_corrections.csv", index=False)
 
print("\nPotential transformer mapping errors:")
print(rec_df)
 
print("\nTotal suspected errors:", len(rec_df))