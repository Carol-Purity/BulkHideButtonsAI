<#
  DemoHider - one-step runner
  ===========================
  Choose WHAT to hide; the runner checks it, opens a page in your browser that
  shows the list and the environment, and after you click "Confirm and send"
  updates Dynamics and shows the progress there (and here).

  TICK BOXES (easiest - opens a page in your browser):
      .\demohider.ps1 -Pick
      Ticked = hidden. Untick a hidden button to bring it back. Click Apply.

  PRESET (reusable rules):
      .\demohider.ps1 -Preset presets\sales-demo.json            hide
      .\demohider.ps1 -Preset presets\sales-demo.json -Unhide    bring back

  MANUAL (tick buttons in Excel):
      .\demohider.ps1 -NewPicker                     create pickers\CUS-Dev.csv
      .\demohider.ps1 -NewPicker -Table contact      only one table
      ...open it in Excel, type x in the 'hide' column, save...
      .\demohider.ps1 -Picker pickers\CUS-Dev.csv               hide ticked
      .\demohider.ps1 -Picker pickers\CUS-Dev.csv -Unhide       bring back ticked

  FIND a button's short name (to use in presets):
      .\demohider.ps1 -Find report
      .\demohider.ps1 -Find excel -Table contact

  RESET / UNDO:
      .\demohider.ps1 -UnhideAll     bring back everything DemoHider has hidden
      .\demohider.ps1 -Undo          undo only the last change

  CHANGE NOT SHOWING / RUN INTERRUPTED:
      .\demohider.ps1 -Publish       publish the last run again (fast)
      .\demohider.ps1 -Send          pack + import + publish the last run again

  A ready-made plan file:
      .\demohider.ps1 -Plan plans\contact-grid-test.json

  SENDING: every change is sent in TWO rounds automatically
  (pack -> import -> publish, wait 60 s, pack -> import -> publish again),
  always for the same, latest run.
      -OneRound        send only once (faster, but the app may not update)
      -RoundWait 120   seconds to wait between the rounds (default 60)

  CONFIRM IN THE TERMINAL instead of the browser (Y/N, like before):
      -NoBrowser       e.g.  .\demohider.ps1 -Preset presets\sales-demo.json -NoBrowser

  Optional:  -EnvName CUS-Dev   -Solution solutions\DemoHider
#>
param(
    [string]$Preset,
    [string]$Picker,
    [string]$Plan,
    [switch]$Pick,
    [switch]$Unhide,
    [switch]$UnhideAll,
    [switch]$Undo,
    [switch]$Publish,
    [switch]$Send,
    [switch]$NewPicker,
    [string]$Find,
    [string]$Table,
    [string]$EnvName = "CUS-Dev",
    [string]$Solution = "solutions\DemoHider",
    [switch]$OneRound,
    [switch]$NoBrowser,
    [int]$RoundWait = 60
)

Set-Location $PSScriptRoot
$env:DEMOHIDER_RUNNER = "1"
$Catalog = "catalog\$EnvName\catalog.json"

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Stop-With($text) {
    Write-Host ""
    Write-Host $text -ForegroundColor Red
    exit 1
}

function Need-File($path, $what) {
    if (-not (Test-Path $path)) { Stop-With "$what not found: $path" }
}

function Get-Runs {
    if (-not (Test-Path "runs")) { return @() }
    return Get-ChildItem "runs" -Directory | Sort-Object Name -Descending
}

function Get-CatalogUrl {
    $head = (Get-Content $Catalog -TotalCount 30) -join "`n"
    if ($head -match '"environment_url"\s*:\s*"([^"]+)"') { return $Matches[1] }
    return $null
}

