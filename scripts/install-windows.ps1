<#
.SYNOPSIS
  One script for Windows: gets Fedora IoT, writes it to the SD card, then installs Shelly-Pi on the Pi.

.DESCRIPTION
  1. Downloads the newest Fedora IoT image for the Raspberry Pi (aarch64), unless you give one with -Image.
  2. Gets 7-Zip if needed (to unpack the .xz image) and writes the image to the SD card you choose.
     This part asks for administrator rights and ERASES the card.
  3. Waits while you put the card in the Pi and finish Fedora's first-boot setup.
  4. Runs scripts\install.ps1, which finds the Pi, copies Shelly-Pi, downloads its container image and
     sets everything up.

  Already have Fedora IoT running on the Pi? Use -SkipSd to go straight to step 4.
  Anything this script doesn't recognise (for example -PiHost, -Ssid, -NoRtc) is passed on to install.ps1.

.PARAMETER Image    A Fedora IoT .raw.xz (or .raw) you already downloaded.
.PARAMETER Release  Fedora release number (default: the newest one that has an IoT image).
.PARAMETER ImageUrl Direct address of the image (ends in .aarch64.raw.xz), if finding it automatically fails.
.PARAMETER Disk     Number of the SD card disk (otherwise you choose from a list).
.PARAMETER SkipSd   Don't write a card; the Pi is already running Fedora IoT.
.PARAMETER Yes      Don't ask for confirmation where it is safe not to.
#>
[CmdletBinding()]
param(
  [string]$Image = "",
  [string]$Release = "",
  [string]$ImageUrl = "",
  [int]$Disk = -1,
  [switch]$SkipSd,
  [switch]$Yes,
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
$ProgressPreference = "SilentlyContinue"   # the progress bar makes Invoke-WebRequest very slow in Windows PowerShell

function Say([string]$m)  { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Note([string]$m) { Write-Host "    $m" }
function Warn([string]$m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die([string]$m)  { Write-Host ""; Write-Host "Error: $m" -ForegroundColor Red; exit 1 }

$Here = $PSScriptRoot
if (-not $Here -or -not (Test-Path (Join-Path $Here "install.ps1"))) { Die "Start this from the Shelly-Pi folder (install.cmd)." }
$Cache = Join-Path $env:LOCALAPPDATA "Shelly-Pi\cache"
New-Item -ItemType Directory -Force -Path $Cache | Out-Null

function Get-Text([string]$url) {
  return (Invoke-WebRequest -UseBasicParsing -Uri $url -ErrorAction Stop).Content
}

function Get-File([string]$url, [string]$dest) {
  $part = "$dest.part"
  if (Test-Path $part) { Remove-Item -Force $part }
  $done = $false
  try {
    if (Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue) {
      Start-BitsTransfer -Source $url -Destination $part -DisplayName "Shelly-Pi download" -ErrorAction Stop
      $done = $true
    }
  } catch { Warn "The Windows background transfer service didn't work ($($_.Exception.Message)); trying a plain download." }
  if (-not $done) {
    $wc = New-Object System.Net.WebClient
    $wc.DownloadFile($url, $part)
  }
  Move-Item -Force $part $dest
}

# ---- 1. the Fedora IoT image -----------------------------------------------------------------------------------------

# Collects image links from Fedora's download page (the links Fedora itself publishes).
function Get-CandidatesFromDownloadPage {
  $out = @()
  foreach ($page in @("https://fedoraproject.org/iot/download", "https://www.fedoraproject.org/iot/download/")) {
    try {
      $html = Get-Text $page
    } catch { Warn "Couldn't read $page ($($_.Exception.Message))"; continue }
    $imgs = [regex]::Matches($html, "https?://[^\s`"'<>]*?(Fedora-IoT-(?:raw-)?(\d+)-([0-9]{8}\.[0-9]+)\.aarch64\.raw\.xz)")
    $sums = @([regex]::Matches($html, "https?://[^\s`"'<>]*Fedora-IoT-[^\s`"'<>]*aarch64[^\s`"'<>]*CHECKSUM") | ForEach-Object { $_.Value } | Sort-Object -Unique)
    foreach ($m in $imgs) {
      $rel = $m.Groups[2].Value; $build = $m.Groups[3].Value
      $sum = $sums | Where-Object { $_ -match "Fedora-IoT-$rel-" -and $_ -match [regex]::Escape($build) } | Select-Object -First 1
      $out += New-Object PSObject -Property @{ Name = $m.Groups[1].Value; Url = $m.Value; SumUrl = $sum; Release = [int]$rel; Build = $build }
    }
    if ($out.Count -gt 0) { break }
  }
  return $out
}

# Fallback: read the folders on Fedora's main server.
function Get-CandidatesFromListings {
  $listBase = "https://dl.fedoraproject.org/pub/alt/iot"
  $out = @()
  $releases = @()
  if ($Release) { $releases = @($Release) } else { $releases = @(48, 47, 46, 45, 44, 43, 42) }
  foreach ($r in $releases) {
    $dir = "$listBase/$r/IoT/aarch64/images/"
    try { $listing = Get-Text $dir } catch { Note "No folder for release ${r}: $dir"; continue }
    foreach ($m in [regex]::Matches($listing, "Fedora-IoT-(?:raw-)?$r-([0-9]{8}\.[0-9]+)\.aarch64\.raw\.xz")) {
      $build = $m.Groups[1].Value
      $sum = $null
      foreach ($cand in @("Fedora-IoT-$r-aarch64-$build-CHECKSUM", "Fedora-IoT-$r-$build-aarch64-CHECKSUM", "Fedora-IoT-$r-aarch64-CHECKSUM")) {
        if ($listing.Contains($cand)) { $sum = "$dir$cand"; break }
      }
      $out += New-Object PSObject -Property @{ Name = $m.Value; Url = "https://download.fedoraproject.org/pub/alt/iot/$r/IoT/aarch64/images/$($m.Value)"; SumUrl = $sum; Release = [int]$r; Build = $build }
    }
    if ($out.Count -gt 0) { break }
  }
  return $out
}

function Get-FedoraImage {
  Say "Looking for the newest Fedora IoT image"
  $name = ""; $url = ""; $sumUrl = ""
  if ($ImageUrl) {
    $url = $ImageUrl
    $name = [System.IO.Path]::GetFileName(([Uri]$ImageUrl).AbsolutePath)
    Warn "Using the address you gave; nothing can be verified for it."
  } else {
    $cands = @(Get-CandidatesFromDownloadPage)
    if ($Release) { $cands = @($cands | Where-Object { $_.Release -eq [int]$Release }) }
    if ($cands.Count -eq 0) {
      Note "Fedora's download page didn't give a link; reading Fedora's folders instead."
      $cands = @(Get-CandidatesFromListings)
    }
    if ($cands.Count -eq 0) {
      Die ("Couldn't find a Fedora IoT aarch64 image. Open https://fedoraproject.org/iot/download in your browser, " +
           "copy the address of the 'Raw image' for aarch64 (it ends in .aarch64.raw.xz) and run:  install.cmd -ImageUrl <that address>   " +
           "(or download the file and pass it with -Image).")
    }
    $best = $cands | Sort-Object @{ Expression = { $_.Release } }, @{ Expression = { $_.Build.Split(".")[0] } }, @{ Expression = { [int]$_.Build.Split(".")[1] } } | Select-Object -Last 1
    $name = $best.Name; $url = $best.Url; $sumUrl = $best.SumUrl
  }
  Note "Found $name"
  $dest = Join-Path $Cache $name
  $want = ""
  if ($sumUrl) {
    try {
      $sumText = Get-Text $sumUrl
      $m = [regex]::Match($sumText, "SHA256 \($([regex]::Escape($name))\) = ([0-9a-fA-F]{64})")
      if ($m.Success) { $want = $m.Groups[1].Value.ToLower() }
    } catch { Warn "Couldn't read the checksum file ($($_.Exception.Message))." }
  }
  if (-not $want -and -not $ImageUrl) { Warn "No checksum found for this image, so the download can't be verified." }
  if ((Test-Path $dest) -and $want -and ((Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower() -eq $want)) {
    Note "Already downloaded and checked."
    return $dest
  }
  Note "Downloading (about 1 GB; a progress bar may appear) ..."
  try {
    Get-File $url $dest
  } catch {
    $alt = $url -replace "^https://download\.fedoraproject\.org/", "https://dl.fedoraproject.org/"
    if ($alt -eq $url) { Die "The download failed: $($_.Exception.Message)" }
    Warn "The mirror didn't work ($($_.Exception.Message)); using Fedora's main server instead (slower)."
    Get-File $alt $dest
  }
  if ($want) {
    $have = (Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower()
    if ($have -ne $want) { Remove-Item -Force $dest; Die "The download doesn't match its checksum, so I deleted it. Run this again." }
    Note "Checksum OK."
  }
  return $dest
}

# ---- 2. 7-Zip, to unpack the .xz --------------------------------------------------------------------------------------

function Find-SevenZip {
  foreach ($p in @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe", (Join-Path $Cache "7zr.exe"))) {
    if ($p -and (Test-Path $p)) { return $p }
  }
  foreach ($n in @("7z", "7zr")) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
  }
  return ""
}

function Get-SevenZip {
  $exe = Find-SevenZip
  if ($exe) { return $exe }
  Say "Getting 7-Zip (needed to unpack Fedora's .xz image)"
  if (-not $Yes) {
    $a = Read-Host "Install 7-Zip with winget, or fall back to its official 7zr.exe? Press Enter to continue, or type n to stop"
    if ($a -match "^[nN]") { Die "Stopped. Install 7-Zip from https://www.7-zip.org/ and start this again." }
  }
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Note "Installing 7-Zip with winget ..."
    & winget install --id 7zip.7zip -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $exe = Find-SevenZip
    if ($exe) { return $exe }
    Warn "winget didn't install it."
  }
  Note "Downloading 7zr.exe from 7-zip.org ..."
  $dest = Join-Path $Cache "7zr.exe"
  Get-File "https://www.7-zip.org/a/7zr.exe" $dest
  $sig = Get-AuthenticodeSignature $dest
  if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Pavlov") {
    Remove-Item -Force $dest
    Die "The downloaded 7zr.exe isn't signed by 7-Zip's author, so I won't run it. Install 7-Zip from https://www.7-zip.org/ and start this again."
  }
  Note "7zr.exe is signed by Igor Pavlov."
  return $dest
}

# ---- run ------------------------------------------------------------------------------------------------------------------

$user = "root"
if (-not $SkipSd) {
  Write-Host ""
  Write-Host "Shelly-Pi installer for Windows" -ForegroundColor Cyan
  Write-Host "  You need: the Raspberry Pi 4 with its power supply, a microSD card (16 GB or more) in a card"
  Write-Host "  reader on this PC, an Ethernet cable to your router, and a screen and keyboard for the Pi's first start."
  Write-Host "  The card will be ERASED. This takes 15 to 40 minutes, mostly download and writing."

  if ($Image) {
    if (-not (Test-Path $Image)) { Die "Can't find $Image." }
    $imagePath = (Resolve-Path $Image).Path
  } else {
    $imagePath = Get-FedoraImage
  }
  $extractor = ""
  if ($imagePath -match "\.xz$") { $extractor = Get-SevenZip }

  Say "Writing the SD card"
  Note "A window will ask for administrator rights, then ask which disk is the card."
  $flash = Join-Path $Here "flash-sd.ps1"
  $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$flash`" -Image `"$imagePath`""
  if ($extractor) { $argLine += " -Extractor `"$extractor`"" }
  if ($Disk -ge 0) { $argLine += " -Disk $Disk" }
  if ($Yes -and $Disk -ge 0) { $argLine += " -Yes" }
  try {
    $p = Start-Process -FilePath "powershell.exe" -ArgumentList $argLine -Verb RunAs -Wait -PassThru
  } catch {
    Die "The card writer couldn't start (was the administrator prompt declined?): $($_.Exception.Message)"
  }
  if ($p.ExitCode -ne 0) { Die "Writing the card didn't finish (see the other window). Nothing else was done." }

  Say "Start the Pi"
  Write-Host "  1. Put the SD card in the Pi."
  Write-Host "  2. Plug the Pi into your router with an Ethernet cable."
  Write-Host "  3. Connect a screen and keyboard to the Pi, then plug in its power."
  Write-Host "  4. Fedora's first-boot setup may ask you to create a user. If it does, make one with an"
  Write-Host "     administrator account and remember its name and password."
  Write-Host "  5. When the Pi shows a login prompt, come back here."
  Write-Host ""
  Write-Host "  If the Pi shows nothing and its green light never blinks after a couple of minutes, it did not"
  Write-Host "  boot from the card; see 'The Pi does not boot' in the README." -ForegroundColor Yellow
  Write-Host ""
  $answer = Read-Host "User name you created on the Pi (just press Enter if there was no setup and root accepts your key)"
  if ($answer.Trim()) { $user = $answer.Trim() }
}

Say "Installing Shelly-Pi on the Pi"
$forward = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $Here "install.ps1")) + $Rest
if (-not ($Rest -contains "-User")) { $forward += @("-User", $user) }
if ($Yes) { $forward += "-Yes" }
& powershell.exe @forward
exit $LASTEXITCODE
