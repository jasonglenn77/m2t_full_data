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
HISTORY = "data/state/reconciliation_history.csv"
FIELD_LIST = "data/outputs/deliverables/field_verification_list.csv"
KNOWN_MAPPING = "data/outputs/known_mapping.csv"
OUT_DIR = "data/outputs/deliverables"

HISTORY_COLS = [
    "WEEK",
    "ON_RECORD",
    "OPEN",
    "FIXED_IN_GIS",
    "GIS_CONFIRMED_CORRECT",
    "FIELD_FOUND_OTHER",
    "LAPSED",
    "FIELD_VERIFIED_TOTAL",
    "RECONCILED_PCT",
    "NEW_THIS_WEEK",
    "RESOLVED_THIS_WEEK",
]

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
FIELD_OTHER = "CLOSED_FIELD_FOUND_OTHER"
LAPSED = "LAPSED"
CLOSED_STATUSES = (RESOLVED, CONFIRMED, FIELD_OTHER)


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
        if status in CLOSED_STATUSES:
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
            if ledger.at[i, "STATUS"] not in CLOSED_STATUSES:
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
    n_fixed = int(counts.get(RESOLVED, 0))
    n_confirmed = int(counts.get(CONFIRMED, 0))
    n_other = int(counts.get(FIELD_OTHER, 0))
    # Anything a crew physically went and looked at, whatever the verdict.
    verified = n_confirmed + n_other
    closed = n_fixed + n_confirmed + n_other

    lines = [
        f"M2T reconciliation summary - {week}",
        "=" * 58,
        "",
        f"Recommendations ever delivered   : {total:,}",
        "",
        "  VERDICTS",
        f"    Fixed in GIS (model was right) : {n_fixed:,}",
        f"    GIS confirmed correct          : {n_confirmed:,}",
        f"    Field found a third transformer: {n_other:,}",
        f"    Still open                     : {int(counts.get(OPEN, 0)):,}",
        f"    Lapsed (no longer flagged)     : {int(counts.get(LAPSED, 0)):,}",
        "",
        f"  Field-verified to date           : {verified:,}",
        f"  Reconciled                       : {closed:,} of {total:,}"
        + (f" ({100 * closed / total:.1f}%)" if total else ""),
        "",
        "  THIS RUN",
        f"    Newly fixed in GIS             : {len(newly_resolved):,}",
        f"    New recommendations            : {n_new:,}",
        f"    Reopened                       : {n_reopened:,}",
        f"    Lapsed                         : {n_lapsed:,}",
        "",
    ]

    # --- Append this week to the running history ----------------------
    row = {
        "WEEK": week,
        "ON_RECORD": total,
        "OPEN": int(counts.get(OPEN, 0)),
        "FIXED_IN_GIS": n_fixed,
        "GIS_CONFIRMED_CORRECT": n_confirmed,
        "FIELD_FOUND_OTHER": n_other,
        "LAPSED": int(counts.get(LAPSED, 0)),
        "FIELD_VERIFIED_TOTAL": verified,
        "RECONCILED_PCT": round(100 * closed / total, 2) if total else 0.0,
        "NEW_THIS_WEEK": n_new,
        "RESOLVED_THIS_WEEK": len(newly_resolved),
    }
    hist = pd.DataFrame(columns=HISTORY_COLS)
    if os.path.exists(HISTORY):
        hist = pd.read_csv(HISTORY, dtype=str)
        for c in HISTORY_COLS:
            if c not in hist.columns:
                hist[c] = ""
        hist = hist[HISTORY_COLS]
        hist = hist[hist["WEEK"] != week]          # re-running a week replaces it
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    hist = hist.sort_values("WEEK", kind="stable")
    hist.to_csv(HISTORY, index=False)

    if len(hist) > 1:
        lines.append("  WEEK OVER WEEK")
        lines.append(
            "    {:<12}{:>9}{:>9}{:>10}{:>8}".format(
                "week", "open", "fixed", "verified", "recon%"
            )
        )
        for _, h in hist.tail(12).iterrows():
            lines.append(
                "    {:<12}{:>9}{:>9}{:>10}{:>8}".format(
                    str(h["WEEK"]),
                    f"{int(float(h['OPEN'])):,}",
                    f"{int(float(h['FIXED_IN_GIS'])):,}",
                    f"{int(float(h['FIELD_VERIFIED_TOTAL'])):,}",
                    f"{float(h['RECONCILED_PCT']):.1f}",
                )
            )
        lines.append("")

    summary = "\n".join(lines)
    with open(os.path.join(OUT_DIR, "reconciliation_summary.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(summary)

    print()
    print(summary)
    print(f"Ledger:   {LEDGER}")
    print(f"History:  {HISTORY}")
    print(f"Resolved: {resolved_path}")


if __name__ == "__main__":
    main()
