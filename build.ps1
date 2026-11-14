# Builds dist\valset.exe and dist\valset-setup.exe.
#   .\build.ps1            exe + setup
#   .\build.ps1 -NoSetup   exe only
# Needs Python 3.10+. PyInstaller goes into a private .build-venv; the setup needs Inno Setup 6
# (https://jrsoftware.org/isdl.php). The version comes from VERSION in valset.py.
param([switch]$NoSetup)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = (Select-String -Path valset.py -Pattern '^VERSION = "(.+?)"').Matches[0].Groups[1].Value
$nums = @(($version -split '-')[0].Split('.') | ForEach-Object { [int]$_ }) + @(0, 0, 0, 0)
$tuple = ($nums[0..3] -join ", ")
Write-Host "valset $version" -ForegroundColor Cyan

# PyInstaller in its own venv, so the build doesn't touch your Python
$py = "python"
if ($env:CI -ne "true") {
    if (-not (Test-Path .build-venv\Scripts\python.exe)) { python -m venv .build-venv }
    $py = (Resolve-Path .build-venv\Scripts\python.exe).Path
}
& $py -m pip install --disable-pip-version-check -q "pyinstaller==6.22.3"
if ($LASTEXITCODE) { throw "pip install failed" }

# name, version and description shown in the exe's Properties and in Task Manager
New-Item -ItemType Directory -Force build | Out-Null
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($tuple), prodvers=($tuple), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'yksaionara'),
      StringStruct('FileDescription', 'valset - VALORANT tracker and settings saver'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'valset'),
      StringStruct('OriginalFilename', 'valset.exe'),
      StringStruct('ProductName', 'valset'),
      StringStruct('ProductVersion', '$version'),
      StringStruct('LegalCopyright', 'Fan project, not made or endorsed by Riot Games')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content build\version_info.txt -Encoding utf8

$root = (Get-Location).Path
& $py -m PyInstaller --noconfirm --clean --onefile --console --name valset `
    --icon "$root\assets\valset.ico" --version-file "$root\build\version_info.txt" `
    --distpath "$root\dist" --workpath "$root\build\pyinstaller" --specpath "$root\build" `
    --log-level WARN "$root\valset.py"
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$out = & .\dist\valset.exe --version
if ($out -ne "valset $version") { throw "dist\valset.exe says '$out', expected 'valset $version'" }
Write-Host "built dist\valset.exe" -ForegroundColor Green

if ($NoSetup) { return }
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { $iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source }
if (-not $iscc) { throw "Inno Setup 6 isn't installed (https://jrsoftware.org/isdl.php). Use -NoSetup for just the exe." }
& $iscc /Q "/DAppVersion=$version" installer\valset.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }
Write-Host "built dist\valset-setup.exe" -ForegroundColor Green
