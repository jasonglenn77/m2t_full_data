# correlation_engine.py
 
import numpy as np
from config import MIN_OVERLAP_POINTS
 
def compute_corr(a, b):
 
    mask = ~np.isnan(a) & ~np.isnan(b)
 
    overlap = mask.sum()
 
    if overlap < MIN_OVERLAP_POINTS:
        return None
 
    a = a[mask]
    b = b[mask]
 
    corr = np.corrcoef(a, b)[0, 1]
 
    return corr