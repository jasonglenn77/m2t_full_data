# stability_report.py
"""
Compute per-badge and per-recommendation stability metrics from the
historical run ledger, then produce three artifacts:

    data/outputs/corrections_with_stability.csv
        All current flagged corrections, enriched with PEER_STABILITY
        and RECOMMENDATION_STABILITY columns.

    data/outputs/corrections_high_confidence.csv
        Subset that meets all of:
          CONFIDENCE_GAP >= 0.5
          MAPPED_PEERS >= 4
          PEER_STABILITY >= 0.7
          RECOMMENDATION_STABILITY >= 0.7

    data/outputs/badge_stability_summary.csv
        Per-badge stability summary for every meter in the current clusters
        (useful for dashboards, not just for the corrections list).

The stability scores are only meaningful once several runs are recorded; expect
mostly NaN scores until ~5 runs accumulate.
"""

import os

import pandas as pd

CLUSTERS = "data/outputs/full_clusters.csv"
CORRECTIONS = "data/outputs/transformer_corrections.csv"
RANKED = "data/outputs/corrections_ranked.csv"

HISTORY_DIR = "data/state/history"
RUNS_FILE = os.path.join(HISTORY_DIR, "runs.parquet")
BADGE_PRESENCE_FILE = os.path.join(HISTORY_DIR, "badge_presence.parquet")
PAIR_COMEM_FILE = os.path.join(HISTORY_DIR, "pair_co_membership.parquet")
REC_HISTORY_FILE = os.path.join(HISTORY_DIR, "recommendation_history.parquet")

OUT_STABILITY = "data/outputs/corrections_with_stability.csv"
OUT_HIGH_CONF = "data/outputs/corrections_high_confidence.csv"
OUT_BADGE_STABILITY = "data/outputs/badge_stability_summary.csv"


def normalize_id(series):
    return (
        series.astype(str)
        .str.replace(r"\.0+$", "", regex=True)
        .replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
    )


