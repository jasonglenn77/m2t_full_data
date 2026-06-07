#find_mapping_errors.py
import os
import pandas as pd

CLUSTER_FILE = "data/outputs/full_clusters.csv"
TRUTH_FILE = "data/outputs/known_mapping.csv"
IGNORE_FILE = "data/state/corrections_ignored.csv"
SUPPRESSED_FILE = "data/outputs/corrections_suppressed.csv"
 
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

# Apply the user-maintained ignore list (data/state/corrections_ignored.csv).
# A row is suppressed when (BADGE, RECOMMENDED_TRANSFORMER) matches an entry.
# If the model later recommends a DIFFERENT transformer for the same badge,
# the new recommendation surfaces — only the specific suggestion is hidden.
if os.path.exists(IGNORE_FILE):
    def _norm(s):
        return (
            s.astype(str)
            .str.replace(r"\.0+$", "", regex=True)
            .replace({"nan": pd.NA, "<NA>": pd.NA, "None": pd.NA})
        )

    ignore = pd.read_csv(IGNORE_FILE, dtype=str)
    required = {"BADGE", "RECOMMENDED_TRANSFORMER"}
    if not ignore.empty and required.issubset(ignore.columns):
        ignore = ignore.dropna(subset=["BADGE", "RECOMMENDED_TRANSFORMER"]).copy()
        ignore["BADGE"] = ignore["BADGE"].astype(str)
        ignore["RECOMMENDED_TRANSFORMER"] = _norm(ignore["RECOMMENDED_TRANSFORMER"])

        rec_norm = rec_df.copy()
        rec_norm["BADGE"] = rec_norm["BADGE"].astype(str)
        rec_norm["RECOMMENDED_TRANSFORMER_NORM"] = _norm(rec_norm["RECOMMENDED_TRANSFORMER"])

        ignore_keys = set(zip(ignore["BADGE"], ignore["RECOMMENDED_TRANSFORMER"]))
        keep_mask = ~rec_norm.apply(
            lambda r: (r["BADGE"], r["RECOMMENDED_TRANSFORMER_NORM"]) in ignore_keys,
            axis=1,
        )

        suppressed_df = rec_df[~keep_mask].copy()
        rec_df = rec_df[keep_mask].reset_index(drop=True)

        os.makedirs(os.path.dirname(SUPPRESSED_FILE), exist_ok=True)
        suppressed_df.to_csv(SUPPRESSED_FILE, index=False)

        print(f"\nSuppressed {len(suppressed_df)} corrections via {IGNORE_FILE}")
        print(f"  (suppressed rows written to {SUPPRESSED_FILE})")

rec_df.to_csv("data/outputs/transformer_corrections.csv", index=False)

print("\nPotential transformer mapping errors:")
print(rec_df)

print("\nTotal suspected errors:", len(rec_df))