<#
.SYNOPSIS
  Build the mpi Electron installer signed with the local code-signing cert.

.DESCRIPTION
  Wires the .pfx created by make-signing-cert.ps1 into electron-builder via the
  standard CSC_LINK / CSC_KEY_PASSWORD environment variables, then runs the normal
  `npm run electron:build`. Assumes dist/data-engine.exe already exists
  (build it with: pyinstaller data-engine.spec).

.PARAMETER Password
  Password for the .pfx. If omitted, read from $env:MPI_CSC_PASSWORD.

.PARAMETER Pfx
  Path to the .pfx. Default: <repo>/certs/mpi-codesign.pfx

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build-signed.ps1 -Password 'S3cret!'
#>
[CmdletBinding()]
param(
    [string]$Password = $env:MPI_CSC_PASSWORD,
    [string]$Pfx
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Pfx)) { $Pfx = Join-Path $repo 'certs\mpi-codesign.pfx' }
if (-not (Test-Path $Pfx)) { throw "PFX not found: $Pfx (run scripts\make-signing-cert.ps1 first)." }
if ([string]::IsNullOrWhiteSpace($Password)) { throw 'Provide -Password or set $env:MPI_CSC_PASSWORD.' }

$env:CSC_LINK = $Pfx
$env:CSC_KEY_PASSWORD = $Password

Push-Location (Join-Path $repo 'frontend')
try {
    npm run electron:build
    if ($LASTEXITCODE -ne 0) { throw "Windows release build failed (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
    Remove-Item Env:\CSC_KEY_PASSWORD -ErrorAction SilentlyContinue
}
