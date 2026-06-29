# pair_accumulator.py
"""
Persistent ledger of daily Pearson correlations per badge pair (v2 model).

For each candidate pair (sorted so BADGE_A < BADGE_B) we keep up to
WINDOW_DAYS most recent daily Pearson values, plus historical metadata.
Daily values older than WINDOW_DAYS days drop off — this is the
structural protection against stickiness.

In-memory storage: PairLedger class with parallel numpy arrays plus a
sorted int64-keyed lookup array (replacing an earlier dict). For 11.7M
pairs total RAM is ~2.7 GB; the lookup structure itself is ~140 MB
(vs ~1.7 GB for a Python dict of tuple keys). Badges are assumed to
be numeric strings that fit in 32 bits, which the pipeline's badge IDs
satisfy.

On-disk storage: data/state/pair_ledger.parquet
    BADGE_A (str)         -- always BADGE_A < BADGE_B
    BADGE_B (str)
    VALUES (list[float])  -- daily Pearson values, oldest first, len <= WINDOW_DAYS
    HISTORICAL_N (int)    -- ever-seen daily-correlation count
    HISTORICAL_MEAN (f64) -- ever-seen mean (for long-term trend comparisons)
    FIRST_DAY (str)       -- 'YYYY-MM-DD' of first observation
    LAST_DAY (str)        -- 'YYYY-MM-DD' of most recent observation

Schema is unchanged from earlier versions, so existing on-disk ledgers
load correctly into the new PairLedger.
"""

import gc
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from config import WINDOW_DAYS

LEDGER_DIR = "data/state"
LEDGER_PATH = os.path.join(LEDGER_DIR, "pair_ledger.parquet")

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

SAVE_CHUNK_SIZE = 500_000
# Small load batches keep per-iteration Python-object allocations modest
# so the load works on memory-constrained machines. 50K rows of objects
# is ~6 MB of strings instead of ~60 MB at 500K.
LOAD_CHUNK_SIZE = 50_000


def canonical_pair(a, b):
    """Sort a pair so BADGE_A < BADGE_B (string comparison)."""
    return (a, b) if a < b else (b, a)


