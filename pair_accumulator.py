# pair_accumulator.py
"""
Persistent ledger of daily Pearson correlations per badge pair (v2 model).

For each candidate pair (sorted so BADGE_A < BADGE_B) we keep up to
WINDOW_DAYS most recent daily Pearson values, plus historical metadata.
Daily values older than WINDOW_DAYS days drop off — this is the
structural protection against stickiness (an early-formed edge can't
persist forever if recent daily correlations don't keep clearing the
gates).

Storage: data/state/pair_ledger.parquet
    BADGE_A (str)         -- always BADGE_A < BADGE_B
    BADGE_B (str)
    VALUES (list[float])  -- daily Pearson values, oldest first, len <= WINDOW_DAYS
    HISTORICAL_N (int)    -- ever-seen daily-correlation count
    HISTORICAL_MEAN (f64) -- ever-seen mean (for long-term trend comparisons)
    FIRST_DAY (str)       -- 'YYYY-MM-DD' of first observation
    LAST_DAY (str)        -- 'YYYY-MM-DD' of most recent observation

Operations are intentionally simple for the Phase 1 prototype: load full
ledger -> dict, mutate in place, dump dict back to DataFrame, write. This
will work fine for a few million pairs; optimize if/when needed.
"""

import os

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from config import WINDOW_DAYS

LEDGER_DIR = "data/state"
LEDGER_PATH = os.path.join(LEDGER_DIR, "pair_ledger.parquet")

LEDGER_COLUMNS = [
    "BADGE_A",
    "BADGE_B",
    "VALUES",
    "HISTORICAL_N",
    "HISTORICAL_MEAN",
    "FIRST_DAY",
    "LAST_DAY",
]

# pyarrow schema matching LEDGER_COLUMNS exactly. Used for streaming saves
# that bypass pandas DataFrame materialization (which doubles peak RAM).
LEDGER_SCHEMA = pa.schema(
    [
        ("BADGE_A", pa.string()),
        ("BADGE_B", pa.string()),
        ("VALUES", pa.list_(pa.float64())),
        ("HISTORICAL_N", pa.int32()),
        ("HISTORICAL_MEAN", pa.float64()),
        ("FIRST_DAY", pa.string()),
        ("LAST_DAY", pa.string()),
    ]
)

# Per-batch write size for save_ledger_from_dict. 500K pairs per chunk keeps
# peak chunk memory in the low hundreds of MB.
SAVE_CHUNK_SIZE = 500_000


def canonical_pair(a, b):
    """Sort a pair so BADGE_A < BADGE_B (string comparison)."""
    return (a, b) if a < b else (b, a)


def load_ledger():
    """Read the ledger from disk. Returns empty DataFrame with correct columns if absent."""
    if not os.path.exists(LEDGER_PATH):
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    return pd.read_parquet(LEDGER_PATH)


def save_ledger(df):
    """Write a DataFrame ledger to disk atomically (write to .tmp then rename).

    Prefer save_ledger_from_dict() in the backfill / daily-update hot path —
    that streams from the working dict directly to Parquet in chunks and
    avoids materializing a multi-GB DataFrame in memory just to save it.
    """
    os.makedirs(LEDGER_DIR, exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    df.to_parquet(tmp, index=False)
    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)
    os.rename(tmp, LEDGER_PATH)


def save_ledger_from_dict(d):
    """
    Stream the in-memory ledger dict directly to Parquet in chunks of
    SAVE_CHUNK_SIZE pairs each. Avoids the memory spike that occurs when
    converting dict -> list-of-dicts -> DataFrame -> Parquet (each step
    roughly doubles RAM).

    Produces a Parquet file with the same LEDGER_SCHEMA as save_ledger(),
    so loaders don't need to care which path was used to write it.

    Atomically replaces LEDGER_PATH via a .tmp file.
    """
    os.makedirs(LEDGER_DIR, exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"

    if not d:
        # Empty ledger: still write a valid Parquet file with the schema.
        empty = {col.name: [] for col in LEDGER_SCHEMA}
        with pq.ParquetWriter(tmp, LEDGER_SCHEMA) as writer:
            writer.write_batch(pa.RecordBatch.from_pydict(empty, schema=LEDGER_SCHEMA))
    else:
        chunk = {
            "BADGE_A": [],
            "BADGE_B": [],
            "VALUES": [],
            "HISTORICAL_N": [],
            "HISTORICAL_MEAN": [],
            "FIRST_DAY": [],
            "LAST_DAY": [],
        }
        with pq.ParquetWriter(tmp, LEDGER_SCHEMA) as writer:
            for (a, b), e in d.items():
                chunk["BADGE_A"].append(a)
                chunk["BADGE_B"].append(b)
                chunk["VALUES"].append(e["VALUES"])
                chunk["HISTORICAL_N"].append(e["HISTORICAL_N"])
                chunk["HISTORICAL_MEAN"].append(e["HISTORICAL_MEAN"])
                chunk["FIRST_DAY"].append(e["FIRST_DAY"])
                chunk["LAST_DAY"].append(e["LAST_DAY"])

                if len(chunk["BADGE_A"]) >= SAVE_CHUNK_SIZE:
                    writer.write_batch(
                        pa.RecordBatch.from_pydict(chunk, schema=LEDGER_SCHEMA)
                    )
                    for key in chunk:
                        chunk[key].clear()

            if chunk["BADGE_A"]:
                writer.write_batch(
                    pa.RecordBatch.from_pydict(chunk, schema=LEDGER_SCHEMA)
                )

    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)
    os.rename(tmp, LEDGER_PATH)