function Confirm-Environment {
    # Make sure pac will import into the same environment the catalogue was built from.
    $catalogUrl = Get-CatalogUrl
    $who = (pac org who 2>&1) -join "`n"
    $pacOk = $LASTEXITCODE -eq 0
    if (-not $pacOk -or -not ($who -match 'Org URL:\s*(\S+)')) {
        Stop-With "Could not check which environment pac is connected to. Run  pac org who  and sign in with  pac auth create  if needed."
    }
    $pacUrl = $Matches[1]
    $norm = { param($u) ($u -replace '/+$', '').ToLower() }
    Write-Host ""
    Write-Host "Environment check" -ForegroundColor Cyan
    Write-Host "  Catalogue ($EnvName): $catalogUrl"
    Write-Host "  pac is connected to : $pacUrl"
    if (-not $catalogUrl) {
        Stop-With "Could not read the environment URL from $Catalog - rebuild the catalogue."
    }
    if ((& $norm $catalogUrl) -ne (& $norm $pacUrl)) {
        Stop-With "These are different environments - nothing was changed. Switch with  pac auth select --name <name>  (see pac auth list), or use the matching -EnvName."
    }
    Write-Host "  OK - same environment." -ForegroundColor Green
}

$Rounds = if ($OneRound) { 1 } else { 2 }
$SendArgs = @("--rounds", $Rounds, "--round-wait", $RoundWait)

function Show-Result($code, $RunDir) {
    # Final message in the terminal (the browser page shows the same result).
    Write-Host ""
    switch ($code) {
        0 {
            Write-Host "Not showing after a few minutes?  .\demohider.ps1 -Publish   (or -Send)"
            Write-Host "Undo this change:  .\demohider.ps1 -Undo      Reset everything:  .\demohider.ps1 -UnhideAll"
        }
        5 {
            Write-Host "Round 1 was sent, round 2 did not complete." -ForegroundColor Yellow
            Write-Host "If the buttons have not changed in a few minutes:  .\demohider.ps1 -Send" -ForegroundColor Yellow
        }
        2 {
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " NOT LIVE YET - your files are updated, but sending did not finish" -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host "Finish it with ONE of these:"
            Write-Host "  .\demohider.ps1 -Publish     (publish again - try this first)" -ForegroundColor Yellow
            Write-Host "  .\demohider.ps1 -Send        (pack + import + publish again)" -ForegroundColor Yellow
            if ($RunDir) { Write-Host "Run folder: $RunDir" }
        }
        3 { Write-Host "Cancelled - nothing was changed." }
        4 { Write-Host "Nothing to change - it's already like that." }
        default { Write-Host "Stopped - see the message above. Nothing was sent to Dynamics." -ForegroundColor Red }
    }
    exit $code
}

function Get-LastSentRun {
    # newest run that changed something (applied, or rolled back by -Undo)
    foreach ($r in Get-Runs) {
        $f = Join-Path $r.FullName "run.json"
        if (-not (Test-Path $f)) { continue }
        $info = Get-Content $f -Raw | ConvertFrom-Json
        if ($info.status -eq "applied" -or $info.status -eq "rolled_back") { return $r }
    }
    return $null
}

Need-File $Catalog "Catalogue"

# ------------------------------------------------------------ helpers that only read
if ($Find) {
    $cmd = @("scripts\make_plan.py", "find", "--catalog", $Catalog, "--text", $Find)
    if ($Table) { $cmd += @("--table", $Table) }
    python @cmd
    exit 0
}

if ($NewPicker) {
    $out = if ($Table) { "pickers\$EnvName-$Table.csv" } else { "pickers\$EnvName.csv" }
    if (Test-Path $out) {
        $answer = Read-Host "$out already exists. Replace it (your ticks will be lost)? (Y/N)"
        if ($answer -notmatch '^[Yy]') { Write-Host "Kept the existing picker."; exit 0 }
    }
    $cmd = @("scripts\make_plan.py", "picker", "--catalog", $Catalog, "--out", $out)
    if ($Table) { $cmd += @("--table", $Table) }
    python @cmd
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "Next: open $out in Excel, type x in the 'hide' column, save, then run:" -ForegroundColor Green
        Write-Host "  .\demohider.ps1 -Picker $out"
    }
    exit 0
}

