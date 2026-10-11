<#
.SYNOPSIS
  Developer commands for the LLM Spend Control Center (Windows PowerShell).

.EXAMPLE
  .\scripts\dev.ps1 setup        # venv + dependencies + .env
  .\scripts\dev.ps1 up           # Postgres + Redis (Docker)
  .\scripts\dev.ps1 migrate      # alembic upgrade head
  .\scripts\dev.ps1 seed         # example budgets + dashboard demo data
  .\scripts\dev.ps1 serve        # the API on http://127.0.0.1:8000
  .\scripts\dev.ps1 worker       # the verification worker
  .\scripts\dev.ps1 dashboard    # the dashboard on http://localhost:8501
  .\scripts\dev.ps1 test         # ruff + pytest
  .\scripts\dev.ps1 smoke        # all smoke tests (API must be running)
  .\scripts\dev.ps1 simulate     # 1,000-prompt simulation, modes A/B/C (+ sweep)
  .\scripts\dev.ps1 report <id>  # rebuild a simulation report
  .\scripts\dev.ps1 full         # everything in containers (docker compose --profile full)
  .\scripts\dev.ps1 down         # stop containers (data is kept)
#>
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$Arg
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)            # always run from the project root

function Python {
    if (Test-Path ".venv\Scripts\python.exe") { return ".venv\Scripts\python.exe" }
    if (Test-Path "..\.venv\Scripts\python.exe") { return "..\.venv\Scripts\python.exe" }
    return "python"
}
$py = Python

function Run([string[]]$cmd) {
    Write-Host "> $($cmd -join ' ')" -ForegroundColor Cyan
    & $cmd[0] $cmd[1..($cmd.Length - 1)]
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $($cmd -join ' ')" }
}

switch ($Command) {
    "setup" {
        if (-not (Test-Path ".venv") -and -not (Test-Path "..\.venv")) { Run @("python", "-m", "venv", ".venv"); $py = Python }
        Run @($py, "-m", "pip", "install", "-r", "requirements.txt")
        if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env from .env.example" }
    }
    "up"        { Run @("docker", "compose", "up", "-d"); Run @("docker", "compose", "ps") }
    "down"      { Run @("docker", "compose", "--profile", "full", "down") }
    "migrate"   { Run @($py, "-m", "alembic", "upgrade", "head") }
    "seed"      { Run @($py, "scripts/seed_budgets.py"); Run @($py, "scripts/seed_demo_data.py") }
    "serve"     { Run @($py, "-m", "uvicorn", "app.main:app", "--reload") }
    "worker"    { Run @($py, "-m", "app.worker") }
    "dashboard" { Run @($py, "-m", "streamlit", "run", "dashboard/app.py") }
    "test"      { Run @($py, "-m", "ruff", "check", "."); Run @($py, "-m", "pytest", "-q") }
    "smoke" {
        foreach ($s in "smoke_test", "smoke_routing", "smoke_quality", "smoke_dashboard") {
            Run @($py, "scripts/$s.py")
        }
    }
    "simulate"  { Run @($py, "scripts/run_simulation.py", "--sweep", "0.05,0.25,0.5") }
    "report" {
        if (-not $Arg) { throw "usage: .\scripts\dev.ps1 report <run_id>" }
        Run @($py, "scripts/build_report.py", $Arg)
    }
    "full"      { Run @("docker", "compose", "--profile", "full", "up", "-d", "--build"); Run @("docker", "compose", "--profile", "full", "ps") }
    default {
        Get-Help $PSCommandPath -Examples | Out-String | Write-Host
    }
}