def ledger_to_dict(df):
    """
    Convert ledger DataFrame to dict keyed by (BADGE_A, BADGE_B).

    Each value is {"VALUES": list[float], "HISTORICAL_N": int,
    "HISTORICAL_MEAN": float, "FIRST_DAY": str, "LAST_DAY": str}.
    """
    if df.empty:
        return {}
    out = {}
    for a, b, vals, hn, hm, fd, ld in zip(
        df["BADGE_A"],
        df["BADGE_B"],
        df["VALUES"],
        df["HISTORICAL_N"],
        df["HISTORICAL_MEAN"],
        df["FIRST_DAY"],
        df["LAST_DAY"],
    ):
        out[(a, b)] = {
            "VALUES": list(vals),
            "HISTORICAL_N": int(hn),
            "HISTORICAL_MEAN": float(hm),
            "FIRST_DAY": fd,
            "LAST_DAY": ld,
        }
    return out


def dict_to_ledger(d):
    """Convert dict back to ledger DataFrame in stable column order."""
    if not d:
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    rows = []
    for (a, b), e in d.items():
        rows.append(
            {
                "BADGE_A": a,
                "BADGE_B": b,
                "VALUES": e["VALUES"],
                "HISTORICAL_N": e["HISTORICAL_N"],
                "HISTORICAL_MEAN": e["HISTORICAL_MEAN"],
                "FIRST_DAY": e["FIRST_DAY"],
                "LAST_DAY": e["LAST_DAY"],
            }
        )
    return pd.DataFrame(rows, columns=LEDGER_COLUMNS)


def update_pair(ledger_dict, badge_a, badge_b, r, day):
    """
    Update one pair's record. Mutates ledger_dict in place.

    Idempotent for re-runs of the same day: if LAST_DAY >= day, skip
    (the dict already has this day or a newer one).

    For new pairs: create an entry with a single-element VALUES list.
    For existing pairs: append r, trim to WINDOW_DAYS most recent values,
    update historical running stats.
    """
    a, b = canonical_pair(badge_a, badge_b)
    key = (a, b)
    entry = ledger_dict.get(key)

    if entry is None:
        ledger_dict[key] = {
            "VALUES": [float(r)],
            "HISTORICAL_N": 1,
            "HISTORICAL_MEAN": float(r),
            "FIRST_DAY": day,
            "LAST_DAY": day,
        }
        return

    if entry["LAST_DAY"] >= day:
        return  # already have this day or newer; skip silently

    values = entry["VALUES"]
    values.append(float(r))
    if len(values) > WINDOW_DAYS:
        values = values[-WINDOW_DAYS:]
    entry["VALUES"] = values

    entry["HISTORICAL_N"] += 1
    n = entry["HISTORICAL_N"]
    entry["HISTORICAL_MEAN"] += (float(r) - entry["HISTORICAL_MEAN"]) / n
    entry["LAST_DAY"] = day


def compute_stats(values):
    """
    Given a list of daily correlation values (length <= WINDOW_DAYS),
    return mean / std / trend / n.

    trend = mean of last third minus mean of first third, used as a
    "relationship deteriorating" signal. Returns 0.0 if fewer than 6
    days are recorded (not enough to compute a meaningful trend).
    """
    arr = np.asarray(values, dtype=np.float64)
    n = arr.size
    if n == 0:
        return {"mean_r": np.nan, "std_r": np.nan, "trend": np.nan, "n": 0}
    mean_r = float(np.mean(arr))
    std_r = float(np.std(arr)) if n >= 2 else 0.0
    if n >= 6:
        k = n // 3
        trend = float(np.mean(arr[-k:]) - np.mean(arr[:k]))
    else:
        trend = 0.0
    return {"mean_r": mean_r, "std_r": std_r, "trend": trend, "n": n}
