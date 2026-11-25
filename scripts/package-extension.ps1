[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ElectronOutputDirectory,
    [string] $ReleaseDirectory = '',
    [string] $Version = '',
    [string] $MinimumHostVersion = '0.5.15'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$extensionId = 'data-extract-tracing'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$packageMetadata = Get-Content -LiteralPath (Join-Path $repositoryRoot 'frontend/package.json') -Raw | ConvertFrom-Json
$expectedVersion = [string] $packageMetadata.version
if (-not $Version) { $Version = $expectedVersion }
# The one-release PerfInspect.exe compatibility alias ended with 2026.7.27-1
# (see frontend/scripts/after-pack-alias.cjs); it is no longer allowed here.
$allowedTopLevelFiles = @(
    'MPI.exe', 'chrome_100_percent.pak', 'chrome_200_percent.pak',
    'd3dcompiler_47.dll', 'dxcompiler.dll', 'dxil.dll', 'ffmpeg.dll',
    'icudtl.dat', 'libEGL.dll', 'libGLESv2.dll', 'LICENSE.electron.txt',
    'LICENSES.chromium.html', 'resources.pak', 'snapshot_blob.bin',
    'v8_context_snapshot.bin', 'version', 'vk_swiftshader.dll',
    'vk_swiftshader_icd.json', 'vulkan-1.dll'
)
$allowedTopLevelDirectories = @('locales', 'resources')
$allowedResourceFiles = @(
    'app.asar', 'companion.apk', 'data-engine.exe', 'default_app.asar',
    'elevate.exe',
    'ios-tunnel.ps1', 'LICENSE.txt', 'THIRD-PARTY-NOTICES.txt'
)
$forbiddenNames = @('.env', 'node_modules', '__pycache__')

if (-not $ReleaseDirectory) {
    $ReleaseDirectory = Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) 'release') $Version
}

function Assert-SafeVersion([string] $Value) {
    if ($Value -notmatch '^\d{4}\.\d{1,2}\.\d{1,2}(?:-\d+)?$') {
        throw "Version must use YYYY.M.D or YYYY.M.D-revision format: $Value"
    }
    if ($Value -ne $expectedVersion) {
        throw "Package version $Value does not match repository version $expectedVersion."
    }
}

function Assert-ChildPath([string] $Candidate, [string] $Root) {
    $candidateFull = [System.IO.Path]::GetFullPath($Candidate)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $candidateFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes the allowed root: $Candidate"
    }
}

function New-PortableArchive([string] $SourceDirectory, [string] $DestinationPath) {
    Add-Type -AssemblyName System.IO.Compression
    $sourceFull = [System.IO.Path]::GetFullPath($SourceDirectory).TrimEnd('\')
    $archiveStream = [System.IO.File]::Open(
        $DestinationPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $archive = [System.IO.Compression.ZipArchive]::new(
            $archiveStream,
            [System.IO.Compression.ZipArchiveMode]::Create,
            $false
        )
        try {
            foreach ($file in Get-ChildItem -LiteralPath $sourceFull -File -Recurse -Force) {
                Assert-ChildPath $file.FullName $sourceFull
                $relative = $file.FullName.Substring($sourceFull.Length + 1).Replace('\', '/')
                $entryName = (Split-Path -Leaf $sourceFull) + '/' + $relative
                $entry = $archive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
                $entryStream = $entry.Open()
                $inputStream = [System.IO.File]::OpenRead($file.FullName)
                try {
                    $inputStream.CopyTo($entryStream)
                } finally {
                    $inputStream.Dispose()
                    $entryStream.Dispose()
                }
            }
        } finally {
            $archive.Dispose()
        }
    } finally {
        $archiveStream.Dispose()
    }
}

Assert-SafeVersion $Version
$source = (Resolve-Path -LiteralPath $ElectronOutputDirectory).Path
if ((Split-Path -Leaf $source) -ne 'win-unpacked') {
    throw "ElectronOutputDirectory must be a win-unpacked directory: $source"
}
$sourceParent = Split-Path -Parent $source
if ((Split-Path -Leaf $sourceParent) -ne "build_output_$Version") {
    throw "Expected frontend\\build_output_$Version\\win-unpacked, got: $source"
}

$sourceItems = @(Get-ChildItem -LiteralPath $source -Force)
foreach ($item in $sourceItems) {
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Reparse points are not allowed: $($item.FullName)"
    }
    if ($item.PSIsContainer) {
        if ($item.Name -notin $allowedTopLevelDirectories) {
            throw "Unexpected top-level directory: $($item.Name)"
        }
    } elseif ($item.Name -notin $allowedTopLevelFiles) {
        throw "Unexpected top-level file: $($item.Name)"
    }
}

$requiredFiles = @(
    (Join-Path $source 'MPI.exe'),
    (Join-Path $source 'resources\app.asar'),
    (Join-Path $source 'resources\data-engine.exe'),
    (Join-Path $source 'resources\LICENSE.txt'),
    (Join-Path $source 'resources\THIRD-PARTY-NOTICES.txt')
)
foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required runtime file is missing: $requiredFile"
    }
}
foreach ($item in Get-ChildItem -LiteralPath $source -Recurse -Force) {
    Assert-ChildPath $item.FullName $source
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Reparse points are not allowed: $($item.FullName)"
    }
    if ($item.Name -in $forbiddenNames -or $item.Name -like '.env.*') {
        throw "Forbidden package content found: $($item.FullName)"
    }
}
$resourceRoot = Join-Path $source 'resources'
foreach ($item in Get-ChildItem -LiteralPath $resourceRoot -Force) {
    if ($item.PSIsContainer -or $item.Name -notin $allowedResourceFiles) {
        throw "Unexpected resources content: $($item.FullName)"
    }
}
$localeRoot = Join-Path $source 'locales'
foreach ($item in Get-ChildItem -LiteralPath $localeRoot -Force) {
    if ($item.PSIsContainer -or $item.Extension -ne '.pak') {
        throw "Unexpected locale content: $($item.FullName)"
    }
}

