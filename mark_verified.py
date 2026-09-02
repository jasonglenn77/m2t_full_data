# mark_verified.py
"""
Take back a completed field verification sheet and close the loop.

Hand this the field_verification_list.csv the crew filled in and returned. It
reads FIELD_CONFIRMED_TRANSFORMER on each row and acts on what was found:

  Confirmed = CURRENT_TRANSFORMER   GIS was already right. The meter is added
                                    to BOTH models' ignore lists so it stops
                                    appearing on future deliveries, and the
                                    reconciliation ledger records it closed.

  Confirmed = RECOMMENDED_TRANSFORMER
                                    The model was right. Nothing to suppress --
                                    once GIS is updated, next Monday's extract
                                    makes reconcile.py close it automatically.
                                    Listed here so you can confirm GIS was
                                    actually changed.

  Confirmed = something else        Neither was right. Suppresses the model's
                                    recommendation and flags the row for
                                    review, since the true assignment differs
                                    from both.

Suppression is per (badge, recommended transformer), matching how the models
already read their ignore lists. If the model later recommends a DIFFERENT
transformer for the same meter, that new recommendation still surfaces -- which
is what you want, because it is new information rather than the finding the
crew already dismissed.

Usage:
    python mark_verified.py returned_sheet.csv
    python mark_verified.py returned_sheet.csv --dry-run
"""

import argparse
import os
from datetime import date

import pandas as pd

IGNORE_V1 = "data/state/corrections_ignored.csv"
IGNORE_V2 = "data/state/corrections_ignored_v2.csv"
LEDGER = "data/state/reconciliation_ledger.csv"
IGNORE_COLS = ["BADGE", "RECOMMENDED_TRANSFORMER", "DATE_ADDED", "NOTES"]

CONFIRMED = "CLOSED_GIS_CONFIRMED"


def norm(series):
    return (
        series.astype(str)
        .str.strip()
        .str.replace(r"\.0+$", "", regex=True)
        .replace({"nan": "", "None": "", "<NA>": ""})
    )


