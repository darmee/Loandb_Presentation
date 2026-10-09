# Builds static/css/app.css from assets/css/app.css with Tailwind CSS.
#
#     powershell -ExecutionPolicy Bypass -File scripts\build_css.ps1          # build once
#     powershell -ExecutionPolicy Bypass -File scripts\build_css.ps1 -Watch   # rebuild on every save
#
# Uses Tailwind's standalone CLI, so nothing here needs Node or npm. The
# binary is downloaded once into tools\ (gitignored - it is about 110 MB).
#
# The built stylesheet IS committed: the server only ever serves static
# files, so deploying never involves this script. Run it after changing
# assets/css/app.css, or after using a Tailwind class in a template or
# script for the first time - Tailwind only emits the utilities it finds.

param([switch]$Watch)

$ErrorActionPreference = "Stop"
$version = "v4.3.3"
$root = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $root "tools\tailwindcss.exe"

if (-not (Test-Path $cli)) {
    New-Item -ItemType Directory -Force (Split-Path -Parent $cli) | Out-Null
    $url = "https://github.com/tailwindlabs/tailwindcss/releases/download/$version/tailwindcss-windows-x64.exe"
    Write-Host "Downloading Tailwind CSS $version (about 110 MB, once)..."
    Invoke-WebRequest -Uri $url -OutFile $cli -UseBasicParsing
}

$cliArgs = @("-i", (Join-Path $root "assets\css\app.css"), "-o", (Join-Path $root "static\css\app.css"), "--minify")
if ($Watch) { $cliArgs += "--watch" }
& $cli @cliArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
