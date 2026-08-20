# M2T Runbook — running the model on the work computer

Everything needed to catch the model up and run it weekly. Written for the
work machine (`C:\Users\jsglenn\OneDrive - Colorado Springs Utilities\Apps\Python\New_M2T_260310`),
which is the only machine that can reach the C2M database.

- [Part 0 — Getting git unstuck](#part-0--getting-git-unstuck)
- [Part A — Loading the latest GIS data](#part-a--loading-the-latest-gis-data)
- [Part B — Running the model](#part-b--running-the-model)
- [Ongoing weekly routine](#ongoing-weekly-routine)
- [Gotchas worth remembering](#gotchas-worth-remembering)
- [Customer deliverables](#customer-deliverables)

---

## Part 0 — Getting git unstuck

Symptoms: a "Merge branch 'master' of ..." message with a **Continue**
button, one outgoing commit, one incoming commit, and dozens of modified
files under `data\`.

That is a `git pull` that started a merge and never finished. Nothing is
lost — the merge is just waiting to be committed.

```powershell
cd "C:\Users\jsglenn\OneDrive - Colorado Springs Utilities\Apps\Python\New_M2T_260310"

# 1. Back up the two files that hold work-machine-specific settings.
#    These are the only local edits that are not regenerable.
New-Item -ItemType Directory -Force "$env:USERPROFILE\Desktop\m2t_backup" | Out-Null
Copy-Item oracle_db_connection.py, save_daily_data.py "$env:USERPROFILE\Desktop\m2t_backup\" -Force

# 2. See exactly where you are
git status
git log --oneline -3

# 3. Finish the merge that is already in progress
git commit --no-edit

# 4. Now pull normally
git pull

# 5. Confirm the three new scripts arrived
Get-ChildItem check_state.py, run_weeks.ps1, load_archive.py
```

The many modified files under `data\outputs*` and `data\state\history*` are
model results that get regenerated on every run. They are safe to leave
dirty, commit, or discard — they will not block the pull.

**Never run `git add -A` / "Stage All Changes" until you have pulled the
`.gitignore` fix that ignores `pair_ledger.parquet`.** That file is ~3 GB,
which is far past GitHub's 100 MB hard limit; staging it would break the
push and permanently bloat the repo. After the pull it is ignored and
staging everything is safe.

If you want the work machine's own commits on GitHub as well:

```powershell
git push origin master
```

That is optional — nothing in Parts A or B depends on it.

---

## Part A — Loading the latest GIS data

**Code edits required: none.** Both scripts that read GIS pick the newest
file automatically. This function is identical in `build_known_mapping.py`
and `enrich_outputs_v2.py`:

```python
def find_latest(pattern):
    files = glob.glob(os.path.join(GIS_DIR, pattern))
    ...
    return max(files, key=os.path.getmtime)
```

**Where:** `GIS_mapping\` in the project root.

**What to name it:** anything matching `ServicePoints*.csv` and
`Transformers*.csv`. Match the existing convention — e.g.
`ServicePoints125_08192026.csv`, `Transformers125_08192026.csv`.

### Three things that matter

1. **Copy both files together.** `enrich_outputs_v2.py` joins ServicePoints
   to Transformers; mismatched vintages cause silent join misses on feeder
   and location columns.

2. **Do not delete the old GIS files.** `enrich_outputs_v2.py` falls back to
   *older* files as a decoder when the newest export is missing columns
   (`d_FEEDERID`, `d_SUBTYPECD`, `STRUCTNO`, `TAG`). The 04282026 files are
   load-bearing — that is why the July export is 97 MB against April's
   194 MB.

3. **Selection is by modified time, not the date in the filename.** A normal
   download sets the timestamp to now, so this is usually automatic. But
   copying in a way that preserves timestamps (robocopy, unzip, restore from
   a share) can leave an older file winning.

### Commands

```powershell
cd "C:\Users\jsglenn\OneDrive - Colorado Springs Utilities\Apps\Python\New_M2T_260310"

# Drop both new CSVs into GIS_mapping\, keep the old ones, then verify.
# The arrow in Section 5 marks the file that will actually be used.
python check_state.py --skip-ledger

# Only if an OLD file is winning, bump the new ones' timestamps:
(Get-Item "GIS_mapping\ServicePoints125_08192026.csv").LastWriteTime = Get-Date
(Get-Item "GIS_mapping\Transformers125_08192026.csv").LastWriteTime = Get-Date

# Rebuild the truth file the model compares against
python build_known_mapping.py
```

Do this **before** the weekly catch-up so every weekly run is scored against
the same GIS truth and the week-over-week numbers stay comparable.

---

## Part B — Running the model

### Step 1 — Session setup

```powershell
cd "C:\Users\jsglenn\OneDrive - Colorado Springs Utilities\Apps\Python\New_M2T_260310"
git pull

# Allow .ps1 for this window only (does not change any machine setting)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# Optional: log everything to a file
Start-Transcript -Path "catchup_log.txt" -Append
```

### Step 2 — Load the latest GIS data

See [Part A](#part-a--loading-the-latest-gis-data), ending with
`python build_known_mapping.py`.

### Step 3 — Find out where you stand

```powershell
python check_state.py --detect-v1-day
```

Takes ~2 minutes, plus a few more for the v1 detection. Check three things:

| Section | What to look for |
|---|---|
| 2 — v2 ledger | Last day applied. Expect `2026-05-16`. This is the v2 resume point. |
| 4 — parquet gaps | **The one blocking decision.** See below. |
| 5 — GIS files | The arrow points at the new files. |

**If Section 4 shows missing days (e.g. 5/17–6/13):** those weeks cannot be
rebuilt later, because the ledger only holds a rolling 45-day window per
pair. Either re-extract them now, or accept fewer weekly runs.

```powershell
# Resumable; skips days already marked done in daily_extract_state.json
python save_daily_data.py
```

### Step 4 — Preview the plan

```powershell
.\run_weeks.ps1 -Through 2026-08-07 -DryRun
```

Instant, changes nothing. Confirm the week list starts the week after the
ledger date and ends on `v2_2026-08-07`.

### Step 5 — Run the catch-up

Roughly 6–13 hours for 12 weeks, single-threaded. Safe to Ctrl-C anytime:
recorded weeks are skipped on re-run and per-day ledger updates are
idempotent.

```powershell
# Chunked across evenings
.\run_weeks.ps1 -Through 2026-06-20
.\run_weeks.ps1 -Through 2026-07-11
.\run_weeks.ps1 -Through 2026-08-07

# ...or all at once overnight
.\run_weeks.ps1 -Through 2026-08-07
```

Each week does three things: catches the ledger up, runs the v2 pipeline and
records the run, then snapshots `data\outputs_v2\` into
`data\archive_v2\<week>\`. That third step is what preserves weekly history —
`data\outputs_v2\` is overwritten by every run.

Useful switches:

| Switch | Effect |
|---|---|
| `-ZipArchive` | ~11 MB per week instead of ~45 MB |
| `-NoArchive` | Skip snapshots entirely (not recommended) |
| `-From <date>` | Override the starting week |
| `-WithV1` | Also run v1 + consensus at the end |

### Step 6 — v1 and the consensus report, once, at the end

```powershell
python build_signature_store.py     # v1 window is ~3 months stale; rebuild
python rerun_clustering.py
python find_mapping_errors.py
python evaluate_results.py
python rank_corrections.py
python record_run.py 2026-08-07_weekly
python stability_report.py
python enrich_outputs.py
python consensus_report.py
```

### Step 7 — Verify

```powershell
# Prove no week was lost: archives vs recorded runs vs ledger
python load_archive.py --verify

# What is in the archive
python load_archive.py --list

# Per-week counts, type breakdown, and how many badges appear in ALL weeks
python load_archive.py --file corrections_high_confidence_enriched.csv --summary

# Confirm the two models are agreeing
python -c "import pandas as pd; print(pd.read_csv('data/outputs/consensus_report.csv')['CONSENSUS_TIER'].value_counts())"

Stop-Transcript
```

A healthy finish: `--verify` reports `OK - N consecutive weekly run(s)`, and
the tier counts show a non-trivial `both_high_confidence` number.

---

## Ongoing weekly routine

```powershell
python save_daily_data.py                      # pull the new week's parquets
python build_known_mapping.py                  # only if new GIS arrived
.\run_weeks.ps1 -Through <this Friday's date>
```

---

## Gotchas worth remembering

**`--all` is not optional.** Never run `python backfill_accumulator.py
--resume` on its own. Argument handling selects only the newest
`WINDOW_DAYS` (45) parquets *before* the resume filter is applied, so with
the ledger at 5/16 and parquets through 8/7 it would silently skip
5/17–6/23 and still report success. `run_weeks.ps1` always passes `--all`.

**Advancing the ledger is not the same as recording a run.**
`backfill_accumulator.py` moves the ledger forward; only
`run_v2_pipeline.py` appends to `data\state\history_v2\`, and that ledger is
the sole source for `PEER_STABILITY` and `RECOMMENDATION_STABILITY` — gates
3 and 4 of the high-confidence filter. One big jump to 8/7 produces one run;
week-by-week produces twelve, which is what makes those gates meaningful.

**Git does not carry the model's data.** Four things move only by
OneDrive/manual copy:

- `data\state\pair_ledger.parquet` (~3 GB)
- `data\processed\signatures\signatures.npy` (~3.5 GB, v1 only)
- `data\raw\daily\*.parquet`
- `GIS_mapping\*.csv`

Everything else — including `badge_ids.npy`, both stability ledgers, and past
output CSVs — is tracked and rides along with a pull. That last part is the
trap: a committed `comparison_<date>\` folder can make it look like the model
ran to that date on a machine where it did not.

**"Parallel" means two models, not multiple cores.** There is no
multiprocessing anywhere in the codebase. v1 (full-window signature
correlation) and v2 (per-day correlation accumulated in a pair ledger, gated
on mean / std / trend) run as independent peers with separate stability
ledgers, and `consensus_report.py` merges them into tiers.

**Nothing records where the v1 signature store ends.** `check_state.py
--detect-v1-day` finds it by matching the store's last day against the daily
parquets.

---

## Customer deliverables

After 10–15 weekly runs, in priority order:

**1. The headline** — `data\outputs\consensus_report.csv` filtered to
`CONSENSUS_TIER = both_high_confidence`. Two methodologically independent
models, each passing four gates including two cross-run stability gates,
converging on the same recommendation. Sort by `RECOMMENDATION_TYPE` and lead
with `cross_feeder_likely_gis_error`.

**2. The easy wins** — uncontroversial, build goodwill:

| File | Why it lands well |
|---|---|
| `badges_missing_from_gis.csv` | Meters GIS has no row for; pure gap-filling |
| `latlon_discrepancies.csv` | >100 m coordinate disagreements; data quality, not model opinion |
| `new_assignment` rows | Unmapped meters with a strong cluster vote |

**3. The evidence only 10–15 weeks can produce.** Run:

```powershell
python load_archive.py --file corrections_high_confidence_enriched.csv --summary
```

The number to quote is how many badges appear in **all** N weeks. "This same
recommendation was produced independently in 14 of 14 consecutive weekly
runs, by two independent models" is far stronger than any single-run
confidence score.

**4. Context docs** — `references\M2T_Executive_Update_July2026.docx`
(regenerate with `python references\build_exec_summary.py` after the run so
metrics are current) and the Technical Reference.

**What not to send as recommendations:** `same_feeder_ambiguous` rows. Field
validation has confirmed false positives there — meters on a shared feeder
produce near-identical voltage signatures regardless of transformer. In the
last run that was 1,955 of 2,160 high-confidence rows, so it dominates by
volume. Label them "field-validation candidates" in a separate tab or leave
them out; sending them as actions is the fastest way to lose credibility on
the 64 cross-feeder rows that are genuinely strong. Also skip
`full_clusters_enriched.csv` (218K rows) — internal join asset, not a
deliverable.
