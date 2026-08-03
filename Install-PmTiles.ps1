[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$destination = Join-Path $PSScriptRoot 'bin'
$executable = Join-Path $destination 'pmtiles.exe'
$headers = @{ 'User-Agent' = 'THS2-Map-Builder' }
$release = Invoke-RestMethod `
    -Uri 'https://api.github.com/repos/protomaps/go-pmtiles/releases/latest' `
    -Headers $headers
$asset = $release.assets |
    Where-Object { $_.name -like '*Windows_x86_64.zip' } |
    Select-Object -First 1
if (-not $asset) {
    throw 'PMTiles CLI for Windows x64 was not found in the latest release.'
}

New-Item -ItemType Directory -Path $destination -Force | Out-Null
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) `
    ('ths2-pmtiles-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $archive = Join-Path $temporary $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -Headers $headers
    Expand-Archive -LiteralPath $archive -DestinationPath $temporary
    $downloaded = Get-ChildItem -LiteralPath $temporary -Recurse -Filter 'pmtiles.exe' |
        Select-Object -First 1
    if (-not $downloaded) {
        throw 'pmtiles.exe was not found in the downloaded archive.'
    }
    Copy-Item -LiteralPath $downloaded.FullName -Destination $executable -Force
    & $executable version
    Write-Host "PMTiles CLI $($release.tag_name) installed: $executable"
}
finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
