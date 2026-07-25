# config.py
 
WINDOW_DAYS = 45 # was 30 (May), then 60 (apr10/apr25); 45 balances seasonal stability vs statistical power
INTERVALS_PER_DAY = 96
SIGNATURE_LENGTH = WINDOW_DAYS * INTERVALS_PER_DAY  # 4320 was 2880 for 30 days then 5760 for 60 days, but 4320 for 45 days is a good balance between data and performance
 
RADIUS_METERS = 175
CORRELATION_THRESHOLD = 0.95 # had 0.96, but 0.95 allows for more matches while still being quite strict
MIN_OVERLAP_POINTS = 3000 # was 2000, then 4000 for 60 days, but 3000 for 45 days is a good balance to ensure enough data points for a reliable correlation while still allowing for some variability in the data
TOP_K_NEIGHBORS = 5
 
RAW_DATA_DIR = "data/raw"
DAILY_RAW_DIR = "data/raw/daily"
PROCESSED_DATA_DIR = "data/processed"
SIGNATURE_DIR = "data/processed/signatures"
OUTPUT_DIR = "data/outputs"
STATE_DIR = "data/state"

EARTH_RADIUS_M = 6371000
ARRAYSIZE = 10000
MAX_RETRIES = 3