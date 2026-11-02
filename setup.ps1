<#
.SYNOPSIS
  One-time setup of the meeting bot on native Windows 10/11 (the `windows` branch).

.DESCRIPTION
  Run in PowerShell, as YOUR user (not "as administrator"), from the repo folder:

      powershell -ExecutionPolicy Bypass -File .\setup.ps1

  It installs, with winget where possible:
    Git for Windows (its bash runs the pipeline's scripts), ffmpeg, Node.js LTS
    (pm2, for the web UI), uv (Python 3.13 and the project venv), deno
    (yt-dlp's JavaScript runtime), poppler (PDF slides), MSYS2 + Pango (the
    PDF renderer's text engine), Firefox ESR into its OWN folder
    (%LOCALAPPDATA%\meeting-bot\firefox-esr, so Windows' per-app sound setting
    for it never touches your own Firefox), geckodriver and yt-dlp, the PDF
    fonts (Bai Jamjuree, Sarabun, CMU Serif, JetBrains Mono) for your user,
    pm2, and a .env with a fresh web UI token.

  It does NOT install VB-Audio Virtual Cable (a driver: it needs an
  administrator and a restart) — it tells you how. See README "Windows".

  Idempotent: re-running is safe and refreshes yt-dlp.

.PARAMETER WithChrome
  Also install Google Chrome (MEETING_BROWSER=chrome; Firefox ESR stays the default).
.PARAMETER NoBrowser
  Skip the browser and its drivers (a PC that only transcribes and summarizes).
#>
param([switch]$WithChrome, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Data = Join-Path $env:LOCALAPPDATA "meeting-bot"
$Bin  = Join-Path $Data "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Note($msg) { Write-Host "    $msg" }

function Add-UserPath($dir) {
  $cur = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not ($cur -split ";" | Where-Object { $_ -eq $dir })) {
    [Environment]::SetEnvironmentVariable("Path", ($cur.TrimEnd(";") + ";" + $dir), "User")
  }
  if (-not ($env:Path -split ";" | Where-Object { $_ -eq $dir })) { $env:Path = "$env:Path;$dir" }
}

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path", "User")
}

function Winget-Install($id) {
  # Already installed is fine (winget then says "no applicable upgrade").
  winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements --scope user *> $null
  if ($LASTEXITCODE -ne 0) {
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements *> $null
  }
  Note "$id (winget exit $LASTEXITCODE)"
}

function Download($url, $dest) {
  Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $dest
}

if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
      [Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Warning "Running as administrator: everything below belongs in YOUR account. Re-run from a normal PowerShell if you can."
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  throw "winget (App Installer, from the Microsoft Store) is required."
}

Say "Packages (winget)"
foreach ($id in @("Git.Git", "Gyan.FFmpeg", "OpenJS.NodeJS.LTS", "astral-sh.uv",
                  "DenoLand.Deno", "oschwartz10612.Poppler", "MSYS2.MSYS2")) {
  Winget-Install $id
}
if ($WithChrome) { Winget-Install "Google.Chrome" }
Refresh-Path
Add-UserPath $Bin

Say "Pango for the PDF renderer (MSYS2)"
# WeasyPrint has no text engine of its own on Windows; it loads Pango's DLLs
# from WEASYPRINT_DLL_DIRECTORIES (written into .env below).
$Msys = "C:\msys64"
if (-not (Test-Path "$Msys\usr\bin\bash.exe")) { $Msys = Join-Path $env:LOCALAPPDATA "msys64" }
if (Test-Path "$Msys\usr\bin\bash.exe") {
  & "$Msys\usr\bin\bash.exe" -lc "pacman -S --noconfirm --needed mingw-w64-ucrt-x86_64-pango" | Out-Null
  $PangoDir = "$Msys\ucrt64\bin"
  Note "Pango in $PangoDir"
} else {
  Write-Warning "MSYS2 was not found; PDFs will not render until Pango is installed (README: Windows)."
  $PangoDir = ""
}

if (-not $NoBrowser) {
  $FirefoxDir = Join-Path $Data "firefox-esr"
  if (-not (Test-Path "$FirefoxDir\firefox.exe")) {
    Say "Firefox ESR -> $FirefoxDir (its own folder, not your Firefox)"
    $inst = Join-Path $env:TEMP "firefox-esr-setup.exe"
    Download "https://download.mozilla.org/?product=firefox-esr-latest-ssl&os=win64&lang=en-US" $inst
    Start-Process -Wait -FilePath $inst -ArgumentList @("/S", "/InstallDirectoryPath=$FirefoxDir",
      "/DesktopShortcut=false", "/TaskbarShortcut=false", "/StartMenuShortcut=false",
      "/PrivateBrowsingShortcut=false", "/MaintenanceService=false", "/RegisterDefaultAgent=false")
    Remove-Item $inst -ErrorAction SilentlyContinue
  }
  Say "geckodriver -> $Bin"
  if (-not (Test-Path "$Bin\geckodriver.exe")) {
    $rel = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/mozilla/geckodriver/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -like "*win64.zip" } | Select-Object -First 1
    $zip = Join-Path $env:TEMP $asset.name
    Download $asset.browser_download_url $zip
    Expand-Archive -Force $zip $Bin
    Remove-Item $zip
  }
}

