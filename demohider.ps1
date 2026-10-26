<#
  DemoHider - one-step runner
  ===========================
  Choose WHAT to hide; the runner checks it, shows you the list, asks for your
  OK, and updates Dynamics.

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

  A ready-made plan file:
      .\demohider.ps1 -Plan plans\contact-grid-test.json

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
    [switch]$NewPicker,
    [string]$Find,
    [string]$Table,
    [string]$EnvName = "CUS-Dev",
    [string]$Solution = "solutions\DemoHider"
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

function Send-ToDynamics($RunDir) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $zip = "solutions\DemoHider_$stamp.zip"

    Write-Step "Packing the solution"
    pac solution pack --zipfile $zip --folder $Solution --packagetype Unmanaged
    if ($LASTEXITCODE -ne 0) { Stop-With "Packing failed - see the message above." }

    Write-Step "Importing into Dynamics (this takes a few minutes)"
    pac solution import --path $zip
    if ($LASTEXITCODE -ne 0) { Stop-With "Import failed - see the message above." }

    Write-Step "Publishing only the tables that changed"
    python scripts\publish_tables.py --run $RunDir
    if ($LASTEXITCODE -eq 2) {
        Write-Host "The import worked; publishing may still be finishing in Dynamics." -ForegroundColor Yellow
    } elseif ($LASTEXITCODE -ne 0) {
        Stop-With "Import worked but publishing failed. Publish from make.powerapps.com > Solutions > DemoHider > Publish all customizations."
    }
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

# ------------------------------------------------------------------ undo last run
if ($Undo) {
    $last = $null
    foreach ($r in Get-Runs) {
        $info = Get-Content (Join-Path $r.FullName "run.json") -Raw | ConvertFrom-Json
        if ($info.status -eq "applied") { $last = $r; break }
    }
    if (-not $last) { Write-Host "Nothing to undo."; exit 0 }
    Need-File $Solution "Solution folder"
    Confirm-Environment

    Write-Step "Undoing: $($last.Name)"
    python scripts\hide_engine.py rollback --run $last.FullName
    if ($LASTEXITCODE -ne 0) { Stop-With "Undo stopped - see the message above." }

    Send-ToDynamics $last.FullName
    Write-Host ""
    Write-Host "Done. In the app press Ctrl+F5 - the buttons are back." -ForegroundColor Green
    exit 0
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
    python scripts\picker_server.py --catalog $Catalog --out $PlanFile --solution $Solution
    if ($LASTEXITCODE -eq 3 -or $LASTEXITCODE -eq 4) { exit 0 }    # cancelled / no changes
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

# ------------------------------------------------------- check, confirm, apply
Write-Step "Step 1 of 4 - Checking (nothing is changed yet)"
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
    if ($shown -lt 40) { Write-Host ("  {0}  {1,-12} {2,-13} {3}" -f $what, $where, $i.command_bar_location, $i.label) }
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

Write-Step "Step 2 of 4 - Updating the solution files"
python scripts\hide_engine.py apply --run $run.FullName
if ($LASTEXITCODE -ne 0) { Stop-With "Apply stopped - see the message above." }

Write-Step "Step 3 of 4 - Final safety check"
python scripts\hide_engine.py validate --catalog $Catalog --solution $Solution
if ($LASTEXITCODE -ne 0) {
    Stop-With "Safety check failed - nothing was sent to Dynamics. Run  .\demohider.ps1 -Undo  to reset the files."
}

Write-Step "Step 4 of 4 - Sending to Dynamics"
Send-ToDynamics $run.FullName

Write-Host ""
Write-Host "Done! In the app press Ctrl+F5 to see the result." -ForegroundColor Green
Write-Host "Undo this change:  .\demohider.ps1 -Undo      Reset everything:  .\demohider.ps1 -UnhideAll"
