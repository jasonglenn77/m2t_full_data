# streaming_update.py
"""
Streaming per-day update for the v2 pair ledger.

Applies today's pair correlations to the on-disk Parquet ledger by
streaming through it in chunks: read a chunk, update matched rows in
place, write to a new ledger. Never loads the full ~12M-pair ledger
into memory.

Peak memory: ~500 MB regardless of ledger size. This lets v2 run on a
memory-constrained machine alongside other applications instead of
needing a dedicated ~3 GB load like the in-memory PairLedger update
path.

Trade-off: each day pays a full sequential re-read of the ledger from
disk (~1-2 min). Acceptable for weekly cadence where a 7-day batch is
under 15 min of I/O.
"""

import gc
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from config import WINDOW_DAYS
from pair_accumulator import LEDGER_DIR, LEDGER_PATH, LEDGER_SCHEMA

# How many ledger rows to read from disk at once during streaming.
# 100K keeps per-batch working set small (~15 MB of Python objects).
STREAM_CHUNK_SIZE = 100_000

# How many new pairs to buffer per write batch at end-of-stream.
NEW_PAIR_CHUNK_SIZE = 50_000


def _try_pack_key(a_str, b_str):
    """Return the int64 packed key for a canonical pair, or None if
    either badge isn't numeric (numeric-only allows vectorized lookup
    via np.searchsorted; non-numeric falls back to a Python dict)."""
    try:
        a_int = int(a_str)
        b_int = int(b_str)
    except (TypeError, ValueError):
        return None
    if a_int > b_int:
        a_int, b_int = b_int, a_int
    return (a_int << 32) | b_int


def get_most_recent_day(ledger_path=None):
    """Scan the LAST_DAY column of the on-disk ledger to find the max
    day. Returns None if the ledger doesn't exist or is empty. Used by
    the backfill --resume path so we don't have to load the whole
    ledger just to find the resume point."""
    path = ledger_path or LEDGER_PATH
    if not os.path.exists(path):
        return None
    reader = pq.ParquetFile(path)
    max_day = None
    for batch in reader.iter_batches(batch_size=500_000, columns=["LAST_DAY"]):
        for d in batch.column("LAST_DAY").to_pylist():
            if d and (max_day is None or d > max_day):
                max_day = d
    return max_day


def _build_lookup(pair_updates):
    """Turn today's list of (badge_a, badge_b, r) updates into a
    sorted-int64-key lookup for vectorized matching during streaming.

    Returns (sorted_keys, sorted_rs, sorted_orig_idx, non_numeric).
    non_numeric is a small fallback dict for pairs where either badge
    can't be parsed as an int; in the normal case it's empty.
    """
    packed_keys = []
    packed_rs = []
    packed_orig_idx = []
    non_numeric = {}

    for i, (a, b, r) in enumerate(pair_updates):
        key = _try_pack_key(a, b)
        if key is None:
            if a > b:
                a, b = b, a
            non_numeric[(a, b)] = (float(r), i)
        else:
            packed_keys.append(key)
            packed_rs.append(float(r))
            packed_orig_idx.append(i)

    if packed_keys:
        keys_arr = np.array(packed_keys, dtype=np.int64)
        rs_arr = np.array(packed_rs, dtype=np.float32)
        orig_arr = np.array(packed_orig_idx, dtype=np.int32)
        del packed_keys, packed_rs, packed_orig_idx
        order = np.argsort(keys_arr, kind="mergesort")
        sorted_keys = keys_arr[order]
        sorted_rs = rs_arr[order]
        sorted_orig = orig_arr[order]
        del keys_arr, rs_arr, orig_arr, order
        gc.collect()
    else:
        sorted_keys = np.empty(0, dtype=np.int64)
        sorted_rs = np.empty(0, dtype=np.float32)
        sorted_orig = np.empty(0, dtype=np.int32)

    return sorted_keys, sorted_rs, sorted_orig, non_numeric