# ------------------------------------------- finish / repeat sending the last run
if ($Publish -or $Send) {
    $last = Get-LastSentRun
    if (-not $last) { Stop-With "No previous run found to publish." }
    Need-File $Solution "Solution folder"
    Confirm-Environment
    Write-Host ""
    Write-Host "Last run: $($last.Name)"
    $action = if ($Send) { "send" } else { "publish" }
    if ($NoBrowser) {
        if ($Send) { python scripts\sender.py send --run $last.FullName --catalog $Catalog --solution $Solution @SendArgs }
        else { python scripts\sender.py publish --run $last.FullName }
    } else {
        python scripts\flow_server.py --catalog $Catalog --solution $Solution --run $last.FullName --action $action @SendArgs
    }
    Show-Result $LASTEXITCODE $last.FullName
}

# ------------------------------------------------------------------ undo last run
if ($Undo) {
    $last = $null
    foreach ($r in Get-Runs) {
        $f = Join-Path $r.FullName "run.json"
        if (-not (Test-Path $f)) { continue }
        $info = Get-Content $f -Raw | ConvertFrom-Json
        if ($info.status -eq "applied") { $last = $r; break }
    }
    if (-not $last) { Write-Host "Nothing to undo."; exit 0 }
    Need-File $Solution "Solution folder"
    Confirm-Environment
    Write-Step "Undo: $($last.Name)"
    if ($NoBrowser) {
        $answer = Read-Host "Undo run $($last.Name) on $(Get-CatalogUrl)? (Y/N)"
        if ($answer -notmatch '^[Yy]') { Show-Result 3 }
        python scripts\sender.py send --undo --run $last.FullName --catalog $Catalog --solution $Solution @SendArgs
    } else {
        python scripts\flow_server.py --catalog $Catalog --solution $Solution --run $last.FullName --action undo @SendArgs
    }
    Show-Result $LASTEXITCODE $last.FullName
}

# ------------------------------------------------------------- build the plan
$chosen = @($Preset, $Picker, $Plan | Where-Object { $_ }).Count + [int][bool]$UnhideAll + [int][bool]$Pick
if ($chosen -eq 0) {
    Stop-With "Say what to do, e.g.  .\demohider.ps1 -Pick   (see the top of this file for all options)"
}
if ($chosen -gt 1) { Stop-With "Use only one of -Pick, -Preset, -Picker, -Plan or -UnhideAll at a time." }
Need-File $Solution "Solution folder"
Confirm-Environment
New-Item -ItemType Directory -Force -Path "plans" | Out-Null

if ($Pick) {
    $PlanFile = "plans\picked.json"
    Write-Step "Opening the tick-box page in your browser"
    if (-not $NoBrowser) {
        # the same browser tab checks, asks you to confirm and shows the progress
        python scripts\picker_server.py --catalog $Catalog --out $PlanFile --solution $Solution --flow @SendArgs
        $code = $LASTEXITCODE
        $run = Get-Runs | Select-Object -First 1
        Show-Result $code $(if ($run) { $run.FullName })
    }
    python scripts\picker_server.py --catalog $Catalog --out $PlanFile --solution $Solution
    if ($LASTEXITCODE -eq 3 -or $LASTEXITCODE -eq 4) { Show-Result $LASTEXITCODE }
    if ($LASTEXITCODE -ne 0) { Stop-With "The tick-box page stopped - see the message above." }
}
elseif ($Preset) {
    Need-File $Preset "Preset"
    $name = [IO.Path]::GetFileNameWithoutExtension($Preset)
    $PlanFile = if ($Unhide) { "plans\$name-unhide.json" } else { "plans\$name.json" }
    Write-Step "Building the list from preset '$name'"
    $cmd = @("scripts\make_plan.py", "from-preset", "--catalog", $Catalog, "--preset", $Preset, "--out", $PlanFile)
    if ($Unhide) { $cmd += "--unhide" }
    python @cmd
    if ($LASTEXITCODE -ne 0) { Stop-With "Could not read the preset - see the message above." }
}
elseif ($Picker) {
    Need-File $Picker "Picker"
    $name = [IO.Path]::GetFileNameWithoutExtension($Picker)
    $PlanFile = if ($Unhide) { "plans\picked-$name-unhide.json" } else { "plans\picked-$name.json" }
    Write-Step "Building the list from your ticks in $Picker"
    $cmd = @("scripts\make_plan.py", "from-picker", "--catalog", $Catalog, "--picker", $Picker, "--out", $PlanFile)
    if ($Unhide) { $cmd += "--unhide" }
    python @cmd
    if ($LASTEXITCODE -ne 0) { Stop-With "Could not read the picker - see the message above." }
}
elseif ($UnhideAll) {
    $PlanFile = "plans\unhide-all.json"
    Write-Step "Building the list of everything DemoHider has hidden"
    python scripts\make_plan.py unhide-all --catalog $Catalog --out $PlanFile
    if ($LASTEXITCODE -ne 0) { Stop-With "Nothing to bring back - see the message above." }
}
else {
    Need-File $Plan "Plan"
    $PlanFile = $Plan
}

