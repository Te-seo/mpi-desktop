<#
.SYNOPSIS
  Build the MPI Electron installer signed with a TRUSTED-CA code-signing cert that
  lives in the Windows cert store (Certum "Standard Code Signing in the Cloud" via
  SimplySign, or any store-resident OV/EV cert). Removes the basis for the Windows
  SmartScreen "unknown publisher" warning (reputation then builds over time).

.DESCRIPTION
  Cloud / token code-signing certs have NO exportable .pfx — the private key stays
  in the cloud HSM (or token). So unlike scripts\build-signed.ps1 (which feeds a
  .pfx via CSC_LINK), this picks the cert from `Cert:\CurrentUser\My` and tells
  electron-builder to sign with it by SHA-1 thumbprint:
     electron-builder --win -c.win.signtoolOptions.certificateSha1=<thumb>

  PRECONDITION: a SimplySign Desktop session must be ACTIVE (authenticate with the
  mobile one-time token; the session lasts ~2 hours). While active, the cloud cert
  is mounted into Cert:\CurrentUser\My and signtool/electron-builder can use it.
  See docs\desktop-code-signing.md.

  Assumes dist\data-engine.exe already exists (pyinstaller data-engine.spec).

.PARAMETER Thumbprint
  SHA-1 thumbprint of the signing cert. If omitted, the script auto-detects a
  single trusted (non-self-signed) code-signing cert; if several exist it lists
  them and asks you to pass -Thumbprint or -SubjectName.

.PARAMETER SubjectName
  Substring of the cert Subject (e.g. your 상호) to disambiguate when multiple
  code-signing certs are present.

.PARAMETER TimeStampServer
  RFC3161 timestamp server. Default: http://time.certum.pl

.PARAMETER AllowSelfSigned
  Permit selecting a self-signed cert (issuer == subject). Off by default so this
  script never silently falls back to the old CN=perfinspect self-signed cert.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build-signed-certum.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build-signed-certum.ps1 -Thumbprint A1B2C3...
#>
[CmdletBinding()]
param(
    [string]$Thumbprint,
    [string]$SubjectName,
    [string]$TimeStampServer = 'http://time.certum.pl',
    [switch]$AllowSelfSigned
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Is-SelfSigned($c) { return $c.Subject -eq $c.Issuer }

# --- Locate the signing certificate in the user's store -----------------------
$certs = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue

if (-not $certs -or $certs.Count -eq 0) {
    throw @'
No code-signing certificate found in Cert:\CurrentUser\My.
If using Certum cloud (SimplySign): open SimplySign Desktop and authenticate with
the mobile one-time token first (the cert is only mounted while the session is
active). Then re-run this script. See docs\desktop-code-signing.md.
'@
}

if ($Thumbprint) {
    $cert = $certs | Where-Object { $_.Thumbprint -ieq ($Thumbprint -replace '\s','') }
    if (-not $cert) { throw "No code-signing cert with thumbprint '$Thumbprint' in Cert:\CurrentUser\My." }
}
elseif ($SubjectName) {
    $cert = $certs | Where-Object { $_.Subject -like "*$SubjectName*" }
    if (-not $cert) { throw "No code-signing cert whose Subject contains '$SubjectName'." }
    if ($cert.Count -gt 1) { throw "Multiple certs match Subject '$SubjectName'. Use -Thumbprint." }
}
else {
    $candidates = if ($AllowSelfSigned) { $certs } else { $certs | Where-Object { -not (Is-SelfSigned $_) } }
    if (-not $candidates) {
        throw "Only self-signed code-signing cert(s) found (e.g. CN=perfinspect). Issue/mount a trusted-CA cert, or pass -AllowSelfSigned to use a self-signed one anyway."
    }
    if (@($candidates).Count -gt 1) {
        Write-Host 'Multiple code-signing certificates found — choose one with -Thumbprint or -SubjectName:' -ForegroundColor Yellow
        $candidates | Select-Object Subject, Thumbprint, NotAfter | Format-Table -AutoSize | Out-String | Write-Host
        throw 'Ambiguous certificate selection.'
    }
    $cert = $candidates
}

# Normalize to a single cert object
$cert = @($cert)[0]

if ((Is-SelfSigned $cert) -and -not $AllowSelfSigned) {
    throw "Selected cert is self-signed ($($cert.Subject)) — that will NOT clear SmartScreen. Pass -AllowSelfSigned only if you really mean it."
}
if ($cert.NotAfter -lt (Get-Date)) { throw "Selected cert expired on $($cert.NotAfter)." }

Write-Host "Signing with:" -ForegroundColor Cyan
Write-Host "  Subject    : $($cert.Subject)"
Write-Host "  Issuer     : $($cert.Issuer)"
Write-Host "  Thumbprint : $($cert.Thumbprint)"
Write-Host "  Expires    : $($cert.NotAfter)"
Write-Host "  Timestamp  : $TimeStampServer"

if (-not (Test-Path (Join-Path $repo 'dist\data-engine.exe'))) {
    Write-Warning "dist\data-engine.exe not found — build it first (pyinstaller data-engine.spec) or the installer will ship without the engine."
}

# --- Build + sign via electron-builder (store cert by thumbprint) -------------
Push-Location (Join-Path $repo 'frontend')
try {
    node ../scripts/check-windows-no-apk.mjs --notices
    if ($LASTEXITCODE -ne 0) { throw "Windows license/build proof failed (exit $LASTEXITCODE)." }
    npm run build:renderer
    if ($LASTEXITCODE -ne 0) { throw "frontend build failed (exit $LASTEXITCODE)." }

    npx electron-builder --win --publish never `
        "-c.win.signtoolOptions.certificateSha1=$($cert.Thumbprint)" `
        "-c.win.signtoolOptions.rfc3161TimeStampServer=$TimeStampServer"
    if ($LASTEXITCODE -ne 0) { throw "electron-builder failed (exit $LASTEXITCODE). Is the SimplySign session still active (2h window)?" }
}
finally {
    Pop-Location
}

Write-Host "`nDone. Verify the signature:" -ForegroundColor Green
Write-Host '  Get-AuthenticodeSignature "frontend\build_output\MPI Setup <ver>.exe" | Format-List Status, SignerCertificate'
Write-Host '  (Status must be Valid — chains to a trusted root.)'
