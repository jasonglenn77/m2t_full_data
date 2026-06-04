#extract_data.py
import time
import pandas as pd
from datetime import timedelta
from config import MAX_RETRIES
from db_oracle import connect_c2m, fetch_hour_df
 
def extract_date_range(start_date, end_date, max_retries=MAX_RETRIES):
    dfs = []
    current = start_date
 
    while current <= end_date:
        for hour in range(24):
            hour_dt = current + timedelta(hours=hour)
 
            last_error = None
            for attempt in range(1, max_retries + 1):
                conn = None
                try:
                    conn = connect_c2m()
                    df = fetch_hour_df(conn, hour_dt)
                    dfs.append(df)
                    print(f"Loaded {hour_dt} rows={len(df)}")
                    break
 
                except Exception as e:
                    last_error = e
                    print(f"Failed {hour_dt} attempt {attempt}/{max_retries}: {e}")
                    time.sleep(2)
 
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
 
            else:
                raise RuntimeError(
                    f"Failed to extract hour {hour_dt} after {max_retries} attempts"
                ) from last_error
 
        current += timedelta(days=1)
 
    if not dfs:
        return pd.DataFrame()
 
    return pd.concat(dfs, ignore_index=True)