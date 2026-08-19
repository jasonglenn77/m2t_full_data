# load_archive.py
"""
Read the per-week v2 output snapshots written by run_weeks.ps1.

Every weekly run overwrites data/outputs_v2/, so run_weeks.ps1 copies the
folder to data/archive_v2/<week-ending>/ after each run. This script
stacks any one of those files across all archived weeks into a single
long-format table with a WEEK_ENDING column, which is what you actually
want for trend analysis.

Usage:
    python load_archive.py --list
    python load_archive.py --file corrections_high_confidence_enriched.csv --summary
    python load_archive.py --file corrections_high_confidence_enriched.csv \
        --out data/outputs_v2/history_high_confidence.csv
    python load_archive.py --badge-trend 12345678

Reads .zip week archives too (written with run_weeks.ps1 -ZipArchive).
"""

import argparse
import glob
import io
import os
import zipfile
from datetime import date

import pandas as pd

ARCHIVE_DIR = "data/archive_v2"

# Files worth stacking, with the columns that matter most for trending.
KNOWN_FILES = {
    "corrections_high_confidence_enriched.csv": "the field-team work list",
    "corrections_with_stability_enriched.csv": "all corrections + stability",
    "full_clusters.csv": "badge -> cluster assignments",
    "full_clusters_enriched.csv": "clusters + GIS context (large)",
    "transformer_corrections.csv": "raw recommendations",
    "badge_stability_summary.csv": "per-badge stability metrics",
}


def week_sources():
    """Return [(week_ending, path_or_zip)] sorted chronologically."""
    if not os.path.isdir(ARCHIVE_DIR):
        return []
    out = []
    for entry in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "*"))):
        base = os.path.basename(entry)
        if os.path.isdir(entry):
            out.append((base, entry))
        elif entry.endswith(".zip"):
            out.append((base[:-4], entry))
    return sorted(out, key=lambda t: t[0])


def read_member(source, filename):
    """Read one CSV out of a week folder or week zip. None if absent."""
    if source.endswith(".zip"):
        with zipfile.ZipFile(source) as zf:
            names = {os.path.basename(n): n for n in zf.namelist()}
            if filename not in names:
                return None
            with zf.open(names[filename]) as fh:
                return pd.read_csv(io.BytesIO(fh.read()), dtype=str)
    path = os.path.join(source, filename)
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, dtype=str)


def read_manifest(source):
    raw = read_manifest_text(source)
    if raw is None:
        return {}
    out = {}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def read_manifest_text(source):
    if source.endswith(".zip"):
        with zipfile.ZipFile(source) as zf:
            names = {os.path.basename(n): n for n in zf.namelist()}
            if "run_manifest.txt" not in names:
                return None
            return zf.read(names["run_manifest.txt"]).decode("utf-8", "replace")
    path = os.path.join(source, "run_manifest.txt")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def cmd_list():
    sources = week_sources()
    if not sources:
        raise SystemExit(
            f"No week archives in {ARCHIVE_DIR}/. "
            "Run run_weeks.ps1 (without -NoArchive) to create them."
        )
    print(f"{len(sources)} archived week(s) in {ARCHIVE_DIR}/\n")
    print(f"{'WEEK ENDING':<14}{'FORM':<8}{'SERVICEPOINTS':<34}{'HIGH-CONF ROWS':>15}")
    print("-" * 71)
    for week, source in sources:
        man = read_manifest(source)
        form = "zip" if source.endswith(".zip") else "folder"
        hc = read_member(source, "corrections_high_confidence_enriched.csv")
        rows = f"{len(hc):,}" if hc is not None else "-"
        print(
            f"{week:<14}{form:<8}{man.get('SERVICEPOINTS', '-'):<34}{rows:>15}"
        )
    print()
    print("Files available in the newest week:")
    newest = sources[-1][1]
    if newest.endswith(".zip"):
        with zipfile.ZipFile(newest) as zf:
            names = sorted({os.path.basename(n) for n in zf.namelist()})
    else:
        names = sorted(os.listdir(newest))
    for n in names:
        note = KNOWN_FILES.get(n, "")
        print(f"  {n:<45}{note}")


