<#
.SYNOPSIS
    Catch the v2 model up one week at a time, recording a model run at
    each week boundary so the stability ledger accumulates real history.

.DESCRIPTION
    Reads the v2 pair ledger to find where you left off, then walks
    forward in 7-day steps to -Through. For each week it:

        1. python backfill_accumulator.py --resume --all --to-date <week>
        2. python run_v2_pipeline.py --run-id v2_<week>
        3. archive data/outputs_v2/ -> data/archive_v2/<week>/

    Step 2 is what writes the week into data/state/history_v2/, which is
    what PEER_STABILITY and RECOMMENDATION_STABILITY are computed from.
    Skipping it means the ledger advances but the week leaves no trace.

    Step 3 exists because data/outputs_v2/ is overwritten by every run.
    Without it only the most recent week's CSVs survive, so there is
    nothing to do historical analytics against later.

    Safe to stop and re-run: weeks already recorded are skipped, and the
    per-day ledger updates are idempotent.

.PARAMETER Through
    Last date to process, YYYY-MM-DD. Required.

.PARAMETER From
    Optional first week-ending date, YYYY-MM-DD. Defaults to the ledger's
    last applied day + 7.

.PARAMETER DryRun
    Print the week plan and exit without running anything.

.PARAMETER WithV1
    After the v2 weeks finish, also run the v1 pipeline once at the
    current window and build the consensus report.

.PARAMETER ArchiveDir
    Where per-week output snapshots go. Default data\archive_v2.

.PARAMETER ZipArchive
    Store each week as a single .zip (~11 MB) instead of a folder of CSVs
    (~45 MB). load_archive.py reads either form.

.PARAMETER NoArchive
    Skip the per-week snapshot entirely.

.EXAMPLE
    .\run_weeks.ps1 -Through 2026-08-07 -DryRun
    .\run_weeks.ps1 -Through 2026-08-07
    .\run_weeks.ps1 -Through 2026-08-07 -WithV1
#>

param(
    [Parameter(Mandatory = $true)][string]$Through,
    [string]$From = "",
    [switch]$DryRun,
    [switch]$WithV1,
    [string]$ArchiveDir = "data\archive_v2",
    [switch]$ZipArchive,
    [switch]$NoArchive
)

$ErrorActionPreference = "Stop"

function Write-Section($text) {
    Write-Host ""
    Write-Host ("=" * 68) -ForegroundColor Cyan
    Write-Host $text -ForegroundColor Cyan
    Write-Host ("=" * 68) -ForegroundColor Cyan
}

function Invoke-Step($label, $exe, $stepArgs) {
    Write-Host ""
    Write-Host ">> $label" -ForegroundColor Yellow
    Write-Host "   $exe $($stepArgs -join ' ')" -ForegroundColor DarkGray
    & $exe @stepArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$label failed with exit code $LASTEXITCODE"
    }
}

