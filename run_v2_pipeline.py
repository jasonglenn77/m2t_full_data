# run_v2_pipeline.py
"""
End-to-end v2 model pipeline orchestrator.

Runs each step as a separate Python subprocess for clean memory
isolation between stages (the clustering step in particular benefits
from freeing the full ledger before the next stage starts).

Stages currently wired up:
    1. cluster_from_accumulator.py        -> data/outputs_v2/full_clusters.csv
    2. find_mapping_errors_v2.py          -> data/outputs_v2/transformer_corrections.csv
    3. evaluate_results_v2.py             -> data/outputs_v2/*_counts.csv
    4. rank_corrections_v2.py             -> data/outputs_v2/corrections_ranked.csv

Future stages (Phase 2b-2):
    5. record_run_v2.py                   -> data/state/history_v2/...
    6. stability_report_v2.py             -> data/outputs_v2/corrections_with_stability.csv
    7. enrich_outputs_v2.py               -> data/outputs_v2/*_enriched.csv
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

CORE_STEPS = [
    "cluster_from_accumulator.py",
    "find_mapping_errors_v2.py",
    "evaluate_results_v2.py",
    "rank_corrections_v2.py",
]

OPTIONAL_STEPS_PHASE_2B2 = [
    # (script, requires_args_callable)
    ("record_run_v2.py", True),
    ("stability_report_v2.py", False),
    ("enrich_outputs_v2.py", False),
]


def run_step(script, *args):
    print()
    print("=" * 70)
    print(f"Running: {script} {' '.join(args)}".rstrip())
    print("=" * 70)
    cmd = [sys.executable, script] + list(args)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"\nERROR: {script} exited with code {result.returncode}")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="Run the v2 pipeline end-to-end."
    )
    parser.add_argument(
        "--skip-cluster",
        action="store_true",
        help="Skip cluster_from_accumulator.py (use existing full_clusters.csv).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Explicit run_id for record_run_v2 (default: v2_<UTC timestamp>).",
    )
    args = parser.parse_args()

    run_id = args.run_id or datetime.now(timezone.utc).strftime(
        "v2_%Y-%m-%d_%H%M%S"
    )

    if not args.skip_cluster:
        run_step("cluster_from_accumulator.py")
    else:
        print("Skipping cluster_from_accumulator.py per --skip-cluster")

    run_step("find_mapping_errors_v2.py")
    run_step("evaluate_results_v2.py")
    run_step("rank_corrections_v2.py")

    # Phase 2b-2 stages: run only if the scripts exist in the repo
    for script, needs_run_id in OPTIONAL_STEPS_PHASE_2B2:
        if not os.path.exists(script):
            print()
            print(f"Skipping {script} (not yet in repo — Phase 2b-2)")
            continue
        if needs_run_id:
            run_step(script, run_id)
        else:
            run_step(script)

    print()
    print("=" * 70)
    print("v2 pipeline complete.")
    print(f"  Outputs in: data/outputs_v2/")
    print(f"  Run ID:     {run_id}")
    print("=" * 70)


if __name__ == "__main__":
    main()
