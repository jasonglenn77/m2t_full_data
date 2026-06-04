#save_daily_data.py
from datetime import datetime, timedelta, timezone
import os
import json
 
from config import DAILY_RAW_DIR, STATE_DIR, MAX_RETRIES
from extract_data import extract_date_range
 
STATE_FILE = os.path.join(STATE_DIR, "daily_extract_state.json")
 
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}
 
def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
 
def save_day(df, day):
    os.makedirs(DAILY_RAW_DIR, exist_ok=True)
    out = os.path.join(DAILY_RAW_DIR, f"{day:%Y-%m-%d}.parquet")
    df.to_parquet(out, index=False)
 
def run_daily_extract(start_date, end_date):
    state = load_state()
    current = start_date
 
    while current <= end_date:
        day_key = current.strftime("%Y-%m-%d")
 
        if state.get(day_key) == "done":
            print(f"Skipping {day_key}, already extracted")
            current += timedelta(days=1)
            continue
 
        print(f"Extracting day {day_key}")
        df = extract_date_range(current, current, max_retries=MAX_RETRIES)
        save_day(df, current)
        state[day_key] = "done"
        save_state(state)
 
        current += timedelta(days=1)
 
if __name__ == "__main__":
    start = datetime(2026, 3, 3, tzinfo=timezone.utc)
    end = datetime(2026, 3, 10, tzinfo=timezone.utc)
    run_daily_extract(start, end)