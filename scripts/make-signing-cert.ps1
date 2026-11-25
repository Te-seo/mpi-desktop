<#
.SYNOPSIS
  Create a self-signed Authenticode code-signing certificate for mpi and
  export it to certs/ as a .pfx (private, for signing) and .cer (public, for trust).

.DESCRIPTION
  A self-signed certificate lets electron-builder produce a *signed* installer so
  the publisher name is consistent and the binary is tamper-evident. It does NOT
  remove the Windows SmartScreen / "Unknown publisher" warning — that requires a
  certificate from a trusted CA (ideally an OV/EV code-signing cert). To make the
  signature trusted on a specific machine, import the .cer into
  "Trusted Root Certification Authorities" and "Trusted Publishers".

.PARAMETER Subject
  Certificate subject / publisher common name. Default: CN=mpi

.PARAMETER Password
  Password protecting the exported .pfx. If omitted, read from
  $env:MPI_CSC_PASSWORD.

.PARAMETER OutDir
  Output directory for the .pfx/.cer. Default: <repo>/certs

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\make-signing-cert.ps1 -Password 'S3cret!'
#>
[CmdletBinding()]
param(
    [string]$Subject = 'CN=mpi',
    [string]$Password = $env:MPI_CSC_PASSWORD,
    [string]$OutDir
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Password)) {
    throw 'Provide -Password or set $env:MPI_CSC_PASSWORD.'
}

$repo = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Join-Path $repo 'certs' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject $Subject `
    -KeyAlgorithm RSA -KeyLength 2048 `
    -HashAlgorithm SHA256 `
    -KeyExportPolicy Exportable `
    -KeyUsage DigitalSignature `
    -FriendlyName 'mpi code signing (self-signed)' `
    -CertStoreLocation 'Cert:\CurrentUser\My' `
    -NotAfter (Get-Date).AddYears(3)

$pfx = Join-Path $OutDir 'mpi-codesign.pfx'
$cer = Join-Path $OutDir 'mpi-codesign.cer'
$securePwd = ConvertTo-SecureString -String $Password -Force -AsPlainText

Export-PfxCertificate -Cert "Cert:\CurrentUser\My\$($cert.Thumbprint)" -FilePath $pfx -Password $securePwd | Out-Null
Export-Certificate  -Cert "Cert:\CurrentUser\My\$($cert.Thumbprint)" -FilePath $cer | Out-Null

Write-Host "Thumbprint : $($cert.Thumbprint)"
Write-Host "Subject    : $($cert.Subject)"
Write-Host "Expires    : $($cert.NotAfter)"
Write-Host "PFX (keep secret) : $pfx"
Write-Host "CER (public)      : $cer"
