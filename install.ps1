# moray-aio one-command installer (Windows PowerShell).
# Creates .venv, installs dependencies and initializes .env from the example file.
# Usage:  powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "== moray-aio installer ==" -ForegroundColor Green

# 1) virtual environment
if (-not (Test-Path ".venv")) {
    Write-Host "[1/3] creating virtual environment .venv ..." -ForegroundColor Cyan
    python -m venv .venv
} else {
    Write-Host "[1/3] .venv already exists, reusing" -ForegroundColor DarkGray
}

# 2) dependencies
Write-Host "[2/3] installing dependencies ..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 3) local config (secrets come from environment variables - never committed)
if (-not (Test-Path ".env")) {
    Write-Host "[3/3] initializing .env from .env.example" -ForegroundColor Cyan
    Copy-Item ".env.example" ".env"
} else {
    Write-Host "[3/3] .env already exists, keeping it" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "done. next steps:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  make dev            # or: python -m uvicorn app.main:app --port 8020"
Write-Host "  python cli.py health"
