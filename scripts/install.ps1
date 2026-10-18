<#
.SYNOPSIS
  Installs Shelly-Pi on a Raspberry Pi 4 from a Windows PC, over SSH. No WSL needed.

.DESCRIPTION
  Before you start
    1. Write Fedora IoT (aarch64) to the SD card with Fedora Media Writer, balenaEtcher or similar.
    2. Put the card in the Pi, plug it into your router with an Ethernet cable and power it on.
    3. Let the Pi finish its first boot (a minute or two). Fedora IoT asks you to create a user the first
       time; do that on a screen and keyboard if the card was not prepared with your SSH key.

  Then double-click install.cmd, or run:   powershell -ExecutionPolicy Bypass -File scripts\install.ps1

  What it does
    - finds the Pi on your network (or uses -PiHost) and makes sure your SSH key can log in
    - copies this folder to the Pi
    - gets the container image: from the newest GitHub release, or a shelly-pi-image.tar you put in this folder
      (if neither works, the Pi builds it itself, which needs internet on the Pi)
    - generates the passwords, runs deploy/setup-host.sh on the Pi, and saves the passwords on this PC

  The Pi never needs internet for this when the image comes from your PC.

.PARAMETER PiHost       Address or name of the Pi. Default: tries fedora.local and shellypi.local, then asks.
.PARAMETER User         SSH user. Default: root. Another user needs passwordless sudo.
.PARAMETER KeyFile      Your public key. Default: the first of id_ed25519, id_ecdsa, id_rsa in ~\.ssh
.PARAMETER Ssid         Name of the Wi-Fi network the Pi creates for the plugs. Default: ShellyPi
.PARAMETER ApPassword   Password for that network (8-63 characters). Default: generated.
.PARAMETER AdminPassword Password for the dashboard (user admin). Default: generated.
.PARAMETER ImageFile    A shelly-pi-image.tar you already have.
.PARAMETER Repo         GitHub repository to download the image from. Default: theGiallo/Shelly-Pi
.PARAMETER Channel      Wi-Fi channel 1-11. Default: 6
.PARAMETER Country      Two-letter country code for Wi-Fi rules. Default: IT
.PARAMETER Hostname     The Pi's host name. Default: shellypi
.PARAMETER NoRtc        No DS3231 clock module is connected.
.PARAMETER BuildOnPi    Don't download or copy an image; let the Pi build it (needs internet on the Pi).
.PARAMETER Yes          Don't ask for confirmation.
#>
[CmdletBinding()]
param(
  [string]$PiHost = "",
  [string]$User = "root",
  [string]$KeyFile = "",
  [string]$Ssid = "ShellyPi",
  [string]$ApPassword = "",
  [string]$AdminPassword = "",
  [string]$ImageFile = "",
  [string]$Repo = "theGiallo/Shelly-Pi",
  [int]$Channel = 6,
  [string]$Country = "IT",
  [string]$Hostname = "shellypi",
  [switch]$NoRtc,
  [switch]$BuildOnPi,
  [switch]$Yes
)

# Native programs (ssh, scp, tar) report problems through their exit codes, which are checked below.
$ErrorActionPreference = "Continue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

