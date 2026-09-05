$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$version = "1.1.0"

$venv = Join-Path $PSScriptRoot ".venv-build"
if (-not (Test-Path $venv)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv $venv
    } else {
        python -m venv $venv
    }
}

$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements-windows.txt
& $python -m PyInstaller --noconfirm --clean BreathMonitor.spec

$app = Join-Path $PSScriptRoot "dist\BreathMonitor\BreathMonitor.exe"
$smoke = Start-Process -FilePath $app -ArgumentList "--self-test" -Wait -PassThru
if ($smoke.ExitCode -ne 0) {
    throw "Packaged self-test failed with exit code $($smoke.ExitCode)."
}

$release = Join-Path $PSScriptRoot "release"
New-Item -ItemType Directory -Force -Path $release | Out-Null
$portable = Join-Path $release "BreathMonitor-Portable-$version.zip"
if (Test-Path $portable) { Remove-Item $portable }
Compress-Archive -Path "dist\BreathMonitor\*" -DestinationPath $portable -CompressionLevel Optimal

$isccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc "installer\BreathMonitor.iss"
    Write-Host "Installer and portable package are in: $release"
} else {
    Write-Warning "Inno Setup 6 was not found. Portable ZIP was built; install Inno Setup and rerun for the installer."
}
