# evaluation.py
 
import pandas as pd

def evaluate(clusters, badges, known_df):
 
    rows = []
 
    for c in clusters:
 
        badge_list = [badges[i] for i in c]
 
        subset = known_df[known_df.BADGE.isin(badge_list)]
 
        rows.append({
            "cluster_size": len(badge_list),
            "known_transformers": subset.TRANSFORMER.nunique()
        })
 
    return pd.DataFrame(rows)