function Say([string]$m)  { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Note([string]$m) { Write-Host "    $m" }
function Warn([string]$m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die([string]$m)  { Write-Host ""; Write-Host "Error: $m" -ForegroundColor Red; exit 1 }

# ---- small helpers ---------------------------------------------------------------------------

function New-Secret([int]$bytes) {
  $b = New-Object byte[] $bytes
  $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
  $rng.GetBytes($b)
  return (($b | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Quote-Arg([string]$a) {
  if ($a -match '[\s"]') { return '"' + ($a -replace '"', '\"') + '"' }
  return $a
}

# Runs a program with text on its standard input, sent as UTF-8 with Unix line endings.
# (PowerShell's own pipe adds Windows line endings, which would end up inside the Pi's files.)
function Invoke-WithInput([string]$exe, [string[]]$arguments, [string]$stdin) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $exe
  $psi.Arguments = (($arguments | ForEach-Object { Quote-Arg $_ }) -join " ")
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes(($stdin -replace "`r`n", "`n"))
  $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
  $p.StandardInput.Close()
  $p.WaitForExit()
  return $p.ExitCode
}

function Test-Port([string]$h, [int]$port, [int]$ms) {
  $c = New-Object System.Net.Sockets.TcpClient
  try {
    $r = $c.BeginConnect($h, $port, $null, $null)
    if (-not $r.AsyncWaitHandle.WaitOne($ms)) { return $false }
    $c.EndConnect($r)
    return $true
  } catch { return $false } finally { $c.Close() }
}

# Runs ssh quietly and returns only its exit code.
function Test-Ssh([string[]]$arguments) {
  & $SshExe @arguments 2>$null | Out-Null
  return $LASTEXITCODE
}

function Sh-Quote([string]$s) { return "'" + ($s -replace "'", "'\''") + "'" }

# ---- 0. checks ----------------------------------------------------------------------------------

Say "Checking this PC"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root -or -not (Test-Path (Join-Path $Root "deploy\setup-host.sh"))) {
  Die "Run this from the Shelly-Pi folder (scripts\install.ps1 next to deploy\setup-host.sh)."
}
foreach ($tool in @("ssh", "scp", "tar")) {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
    if ($tool -eq "tar") { Die "'tar' is missing. It comes with Windows 10 (version 1803) and newer." }
    Die "'$tool' is missing. Turn on 'OpenSSH Client' in Windows: Settings > Apps > Optional features."
  }
}
$SshExe = (Get-Command ssh).Source
if ($Ssid.Length -gt 32) { Die "The Wi-Fi name can be at most 32 characters." }
if ($ApPassword -and ($ApPassword.Length -lt 8 -or $ApPassword.Length -gt 63)) { Die "The Wi-Fi password must be 8 to 63 characters." }
if ($Channel -lt 1 -or $Channel -gt 11) { Die "-Channel must be between 1 and 11." }
foreach ($v in @($Ssid, $ApPassword, $AdminPassword, $Hostname, $Country)) {
  if ($v -match "[`r`n]") { Die "Names and passwords can't contain line breaks." }
}
Note "Project folder: $Root"

# ---- 1. SSH key -----------------------------------------------------------------------------------

$sshDir = Join-Path $env:USERPROFILE ".ssh"
if (-not $KeyFile) {
  foreach ($k in @("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")) {
    $cand = Join-Path $sshDir $k
    if (Test-Path $cand) { $KeyFile = $cand; break }
  }
}
if (-not $KeyFile -or -not (Test-Path $KeyFile)) {
  Say "You don't have an SSH key yet"
  Note "I'll make one now. Pick a passphrase if you like (Enter for none)."
  New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
  $newKey = Join-Path $sshDir "id_ed25519"
  & ssh-keygen -t ed25519 -f $newKey
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$newKey.pub")) { Die "Couldn't make an SSH key. Run ssh-keygen yourself, then start this again." }
  $KeyFile = "$newKey.pub"
}
$PubKey = (Get-Content -Raw $KeyFile).Trim()
if ($PubKey -notmatch "^(ssh-|ecdsa-)") { Die "$KeyFile doesn't look like a public key (it should start with ssh-ed25519, ssh-rsa or ecdsa-)." }
Note "SSH key: $KeyFile"

# ---- 2. find the Pi ----------------------------------------------------------------------------------

Say "Looking for the Pi"
if (-not $PiHost) {
  foreach ($name in @("fedora.local", "shellypi.local")) {
    Note "Trying $name ..."
    if (Test-Port $name 22 2500) { $PiHost = $name; break }
  }
}
if (-not $PiHost -or -not (Test-Port $PiHost 22 4000)) {
  if ($PiHost) { Warn "$PiHost doesn't answer on the SSH port." }
  if ($Yes) { Die "Couldn't find the Pi. Pass its address with -PiHost (look in your router's device list)." }
  Note "I couldn't find it by name. Look in your router's list of devices for the Pi's address."
  $PiHost = (Read-Host "Address of the Pi (for example 192.168.1.50)").Trim()
  if (-not $PiHost) { Die "No address given." }
  if (-not (Test-Port $PiHost 22 6000)) {
    Die "Nothing answers at $PiHost on the SSH port (22). Is the Pi on Ethernet and fully booted? A card that hasn't finished its first-boot setup doesn't accept SSH."
  }
}
Note "Found it at $PiHost."
$Target = "$User@$PiHost"
$SshOpts = @("-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10")

# ---- 3. log in ------------------------------------------------------------------------------------------

Say "Logging in as $User"
if ((Test-Ssh ($SshOpts + @("-o", "BatchMode=yes", $Target, "true"))) -ne 0) {
  Note "Your key isn't accepted yet. I'll put it on the Pi; type the password of '$User' on the Pi when asked."
  $install = "umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys"
  $code = Invoke-WithInput $SshExe ($SshOpts + @($Target, $install)) ($PubKey + "`n")
  if ($code -ne 0) {
    Warn "That didn't work."
    Note "If you saw 'REMOTE HOST IDENTIFICATION HAS CHANGED', the card was written again since you last connected. Run:  ssh-keygen -R $PiHost"
    Note "If '$User' can't log in with a password, create a user on the Pi's own screen and keyboard, then use  -User thatname"
    Die "Couldn't log in to $Target."
  }
  if ((Test-Ssh ($SshOpts + @("-o", "BatchMode=yes", $Target, "true"))) -ne 0) {
    Note "Key login still isn't working. If your key has a passphrase, load it first with ssh-add, then start this again."
    Die "Couldn't log in to $Target with your key."
  }
}
Note "Logged in."
$RemoteHome = ((& $SshExe @SshOpts $Target 'echo $HOME' | Select-Object -First 1) | Out-String).Trim()
if (-not $RemoteHome) { $RemoteHome = "~" }
$SudoPassword = ""
if ($User -ne "root") {
  Note "'$User' isn't root, so the setup runs through sudo. Type its password (the one you gave the Pi) so I can pass it on."
  $sec = Read-Host "Password of $User on the Pi" -AsSecureString
  $SudoPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

# Runs a bash script on the Pi as root: uploads it to a private file, runs it (through sudo when the user isn't root), removes it.
function Invoke-RemoteRoot([string]$script) {
  $code = Invoke-WithInput $SshExe ($SshOpts + @($Target, "umask 077; cat > ~/.shelly-pi-run.sh")) $script
  if ($code -ne 0) { return $code }
  if ($User -eq "root") {
    $run = "bash ~/.shelly-pi-run.sh; rc=`$?; rm -f ~/.shelly-pi-run.sh; exit `$rc"
    return (Invoke-WithInput $SshExe ($SshOpts + @($Target, $run)) "")
  }
  $run = "sudo -S -p '' bash ~/.shelly-pi-run.sh; rc=`$?; rm -f ~/.shelly-pi-run.sh; exit `$rc"
  return (Invoke-WithInput $SshExe ($SshOpts + @($Target, $run)) ($SudoPassword + "`n"))
}
$remoteOs = & $SshExe @SshOpts $Target "uname -m; . /etc/os-release 2>/dev/null; echo `$PRETTY_NAME"
if ($LASTEXITCODE -eq 0) {
  Note ("The Pi says: " + (($remoteOs | ForEach-Object { "$_" }) -join ", "))
  if (($remoteOs | Select-Object -First 1) -ne "aarch64") { Warn "This doesn't look like a 64-bit (aarch64) system." }
}

# ---- 4. settings ------------------------------------------------------------------------------------------

$generated = @()
if (-not $ApPassword)    { $ApPassword = New-Secret 10;    $generated += "Plugs' Wi-Fi password: $ApPassword" }
if (-not $AdminPassword) { $AdminPassword = New-Secret 8;  $generated += "Dashboard password (user admin): $AdminPassword" }

Say "Ready to install"
Note "On:               $Target"
Note "Wi-Fi for plugs:  $Ssid (channel $Channel, country $Country)"
Note "Host name:        $Hostname"
if ($NoRtc) { Note "Hardware clock:   none" } else { Note "Hardware clock:   DS3231 module (use -NoRtc if you have none)" }
if (-not $Yes) {
  $answer = Read-Host "Continue? (Y/n)"
  if ($answer -match "^[nN]") { Die "Stopped. Nothing was changed on the Pi." }
}

# ---- 5. copy the project -------------------------------------------------------------------------------------

Say "Copying the project to the Pi"
$tmpTgz = Join-Path ([System.IO.Path]::GetTempPath()) "shelly-pi-src.tgz"
if (Test-Path $tmpTgz) { Remove-Item -Force $tmpTgz }
& tar -czf $tmpTgz --exclude=.git --exclude=.venv --exclude=venv --exclude=demo-data --exclude=__pycache__ `
  --exclude=.pytest_cache --exclude=node_modules --exclude=*.tar --exclude=*.tar.gz --exclude=*credentials*.txt -C $Root .
if ($LASTEXITCODE -ne 0) { Die "Couldn't pack the project folder." }
& scp @SshOpts $tmpTgz "${Target}:/tmp/shelly-pi-src.tgz"
if ($LASTEXITCODE -ne 0) { Die "Couldn't copy the project to the Pi." }
# Files that left Windows with CRLF line endings would not run on the Pi, so fix those while unpacking.
$unpack = @'
set -e
mkdir -p Shelly-Pi
tar -xzf /tmp/shelly-pi-src.tgz -C Shelly-Pi
rm -f /tmp/shelly-pi-src.tgz
find Shelly-Pi -type f \( -name '*.sh' -o -name '*.py' -o -name '*.service' -o -name '*.path' -o -name '*.container' -o -name '*.rules' -o -name 'Containerfile' \) -exec sed -i 's/\r$//' {} +
'@
$code = Invoke-WithInput $SshExe ($SshOpts + @($Target, "bash -s")) $unpack
if ($code -ne 0) { Die "Couldn't unpack the project on the Pi." }
Remove-Item -Force $tmpTgz -ErrorAction SilentlyContinue
Note "Copied to ~/Shelly-Pi on the Pi."

# ---- 6. the container image -----------------------------------------------------------------------------------------

function Get-File([string]$url, [string]$dest) {
  $part = "$dest.part"
  if (Test-Path $part) { Remove-Item -Force $part }
  $req = [System.Net.HttpWebRequest]::Create($url)
  $req.UserAgent = "Shelly-Pi-installer"
  $req.AllowAutoRedirect = $true
  $req.ReadWriteTimeout = 60000
  $resp = $req.GetResponse()
  $total = $resp.ContentLength
  $in = $resp.GetResponseStream()
  $out = [System.IO.File]::Create($part)
  try {
    $buf = New-Object byte[] (1024 * 1024)
    [long]$done = 0
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $lastShown = -1
    while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
      $out.Write($buf, 0, $n)
      $done += $n
      $sec = [int]$clock.Elapsed.TotalSeconds
      if ($sec -ne $lastShown) {
        $lastShown = $sec
        $mb = [math]::Round($done / 1MB)
        $speed = if ($clock.Elapsed.TotalSeconds -gt 0) { [math]::Round($done / 1MB / $clock.Elapsed.TotalSeconds, 1) } else { 0 }
        if ($total -gt 0) {
          $pct = [math]::Floor($done * 100 / $total)
          Write-Host ("`r    {0} of {1} MB ({2}%)  {3} MB/s   " -f $mb, [math]::Round($total / 1MB), $pct, $speed) -NoNewline
        } else {
          Write-Host ("`r    {0} MB  {1} MB/s   " -f $mb, $speed) -NoNewline
        }
      }
    }
    Write-Host ""
  } finally {
    $out.Close(); $in.Close(); $resp.Close()
  }
  if ($total -gt 0 -and (Get-Item $part).Length -ne $total) { Remove-Item -Force $part; throw "the download stopped early" }
  Move-Item -Force $part $dest
}


