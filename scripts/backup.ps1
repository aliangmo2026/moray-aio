<#
.SYNOPSIS
    Copy a single file into .backup\<yyyyMMdd-HHmmss>-<filename> without touching the original.
.DESCRIPTION
    moray-aio helper: a read-only-minded backup utility. It never modifies,
    truncates or deletes the source file; it only creates a timestamped copy
    under .backup\ in the current working directory.
.EXAMPLE
    .\scripts\backup.ps1 -Path data\memory.sqlite3
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    Write-Error "File not found: $Path"
    exit 1
}

$backupRoot = Join-Path (Get-Location) ".backup"
if (-not (Test-Path -LiteralPath $backupRoot)) {
    New-Item -ItemType Directory -Path $backupRoot | Out-Null
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$fileName = Split-Path -Leaf $Path
$dest = Join-Path $backupRoot ("{0}-{1}" -f $stamp, $fileName)

Copy-Item -LiteralPath $Path -Destination $dest
Write-Output "Backed up: $Path -> $dest"