# ------------------------------------- confirm in the browser (default)
if (-not $NoBrowser -and -not $Pick) {
    Write-Step "Opening the confirmation page in your browser"
    python scripts\flow_server.py --catalog $Catalog --solution $Solution --plan $PlanFile @SendArgs
    $code = $LASTEXITCODE
    $run = Get-Runs | Select-Object -First 1
    Show-Result $code $(if ($run) { $run.FullName })
}

# ------------------------------------- or in the terminal (-NoBrowser)
Write-Step "Step 1 of 2 - Checking (nothing is changed yet)"
python scripts\hide_engine.py preview --catalog $Catalog --plan $PlanFile --solution $Solution
if ($LASTEXITCODE -ne 0) { Stop-With "The check stopped - see the message above." }

$run = Get-Runs | Select-Object -First 1
$info = Get-Content (Join-Path $run.FullName "run.json") -Raw | ConvertFrom-Json

Write-Host ""
Write-Host "What will happen:" -ForegroundColor Yellow
$shown = 0
foreach ($i in $info.items) {
    if ($i.status -eq "skip") { continue }
    $what = switch ($i.status) { "hide" { "HIDE   " } "unhide" { "SHOW   " } default { "BLOCKED" } }
    $where = if ($i.scope -eq "application") { "ALL tables" } else { $i.table }
    $place = switch ($i.command_bar_location) { "HomepageGrid" { "Main grid" } "SubGrid" { "Subgrid" } default { $i.command_bar_location } }
    if ($shown -lt 40) { Write-Host ("  {0}  {1,-12} {2,-10} {3}" -f $what, $where, $place, $i.label) }
    $shown++
}
if ($shown -gt 40) { Write-Host "  ... and $($shown - 40) more" }
$skipped = @($info.items | Where-Object { $_.status -eq "skip" }).Count
if ($skipped -gt 0) { Write-Host "  ($skipped already done - skipped)" }
Write-Host "  Full details: $($run.FullName)\preview.md"

if ($info.change_count -eq 0) { Write-Host ""; Write-Host "Nothing to change - it's already like that."; exit 0 }
if ($info.blocked_count -gt 0) {
    Stop-With "$($info.blocked_count) item(s) are BLOCKED - nothing was changed. The reasons are listed above."
}

Write-Host ""
$answer = Read-Host "Apply these $($info.change_count) change(s) to $(Get-CatalogUrl)? (Y/N)"
if ($answer -notmatch '^[Yy]') { Write-Host "Cancelled - nothing was changed."; exit 0 }

Write-Step "Step 2 of 2 - Updating the files and sending to Dynamics"
python scripts\sender.py send --apply --run $run.FullName --catalog $Catalog --solution $Solution @SendArgs
Show-Result $LASTEXITCODE $run.FullName
