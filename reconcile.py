# reconcile.py
"""
Track what happened to every recommendation the model has delivered.

Run each Monday AFTER build_deliverable.py. Maintains a persistent ledger at
data/state/reconciliation_ledger.csv, one row per (badge, recommended
transformer) ever delivered, and closes items out automatically:

  RESOLVED_GIS_UPDATED   The current GIS extract now assigns this meter to the
                         transformer the model recommended. The field visit
                         confirmed the model and GIS was corrected. Detected by
                         reading the refreshed known_mapping.csv -- no manual
                         step needed.

  CLOSED_GIS_CONFIRMED   The field visit confirmed the GIS record was already
                         right. Recorded by mark_verified.py, which adds the
                         meter to the models' ignore lists so it stops being
                         reported.

  OPEN                   Still on the current field list.

  LAPSED                 No longer flagged, but GIS was not changed either --
                         usually sparse data or a shifted cluster. Reopens
                         automatically if it comes back.

Writes:
    data/state/reconciliation_ledger.csv        the running ledger
    data/outputs/deliverables/resolved_since_last_run.csv
    data/outputs/deliverables/reconciliation_summary.txt

Usage:
    python reconcile.py                     # uses today's date as the run label
    python reconcile.py --week 2026-08-30
"""

import argparse
import os
from datetime import date

import pandas as pd

LEDGER = "data/state/reconciliation_ledger.csv"
FIELD_LIST = "data/outputs/deliverables/field_verification_list.csv"
KNOWN_MAPPING = "data/outputs/known_mapping.csv"
OUT_DIR = "data/outputs/deliverables"

LEDGER_COLS = [
    "BADGE",
    "RECOMMENDED_TRANSFORMER",
    "GIS_AT_FIRST_DELIVERY",
    "FIELD_PRIORITY",
    "FIRST_DELIVERED",
    "LAST_DELIVERED",
    "STATUS",
    "CLOSED_WEEK",
    "NOTES",
]

OPEN = "OPEN"
RESOLVED = "RESOLVED_GIS_UPDATED"
CONFIRMED = "CLOSED_GIS_CONFIRMED"
LAPSED = "LAPSED"


def norm(series):
    return (
        series.astype(str)
        .str.strip()
        .str.replace(r"\.0+$", "", regex=True)
        .replace({"nan": "", "None": "", "<NA>": ""})
    )


def load_ledger():
    if os.path.exists(LEDGER):
        led = pd.read_csv(LEDGER, dtype=str).fillna("")
        for c in LEDGER_COLS:
            if c not in led.columns:
                led[c] = ""
        return led[LEDGER_COLS]
    return pd.DataFrame(columns=LEDGER_COLS)


