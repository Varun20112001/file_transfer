$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Create the project virtual environment first: $venvPython was not found."
}

Push-Location $projectRoot
try {
    & $venvPython -m pip install -r requirements.txt pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'Installing SBM packaging dependencies failed.' }

    $bundleArgs = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
        '--name', 'SBM', '--distpath', 'dist', '--workpath', 'build\pyinstaller',
        '--icon', 'static\images\sbm-app.ico',
        '--add-data', 'templates;templates',
        '--add-data', 'static;static',
        '--add-data', 'migrations;migrations',
        '--hidden-import', 'logging.config',
        '--collect-all', 'pillow_heif',
        '--collect-all', 'pystray',
        '--collect-all', 'qrcode',
        '--collect-all', 'flask_migrate',
        '--collect-all', 'flask_sqlalchemy',
        '--collect-all', 'sqlalchemy',
        '--collect-all', 'alembic',
        '--collect-all', 'psycopg',
        '--collect-all', 'psutil',
        'launcher.py'
    )
    & $venvPython @bundleArgs
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed to build SBM.' }

    $bundleDir = Join-Path $projectRoot 'dist\SBM'
    Set-Content -LiteralPath (Join-Path $bundleDir 'portable.mode') -Value 'SBM portable mode' -Encoding ascii
    @'
SBM Portable

Run SBM.exe. It stays in the Windows notification area (system tray).
Right-click the tray icon for Open admin, Show phone QR code, or Exit SBM.
The first run opens the one-time administrator setup dialog.

The data folder is created beside SBM.exe. Keep it when updating SBM; it contains
your accounts, audit history, secret key, and cached thumbnails. Completed and
partial transfers remain in the configured SBM_SAVE_DIR (default:
D:\Documents\transfer).
'@ | Set-Content -LiteralPath (Join-Path $bundleDir 'README-Portable.txt') -Encoding utf8

    $portableZip = Join-Path $projectRoot 'dist\SBM-Portable.zip'
    if (Test-Path -LiteralPath $portableZip) { Remove-Item -LiteralPath $portableZip -Force }
    Compress-Archive -Path $bundleDir -DestinationPath $portableZip -CompressionLevel Optimal
    Write-Host "Portable package created: $portableZip"
} finally {
    Pop-Location
}