function Save-WeekSnapshot($week, $runId) {
    # data\outputs_v2 is rewritten by every run, so snapshot it now or the
    # week is gone. Also stamp a manifest: months from now the CSVs alone
    # will not tell you which GIS vintage or thresholds produced them.
    $src = "data\outputs_v2"
    if (-not (Test-Path $src)) {
        Write-Host "No $src to archive." -ForegroundColor DarkYellow
        return
    }

    if (-not (Test-Path $ArchiveDir)) {
        New-Item -ItemType Directory -Path $ArchiveDir -Force | Out-Null
    }
    $dest = Join-Path $ArchiveDir $week
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Copy-Item $src $dest -Recurse -Force

    $sp = Get-ChildItem "GIS_mapping\ServicePoints*.csv" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $tx = Get-ChildItem "GIS_mapping\Transformers*.csv" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1

    $manifest = @(
        "WEEK_ENDING=$week"
        "RUN_ID=$runId"
        "ARCHIVED_AT=$((Get-Date).ToString('s'))"
        "SERVICEPOINTS=$(if ($sp) { $sp.Name } else { 'none' })"
        "TRANSFORMERS=$(if ($tx) { $tx.Name } else { 'none' })"
    )
    $cfg = python -c "import config, cluster_from_accumulator as c; print(f'WINDOW_DAYS={config.WINDOW_DAYS}'); print(f'TOP_K_NEIGHBORS={config.TOP_K_NEIGHBORS}'); print(f'V2_THRESHOLD={c.V2_THRESHOLD}'); print(f'V2_STD_MAX={c.V2_STD_MAX}'); print(f'V2_TREND_MIN={c.V2_TREND_MIN}'); print(f'V2_MIN_DAYS={c.V2_MIN_DAYS}')"
    if ($LASTEXITCODE -eq 0) { $manifest += $cfg }

    $manifest | Set-Content -Path (Join-Path $dest "run_manifest.txt") -Encoding utf8

    if ($ZipArchive) {
        $zip = "$dest.zip"
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Compress-Archive -Path "$dest\*" -DestinationPath $zip -CompressionLevel Optimal
        Remove-Item $dest -Recurse -Force
        $mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
        Write-Host "Archived -> $zip ($mb MB)" -ForegroundColor Green
    }
    else {
        $mb = [math]::Round(
            (Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
        Write-Host "Archived -> $dest ($mb MB)" -ForegroundColor Green
    }
}

# ---------------------------------------------------------------- plan
Write-Section "Reading v2 ledger position"

if ($From -ne "") {
    $firstWeek = [datetime]::ParseExact($From, "yyyy-MM-dd", $null)
    Write-Host "Starting from -From $From"
}
else {
    $ledgerDay = python -c "from streaming_update import get_most_recent_day; print(get_most_recent_day())"
    if ($LASTEXITCODE -ne 0) { throw "Could not read the ledger." }
    $ledgerDay = $ledgerDay.Trim()
    if ($ledgerDay -eq "None" -or $ledgerDay -eq "") {
        throw "Ledger is empty. Build it first with: python backfill_accumulator.py --all"
    }
    Write-Host "Ledger's last applied day: $ledgerDay"
    $firstWeek = [datetime]::ParseExact($ledgerDay, "yyyy-MM-dd", $null).AddDays(7)
}

$end = [datetime]::ParseExact($Through, "yyyy-MM-dd", $null)
if ($firstWeek -gt $end) {
    Write-Host "Nothing to do: ledger is already at or past $Through." -ForegroundColor Green
    exit 0
}

$weeks = @()
$cursor = $firstWeek
while ($cursor -le $end) {
    $weeks += $cursor.ToString("yyyy-MM-dd")
    $cursor = $cursor.AddDays(7)
}
# Include a final short week so the run ends exactly on -Through.
if ($weeks[-1] -ne $Through) { $weeks += $Through }

Write-Host ""
Write-Host "$($weeks.Count) weekly run(s) planned:"
foreach ($w in $weeks) { Write-Host "  v2_$w" }

if ($NoArchive) {
    Write-Host ""
    Write-Host "Archiving DISABLED - only the final week's CSVs will survive." -ForegroundColor DarkYellow
}
else {
    $perWeek = "~45 MB of CSVs"
    if ($ZipArchive) { $perWeek = "~11 MB zipped" }
    Write-Host ""
    Write-Host "Archiving to $ArchiveDir ($perWeek per week)."
}

if ($DryRun) {
    Write-Host ""
    Write-Host "-DryRun set; stopping here." -ForegroundColor Green
    exit 0
}

# ------------------------------------------------------------ v2 weeks
$started = Get-Date
$done = 0
$skipped = 0

foreach ($week in $weeks) {
    $runId = "v2_$week"
    Write-Section "Week ending $week   ($($done + $skipped + 1) of $($weeks.Count))"

    $already = python -c "import os,sys,pandas as pd; p='data/state/history_v2/runs.parquet'; sys.stdout.write('yes' if os.path.exists(p) and '$runId' in set(pd.read_parquet(p)['RUN_ID']) else 'no')"
    if ($already.Trim() -eq "yes") {
        Write-Host "Run $runId is already recorded - skipping." -ForegroundColor DarkGray
        $skipped++
        continue
    }

    # --all matters: without it, backfill only considers the newest
    # WINDOW_DAYS parquets, which silently drops older unprocessed days.
    Write-Host ""
    Write-Host ">> Ledger catch-up through $week" -ForegroundColor Yellow
    $backfillArgs = @(
        "backfill_accumulator.py", "--resume", "--all", "--to-date", $week
    )
    Write-Host "   python $($backfillArgs -join ' ')" -ForegroundColor DarkGray
    # Tee (not assign) so per-day progress still streams to the console
    # during what can be a 20+ minute step, while $teed keeps the text.
    & python @backfillArgs 2>&1 | Tee-Object -Variable teed
    $backfillCode = $LASTEXITCODE
    if ($backfillCode -ne 0) {
        # A week with no parquets at all (a hole in your daily data) is a
        # skip, not a failure. Recording a run for it would add a duplicate
        # snapshot to the stability ledger and skew the denominators.
        if (($teed -join "`n") -match "No parquets match the requested range") {
            Write-Host "No daily data for this week - skipping (data gap)." -ForegroundColor DarkYellow
            $skipped++
            continue
        }
        throw "Ledger catch-up through $week failed with exit code $backfillCode"
    }

    Invoke-Step "v2 pipeline for $week" "python" @(
        "run_v2_pipeline.py", "--run-id", $runId
    )

    if (-not $NoArchive) {
        Write-Host ""
        Write-Host ">> Archiving week $week" -ForegroundColor Yellow
        Save-WeekSnapshot $week $runId
    }

    $done++
    $elapsed = (Get-Date) - $started
    Write-Host ""
    Write-Host "Completed $done week(s); elapsed $([int]$elapsed.TotalMinutes) min." -ForegroundColor Green
}

# ----------------------------------------------------------- v1 + consensus
if ($WithV1) {
    Write-Section "v1 pipeline (current window) + consensus report"
    Invoke-Step "v1 clustering"        "python" @("rerun_clustering.py")
    Invoke-Step "v1 mapping errors"    "python" @("find_mapping_errors.py")
    Invoke-Step "v1 evaluation"        "python" @("evaluate_results.py")
    Invoke-Step "v1 ranking"           "python" @("rank_corrections.py")
    Invoke-Step "v1 record run"        "python" @("record_run.py", "$Through`_weekly")
    Invoke-Step "v1 stability"         "python" @("stability_report.py")
    Invoke-Step "v1 enrichment"        "python" @("enrich_outputs.py")
    Invoke-Step "consensus report"     "python" @("consensus_report.py")
}

Write-Section "Done"
Write-Host "$done week(s) run, $skipped skipped."
Write-Host "v2 outputs : data\outputs_v2\corrections_high_confidence_enriched.csv"
if (-not $NoArchive) {
    Write-Host "Week archive: $ArchiveDir"
    Write-Host "  Query it with: python load_archive.py --list"
}
if ($WithV1) {
    Write-Host "Consensus  : data\outputs\consensus_report.csv"
}
Write-Host "Total time : $([int]((Get-Date) - $started).TotalMinutes) min"