def _write_new_pairs(writer, pair_updates, indices, day_label):
    """Append rows for previously-unseen pairs (buffered in chunks)."""
    if len(indices) == 0:
        return
    for start in range(0, len(indices), NEW_PAIR_CHUNK_SIZE):
        end = min(start + NEW_PAIR_CHUNK_SIZE, len(indices))
        sub = indices[start:end]
        badges_a = []
        badges_b = []
        rs = []
        for i in sub:
            a, b, r = pair_updates[int(i)]
            if a > b:
                a, b = b, a
            badges_a.append(a)
            badges_b.append(b)
            rs.append(float(r))
        chunk = {
            "BADGE_A": badges_a,
            "BADGE_B": badges_b,
            "VALUES": [[r] for r in rs],
            "HISTORICAL_N": [1] * len(sub),
            "HISTORICAL_MEAN": rs,
            "FIRST_DAY": [day_label] * len(sub),
            "LAST_DAY": [day_label] * len(sub),
        }
        writer.write_batch(
            pa.RecordBatch.from_pydict(chunk, schema=LEDGER_SCHEMA)
        )


def _write_fresh_ledger(pair_updates, day_label):
    """Write a fresh ledger from scratch (no existing ledger to merge)."""
    os.makedirs(LEDGER_DIR, exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with pq.ParquetWriter(tmp, LEDGER_SCHEMA) as writer:
        if pair_updates:
            _write_new_pairs(
                writer,
                pair_updates,
                np.arange(len(pair_updates), dtype=np.int32),
                day_label,
            )
        else:
            empty = {col.name: [] for col in LEDGER_SCHEMA}
            writer.write_batch(
                pa.RecordBatch.from_pydict(empty, schema=LEDGER_SCHEMA)
            )
    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)
    os.rename(tmp, LEDGER_PATH)