Say "Container image"
$image = ""
if ($BuildOnPi) {
  Note "The Pi will build the image itself (it needs internet access for a few minutes)."
} elseif ($ImageFile) {
  if (-not (Test-Path $ImageFile)) { Die "Can't find $ImageFile." }
  $image = (Resolve-Path $ImageFile).Path
} elseif (Test-Path (Join-Path $Root "shelly-pi-image.tar")) {
  $image = Join-Path $Root "shelly-pi-image.tar"
  Note "Using the shelly-pi-image.tar in this folder."
} else {
  $cache = Join-Path $env:LOCALAPPDATA "Shelly-Pi"
  New-Item -ItemType Directory -Force -Path $cache | Out-Null
  $base = "https://github.com/$Repo/releases/latest/download"
  $tar = Join-Path $cache "shelly-pi-image.tar"
  $sum = Join-Path $cache "shelly-pi-image.tar.sha256"
  $old = $ProgressPreference
  try {
    $ProgressPreference = "SilentlyContinue"   # the progress bar makes downloads in Windows PowerShell very slow
    Note "Downloading the image from the newest release of $Repo (about 180 MB) ..."
    Invoke-WebRequest -UseBasicParsing -Uri "$base/shelly-pi-image.tar.sha256" -OutFile $sum -ErrorAction Stop
    Get-File "$base/shelly-pi-image.tar" $tar
    $want = ((Get-Content -Raw $sum).Trim() -split "\s+")[0].ToLower()
    $have = (Get-FileHash -Algorithm SHA256 $tar -ErrorAction Stop).Hash.ToLower()
    if ($want -ne $have) { throw "the download is damaged (checksum differs)" }
    $image = $tar
    Note "Downloaded and checked."
  } catch {
    Warn "Couldn't get the image from GitHub: $($_.Exception.Message)"
    Warn "The Pi will build it itself instead, which needs internet access on the Pi."
    $image = ""
  } finally { $ProgressPreference = $old }
}
if ($image) {
  Note "Sending the image to the Pi ..."
  & scp @SshOpts $image "${Target}:Shelly-Pi/shelly-pi-image.tar"
  if ($LASTEXITCODE -ne 0) { Die "Couldn't copy the image to the Pi." }
}

