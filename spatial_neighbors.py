# spatial_neighbors.py
 
import numpy as np
from sklearn.neighbors import BallTree
from config import RADIUS_METERS
 
 
def build_tree(lat, lon):
 
    coords = np.vstack((lat, lon)).T
 
    coords_rad = np.radians(coords)
 
    tree = BallTree(coords_rad, metric="haversine")
 
    return tree, coords_rad
 
 
def find_neighbors(tree, coords_rad):
 
    radius = RADIUS_METERS / 6371000
 
    neighbors = tree.query_radius(coords_rad, r=radius)
 
    return neighbors