# rolling_window.py
 
import numpy as np
from config import INTERVALS_PER_DAY
 
def update_window(signatures, new_day_matrix):
 
    signatures = signatures[:, INTERVALS_PER_DAY:]
 
    signatures = np.concatenate([signatures, new_day_matrix], axis=1)
 
    return signatures