def append_ignores(path, new_rows, dry_run):
    existing = pd.DataFrame(columns=IGNORE_COLS)
    if os.path.exists(path):
        existing = pd.read_csv(path, dtype=str).fillna("")
        for c in IGNORE_COLS:
            if c not in existing.columns:
                existing[c] = ""
        existing = existing[IGNORE_COLS]
        existing["BADGE"] = norm(existing["BADGE"])
        existing["RECOMMENDED_TRANSFORMER"] = norm(
            existing["RECOMMENDED_TRANSFORMER"]
        )

    have = set(zip(existing["BADGE"], existing["RECOMMENDED_TRANSFORMER"]))
    fresh = [r for r in new_rows
             if (r["BADGE"], r["RECOMMENDED_TRANSFORMER"]) not in have]

    if not fresh:
        print(f"  {path}: nothing new to add ({len(existing):,} already listed)")
        return 0

    if dry_run:
        print(f"  {path}: WOULD add {len(fresh):,} row(s)")
        return len(fresh)

    out = pd.concat(
        [existing, pd.DataFrame(fresh, columns=IGNORE_COLS)], ignore_index=True
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out.to_csv(path, index=False)
    print(f"  {path}: added {len(fresh):,} row(s), {len(out):,} total")
    return len(fresh)


def update_ledger(keys, week, dry_run):
    if not os.path.exists(LEDGER):
        print(f"  {LEDGER} not found - skipping ledger update.")
        return
    led = pd.read_csv(LEDGER, dtype=str).fillna("")
    led["BADGE"] = norm(led["BADGE"])
    led["RECOMMENDED_TRANSFORMER"] = norm(led["RECOMMENDED_TRANSFORMER"])

    n = 0
    for i, r in led.iterrows():
        if (r["BADGE"], r["RECOMMENDED_TRANSFORMER"]) in keys:
            if r["STATUS"] != CONFIRMED:
                led.at[i, "STATUS"] = CONFIRMED
                led.at[i, "CLOSED_WEEK"] = week
                led.at[i, "NOTES"] = "field visit confirmed the GIS record"
                n += 1
    if dry_run:
        print(f"  {LEDGER}: WOULD close {n:,} row(s)")
        return
    led.to_csv(LEDGER, index=False)
    print(f"  {LEDGER}: closed {n:,} row(s) as {CONFIRMED}")


def main():
    parser = argparse.ArgumentParser(
        description="Process a returned field verification sheet."
    )
    parser.add_argument("sheet", help="The completed field_verification_list.csv")
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Date recorded against these verifications (default: today).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    args = parser.parse_args()

    if not os.path.exists(args.sheet):
        raise SystemExit(f"{args.sheet} not found.")

    df = pd.read_csv(args.sheet, dtype=str).fillna("")
    required = {"BADGE", "CURRENT_TRANSFORMER", "RECOMMENDED_TRANSFORMER",
                "FIELD_CONFIRMED_TRANSFORMER"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(
            f"{args.sheet} is missing required column(s): {sorted(missing)}. "
            "Expected the field_verification_list.csv layout."
        )

    for c in ("BADGE", "CURRENT_TRANSFORMER", "RECOMMENDED_TRANSFORMER",
              "FIELD_CONFIRMED_TRANSFORMER"):
        df[c] = norm(df[c])

    done = df[df["FIELD_CONFIRMED_TRANSFORMER"] != ""].copy()
    print(f"Read {args.sheet}: {len(df):,} row(s), {len(done):,} verified")
    if done.empty:
        raise SystemExit(
            "No rows have FIELD_CONFIRMED_TRANSFORMER filled in - nothing to do."
        )

    gis_right = done[
        done["FIELD_CONFIRMED_TRANSFORMER"] == done["CURRENT_TRANSFORMER"]
    ]
    model_right = done[
        done["FIELD_CONFIRMED_TRANSFORMER"] == done["RECOMMENDED_TRANSFORMER"]
    ]
    neither = done[
        (done["FIELD_CONFIRMED_TRANSFORMER"] != done["CURRENT_TRANSFORMER"])
        & (done["FIELD_CONFIRMED_TRANSFORMER"] != done["RECOMMENDED_TRANSFORMER"])
    ]

    print()
    print(f"  GIS was right     : {len(gis_right):,}  -> suppress from future lists")
    print(f"  Model was right   : {len(model_right):,}  -> update GIS; auto-closes next run")
    print(f"  Neither was right : {len(neither):,}  -> suppress and review")
    print()

    # Suppress the model's recommendation wherever the field did not confirm it.
    to_suppress = pd.concat([gis_right, neither], ignore_index=True)
    rows = []
    for _, r in to_suppress.iterrows():
        if r["FIELD_CONFIRMED_TRANSFORMER"] == r["CURRENT_TRANSFORMER"]:
            note = f"field visit {args.date}: GIS record confirmed correct"
        else:
            note = (
                f"field visit {args.date}: found on "
                f"{r['FIELD_CONFIRMED_TRANSFORMER']}, neither GIS nor model"
            )
        rows.append(
            {
                "BADGE": r["BADGE"],
                "RECOMMENDED_TRANSFORMER": r["RECOMMENDED_TRANSFORMER"],
                "DATE_ADDED": args.date,
                "NOTES": note,
            }
        )

    print("Ignore lists:")
    append_ignores(IGNORE_V1, rows, args.dry_run)
    append_ignores(IGNORE_V2, rows, args.dry_run)

    print()
    print("Reconciliation ledger:")
    update_ledger(
        set(zip(gis_right["BADGE"], gis_right["RECOMMENDED_TRANSFORMER"])),
        args.date,
        args.dry_run,
    )

    if not neither.empty:
        out = "data/outputs/deliverables/field_found_third_transformer.csv"
        if not args.dry_run:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            neither.to_csv(out, index=False)
        print()
        print(f"{len(neither):,} meter(s) were on a transformer neither GIS nor")
        print(f"the model expected. Worth a look: {out}")

    if not model_right.empty:
        print()
        print(f"{len(model_right):,} meter(s) confirmed the model. Make sure GIS is")
        print("updated for these - next Monday's extract closes them automatically.")

    if args.dry_run:
        print()
        print("--dry-run: nothing was written.")


if __name__ == "__main__":
    main()
