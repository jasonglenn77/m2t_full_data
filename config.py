# config.py
 
WINDOW_DAYS = 60 # was 30, but 60 gives us more data to work with for better signatures
INTERVALS_PER_DAY = 96
SIGNATURE_LENGTH = WINDOW_DAYS * INTERVALS_PER_DAY  # 5760 was 2880 for 30 days
 
RADIUS_METERS = 175
CORRELATION_THRESHOLD = 0.96 # was 0.95, then 0.97, but 0.96 is more selective and gives us better matches
MIN_OVERLAP_POINTS = 4000 # was 2000, but 4000 ensures we have enough data for a reliable correlation calculation
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