<#
.SYNOPSIS
    The full Monday routine: new GIS, extract the week's parquets, run v2
    and v1, build the consensus report, and produce the deliverable file.

.DESCRIPTION
    Computes the week from today's date - the most recent Sunday and the
    Monday six days before it - so nothing needs editing week to week.
    Run it on a Monday and it processes the prior Monday through Sunday.

    Phases, in order:

        gis        prompt for the new GIS files, rebuild known_mapping.csv
        extract    pull the week's daily parquets from C2M
        v2         run_weeks.ps1 for the week (ledger + pipeline + archive)
        v1         advance the signature window 7 days, run the v1 pipeline
        consensus  consensus_report.py + the both_high_confidence deliverable
        verify     coverage check and the stability summary

    Any phase can be skipped, and -StartAt resumes partway through after a
    failure. Budget 4-6 hours for a full run.

.PARAMETER WeekEnd
    Week-ending Sunday, YYYY-MM-DD. Defaults to the most recent Sunday.

.PARAMETER StartAt
    Resume from a phase: gis, extract, v2, v1, consensus, or verify.

.PARAMETER SkipGis
    No new GIS export this week - leaves known_mapping.csv as is.

.PARAMETER SkipExtract
    Parquets for this week are already in data\raw\daily\.

.PARAMETER SkipV1
    v2 only. Faster, but produces no consensus report.

.PARAMETER V1From
    Last day already in the v1 signature window, YYYY-MM-DD. Only needed
    the first time; afterwards the script tracks it in
    data\state\v1_window_end.txt.

.PARAMETER NoPrompt
    Never wait for input. The gis phase assumes files are already in place.

.PARAMETER DryRun
    Print the plan and exit.

.EXAMPLE
    .\run_monday.ps1 -DryRun
    .\run_monday.ps1
    .\run_monday.ps1 -SkipGis -SkipExtract
    .\run_monday.ps1 -StartAt v1
    .\run_monday.ps1 -WeekEnd 2026-08-30 -V1From 2026-08-23
#>

param(
    [string]$WeekEnd = "",
    [string]$StartAt = "",
    [switch]$SkipGis,
    [switch]$SkipExtract,
    [switch]$SkipV1,
    [string]$V1From = "",
    [switch]$NoPrompt,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$PHASES = @("gis", "extract", "v2", "v1", "consensus", "verify")
$V1_STATE = "data\state\v1_window_end.txt"

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
        throw "$label failed with exit code $LASTEXITCODE. Resume with: .\run_monday.ps1 -StartAt <phase>"
    }
}

function Test-Phase($name) {
    return ($PHASES.IndexOf($name) -ge $script:startIndex)
}

function Suspend-Sleep {
    # Keep the machine awake for the life of this window. This is a
    # user-level API call, so it works where `powercfg` is blocked by
    # group policy on a managed machine.
    try {
        if (-not ("Win32.MondayPower" -as [type])) {
            $sig = '[DllImport("kernel32.dll", SetLastError = true)] public static extern uint SetThreadExecutionState(uint esFlags);'
            Add-Type -MemberDefinition $sig -Name MondayPower -Namespace Win32 | Out-Null
        }
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        [Win32.MondayPower]::SetThreadExecutionState(0x80000000 -bor 0x00000001) | Out-Null
        Write-Host "Sleep suppressed for this window." -ForegroundColor DarkGray
    }
    catch {
        Write-Host "Could not suppress sleep (harmless; the run is resumable)." -ForegroundColor DarkYellow
    }
}

# ------------------------------------------------------------------ dates
if ($WeekEnd -ne "") {
    try {
        $sunday = [datetime]::ParseExact($WeekEnd, "yyyy-MM-dd", $null)
    }
    catch {
        throw "-WeekEnd must be YYYY-MM-DD; got '$WeekEnd'"
    }
}
else {
    $today = (Get-Date).Date
    $back = [int]$today.DayOfWeek          # Sunday = 0, Monday = 1, ...
    if ($back -eq 0) { $back = 7 }         # on a Sunday, use the previous one
    $sunday = $today.AddDays(-$back)
}
$monday = $sunday.AddDays(-6)
$weekEndStr = $sunday.ToString("yyyy-MM-dd")
$weekStartStr = $monday.ToString("yyyy-MM-dd")