class PairLedger:
    """
    In-memory ledger of pair correlations using parallel numpy arrays.

    Storage layout (capacity-sized arrays, only first n entries are valid):
        badge_a:         object array of badge IDs (string-like)
        badge_b:         object array of badge IDs (string-like)
        daily_values:    (capacity, WINDOW_DAYS) float32, NaN-filled
        n_recent:        int16 — how many leading slots of daily_values are populated
        historical_n:    int32 — total count ever observed (may exceed WINDOW_DAYS)
        historical_mean: float64 — running mean over all-time observations
        first_day:       object array of 'YYYY-MM-DD' strings
        last_day:        object array of 'YYYY-MM-DD' strings

    Lookup: pairs are addressed by an int64 packed key
    (int(BADGE_A) << 32) | int(BADGE_B) with the badges already in
    canonical order (a < b). Sorted in _sorted_keys/_sorted_rows so
    lookups use numpy.searchsorted (O(log n) but vectorizable). New
    pairs are first parked in _pending (a small Python dict) and
    periodically merged into the sorted arrays via _flush_pending().
    """

    INITIAL_CAPACITY = 13_000_000
    # Maximum size of the pending-insert buffer before we re-sort.
    PENDING_FLUSH_THRESHOLD = 50_000

    def __init__(self, capacity=None):
        self.n = 0
        self.capacity = 0
        self.badge_a = None
        self.badge_b = None
        self.daily_values = None
        self.n_recent = None
        self.historical_n = None
        self.historical_mean = None
        self.first_day = None
        self.last_day = None
        # Sorted-array lookup (replaces the old Python dict).
        self._sorted_keys = np.empty(0, dtype=np.int64)
        self._sorted_rows = np.empty(0, dtype=np.int32)
        # Newly-inserted pairs waiting to be merged into the sorted arrays.
        self._pending = {}
        self._grow(capacity or self.INITIAL_CAPACITY)

    def __len__(self):
        return self.n

    def _grow(self, target_capacity):
        """Allocate or expand arrays to target_capacity, copying existing data."""
        if target_capacity <= self.capacity:
            return

        new_badge_a = np.empty(target_capacity, dtype=object)
        new_badge_b = np.empty(target_capacity, dtype=object)
        new_daily_values = np.full(
            (target_capacity, WINDOW_DAYS), np.nan, dtype=np.float32
        )
        new_n_recent = np.zeros(target_capacity, dtype=np.int16)
        new_historical_n = np.zeros(target_capacity, dtype=np.int32)
        # float32 plenty for a running mean bounded in [-1, 1] and saves
        # 50 MB at 13M capacity vs float64.
        new_historical_mean = np.zeros(target_capacity, dtype=np.float32)
        new_first_day = np.empty(target_capacity, dtype=object)
        new_last_day = np.empty(target_capacity, dtype=object)

        if self.n > 0:
            new_badge_a[: self.n] = self.badge_a[: self.n]
            new_badge_b[: self.n] = self.badge_b[: self.n]
            new_daily_values[: self.n] = self.daily_values[: self.n]
            new_n_recent[: self.n] = self.n_recent[: self.n]
            new_historical_n[: self.n] = self.historical_n[: self.n]
            new_historical_mean[: self.n] = self.historical_mean[: self.n]
            new_first_day[: self.n] = self.first_day[: self.n]
            new_last_day[: self.n] = self.last_day[: self.n]

            # Free old arrays before retaining the new ones to avoid peak-RAM doubling
            self.badge_a = None
            self.badge_b = None
            self.daily_values = None
            self.n_recent = None
            self.historical_n = None
            self.historical_mean = None
            self.first_day = None
            self.last_day = None
            gc.collect()

        self.badge_a = new_badge_a
        self.badge_b = new_badge_b
        self.daily_values = new_daily_values
        self.n_recent = new_n_recent
        self.historical_n = new_historical_n
        self.historical_mean = new_historical_mean
        self.first_day = new_first_day
        self.last_day = new_last_day
        self.capacity = target_capacity

    @staticmethod
    def _pack_key(a_str, b_str):
        """Return the int64 packed key for a canonical pair. Returns None
        for non-numeric badge IDs (those can't use the int-packed index
        and must stay in the _pending fallback dict)."""
        try:
            a_int = int(a_str)
            b_int = int(b_str)
        except (TypeError, ValueError):
            return None
        if a_int > b_int:
            a_int, b_int = b_int, a_int
        return (a_int << 32) | b_int

    def _lookup_row(self, a_str, b_str):
        """Return the row index for (a, b) if known, else -1.
        Checks the pending buffer first (recent inserts), then the
        sorted array via binary search."""
        if a_str > b_str:
            a_str, b_str = b_str, a_str
        pair = (a_str, b_str)
        row = self._pending.get(pair, -1)
        if row != -1:
            return row
        key = self._pack_key(a_str, b_str)
        if key is None or self._sorted_keys.size == 0:
            return -1
        idx = np.searchsorted(self._sorted_keys, key)
        if idx < self._sorted_keys.size and int(self._sorted_keys[idx]) == key:
            return int(self._sorted_rows[idx])
        return -1

    def _flush_pending(self):
        """Merge the _pending buffer into the sorted arrays. Idempotent."""
        if not self._pending:
            return
        packed_keys = []
        rows = []
        non_numeric = {}
        for (a, b), row in self._pending.items():
            key = self._pack_key(a, b)
            if key is None:
                # Non-numeric badge IDs can't be packed; keep them in the
                # buffer (small, lookups still hit the pending dict).
                non_numeric[(a, b)] = row
                continue
            packed_keys.append(key)
            rows.append(row)
        if packed_keys:
            new_keys_arr = np.array(packed_keys, dtype=np.int64)
            new_rows_arr = np.array(rows, dtype=np.int32)
            if self._sorted_keys.size == 0:
                merged_keys = new_keys_arr
                merged_rows = new_rows_arr
            else:
                merged_keys = np.concatenate([self._sorted_keys, new_keys_arr])
                merged_rows = np.concatenate([self._sorted_rows, new_rows_arr])
            order = np.argsort(merged_keys, kind="mergesort")
            self._sorted_keys = merged_keys[order]
            self._sorted_rows = merged_rows[order]
        self._pending = non_numeric

    def update(self, badge_a, badge_b, r, day):
        """
        Apply one pair's correlation for a given day.

        Idempotent for re-runs of the same day: if LAST_DAY >= day, skip.

        For new pairs: append a fresh entry to ledger arrays and stash the
        (pair -> row) mapping in _pending; pending is merged into the
        sorted arrays periodically.
        For existing pairs: append r to the rolling window, trim to
        WINDOW_DAYS most recent values, update historical running stats.
        """
        if badge_b < badge_a:
            badge_a, badge_b = badge_b, badge_a

        if len(self._pending) >= self.PENDING_FLUSH_THRESHOLD:
            self._flush_pending()

        idx = self._lookup_row(badge_a, badge_b)

        if idx == -1:
            if self.n >= self.capacity:
                self._grow(max(self.capacity + 1, int(self.capacity * 1.5)))
            idx = self.n
            self.n += 1
            self.badge_a[idx] = badge_a
            self.badge_b[idx] = badge_b
            self.daily_values[idx, 0] = r
            self.n_recent[idx] = 1
            self.historical_n[idx] = 1
            self.historical_mean[idx] = float(r)
            self.first_day[idx] = day
            self.last_day[idx] = day
            self._pending[(badge_a, badge_b)] = idx
            return

        if self.last_day[idx] >= day:
            return

        cur_n = int(self.n_recent[idx])
        if cur_n < WINDOW_DAYS:
            self.daily_values[idx, cur_n] = r
            self.n_recent[idx] = cur_n + 1
        else:
            # Shift the rolling window left, drop oldest, append newest
            self.daily_values[idx, :-1] = self.daily_values[idx, 1:]
            self.daily_values[idx, -1] = r

        self.historical_n[idx] += 1
        n_hist = int(self.historical_n[idx])
        self.historical_mean[idx] += (
            float(r) - self.historical_mean[idx]
        ) / n_hist
        self.last_day[idx] = day

    def most_recent_day(self):
        """Return the maximum LAST_DAY across all pairs, or None if empty."""
        if self.n == 0:
            return None
        return max(self.last_day[: self.n])

    def save(self):
        """
        Stream to Parquet in SAVE_CHUNK_SIZE-row batches. Atomically replaces
        LEDGER_PATH via a .tmp file.
        """
        # Make sure recently-inserted pairs are searchable on next load
        # by being represented in the on-disk row order (no work needed —
        # the rows themselves are already in self.badge_a/b at their final
        # indices), but flush the in-memory lookup so it's consistent.
        self._flush_pending()

        os.makedirs(LEDGER_DIR, exist_ok=True)
        tmp = LEDGER_PATH + ".tmp"

        if self.n == 0:
            empty = {col.name: [] for col in LEDGER_SCHEMA}
            with pq.ParquetWriter(tmp, LEDGER_SCHEMA) as writer:
                writer.write_batch(
                    pa.RecordBatch.from_pydict(empty, schema=LEDGER_SCHEMA)
                )
        else:
            with pq.ParquetWriter(tmp, LEDGER_SCHEMA) as writer:
                for start in range(0, self.n, SAVE_CHUNK_SIZE):
                    end = min(start + SAVE_CHUNK_SIZE, self.n)

                    # Build per-row variable-length VALUES lists for this chunk.
                    # daily_values is fixed-width (capacity, WINDOW_DAYS); we
                    # serialize only the populated leading slots per row.
                    values_lists = []
                    for i in range(start, end):
                        cur_n = int(self.n_recent[i])
                        values_lists.append(
                            self.daily_values[i, :cur_n].astype(np.float64).tolist()
                        )

                    chunk = {
                        "BADGE_A": self.badge_a[start:end].tolist(),
                        "BADGE_B": self.badge_b[start:end].tolist(),
                        "VALUES": values_lists,
                        "HISTORICAL_N": self.historical_n[start:end].tolist(),
                        "HISTORICAL_MEAN": self.historical_mean[start:end].tolist(),
                        "FIRST_DAY": self.first_day[start:end].tolist(),
                        "LAST_DAY": self.last_day[start:end].tolist(),
                    }
                    writer.write_batch(
                        pa.RecordBatch.from_pydict(chunk, schema=LEDGER_SCHEMA)
                    )

        if os.path.exists(LEDGER_PATH):
            os.remove(LEDGER_PATH)
        os.rename(tmp, LEDGER_PATH)

    @classmethod
    def load(cls):
        """
        Stream-load from LEDGER_PATH, populating the in-memory arrays
        without ever materializing the full ledger as a pandas DataFrame.

        Returns an empty PairLedger if no file exists yet.
        """
        if not os.path.exists(LEDGER_PATH):
            return cls()

        parquet_file = pq.ParquetFile(LEDGER_PATH)
        n_total = parquet_file.metadata.num_rows
        ledger = cls(capacity=max(cls.INITIAL_CAPACITY, int(n_total * 1.2) + 1))

        # Build the sorted-key lookup directly during load — never
        # materialize a Python dict of all 11M+ pairs (that's ~1.7 GB of
        # overhead the machine doesn't have). Non-numeric badges (rare)
        # fall back to the _pending dict.
        all_keys = np.empty(n_total, dtype=np.int64)
        all_keys_valid = np.zeros(n_total, dtype=bool)

        offset = 0
        for batch in parquet_file.iter_batches(batch_size=LOAD_CHUNK_SIZE):
            n_batch = batch.num_rows
            end = offset + n_batch

            badge_a_arr = batch.column("BADGE_A").to_pylist()
            badge_b_arr = batch.column("BADGE_B").to_pylist()
            values_arr = batch.column("VALUES").to_pylist()
            historical_n_arr = batch.column("HISTORICAL_N").to_numpy()
            historical_mean_arr = batch.column("HISTORICAL_MEAN").to_numpy()
            first_day_arr = batch.column("FIRST_DAY").to_pylist()
            last_day_arr = batch.column("LAST_DAY").to_pylist()

            ledger.badge_a[offset:end] = badge_a_arr
            ledger.badge_b[offset:end] = badge_b_arr

            for i, (a, b) in enumerate(zip(badge_a_arr, badge_b_arr)):
                key = cls._pack_key(a, b)
                if key is None:
                    ledger._pending[(a, b)] = offset + i
                else:
                    all_keys[offset + i] = key
                    all_keys_valid[offset + i] = True

            for i, vals in enumerate(values_arr):
                cur_n = len(vals)
                if cur_n > 0:
                    ledger.daily_values[offset + i, :cur_n] = vals
                    ledger.n_recent[offset + i] = cur_n

            ledger.historical_n[offset:end] = historical_n_arr.astype(np.int32)
            ledger.historical_mean[offset:end] = historical_mean_arr.astype(
                np.float32
            )
            ledger.first_day[offset:end] = first_day_arr
            ledger.last_day[offset:end] = last_day_arr

            offset = end

            del badge_a_arr, badge_b_arr, values_arr
            del historical_n_arr, historical_mean_arr
            del first_day_arr, last_day_arr
            gc.collect()

        ledger.n = offset

        # Sort the int64 keys for searchsorted-based lookup.
        # Fast path: if every loaded pair is numeric (the normal case),
        # skip the np.where filter entirely — that avoids allocating an
        # intermediate ~90 MB int64 index array we don't actually need.
        n_valid = int(all_keys_valid[:offset].sum())
        if n_valid == offset:
            valid_keys = all_keys[:offset]
            valid_rows = np.arange(offset, dtype=np.int32)
            del all_keys_valid
        else:
            valid_idx = np.where(all_keys_valid[:offset])[0]
            valid_keys = all_keys[valid_idx]
            valid_rows = valid_idx.astype(np.int32)
            del all_keys_valid, valid_idx
        del all_keys
        gc.collect()
        order = np.argsort(valid_keys, kind="mergesort")
        ledger._sorted_keys = valid_keys[order]
        ledger._sorted_rows = valid_rows[order]
        del valid_keys, valid_rows, order
        gc.collect()
        return ledger


def compute_stats(values):
    """
    Given an iterable of daily correlation values (length <= WINDOW_DAYS),
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