def cmd_verify():
    """Cross-check the three things that have to agree if no week was lost:
    the archived snapshots, the recorded runs in the stability ledger, and
    the pair ledger's last applied day."""
    print("Verifying weekly coverage ...\n")

    archived = [w for w, _ in week_sources()]
    print(f"Archived week snapshots : {len(archived)}")

    runs_path = "data/state/history_v2/runs.parquet"
    if os.path.exists(runs_path):
        runs = pd.read_parquet(runs_path)
        run_ids = list(runs["RUN_ID"])
    else:
        run_ids = []
    weekly_runs = [r for r in run_ids if r.startswith("v2_") and len(r) == len("v2_") + 10]
    print(f"Recorded weekly runs    : {len(weekly_runs)}  (of {len(run_ids)} total v2 runs)")

    try:
        from streaming_update import get_most_recent_day

        ledger_day = get_most_recent_day()
    except Exception as exc:
        ledger_day = None
        print(f"Ledger day              : unreadable ({exc})")
    if ledger_day:
        print(f"Ledger last applied day : {ledger_day}")

    archived_set = set(archived)
    run_weeks = {r[len("v2_"):] for r in weekly_runs}

    print()
    missing_archive = sorted(run_weeks - archived_set)
    missing_runs = sorted(archived_set - run_weeks)
    problems = False

    if missing_archive:
        problems = True
        print("RECORDED BUT NOT ARCHIVED (outputs were overwritten):")
        for w in missing_archive:
            print(f"  {w}")
    if missing_runs:
        problems = True
        print("ARCHIVED BUT NOT RECORDED (week left no stability history):")
        for w in missing_runs:
            print(f"  {w}")

    # Gaps between consecutive weeks that are wider than 7 days mean a week
    # was skipped -- usually because its daily parquets were missing.
    ordered = sorted(archived_set | run_weeks)
    if len(ordered) >= 2:
        jumps = []
        for a, b in zip(ordered, ordered[1:]):
            delta = (date.fromisoformat(b) - date.fromisoformat(a)).days
            if delta > 7:
                jumps.append((a, b, delta))
        if jumps:
            problems = True
            print("GAPS WIDER THAN 7 DAYS between consecutive weeks:")
            for a, b, delta in jumps:
                print(f"  {a} -> {b}  ({delta} days)")

    if ledger_day and ordered and ordered[-1] < ledger_day:
        problems = True
        print(
            f"LEDGER IS AHEAD OF THE LAST RUN: ledger at {ledger_day}, "
            f"last weekly run {ordered[-1]}."
        )
        print("  Days were folded into the ledger without a run being recorded.")

    if not problems:
        span = f"{ordered[0]} -> {ordered[-1]}" if ordered else "(none)"
        print(f"OK - {len(ordered)} consecutive weekly run(s), {span}.")
        print("Every recorded run has a snapshot and every snapshot has a run.")


def stack(filename):
    sources = week_sources()
    if not sources:
        raise SystemExit(f"No week archives in {ARCHIVE_DIR}/.")
    frames = []
    for week, source in sources:
        df = read_member(source, filename)
        if df is None:
            print(f"  {week}: {filename} not present - skipped")
            continue
        df.insert(0, "WEEK_ENDING", week)
        frames.append(df)
        print(f"  {week}: {len(df):,} rows")
    if not frames:
        raise SystemExit(f"{filename} not found in any archived week.")
    return pd.concat(frames, ignore_index=True)


def cmd_file(filename, out_path, summary):
    print(f"Stacking {filename} across archived weeks ...")
    combined = stack(filename)
    print(f"\nCombined: {len(combined):,} rows x {len(combined.columns)} columns")

    if summary:
        print("\nRows per week:")
        for week, n in combined.groupby("WEEK_ENDING").size().items():
            print(f"  {week}  {n:>8,}")
        if "RECOMMENDATION_TYPE" in combined.columns:
            print("\nRecommendation type by week:")
            pivot = pd.crosstab(
                combined["WEEK_ENDING"], combined["RECOMMENDATION_TYPE"]
            )
            print(pivot.to_string())
        if "BADGE" in combined.columns:
            per_badge = combined.groupby("BADGE")["WEEK_ENDING"].nunique()
            n_weeks = combined["WEEK_ENDING"].nunique()
            print(
                f"\n{len(per_badge):,} distinct badges; "
                f"{int((per_badge == n_weeks).sum()):,} appear in all {n_weeks} weeks"
            )

    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        combined.to_csv(out_path, index=False)
        print(f"\nWrote {out_path}")


def cmd_badge_trend(badge):
    """Show one badge's week-by-week recommendation history."""
    filename = "corrections_with_stability_enriched.csv"
    print(f"Tracking badge {badge} across archived weeks ...\n")
    sources = week_sources()
    if not sources:
        raise SystemExit(f"No week archives in {ARCHIVE_DIR}/.")

    cols = [
        "RECOMMENDED_TRANSFORMER",
        "CURRENT_TRANSFORMER",
        "CONFIDENCE_GAP",
        "MAPPED_PEERS",
        "RECOMMENDATION_TYPE",
        "RECOMMENDATION_STABILITY",
    ]
    rows = []
    for week, source in sources:
        df = read_member(source, filename)
        if df is None or "BADGE" not in df.columns:
            continue
        hit = df[df["BADGE"].astype(str) == str(badge)]
        if hit.empty:
            rows.append({"WEEK_ENDING": week, "RECOMMENDED_TRANSFORMER": "(not flagged)"})
            continue
        rec = {"WEEK_ENDING": week}
        for c in cols:
            if c in hit.columns:
                rec[c] = hit.iloc[0][c]
        rows.append(rec)

    if not rows:
        raise SystemExit(f"{filename} not found in any archived week.")
    out = pd.DataFrame(rows)
    print(out.to_string(index=False))


def main():
    parser = argparse.ArgumentParser(
        description="Query the per-week v2 output archive."
    )
    parser.add_argument(
        "--list", action="store_true", help="List archived weeks and available files."
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check that no week was lost: archives vs recorded runs vs ledger.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Filename to stack across all archived weeks.",
    )
    parser.add_argument(
        "--out", default=None, help="Write the stacked result to this CSV."
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print per-week counts and breakdowns for --file.",
    )
    parser.add_argument(
        "--badge-trend",
        default=None,
        help="Show one badge's recommendation history across weeks.",
    )
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.verify:
        cmd_verify()
    elif args.badge_trend:
        cmd_badge_trend(args.badge_trend)
    elif args.file:
        cmd_file(args.file, args.out, args.summary)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()