def stream_update_ledger(day_label, pair_updates):
    """
    Update the on-disk pair ledger with today's correlations.

    Streams the existing ledger chunk by chunk, applying updates in
    place for matched rows and passing unchanged rows through. Pairs
    not in the existing ledger are appended at the end.

    Idempotent: rows whose LAST_DAY >= day_label are left alone (the
    day was already applied in a prior run). Peak memory ~500 MB.
    """
    if not os.path.exists(LEDGER_PATH):
        print(f"    No existing ledger; writing fresh ({len(pair_updates):,} pairs)")
        _write_fresh_ledger(pair_updates, day_label)
        return

    sorted_keys, sorted_rs, sorted_orig, non_numeric = _build_lookup(pair_updates)
    n_pairs = len(pair_updates)
    matched = np.zeros(n_pairs, dtype=bool)

    n_updated = 0
    n_skipped = 0
    n_new = 0

    tmp = LEDGER_PATH + ".tmp"
    # Clean up leftover .tmp from a prior crashed run so the writer
    # can start fresh.
    if os.path.exists(tmp):
        os.remove(tmp)

    # Open the ledger file with an explicit handle we control. Passing
    # a filesystem path to pq.ParquetFile leaves handle-release timing
    # up to pyarrow's internals — on Windows that lets the underlying
    # OS handle outlive the reader object, and os.remove() below then
    # fails with PermissionError. The `with` block guarantees closure
    # before we swap.
    rows_processed = 0
    rows_last_log = 0

    with open(LEDGER_PATH, "rb") as ledger_fh, \
            pq.ParquetWriter(tmp, LEDGER_SCHEMA) as writer:
        reader = pq.ParquetFile(ledger_fh)
        total_rows = reader.metadata.num_rows
        log_every = max(500_000, total_rows // 20)

        for batch in reader.iter_batches(batch_size=STREAM_CHUNK_SIZE):
            n_batch = batch.num_rows
            badge_a_batch = batch.column("BADGE_A").to_pylist()
            badge_b_batch = batch.column("BADGE_B").to_pylist()
            values_batch = batch.column("VALUES").to_pylist()
            hist_n_batch = batch.column("HISTORICAL_N").to_numpy().copy()
            hist_mean_batch = batch.column("HISTORICAL_MEAN").to_numpy().copy()
            first_day_batch = batch.column("FIRST_DAY").to_pylist()
            last_day_batch = batch.column("LAST_DAY").to_pylist()

            batch_keys = np.zeros(n_batch, dtype=np.int64)
            batch_key_valid = np.zeros(n_batch, dtype=bool)
            for i in range(n_batch):
                key = _try_pack_key(badge_a_batch[i], badge_b_batch[i])
                if key is not None:
                    batch_keys[i] = key
                    batch_key_valid[i] = True

            if sorted_keys.size > 0:
                idx = np.searchsorted(sorted_keys, batch_keys)
                idx_bounded = np.minimum(idx, sorted_keys.size - 1)
                hit = batch_key_valid & (sorted_keys[idx_bounded] == batch_keys)
                hit_local = np.where(hit)[0]
                hit_positions = idx_bounded[hit_local]
                hit_rs = sorted_rs[hit_positions]
                hit_orig = sorted_orig[hit_positions]
                matched[hit_orig] = True

                for local_i, r in zip(hit_local, hit_rs):
                    local_i = int(local_i)
                    if last_day_batch[local_i] >= day_label:
                        n_skipped += 1
                        continue
                    values = values_batch[local_i]
                    values.append(float(r))
                    if len(values) > WINDOW_DAYS:
                        values = values[-WINDOW_DAYS:]
                    values_batch[local_i] = values
                    hist_n_batch[local_i] += 1
                    n_hist = int(hist_n_batch[local_i])
                    hist_mean_batch[local_i] += (
                        float(r) - hist_mean_batch[local_i]
                    ) / n_hist
                    last_day_batch[local_i] = day_label
                    n_updated += 1

            if non_numeric:
                for local_i in np.where(~batch_key_valid)[0]:
                    local_i = int(local_i)
                    a = badge_a_batch[local_i]
                    b = badge_b_batch[local_i]
                    key = (a, b) if a < b else (b, a)
                    if key in non_numeric:
                        r, orig_idx = non_numeric[key]
                        matched[orig_idx] = True
                        if last_day_batch[local_i] >= day_label:
                            n_skipped += 1
                            continue
                        values = values_batch[local_i]
                        values.append(r)
                        if len(values) > WINDOW_DAYS:
                            values = values[-WINDOW_DAYS:]
                        values_batch[local_i] = values
                        hist_n_batch[local_i] += 1
                        n_hist = int(hist_n_batch[local_i])
                        hist_mean_batch[local_i] += (
                            r - hist_mean_batch[local_i]
                        ) / n_hist
                        last_day_batch[local_i] = day_label
                        n_updated += 1

            chunk = {
                "BADGE_A": badge_a_batch,
                "BADGE_B": badge_b_batch,
                "VALUES": values_batch,
                "HISTORICAL_N": hist_n_batch.tolist(),
                "HISTORICAL_MEAN": hist_mean_batch.tolist(),
                "FIRST_DAY": first_day_batch,
                "LAST_DAY": last_day_batch,
            }
            writer.write_batch(
                pa.RecordBatch.from_pydict(chunk, schema=LEDGER_SCHEMA)
            )

            rows_processed += n_batch
            if rows_processed - rows_last_log >= log_every:
                print(
                    f"    Streamed {rows_processed:,}/{total_rows:,} ledger rows"
                )
                rows_last_log = rows_processed

            del badge_a_batch, badge_b_batch, values_batch
            del hist_n_batch, hist_mean_batch
            del first_day_batch, last_day_batch
            del batch_keys, batch_key_valid
            gc.collect()

        new_indices = np.where(~matched)[0]
        n_new = int(new_indices.size)
        if n_new > 0:
            _write_new_pairs(writer, pair_updates, new_indices, day_label)

    del sorted_keys, sorted_rs, sorted_orig, non_numeric, matched
    # Extra paranoia — ledger_fh is closed by the `with` above, but drop
    # the reader reference too and force a collect before the swap.
    reader = None
    gc.collect()

    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)
    os.rename(tmp, LEDGER_PATH)

    print(
        f"    Updated {n_updated:,} existing pairs, added {n_new:,} new pairs, "
        f"skipped {n_skipped:,} already-applied"
    )