$releaseRoot = [System.IO.Path]::GetFullPath($ReleaseDirectory)
if (-not (Test-Path -LiteralPath $releaseRoot)) {
    New-Item -ItemType Directory -Path $releaseRoot | Out-Null
}
$releaseRoot = (Resolve-Path -LiteralPath $releaseRoot).Path
$artifactName = "$extensionId-$Version.zip"
$manifestName = "$extensionId-$Version.manifest.json"
$artifactPath = Join-Path $releaseRoot $artifactName
$manifestPath = Join-Path $releaseRoot $manifestName
$checksumPath = Join-Path $releaseRoot 'SHA256SUMS.txt'
$releaseNotesPath = Join-Path $releaseRoot 'RELEASE_NOTES.md'
$targets = @($artifactPath, $manifestPath, $checksumPath, $releaseNotesPath)
foreach ($target in $targets) {
    Assert-ChildPath $target $releaseRoot
    if (Test-Path -LiteralPath $target) {
        throw "Refusing to overwrite existing release output: $target"
    }
}

$stagingRoot = Join-Path $releaseRoot ('.staging-' + [Guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $stagingRoot $extensionId
$verificationRoot = Join-Path $releaseRoot ('.verify-' + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $packageRoot | Out-Null
    foreach ($item in $sourceItems) {
        Copy-Item -LiteralPath $item.FullName -Destination $packageRoot -Recurse
    }
    [ordered]@{
        schemaVersion = 1; id = $extensionId; version = $Version
        entrypoint = "$extensionId/MPI.exe"
        workingDirectory = $extensionId; platform = 'win32'; arch = 'x64'
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $packageRoot 'extension.runtime.json') -Encoding utf8

    New-PortableArchive $packageRoot $artifactPath
    Expand-Archive -LiteralPath $artifactPath -DestinationPath $verificationRoot
    $verifiedPackageRoot = Join-Path $verificationRoot $extensionId
    $verifiedEntrypoint = Join-Path $verifiedPackageRoot 'MPI.exe'
    if (-not (Test-Path -LiteralPath $verifiedEntrypoint -PathType Leaf)) {
        throw "Archive verification failed: entrypoint is missing."
    }
    foreach ($item in Get-ChildItem -LiteralPath $verificationRoot -Recurse -Force) {
        Assert-ChildPath $item.FullName $verificationRoot
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "Archive verification failed: reparse point found: $($item.FullName)"
        }
        if ($item.Name -in $forbiddenNames -or $item.Name -like '.env.*') {
            throw "Archive verification failed: forbidden content found: $($item.FullName)"
        }
    }
    $artifact = Get-Item -LiteralPath $artifactPath
    $artifactHash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [ordered]@{
        schemaVersion = 1; id = $extensionId; version = $Version
        artifact = $artifactName; size = $artifact.Length; sha256 = $artifactHash
        entrypoint = "$extensionId/MPI.exe"; workingDirectory = $extensionId
        platform = 'win32'; arch = 'x64'; minimumHostVersion = $MinimumHostVersion
        publishedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    } | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8

    $manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    @("$artifactHash  $artifactName", "$manifestHash  $manifestName") |
        Set-Content -LiteralPath $checksumPath -Encoding ascii
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '..\RELEASE_NOTES.md') -Destination $releaseNotesPath
    Write-Host "Created $artifactPath ($($artifact.Length) bytes, SHA-256 $artifactHash)"
} catch {
    foreach ($target in $targets) {
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
    }
    throw
} finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $verificationRoot) {
        Remove-Item -LiteralPath $verificationRoot -Recurse -Force
    }
}
