# M2T — Meter-to-Transformer Mapping Model

The M2T model identifies which secondary distribution transformer each
single-phase electric meter is connected to, by correlating the meters'
voltage signatures over time. It is built to validate and improve the
GIS-side meter↔transformer assignments by surfacing meters whose voltage
behavior is inconsistent with their currently-assigned transformer.

The model is designed to stand alone as a peer to GIS, not as a downstream
consumer of it. The known_mapping (built from the GIS ServicePoints file)
is treated as one source of truth; the model is treated as another. When
they disagree, the disagreement itself is the signal worth investigating.

---

## Quick start (weekly run)

```powershell
cd <project-root>

# 1. Pull new daily parquet files and refresh GIS data (optional)
# Drop new ServicePoints*.csv or Transformers*.csv into GIS_mapping/ if you got one.

# 2. Slide the rolling window forward one day at a time
python rolling_update.py "data\raw\daily\<YYYY-MM-DD>.parquet"
# ... repeat per new parquet day ...

# 3. Refresh the GIS-derived truth file (if you added a new ServicePoints CSV)
python build_known_mapping.py

# 4. Run the model
python rerun_clustering.py
python find_mapping_errors.py
python evaluate_results.py
python rank_corrections.py

# 5. Persist this run and produce enriched / stability-scored outputs
python record_run.py <YYYY-MM-DD>_<label>
python stability_report.py
python enrich_outputs.py

# 6. Compare against a prior baseline
python compare_runs.py "data\<some_baseline_folder>"
```

