param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$destinationPath = [IO.Path]::GetFullPath($Destination)
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
