param(
    [string]$Version = '1.0.0.0'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

if (-not (Test-Path '.build-venv\Scripts\python.exe')) {
    python -m venv .build-venv
}
& '.build-venv\Scripts\python.exe' -m pip install --upgrade pip
& '.build-venv\Scripts\python.exe' -m pip install -r 'visual_loop_studio\requirements.txt'
# Keep local artifacts compatible with the updater bridge used by CI. Once a
# release containing PYINSTALLER_RESET_ENVIRONMENT is widely installed, this
# can be moved back to PyInstaller 6.16 or newer.
& '.build-venv\Scripts\python.exe' -m pip install pyinstaller==6.8.0

& 'tools\bundle_ffmpeg.ps1' -Destination 'visual_loop_studio\vendor\bin'

$versionFile = Join-Path $root 'visual_loop_studio\version.py'
$originalVersionFile = Get-Content -Raw -LiteralPath $versionFile
try {
    & '.build-venv\Scripts\python.exe' 'tools\stamp_version.py' --version $Version --commit local
    & '.build-venv\Scripts\pyinstaller.exe' --noconfirm --clean --distpath dist --workpath .pyinstaller 'build\windows.spec'
}
finally {
    [IO.File]::WriteAllText($versionFile, $originalVersionFile, [Text.UTF8Encoding]::new($false))
}
Write-Host "Built: $root\dist\VisualLoopStudio-Windows-x64.exe"