The first time you set up the model from scratch (no signature store
yet), run `python build_signature_store.py` once with at least
`WINDOW_DAYS` (currently 60) daily parquets in `data\raw\daily\`.

---

## Pipeline architecture

1. **Extract** — `save_daily_data.py` runs the SQL in `db_oracle.py`
   against C2M for each hour of each requested day, writes one parquet
   per day to `data\raw\daily\`. Each row is one meter's 15-minute
   voltage measurement converted to per-unit (PU) using the nominal
   voltage implied by the meter's device type.

2. **Build / roll the signature store** — `build_signature_store.py`
   assembles a memmapped float32 matrix of shape
   (n_badges × WINDOW_DAYS × INTERVALS_PER_DAY). Each row is one
   meter's per-unit voltage signature across the rolling window
   (currently 5,760 points = 60 days × 96 15-minute slots).
   `rolling_update.py` slides the window forward by one day at a time:
   drops the oldest day, appends the newest.

3. **Spatial filter** — `spatial_neighbors.py` builds a haversine
   BallTree and, for each meter, returns the indices of all other
   meters within `RADIUS_METERS` (default 175 m). This is purely a
   candidate filter; the radius does not enter the correlation math.

4. **Correlation** — `correlation_engine.py` runs pairwise Pearson
   correlation on the full 5,760-point signatures of candidate pairs.
   Requires at least `MIN_OVERLAP_POINTS` non-NaN overlapping samples
   (default 4,000 ≈ 70% data presence). Pearson is scale- and
   offset-invariant, so what's being matched is the *shape* of the
   load signature, not its magnitude.

5. **Edge filtering** — `edge_builder.py` keeps only pairs with
   correlation ≥ `CORRELATION_THRESHOLD` (currently 0.96), then trims
   to each node's top `TOP_K_NEIGHBORS` (currently 5), then requires
   the relationship to be *mutual* — both meters must rank each other
   in their top-K. This is a strong noise rejector.

6. **Cluster** — `clustering_engine.py` builds an undirected graph
   from the surviving mutual edges and returns connected components.
   `rerun_clustering.py` orchestrates steps 3–6 and writes
   `data\outputs\full_clusters.csv`.

7. **Compare against GIS** — `find_mapping_errors.py` joins clusters
   with `known_mapping.csv`, finds each cluster's majority transformer
   (by mapped-peer vote), and flags any meter whose assigned
   transformer differs from the majority. `evaluate_results.py`
   reports cluster purity and transformer completeness summary
   statistics.

8. **Score and rank** — `rank_corrections.py` adds confidence_gap and
   majority_share columns. `record_run.py` appends this run to the
   stability ledger. `stability_report.py` computes peer-co-membership
   and recommendation-consistency scores across all recorded runs.
   `enrich_outputs.py` adds GIS context columns (address, feeder,
   transformer location, distances, mismatch classification) and
   builds supplementary reports.

---

## Output files (priority order)

### Primary outputs — these are the files you actually use

| File | Rows (typical) | What it is | Use it for |
|---|---:|---|---|
| **`corrections_high_confidence_enriched.csv`** | ~1,800 | Recommendations passing all four gates: cluster confidence_gap ≥ 0.5, mapped peers ≥ 4, peer stability ≥ 0.7, recommendation stability ≥ 0.7 | **The field-team work list.** Sort by `RECOMMENDATION_TYPE`. |
| `corrections_with_stability_enriched.csv` | ~12,000 | Every flagged correction + stability scores + GIS context | Lower-bar review and deeper audits |
| `badges_missing_from_gis.csv` | ~125 | Badges in your model with no GIS row | Push to GIS team — gaps to add |
| `latlon_discrepancies.csv` | ~1,300 | Badges where model lat/lon differs from GIS by >100 m | Push to GIS team — data quality candidates |
| `full_clusters_enriched.csv` | ~218,000 | Every badge with cluster ID + GIS context (address, X/Y, feeder, GIS transformer) | Downstream mapping / analytics joins |

### Reference outputs

| File | What it is |
|---|---|
| `full_clusters.csv` | Bare badge → cluster ID assignments (source of truth for cluster membership) |
| `known_mapping.csv` | Generated from latest ServicePoints; the truth file the model compares against |
| `cluster_to_transformer_counts.csv` | Per cluster: distinct transformer count. 1 = clean cluster. |
| `transformer_to_cluster_counts.csv` | Per transformer: cluster count it spans. 1 = clean transformer. |
| `badge_stability_summary.csv` | Per-badge stability metrics (useful for dashboards) |
| `excluded_missing_coords.csv` | Badges excluded for missing lat/lon — should be empty in healthy runs |

### Intermediate outputs (rarely opened directly)

| File | What it is |
|---|---|
| `transformer_corrections.csv` | Raw model recommendations before suppression/enrichment |
| `corrections_ranked.csv` | Raw recommendations sorted by confidence |
| `corrections_with_stability.csv` | Recommendations + stability columns, no GIS context |
| `corrections_high_confidence.csv` | Pre-enrichment subset |
| `transformer_corrections_enriched.csv`, `corrections_ranked_enriched.csv` | Enriched but pre-stability (use `corrections_high_confidence_enriched.csv` instead) |
| `corrections_suppressed.csv` | Recommendations suppressed via `data\state\corrections_ignored.csv` |

### Comparison run subfolders (`comparison_<timestamp>\`)

Created by `compare_runs.py`:
- `summary.txt` — printed report
- `badge_cluster_changes.csv`, `old_clusters_that_split.csv`, `new_clusters_that_merged.csv`
- `transformer_corrections_diff.csv`

---

## RECOMMENDATION_TYPE values

Each enriched correction carries one of four labels reflecting how to
interpret it. Sort priority work by this column first.

| Value | Meaning | How to triage |
|---|---|---|
| `cross_feeder_likely_gis_error` | Current and recommended transformers are on different feeders | **High signal.** Voltage signatures shouldn't cross feeders, so the parsimonious read is that GIS has the badge on the wrong feeder/transformer. |
| `new_assignment` | Badge has no current transformer in GIS | **Easy win.** Push these to the GIS team as first-time assignment candidates. |
| `same_feeder_ambiguous` | Both transformers on the same feeder | **Lower signal.** Same-feeder meters share upstream voltage; signatures can match across genuinely-different transformers. Combine with high stability AND field validation before acting. |
| `unknown_feeder` | Feeder data missing for one or both transformers | Flag the underlying transformer to GIS to fill in feeder data; re-evaluate next run. |

### About the model's confidence limits

Voltage-signature correlation has a fundamental ceiling on what it can
distinguish. Two transformers sharing an upstream feeder, especially
adjacent ones in residential subdivisions, can produce voltage signatures
that look identical to the model. Field validation has confirmed this in
practice. The `same_feeder_ambiguous` label is the model's way of saying
"I have a recommendation but I can't reliably verify it from voltage
alone."

---

## Config knobs (`config.py`)

| Parameter | Current value | Effect |
|---|---:|---|
| `WINDOW_DAYS` | 60 | Length of the rolling signature window in days |
| `INTERVALS_PER_DAY` | 96 | 15-minute intervals per day |
| `SIGNATURE_LENGTH` | 5,760 | `WINDOW_DAYS × INTERVALS_PER_DAY` |
| `RADIUS_METERS` | 175 | Geographic candidate filter; not part of correlation math |
| `CORRELATION_THRESHOLD` | 0.96 | Minimum Pearson r for an edge to form |
| `MIN_OVERLAP_POINTS` | 4,000 | Minimum non-NaN overlapping samples to compute correlation |
| `TOP_K_NEIGHBORS` | 5 | Maximum candidate edges per node before the mutual filter |
| `EARTH_RADIUS_M` | 6,371,000 | Used in haversine distance calculation |
| `MAX_RETRIES` | 3 | SQL extraction retry count |

Window-length tradeoffs (empirical):

- **30 days** — least seasonal mixing; tightest threshold-sensitivity
- **45 days** — middle ground; modest improvement on transformer completeness
- **60 days** (current) — more statistical power per pair but spans winter→spring transitions
- **>60 days** — marginal benefit, real seasonal drift risk; not recommended

Threshold tradeoffs:

- Lower threshold (e.g. 0.93) — more edges form, fewer fragmented transformers, more bridge-error risk
- Higher threshold (e.g. 0.97) — cleaner clusters, more singletons, more fragmentation

---

## Stability ledger (`data\state\history\`)

Each run is recorded into a persistent ledger so we can score how
consistent the model is across runs:

- `runs.parquet` — one row per recorded run (RUN_ID, RUN_DATE, badge/cluster counts)
- `badge_presence.parquet` — per-badge appearance counts
- `pair_co_membership.parquet` — per-pair (BADGE_A, BADGE_B) co-cluster counts
- `recommendation_history.parquet` — per (BADGE, RECOMMENDED_TRANSFORMER) recommendation counts

`stability_report.py` reads the ledger and produces:

- **PEER_STABILITY** — for each badge's current peers, what fraction of runs have they actually been peers? High = the cluster is stable across runs.
- **RECOMMENDATION_STABILITY** — for each (badge, recommended_transformer) pair, what fraction of runs has produced this same recommendation? High = the model has been consistent in flagging this.

The first ~5 runs are when stability scores become meaningful. Weekly
cadence is recommended for accumulating runs.

---

## Suppression list (`data\state\corrections_ignored.csv`)

A user-maintained CSV with columns `BADGE, RECOMMENDED_TRANSFORMER,
DATE_ADDED, NOTES`. Any (BADGE, RECOMMENDED_TRANSFORMER) pair listed
here is suppressed from `transformer_corrections.csv` going forward
(it appears in `corrections_suppressed.csv` for audit instead). The
match is on the *pair* — if the model later recommends a different
transformer for the same badge, the new recommendation still surfaces.

---

## GIS source files (`GIS_mapping\`)

Not committed to the repo because of file size (ServicePoints is ~200 MB
and exceeds GitHub's 100 MB hard limit). Place the latest files in
`GIS_mapping\`:

- `ServicePoints*.csv` — every electric meter with badge → transformer
  mapping. The model uses BADGENUMBER, TRANSFORMERBANKOBJECTID,
  TRANSBANKTAG, CCBADDRESS1, POINT_X, POINT_Y, FEEDERID, and a few others.
- `Transformers*.csv` — transformer metadata. The model uses TAG (join
  key to `ServicePoint.TRANSBANKTAG`), STRUCTNO, FEEDERID, VAULTCD,
  POINT_X, POINT_Y, TOTALKVA.

Both `build_known_mapping.py` and `enrich_outputs.py` pick the
most-recently-modified files matching their respective glob patterns,
so when GIS sends a new file just drop it in and re-run.

---

## Database credentials

`oracle_db_connection.py` reads C2M / LG credentials from environment
variables (no secrets in source). Set them once on the work machine:

```powershell
[Environment]::SetEnvironmentVariable("C2M_USER",     "<your username>", "User")
[Environment]::SetEnvironmentVariable("C2M_PASSWORD", "<your password>", "User")
[Environment]::SetEnvironmentVariable("LG_USER",      "<your username>", "User")
[Environment]::SetEnvironmentVariable("LG_PASSWORD",  "<your password>", "User")
```

Restart PowerShell after setting them. Optional overrides:
`C2M_HOST`, `C2M_PORT`, `C2M_SERVICE`, `LG_TNS_ADMIN`, `LG_DSN`.

---

## Dependencies

```
numpy
pandas
pyarrow
scikit-learn
networkx
oracledb       # only needed on the machine that runs save_daily_data.py
```

Install with `pip install -r requirements.txt` (if present) or
`pip install numpy pandas pyarrow scikit-learn networkx`.

---

## Troubleshooting

**`build_signature_store.py` says "Need at least 60 daily files"** —
You need at least `WINDOW_DAYS` daily parquets in `data\raw\daily\`
before the initial build. Each missing day is roughly a 1.7% data gap
in the resulting signatures.

**`rolling_update.py` runs out of memory** — Check that you're on the
latest version; the file was rewritten to use memmaps end-to-end and
should peak around 1 GB of RAM per call regardless of window size.

**`compare_runs.py` shows huge churn between consecutive daily runs** —
Expected. The rolling window only shifts by ~1.7% per day. Use weekly
or longer cadence for meaningful stability signals.

**`stability_report.py` says no history found** — Run `record_run.py`
at least once first. The ledger needs to exist before it can be read.