def load_current_gis():
    """badge -> transformer currently assigned in GIS, from the truth file
    rebuilt each Monday from the latest ServicePoints extract."""
    if not os.path.exists(KNOWN_MAPPING):
        raise SystemExit(
            f"{KNOWN_MAPPING} not found. Run build_known_mapping.py first."
        )
    km = pd.read_csv(KNOWN_MAPPING, dtype=str)
    km = km.rename(columns={"badge": "BADGE", "transf_id": "TRANSFORMER"})
    km["BADGE"] = norm(km["BADGE"])
    km["TRANSFORMER"] = norm(km["TRANSFORMER"])
    return dict(zip(km["BADGE"], km["TRANSFORMER"]))


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile delivered recommendations against the latest GIS."
    )
    parser.add_argument(
        "--week",
        default=date.today().isoformat(),
        help="Run label, YYYY-MM-DD (default: today).",
    )
    args = parser.parse_args()
    week = args.week

    if not os.path.exists(FIELD_LIST):
        raise SystemExit(
            f"{FIELD_LIST} not found. Run build_deliverable.py first."
        )

    print(f"Reconciling as of {week}")
    ledger = load_ledger()
    gis = load_current_gis()
    print(f"  Ledger: {len(ledger):,} recommendation(s) on record")
    print(f"  GIS:    {len(gis):,} mapped meters in the current extract")

    current = pd.read_csv(FIELD_LIST, dtype=str)
    current["BADGE"] = norm(current["BADGE"])
    current["RECOMMENDED_TRANSFORMER"] = norm(current["RECOMMENDED_TRANSFORMER"])
    current_keys = set(zip(current["BADGE"], current["RECOMMENDED_TRANSFORMER"]))
    print(f"  This week's field list: {len(current):,} row(s)")

    if not ledger.empty:
        ledger["BADGE"] = norm(ledger["BADGE"])
        ledger["RECOMMENDED_TRANSFORMER"] = norm(ledger["RECOMMENDED_TRANSFORMER"])

    index = {}
    for i, r in ledger.iterrows():
        index[(r["BADGE"], r["RECOMMENDED_TRANSFORMER"])] = i

    newly_resolved = []
    n_new = n_reopened = n_lapsed = 0

    # --- 1. Close out anything GIS has since adopted -------------------
    # A meter whose GIS assignment now equals what we recommended has been
    # corrected. This is the automatic half of the loop: no one has to tell
    # the model, it just reads Monday's refreshed extract.
    for key, i in index.items():
        badge, rec_tx = key
        status = ledger.at[i, "STATUS"]
        if status in (RESOLVED, CONFIRMED):
            continue
        if rec_tx and gis.get(badge, "") == rec_tx:
            ledger.at[i, "STATUS"] = RESOLVED
            ledger.at[i, "CLOSED_WEEK"] = week
            ledger.at[i, "NOTES"] = "GIS now matches the recommendation"
            newly_resolved.append(
                {
                    "BADGE": badge,
                    "WAS_ASSIGNED": ledger.at[i, "GIS_AT_FIRST_DELIVERY"],
                    "NOW_ASSIGNED": rec_tx,
                    "FIRST_DELIVERED": ledger.at[i, "FIRST_DELIVERED"],
                    "RESOLVED_WEEK": week,
                }
            )

    # --- 2. Add or refresh everything on this week's list --------------
    rows_to_add = []
    for _, r in current.iterrows():
        key = (r["BADGE"], r["RECOMMENDED_TRANSFORMER"])
        if key in index:
            i = index[key]
            if ledger.at[i, "STATUS"] == LAPSED:
                ledger.at[i, "STATUS"] = OPEN
                ledger.at[i, "NOTES"] = "reopened - flagged again"
                n_reopened += 1
            if ledger.at[i, "STATUS"] not in (RESOLVED, CONFIRMED):
                ledger.at[i, "LAST_DELIVERED"] = week
                ledger.at[i, "FIELD_PRIORITY"] = r.get("FIELD_PRIORITY", "")
        else:
            rows_to_add.append(
                {
                    "BADGE": r["BADGE"],
                    "RECOMMENDED_TRANSFORMER": r["RECOMMENDED_TRANSFORMER"],
                    "GIS_AT_FIRST_DELIVERY": norm(
                        pd.Series([r.get("CURRENT_TRANSFORMER", "")])
                    ).iloc[0],
                    "FIELD_PRIORITY": r.get("FIELD_PRIORITY", ""),
                    "FIRST_DELIVERED": week,
                    "LAST_DELIVERED": week,
                    "STATUS": OPEN,
                    "CLOSED_WEEK": "",
                    "NOTES": "",
                }
            )
            n_new += 1

    if rows_to_add:
        ledger = pd.concat(
            [ledger, pd.DataFrame(rows_to_add, columns=LEDGER_COLS)],
            ignore_index=True,
        )

    # --- 3. Mark open items that dropped off without a GIS change ------
    for i, r in ledger.iterrows():
        if r["STATUS"] != OPEN:
            continue
        if (r["BADGE"], r["RECOMMENDED_TRANSFORMER"]) not in current_keys:
            ledger.at[i, "STATUS"] = LAPSED
            ledger.at[i, "NOTES"] = "no longer flagged; GIS unchanged"
            n_lapsed += 1

    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    ledger.to_csv(LEDGER, index=False)
    os.makedirs(OUT_DIR, exist_ok=True)

    resolved_path = os.path.join(OUT_DIR, "resolved_since_last_run.csv")
    pd.DataFrame(
        newly_resolved,
        columns=["BADGE", "WAS_ASSIGNED", "NOW_ASSIGNED", "FIRST_DELIVERED",
                 "RESOLVED_WEEK"],
    ).to_csv(resolved_path, index=False)

    counts = ledger["STATUS"].value_counts()
    total = len(ledger)
    closed = int(counts.get(RESOLVED, 0)) + int(counts.get(CONFIRMED, 0))

    lines = [
        f"M2T reconciliation summary - {week}",
        "=" * 56,
        "",
        f"Recommendations ever delivered : {total:,}",
        f"  Resolved, GIS updated        : {int(counts.get(RESOLVED, 0)):,}",
        f"  Closed, GIS confirmed correct: {int(counts.get(CONFIRMED, 0)):,}",
        f"  Still open                   : {int(counts.get(OPEN, 0)):,}",
        f"  Lapsed (no longer flagged)   : {int(counts.get(LAPSED, 0)):,}",
        "",
        f"Reconciled: {closed:,} of {total:,}"
        + (f" ({100 * closed / total:.1f}%)" if total else ""),
        "",
        f"Newly resolved since last run  : {len(newly_resolved):,}",
        f"New recommendations this run   : {n_new:,}",
        f"Reopened this run              : {n_reopened:,}",
        f"Lapsed this run                : {n_lapsed:,}",
        "",
    ]
    summary = "\n".join(lines)
    with open(os.path.join(OUT_DIR, "reconciliation_summary.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(summary)

    print()
    print(summary)
    print(f"Ledger:   {LEDGER}")
    print(f"Resolved: {resolved_path}")


if __name__ == "__main__":
    main()
