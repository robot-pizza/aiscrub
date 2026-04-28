# Build a local PyInstaller binary and zip it. Mirrors the release workflow.
$ErrorActionPreference = "Stop"

$asset = "aiscrub-windows-x86_64"

uv sync --extra build
uv run pyinstaller --onefile --name aiscrub --console aiscrub.py

if (Test-Path staging) { Remove-Item -Recurse -Force staging }
New-Item -ItemType Directory -Path staging | Out-Null

Copy-Item dist\aiscrub.exe staging\aiscrub.exe
if (Test-Path README.md) { Copy-Item README.md staging\ }
if (Test-Path LICENSE)   { Copy-Item LICENSE   staging\ }

if (Test-Path "$asset.zip") { Remove-Item "$asset.zip" }
Compress-Archive -Path staging\* -DestinationPath "$asset.zip"

Write-Output ""
Write-Output "built: $asset.zip"