Say "yt-dlp -> $Bin (refreshed every run: YouTube breaks old versions)"
Download "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" "$Bin\yt-dlp.exe"
& "$Bin\yt-dlp.exe" --version

Say "Python venv (.venv, uv, Python 3.13)"
$Venv = Join-Path $Repo ".venv"
if (-not (Test-Path "$Venv\Scripts\python.exe")) { uv venv --seed --python 3.13 $Venv }
$reqs = @("-r", (Join-Path $Repo "requirements-windows.txt"))
if (-not $NoBrowser) { $reqs += @("-r", (Join-Path $Repo "requirements-browser-windows.txt")) }
uv pip install --quiet --python "$Venv\Scripts\python.exe" @reqs
# The bash scripts call "$MEETING_BOT_VENV/bin/python3", the Linux layout; a
# Windows venv has Scripts\python.exe. Two tiny shims make both work.
New-Item -ItemType Directory -Force -Path "$Venv\bin" | Out-Null
$shim = "#!/bin/sh`nexec `"`$(dirname `"`$0`")/../Scripts/python.exe`" `"`$@`"`n"
foreach ($n in @("python3", "python")) {
  [IO.File]::WriteAllText("$Venv\bin\$n", $shim, (New-Object Text.UTF8Encoding $false))
}

Say "PDF fonts for your user"
$UserFonts = Join-Path $env:LOCALAPPDATA "Microsoft\Windows\Fonts"
New-Item -ItemType Directory -Force -Path $UserFonts | Out-Null
function Install-Font($path) {
  $name = [IO.Path]::GetFileName($path)
  $dest = Join-Path $UserFonts $name
  Copy-Item -Force $path $dest
  $kind = if ($name -like "*.otf") { "OpenType" } else { "TrueType" }
  New-ItemProperty -Force -Path "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts" `
    -Name ("{0} ({1})" -f [IO.Path]::GetFileNameWithoutExtension($name), $kind) -Value $dest | Out-Null
}
Get-ChildItem -Recurse -Path (Join-Path $Repo "fonts") -Include *.ttf | ForEach-Object { Install-Font $_.FullName }
$tmpf = Join-Path $env:TEMP "meeting-bot-fonts"
New-Item -ItemType Directory -Force -Path $tmpf | Out-Null
foreach ($f in @("cmunrm", "cmunbx", "cmunti", "cmunbi", "cmuntt")) {
  # CMU Serif = Computer Modern, the English body face and the maths face.
  if (-not (Test-Path (Join-Path $UserFonts "$f.otf"))) {
    try { Download "https://mirrors.ctan.org/fonts/cm-unicode/fonts/otf/$f.otf" "$tmpf\$f.otf"; Install-Font "$tmpf\$f.otf" }
    catch { Write-Warning "Could not fetch $f.otf from CTAN: $_" }
  }
}
if (-not (Test-Path (Join-Path $UserFonts "JetBrainsMono-Regular.ttf"))) {
  try {
    $rel = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/JetBrains/JetBrainsMono/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -like "JetBrainsMono-*.zip" } | Select-Object -First 1
    Download $asset.browser_download_url "$tmpf\jbm.zip"
    Expand-Archive -Force "$tmpf\jbm.zip" "$tmpf\jbm"
    Get-ChildItem -Recurse "$tmpf\jbm" -Include "JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf", "JetBrainsMono-Italic.ttf" |
      Where-Object { $_.FullName -like "*\ttf\*" } | ForEach-Object { Install-Font $_.FullName }
  } catch { Write-Warning "Could not fetch JetBrains Mono: $_" }
}
Remove-Item -Recurse -Force $tmpf -ErrorAction SilentlyContinue

