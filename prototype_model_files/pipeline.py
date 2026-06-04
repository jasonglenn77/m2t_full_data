# pipeline.py
 
from signature_builder import build_signatures
from spatial_neighbors import build_tree, find_neighbors
from correlation_engine import compute_corr
from edge_builder import build_candidate_edges, mutual_edges
from clustering_engine import build_clusters

def run_pipeline(df):
 
    badges, signatures, lat, lon = build_signatures(df)
 
    tree, coords = build_tree(lat, lon)
 
    neighbors = find_neighbors(tree, coords)
 
    candidate_edges = build_candidate_edges(
        badges,
        signatures,
        neighbors,
        compute_corr
    )
 
    edges = mutual_edges(candidate_edges)
 
    clusters = build_clusters(edges, len(badges))
 
    return badges, clusters