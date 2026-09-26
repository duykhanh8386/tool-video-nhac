param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [switch]$Download
)

$ErrorActionPreference = 'Stop'
$destinationPath = [IO.Path]::GetFullPath($Destination)

if ($Download) {
    $archiveUrl = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
    $checksumUrl = "$archiveUrl.sha256"
    $temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("visual-loop-ffmpeg-" + [Guid]::NewGuid().ToString('N'))
    $archivePath = Join-Path $temporaryRoot 'ffmpeg.zip'
    $checksumPath = Join-Path $temporaryRoot 'ffmpeg.zip.sha256'
    $extractPath = Join-Path $temporaryRoot 'extracted'
    New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $archiveUrl -OutFile $archivePath
        Invoke-WebRequest -UseBasicParsing -Uri $checksumUrl -OutFile $checksumPath
        $expectedHash = (Get-Content -Raw -LiteralPath $checksumPath).Trim().Split()[0].ToLowerInvariant()
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
        if ($expectedHash -notmatch '^[0-9a-f]{64}$' -or $actualHash -ne $expectedHash) {
            throw 'Downloaded FFmpeg archive failed SHA-256 verification.'
        }
        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath -Force
        $ffmpegFile = Get-ChildItem -LiteralPath $extractPath -Filter ffmpeg.exe -Recurse -File | Select-Object -First 1
        if (-not $ffmpegFile) { throw 'The verified FFmpeg archive does not contain ffmpeg.exe.' }
        $ffprobeFile = Join-Path $ffmpegFile.DirectoryName 'ffprobe.exe'
        if (-not (Test-Path -LiteralPath $ffprobeFile)) { throw 'The verified FFmpeg archive does not contain ffprobe.exe.' }
        New-Item -ItemType Directory -Force -Path $destinationPath | Out-Null
        Copy-Item -LiteralPath $ffmpegFile.FullName -Destination (Join-Path $destinationPath 'ffmpeg.exe') -Force
        Copy-Item -LiteralPath $ffprobeFile -Destination (Join-Path $destinationPath 'ffprobe.exe') -Force
        Write-Host "Downloaded and verified FFmpeg: $actualHash"
    }
    finally {
        if (Test-Path -LiteralPath $temporaryRoot) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        }
    }
    return
}

$candidateDirectories = [Collections.Generic.List[string]]::new()

Get-Command ffmpeg.exe -All -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Source) {
        $candidateDirectories.Add([IO.Path]::GetDirectoryName($_.Source))
    }
}

$searchRoots = @(
    $(if ($env:ChocolateyInstall) { Join-Path $env:ChocolateyInstall 'lib' }),
    'C:\tools',
    'C:\ffmpeg'
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

foreach ($searchRoot in $searchRoots) {
    Get-ChildItem -LiteralPath $searchRoot -Filter ffmpeg.exe -Recurse -File -ErrorAction SilentlyContinue |
        ForEach-Object { $candidateDirectories.Add($_.DirectoryName) }
}

$sourceDirectory = $null
foreach ($directory in $candidateDirectories | Select-Object -Unique) {
    if (-not $directory) { continue }
    $ffmpeg = Join-Path $directory 'ffmpeg.exe'
    $ffprobe = Join-Path $directory 'ffprobe.exe'
    if ((Test-Path -LiteralPath $ffmpeg) -and (Test-Path -LiteralPath $ffprobe)) {
        # Chocolatey command shims are only a few KB and are not portable.
        if ((Get-Item -LiteralPath $ffmpeg).Length -gt 1MB -and (Get-Item -LiteralPath $ffprobe).Length -gt 1MB) {
            $sourceDirectory = $directory
            break
        }
    }
}

if (-not $sourceDirectory) {
    throw 'Could not find portable ffmpeg.exe and ffprobe.exe binaries (Chocolatey shims are intentionally rejected).'
}

New-Item -ItemType Directory -Force -Path $destinationPath | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceDirectory 'ffmpeg.exe') -Destination (Join-Path $destinationPath 'ffmpeg.exe') -Force
Copy-Item -LiteralPath (Join-Path $sourceDirectory 'ffprobe.exe') -Destination (Join-Path $destinationPath 'ffprobe.exe') -Force
Write-Host "Bundled FFmpeg from: $sourceDirectory"
