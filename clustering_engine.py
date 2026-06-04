# clustering_engine.py

import networkx as nx
 
def build_clusters(edges, num_nodes):
 
    G = nx.Graph()
 
    G.add_nodes_from(range(num_nodes))
 
    for a, b, w in edges:
 
        G.add_edge(a, b, weight=w)
 
    clusters = list(nx.connected_components(G))
 
    return clusters