Say "pm2 (runs the web UI; nothing starts at boot)"
if (-not (Get-Command pm2 -ErrorAction SilentlyContinue)) { npm install -g pm2 | Out-Null }

Say ".env"
$EnvFile = Join-Path $Repo ".env"
if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Repo ".env.example") $EnvFile; Note "created from .env.example" }
$envText = Get-Content -Raw $EnvFile
function Set-EnvLine($key, $value, [switch]$OnlyIfEmpty) {
  $script:envText = if ($script:envText -match "(?m)^$key=(.*)$") {
    if ($OnlyIfEmpty -and $Matches[1].Trim()) { $script:envText }
    else { $script:envText -replace "(?m)^$key=.*$", "$key=$value" }
  } else { $script:envText.TrimEnd() + "`n$key=$value`n" }
}
$token = [Convert]::ToBase64String((1..18 | ForEach-Object { Get-Random -Maximum 256 }) -as [byte[]]).TrimEnd("=").Replace("+", "-").Replace("/", "_")
Set-EnvLine "MEETING_BOT_TOKEN" $token -OnlyIfEmpty
$gitBash = Join-Path $env:ProgramFiles "Git\bin\bash.exe"
if (-not (Test-Path $gitBash)) { $gitBash = Join-Path $env:LOCALAPPDATA "Programs\Git\bin\bash.exe" }
Set-EnvLine "MEETING_BOT_BASH" ($gitBash -replace "\\", "/") -OnlyIfEmpty
if ($PangoDir) { Set-EnvLine "WEASYPRINT_DLL_DIRECTORIES" ($PangoDir -replace "\\", "/") -OnlyIfEmpty }
[IO.File]::WriteAllText($EnvFile, ($envText -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
# The bot's pm2 apps run bash; pm2 itself is told where it is (ecosystem.config.js).
[Environment]::SetEnvironmentVariable("MEETING_BOT_BASH", $gitBash, "User")

Say "VB-Audio Virtual Cable (the meeting's sound)"
$cable = Get-CimInstance Win32_SoundDevice | Where-Object { $_.Name -like "*VB-Audio*" }
if ($cable) { Note "found: $($cable.Name -join ', ')" }
else {
  Write-Warning "VB-CABLE is not installed. Download it from https://vb-audio.com/Cable/, run VBCABLE_Setup_x64.exe as administrator, and restart."
}

Say "The claude CLI (the summarizer spends your Claude subscription)"
if (Get-Command claude -ErrorAction SilentlyContinue) { Note (claude --version) }
else {
  Note "not installed. In PowerShell:   irm https://claude.ai/install.ps1 | iex"
  Note "then:                           claude auth login"
}

Write-Host ""
Say "Done. Next (README, section Windows):"
Note "1. Restart this terminal (new PATH). Open Git Bash in this folder for everything below."
Note "2. Edit .env: BOT_GOOGLE_ACCOUNT, your API keys, the output folders, MEETING_BOT_KIOSK_MONITOR."
Note "3. ./first_time_login.sh       - sign the bot's Firefox in as BOT_GOOGLE_ACCOUNT."
Note "4. Route the bot's sound: .venv/bin/python3 screen/windows_sound_setup.py, then while it plays:"
Note "   Settings > System > Sound > Volume mixer > the bot's firefox.exe > Output: CABLE Input."
Note "5. ./pipeline.sh <url-or-file>  - or ./webui.sh on for the web UI."