def main():
    if not os.path.exists(RUNS_FILE):
        raise SystemExit(
            "No history ledger found at data/state/history/. "
            "Run `python record_run.py` at least once first."
        )

    runs = pd.read_parquet(RUNS_FILE)
    presence = pd.read_parquet(BADGE_PRESENCE_FILE)
    pairs = (
        pd.read_parquet(PAIR_COMEM_FILE)
        if os.path.exists(PAIR_COMEM_FILE)
        else pd.DataFrame()
    )
    rec_history = (
        pd.read_parquet(REC_HISTORY_FILE)
        if os.path.exists(REC_HISTORY_FILE)
        else pd.DataFrame()
    )

    n_runs = len(runs)
    print(f"History: {n_runs} run(s) recorded.")
    if n_runs < 5:
        print(
            "  NOTE: stability scores are not very meaningful below ~5 runs. "
            "The output files will still be written, but treat the scores as preliminary."
        )

    clusters = pd.read_csv(CLUSTERS, dtype=str)
    clusters["CLUSTER"] = normalize_id(clusters["CLUSTER"])

    # Pick richest candidate source: ranked > plain corrections
    if os.path.exists(RANKED):
        candidates = pd.read_csv(RANKED, dtype=str)
        source = RANKED
    elif os.path.exists(CORRECTIONS):
        candidates = pd.read_csv(CORRECTIONS, dtype=str)
        source = CORRECTIONS
    else:
        raise SystemExit("No corrections file found in data/outputs/.")

    print(f"Source for corrections: {source} ({len(candidates):,} rows)")

    candidates["RECOMMENDED_TRANSFORMER"] = normalize_id(
        candidates["RECOMMENDED_TRANSFORMER"]
    )
    if "CONFIDENCE_GAP" in candidates.columns:
        candidates["CONFIDENCE_GAP"] = pd.to_numeric(
            candidates["CONFIDENCE_GAP"], errors="coerce"
        )
    if "MAPPED_PEERS" in candidates.columns:
        candidates["MAPPED_PEERS"] = pd.to_numeric(
            candidates["MAPPED_PEERS"], errors="coerce"
        ).fillna(0).astype(int)

    # ----- Peer stability per badge -----
    print("Computing peer stability ...")

    presence_dict = dict(zip(presence["BADGE"], presence["N_RUNS_SEEN"].astype(int)))

    pairs_dict = {}
    if not pairs.empty:
        pairs["TIMES_TOGETHER"] = pairs["TIMES_TOGETHER"].astype(int)
        for a, b, t in zip(pairs["BADGE_A"], pairs["BADGE_B"], pairs["TIMES_TOGETHER"]):
            pairs_dict[(a, b)] = t

    badge_peer_stability = {}
    for cid, group in clusters.groupby("CLUSTER"):
        members = group["BADGE"].tolist()
        if len(members) < 2:
            for m in members:
                badge_peer_stability[m] = (float("nan"), 0)
            continue
        for m in members:
            rates = []
            m_runs = presence_dict.get(m, n_runs)
            for peer in members:
                if peer == m:
                    continue
                a, b = (m, peer) if m < peer else (peer, m)
                times = pairs_dict.get((a, b), 0)
                denom = min(m_runs, presence_dict.get(peer, n_runs))
                if denom > 0:
                    rates.append(times / denom)
            if rates:
                badge_peer_stability[m] = (sum(rates) / len(rates), len(rates))
            else:
                badge_peer_stability[m] = (float("nan"), 0)

    candidates["PEER_STABILITY"] = candidates["BADGE"].map(
        lambda b: badge_peer_stability.get(b, (float("nan"), 0))[0]
    )
    candidates["PEERS_CONSIDERED"] = candidates["BADGE"].map(
        lambda b: badge_peer_stability.get(b, (float("nan"), 0))[1]
    )

    # ----- Recommendation stability per (badge, transformer) -----
    print("Computing recommendation stability ...")
    if not rec_history.empty:
        rec_history["TIMES_RECOMMENDED"] = rec_history["TIMES_RECOMMENDED"].astype(int)
        rec_history["RECOMMENDED_TRANSFORMER"] = normalize_id(
            rec_history["RECOMMENDED_TRANSFORMER"]
        )
        candidates = candidates.merge(
            rec_history[["BADGE", "RECOMMENDED_TRANSFORMER", "TIMES_RECOMMENDED"]],
            on=["BADGE", "RECOMMENDED_TRANSFORMER"],
            how="left",
        )
        candidates["TIMES_RECOMMENDED"] = (
            candidates["TIMES_RECOMMENDED"].fillna(0).astype(int)
        )
        candidates["BADGE_N_RUNS"] = (
            candidates["BADGE"].map(presence_dict).fillna(n_runs).astype(int)
        )
        candidates["RECOMMENDATION_STABILITY"] = (
            candidates["TIMES_RECOMMENDED"]
            / candidates["BADGE_N_RUNS"].replace(0, pd.NA)
        )
    else:
        candidates["TIMES_RECOMMENDED"] = 0
        candidates["BADGE_N_RUNS"] = 1
        candidates["RECOMMENDATION_STABILITY"] = float("nan")

    # ----- Write stability-enriched corrections -----
    for c in ["PEER_STABILITY", "RECOMMENDATION_STABILITY"]:
        candidates[c] = pd.to_numeric(candidates[c], errors="coerce").round(3)

    preferred_order = [
        "BADGE",
        "CURRENT_TRANSFORMER",
        "RECOMMENDED_TRANSFORMER",
        "CLUSTER",
        "CLUSTER_SIZE",
        "MAPPED_PEERS",
        "CURRENT_VOTES",
        "MAJORITY_VOTES",
        "MAJORITY_SHARE",
        "CONFIDENCE_GAP",
        "PEER_STABILITY",
        "PEERS_CONSIDERED",
        "TIMES_RECOMMENDED",
        "BADGE_N_RUNS",
        "RECOMMENDATION_STABILITY",
    ]
    cols = [c for c in preferred_order if c in candidates.columns]
    candidates = candidates[cols].sort_values(
        ["RECOMMENDATION_STABILITY", "PEER_STABILITY"], ascending=[False, False]
    )
    candidates.to_csv(OUT_STABILITY, index=False)
    print(f"Wrote {OUT_STABILITY}: {len(candidates):,} rows")

    # ----- High-confidence filter -----
    filt = (
        (candidates["PEER_STABILITY"] >= 0.7)
        & (candidates["RECOMMENDATION_STABILITY"] >= 0.7)
    )
    if "CONFIDENCE_GAP" in candidates.columns:
        filt = filt & (candidates["CONFIDENCE_GAP"] >= 0.5)
    if "MAPPED_PEERS" in candidates.columns:
        filt = filt & (candidates["MAPPED_PEERS"] >= 4)

    high_conf = candidates[filt].copy()
    high_conf.to_csv(OUT_HIGH_CONF, index=False)
    print(f"Wrote {OUT_HIGH_CONF}: {len(high_conf):,} high-confidence corrections")

    if n_runs < 5:
        print(
            "  (this list will fill in as more runs are recorded; "
            "expect it to be very short or empty for now)"
        )

    # ----- Per-badge stability summary -----
    print("Writing per-badge stability summary ...")
    bs_rows = []
    for b, (ps, n_peers) in badge_peer_stability.items():
        bs_rows.append(
            {
                "BADGE": b,
                "PEER_STABILITY": round(ps, 3) if pd.notna(ps) else float("nan"),
                "PEERS_CONSIDERED": n_peers,
                "N_RUNS_SEEN": presence_dict.get(b, 0),
            }
        )
    bs = pd.DataFrame(bs_rows)
    bs.to_csv(OUT_BADGE_STABILITY, index=False)
    print(f"Wrote {OUT_BADGE_STABILITY}: {len(bs):,} badges")


if __name__ == "__main__":
    main()