$script:startIndex = 0
if ($StartAt -ne "") {
    $script:startIndex = $PHASES.IndexOf($StartAt.ToLower())
    if ($script:startIndex -lt 0) {
        throw "-StartAt must be one of: $($PHASES -join ', ')"
    }
}

# ------------------------------------------------------------------- plan
Write-Section "Monday run - week of $weekStartStr through $weekEndStr"

if ((Get-Date).DayOfWeek -ne "Monday" -and $WeekEnd -eq "") {
    Write-Host "Note: today is $((Get-Date).DayOfWeek), not Monday. Using the most" -ForegroundColor DarkYellow
    Write-Host "recent Sunday ($weekEndStr). Pass -WeekEnd to override." -ForegroundColor DarkYellow
    Write-Host ""
}

foreach ($p in $PHASES) {
    $state = "run"
    if (-not (Test-Phase $p)) { $state = "skip (before -StartAt)" }
    elseif ($p -eq "gis" -and $SkipGis) { $state = "skip (-SkipGis)" }
    elseif ($p -eq "extract" -and $SkipExtract) { $state = "skip (-SkipExtract)" }
    elseif (($p -eq "v1" -or $p -eq "consensus") -and $SkipV1) { $state = "skip (-SkipV1)" }
    Write-Host ("  {0,-10} {1}" -f $p, $state)
}

if ($DryRun) {
    Write-Host ""
    Write-Host "-DryRun set; stopping here." -ForegroundColor Green
    exit 0
}

Suspend-Sleep
$started = Get-Date

# -------------------------------------------------------------- 1. GIS
if ((Test-Phase "gis") -and -not $SkipGis) {
    Write-Section "Phase 1/6 - GIS refresh"

    if (-not $NoPrompt) {
        Write-Host "Drop the new ServicePoints*.csv and Transformers*.csv into" -ForegroundColor Yellow
        Write-Host "GIS_mapping\ now. Keep the older files - enrich_outputs_v2.py" -ForegroundColor Yellow
        Write-Host "reads them as a decoder for columns the new exports drop." -ForegroundColor Yellow
        Write-Host ""
        Read-Host "Press Enter when both files are in place (Ctrl+C to abort)" | Out-Null
    }

    Invoke-Step "Confirm GIS file selection" "python" @("check_state.py", "--skip-ledger")

    if (-not $NoPrompt) {
        $ok = Read-Host "Did the '-->' arrows point at your NEW files? (y/n)"
        if ($ok.Trim().ToLower() -ne "y") {
            throw "Stopping. Fix the file timestamps, then resume: .\run_monday.ps1 -StartAt gis"
        }
    }

    Invoke-Step "Rebuild known_mapping.csv" "python" @("build_known_mapping.py")
}

# ---------------------------------------------------------- 2. EXTRACT
if ((Test-Phase "extract") -and -not $SkipExtract) {
    Write-Section "Phase 2/6 - Extract parquets for $weekStartStr .. $weekEndStr"

    # Calls run_daily_extract directly so save_daily_data.py never needs
    # its hardcoded dates edited. Days already marked done are skipped.
    $py = "from datetime import datetime, timezone; " +
          "from save_daily_data import run_daily_extract; " +
          "run_daily_extract(" +
          "datetime($($monday.Year),$($monday.Month),$($monday.Day),tzinfo=timezone.utc), " +
          "datetime($($sunday.Year),$($sunday.Month),$($sunday.Day),tzinfo=timezone.utc))"
    Invoke-Step "save_daily_data extract" "python" @("-c", $py)
}

# --------------------------------------------------------------- 3. v2
if (Test-Phase "v2") {
    Write-Section "Phase 3/6 - v2 model (ledger + pipeline + weekly archive)"
    & .\run_weeks.ps1 -Through $weekEndStr
    if (-not $?) {
        throw "v2 phase failed. Resume with: .\run_monday.ps1 -StartAt v2"
    }
}