# ---- 7. run the setup on the Pi ------------------------------------------------------------------------------------------

Say "Setting up the Pi (this takes a few minutes)"
$flags = "--yes --ssid " + (Sh-Quote $Ssid) + " --channel $Channel --country " + (Sh-Quote $Country) + " --hostname " + (Sh-Quote $Hostname)
if ($NoRtc) { $flags += " --no-rtc" } else { $flags += " --rtc" }
$remote = @"
set -eo pipefail
export SHELLY_AP_PASSWORD=$(Sh-Quote $ApPassword)
export SHELLY_ADMIN_PASSWORD=$(Sh-Quote $AdminPassword)
cd $(Sh-Quote "$RemoteHome/Shelly-Pi")
bash deploy/setup-host.sh $flags 2>&1 | tee /tmp/shelly-pi-setup.log
"@
$code = Invoke-RemoteRoot $remote
if ($code -ne 0) {
  Die "The setup on the Pi stopped with an error (see above). The log is on the Pi in /tmp/shelly-pi-setup.log. You can run this script again; it reuses what is already there."
}

# ---- 8. save the passwords on this PC -----------------------------------------------------------------------------------------

$credFile = Join-Path (Get-Location).Path ("shelly-pi-credentials_" + (Get-Date -Format "yyyy-MM-dd") + ".txt")
$lines = @(
  "Shelly-Pi, installed on $Target on " + (Get-Date -Format "yyyy-MM-dd HH:mm"),
  "",
  "Dashboard:                http://$PiHost/   (user: admin)",
  "Dashboard password:       $AdminPassword",
  "Plugs' Wi-Fi name:        $Ssid",
  "Plugs' Wi-Fi password:    $ApPassword",
  "",
  "Keep this file private."
)
[System.IO.File]::WriteAllText($credFile, (($lines -join "`r`n") + "`r`n"))
Say "Done"
Note "Open the dashboard:  http://$PiHost/   (user admin)"
Note "Passwords saved in:  $credFile"
if ($generated.Count -gt 0) {
  Write-Host ""
  Write-Host "    Write these down:"
  foreach ($g in $generated) { Note $g }
}

# ---- 9. reboot if the clock module needs it ------------------------------------------------------------------------------------

if ((Test-Ssh ($SshOpts + @($Target, "grep -q 'Reboot now' /tmp/shelly-pi-setup.log"))) -eq 0) {
  Write-Host ""
  Note "The Pi needs a reboot to switch on the hardware clock."
  $go = "y"
  if (-not $Yes) { $go = Read-Host "Reboot it now? (Y/n)" }
  if ($go -notmatch "^[nN]") {
    Invoke-RemoteRoot "systemctl reboot" | Out-Null
    Note "Rebooting. In a minute, open Settings in the dashboard and press 'Set the Pi's clock'."
  } else {
    Note "Reboot later with:  ssh $Target sudo systemctl reboot"
  }
}
Test-Ssh ($SshOpts + @($Target, "rm -f /tmp/shelly-pi-setup.log")) | Out-Null
