<#
.SYNOPSIS
  Installs PGDAS on Windows from a copy of this folder: virtual environment, packages, .env with a fresh secret key, database
  tables, the one-time setup token, and (optionally) scheduled tasks that start the server at boot and run the samplers.

.DESCRIPTION
  Run from the PGDAS folder in an elevated PowerShell when you want the scheduled tasks (-Tasks):
      powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Tasks
  Before the first run create PGDAS's own PostgreSQL database and login (see the README, "Where PGDAS keeps its own data") and put
  PGDAS_APP_DB_PASSWORD in .env afterwards, or pass -Sqlite to try PGDAS with one file and nothing else.
  Nothing outside this folder is changed except the scheduled tasks you ask for. Run it again later to update packages and tables.

.PARAMETER Port      The port the server listens on (127.0.0.1 only; put an https reverse proxy in front before exposing it).
.PARAMETER Sqlite    Use one SQLite file instead of PostgreSQL for PGDAS's own data (one person, trying it out).
.PARAMETER Tasks     Register the scheduled tasks "PGDAS server" (at startup), "PGDAS sampler" (every minute) and "PGDAS backups" (every 5 minutes).
.PARAMETER DryRun    Only say what would be done.
#>
param(
    [int]$Port = 8000,
    [switch]$Sqlite,
    [switch]$Tasks,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Run($what, [scriptblock]$action) {
    if ($DryRun) { Write-Host "    (dry run) $what" } else { & $action }
}

Step "Looking for Python 3.12 or newer"
$python = $null
foreach ($candidate in @("py -3.13", "py -3.12", "python")) {
    try {
        $parts = $candidate.Split(" ")
        $version = & $parts[0] ($parts[1..($parts.Length)] + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
        if ($version -and [version]$version -ge [version]"3.12") { $python = $candidate; break }
    } catch { }
}
if (-not $python) { throw "Python 3.12 or newer was not found. Install it from https://www.python.org/downloads/ and run this again." }
Write-Host "    using: $python"

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Step "Creating the virtual environment (.venv)"
    Run "$python -m venv .venv" { $p = $python.Split(" "); & $p[0] ($p[1..($p.Length)] + @("-m", "venv", ".venv")) }
}

Step "Installing the packages"
Run ".venv\Scripts\python.exe -m pip install -r requirements.txt" {
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -r (Join-Path $root "requirements.txt")
}

$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Step "Writing .env with a new secret key"
    Run "copy .env.example to .env and set PGDAS_SECRET_KEY" {
        $secret = & $venvPython -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
        $text = Get-Content (Join-Path $root ".env.example") -Raw
        $text = $text -replace "(?m)^PGDAS_SECRET_KEY=.*$", "PGDAS_SECRET_KEY=$secret"
        if ($Sqlite) { $text = $text -replace "(?m)^# PGDAS_APP_DB_ENGINE=postgresql", "PGDAS_APP_DB_ENGINE=sqlite" }
        Set-Content -Path $envFile -Value $text -Encoding UTF8
    }
    if (-not $Sqlite) {
        Write-Host "    Edit .env now: set PGDAS_APP_DB_PASSWORD (and the name/host if yours differ), then run this script again." -ForegroundColor Yellow
        if (-not $DryRun) { exit 0 }
    }
} else {
    Write-Host "    .env already exists: left as it is"
}

Step "Creating or updating PGDAS's own tables"
Run "manage.py migrate" { & $venvPython (Join-Path $root "manage.py") migrate --noinput }

Step "Collecting the static files"
Run "manage.py collectstatic" { & $venvPython (Join-Path $root "manage.py") collectstatic --noinput | Out-Null }

Step "The one-time setup token (open http://127.0.0.1:$Port/setup/ and enter it)"
Run "manage.py installation_token" { & $venvPython (Join-Path $root "manage.py") installation_token }

if ($Tasks) {
    Step "Registering the scheduled tasks (needs an elevated PowerShell)"
    $waitress = Join-Path $root ".venv\Scripts\waitress-serve.exe"
    $manage = Join-Path $root "manage.py"
    $definitions = @(
        @{ Name = "PGDAS server"; Exe = $waitress; Args = "--listen=127.0.0.1:$Port pgdas.wsgi:application"; Trigger = (New-ScheduledTaskTrigger -AtStartup) },
        @{ Name = "PGDAS sampler"; Exe = $venvPython; Args = "`"$manage`" run_watch"; Trigger = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)) },
        @{ Name = "PGDAS backups"; Exe = $venvPython; Args = "`"$manage`" run_backups"; Trigger = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)) }
    )
    foreach ($d in $definitions) {
        Run "register task '$($d.Name)': $($d.Exe) $($d.Args)" {
            $action = New-ScheduledTaskAction -Execute $d.Exe -Argument $d.Args -WorkingDirectory $root
            $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 0)
            Register-ScheduledTask -TaskName $d.Name -Action $action -Trigger $d.Trigger -Settings $settings -RunLevel Highest -Force | Out-Null
            Write-Host "    registered: $($d.Name)"
        }
    }
    Run "start the server task now" { Start-ScheduledTask -TaskName "PGDAS server" }
}

Write-Host ""
Write-Host "Done. PGDAS is at http://127.0.0.1:$Port/" -ForegroundColor Green
if (-not $Tasks) {
    Write-Host "To start it by hand:  .venv\Scripts\waitress-serve.exe --listen=127.0.0.1:$Port pgdas.wsgi:application"
    Write-Host "To start it at boot and run the samplers, run this script again with -Tasks (elevated)."
}