# --------------------------------------------------------------- 4. v1
if ((Test-Phase "v1") -and -not $SkipV1) {
    Write-Section "Phase 4/6 - v1 model"

    $from = $V1From
    if ($from -eq "" -and (Test-Path $V1_STATE)) {
        $from = (Get-Content $V1_STATE -Raw).Trim()
    }

    if ($from -eq "") {
        Write-Host "v1 window position is unknown, so advancing it could silently" -ForegroundColor DarkYellow
        Write-Host "leave a gap. Skipping the v1 phase." -ForegroundColor DarkYellow
        Write-Host ""
        Write-Host "To fix, once:" -ForegroundColor DarkYellow
        Write-Host "  python build_signature_store.py" -ForegroundColor DarkYellow
        Write-Host "  Set-Content $V1_STATE '<newest parquet date>'" -ForegroundColor DarkYellow
        Write-Host "or pass -V1From <YYYY-MM-DD> to state it explicitly." -ForegroundColor DarkYellow
    }
    else {
        Write-Host "v1 window currently ends: $from"
        $v1Files = Get-ChildItem "data\raw\daily\*.parquet" |
            Where-Object { $_.BaseName -gt $from -and $_.BaseName -le $weekEndStr } |
            Sort-Object Name

        if ($v1Files.Count -eq 0) {
            Write-Host "v1 is already current through $weekEndStr." -ForegroundColor DarkGray
        }
        else {
            Write-Host "Advancing $($v1Files.Count) day(s) to $weekEndStr ..."
            foreach ($f in $v1Files) {
                Invoke-Step "rolling_update $($f.BaseName)" "python" @("rolling_update.py", $f.FullName)
                # Stamp after each day so an interrupted loop resumes correctly.
                Set-Content -Path $V1_STATE -Value $f.BaseName -Encoding utf8
            }
        }

        Invoke-Step "v1 clustering"     "python" @("rerun_clustering.py")
        Invoke-Step "v1 mapping errors" "python" @("find_mapping_errors.py")
        Invoke-Step "v1 evaluation"     "python" @("evaluate_results.py")
        Invoke-Step "v1 ranking"        "python" @("rank_corrections.py")
        Invoke-Step "v1 record run"     "python" @("record_run.py", "${weekEndStr}_weekly")
        Invoke-Step "v1 stability"      "python" @("stability_report.py")
        Invoke-Step "v1 enrichment"     "python" @("enrich_outputs.py")
    }
}

# -------------------------------------------------------- 5. CONSENSUS
if ((Test-Phase "consensus") -and -not $SkipV1) {
    Write-Section "Phase 5/6 - Consensus report and deliverable"
    Invoke-Step "consensus_report" "python" @("consensus_report.py")
    Invoke-Step "build deliverables" "python" @("build_deliverable.py")
}

# ----------------------------------------------------------- 6. VERIFY
if (Test-Phase "verify") {
    Write-Section "Phase 6/6 - Verify"
    Invoke-Step "coverage check" "python" @("load_archive.py", "--verify")
    Invoke-Step "stability summary" "python" @(
        "load_archive.py", "--file", "corrections_high_confidence_enriched.csv", "--summary"
    )
}

# ---------------------------------------------------------------- done
Write-Section "Monday run complete - week ending $weekEndStr"
Write-Host "Elapsed: $([int]((Get-Date) - $started).TotalMinutes) min"
Write-Host ""
Write-Host "Deliverables (see README.txt in that folder for who gets what):" -ForegroundColor Green
Write-Host "  data\outputs\deliverables\action_cross_feeder.csv     field team, highest signal"
Write-Host "  data\outputs\deliverables\action_new_assignment.csv   GIS team, easy wins"
Write-Host "  data\outputs\deliverables\review_same_feeder.csv      validation candidates only"
Write-Host ""
Write-Host "Also for the GIS team:" -ForegroundColor Green
Write-Host "  data\outputs_v2\badges_missing_from_gis.csv"
Write-Host "  data\outputs_v2\latlon_discrepancies.csv"
Write-Host ""
Write-Host "Hold back: same_feeder_ambiguous rows (confirmed false positives)," -ForegroundColor DarkYellow
Write-Host "and full_clusters_enriched.csv (internal join asset, 218K rows)." -ForegroundColor DarkYellow
Write-Host ""
Write-Host "Week archive: data\archive_v2\$weekEndStr"
