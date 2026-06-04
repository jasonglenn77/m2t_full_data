# edge_builder.py
 
from config import CORRELATION_THRESHOLD, TOP_K_NEIGHBORS
 
def build_candidate_edges(badges, signatures, neighbors, corr_func):
 
    results = {}
 
    for i in range(len(badges)):
 
        sig_i = signatures[i]
 
        scores = []
 
        for j in neighbors[i]:
 
            if i == j:
                continue
 
            corr = corr_func(sig_i, signatures[j])
 
            if corr is None:
                continue
 
            if corr >= CORRELATION_THRESHOLD:
 
                scores.append((j, corr))
 
        scores.sort(key=lambda x: x[1], reverse=True)
 
        results[i] = scores[:TOP_K_NEIGHBORS]
 
    return results
 
 
def mutual_edges(candidate_edges):
 
    edges = []
 
    for a in candidate_edges:
 
        for b, corr in candidate_edges[a]:
 
            if b in candidate_edges:
 
                if any(x[0] == a for x in candidate_edges[b]):
 
                    edges.append((a, b, corr))
 
    return edges