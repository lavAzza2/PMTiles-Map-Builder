[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$python = (Get-Command python.exe -ErrorAction Stop).Source
$pmtilesCli = Join-Path $PSScriptRoot 'bin\pmtiles.exe'
if (-not (Test-Path -LiteralPath $pmtilesCli)) {
    throw 'Run Install-PmTiles.ps1 before building the Windows app.'
}
& $python -m pip install --user --upgrade pyinstaller
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name 'THS2 Map Builder' `
    --paths $PSScriptRoot `
    --add-binary "$pmtilesCli;bin" `
    --distpath (Join-Path $PSScriptRoot 'dist') `
    --workpath (Join-Path $env:TEMP 'ths2-map-builder-build') `
    --specpath (Join-Path $env:TEMP 'ths2-map-builder-spec') `
    (Join-Path $PSScriptRoot 'ths2_map_builder.pyw')

Write-Host "Built: $(Join-Path $PSScriptRoot 'dist\THS2 Map Builder\THS2 Map Builder.exe')"
