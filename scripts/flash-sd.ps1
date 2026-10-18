<#
.SYNOPSIS
  Writes a Fedora IoT image (.raw.xz, .raw or .img) to an SD card on Windows. ERASES THE CARD.

.DESCRIPTION
  Normally started for you by install.cmd (which downloads the image first and asks for administrator rights).
  It only offers USB and SD disks, never your system disk, and asks you to confirm which one.

.PARAMETER Image      The image file. A .raw.xz needs -Extractor.
.PARAMETER Extractor  Path to 7z.exe or 7zr.exe, used to unpack .xz files while writing.
.PARAMETER Disk       Disk number to write to (see Disk Management). Without it, you choose from a list.
.PARAMETER ToolsDir   Folder with debugfs.exe and e2fsck.exe. With -PublicKey, the key is added to the card's root account.
.PARAMETER PublicKey  Public SSH key file to let in as root, so the Pi needs no login setup.
.PARAMETER Yes        Don't ask for confirmation. Only works together with -Disk.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Image,
  [string]$Extractor = "",
  [int]$Disk = -1,
  [string]$ToolsDir = "",
  [string]$PublicKey = "",
  [switch]$Yes
)

$ErrorActionPreference = "Stop"
function Say([string]$m)  { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Note([string]$m) { Write-Host "    $m" }
function Warn([string]$m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Fail([string]$m) {
  Write-Host ""; Write-Host "Error: $m" -ForegroundColor Red
  Read-Host "Press Enter to close this window" | Out-Null
  exit 1
}

try {
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $admin) { Fail "Writing to an SD card needs administrator rights. Start it through install.cmd, or from a PowerShell opened as administrator." }
  if (-not (Test-Path $Image)) { Fail "Can't find the image: $Image" }
  $isXz = $Image -match "\.xz$"
  if ($isXz -and (-not $Extractor -or -not (Test-Path $Extractor))) { Fail "Unpacking a .xz file needs 7-Zip (7z.exe or 7zr.exe). Pass its path with -Extractor." }

  # ---- how big is the image once unpacked? ------------------------------------------------------------
  [long]$imageBytes = 0
  if ($isXz) {
    $ErrorActionPreference = "Continue"      # 7-Zip prints its banner on the error stream
    $info = & $Extractor l -slt $Image 2>&1 | Out-String
    $ErrorActionPreference = "Stop"
    $m = [regex]::Match($info, "(?m)^Size = (\d+)")
    if ($m.Success) { $imageBytes = [long]$m.Groups[1].Value }
  } else {
    $imageBytes = (Get-Item $Image).Length
  }

  # ---- choose the disk ----------------------------------------------------------------------------------------
  Say "Which disk is the SD card?"
  $candidates = @(Get-Disk | Where-Object {
      -not $_.IsSystem -and -not $_.IsBoot -and ($_.BusType -eq "USB" -or $_.BusType -eq "SD" -or $_.BusType -eq "MMC") -and
      $_.Size -ge 7GB -and $_.Size -le 512GB
    })
  if ($candidates.Count -eq 0) {
    Fail "No SD card or USB disk of 8 GB to 512 GB found. Put the card in the reader, wait a few seconds and start again. (Your system disk is never offered.)"
  }
  foreach ($d in $candidates) {
    $letters = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter } | ForEach-Object { "$($_.DriveLetter):" }) -join " "
    Note ("Disk {0}: {1}, {2:N1} GB, {3}{4}" -f $d.Number, $d.FriendlyName, ($d.Size / 1GB), $d.BusType, $(if ($letters) { ", drives $letters" } else { "" }))
  }

  if ($Disk -ge 0) {
    $target = $candidates | Where-Object { $_.Number -eq $Disk }
    if (-not $target) { Fail "Disk $Disk isn't an SD card or USB disk I'm allowed to write to. The ones above are." }
  } elseif ($candidates.Count -eq 1) {
    $target = $candidates[0]
  } else {
    $pick = Read-Host "Type the number of the SD card"
    $target = $candidates | Where-Object { "$($_.Number)" -eq $pick.Trim() }
    if (-not $target) { Fail "'$pick' isn't one of the disks above." }
  }
  $n = $target.Number
  if ($imageBytes -gt 0 -and $imageBytes -gt $target.Size) {
    Fail ("The image needs {0:N1} GB but disk {1} is only {2:N1} GB." -f ($imageBytes / 1GB), $n, ($target.Size / 1GB))
  }

  Write-Host ""
  Write-Host ("  Disk {0}: {1}, {2:N1} GB" -f $n, $target.FriendlyName, ($target.Size / 1GB)) -ForegroundColor Yellow
  Write-Host "  EVERYTHING ON THIS DISK WILL BE ERASED." -ForegroundColor Yellow
  if (-not ($Yes -and $Disk -ge 0)) {
    $typed = Read-Host "Type the disk number ($n) to erase it, or anything else to stop"
    if ($typed.Trim() -ne "$n") { Fail "Stopped. Nothing was changed." }
  }

  # ---- the writer (compiled once, by Windows PowerShell itself) -----------------------------------------------------
  if (-not ("SdWriter" -as [type])) {
    Add-Type -TypeDefinition ([System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "sd-writer.cs")))
  }

  # ---- clean the disk and write -----------------------------------------------------------------------------------------
  Say "Preparing disk $n"
  try { Set-Disk -Number $n -IsReadOnly $false -ErrorAction SilentlyContinue } catch { }
  if ((Get-Disk -Number $n).PartitionStyle -ne "RAW") {
    Clear-Disk -Number $n -RemoveData -RemoveOEM -Confirm:$false
  }

  Say "Writing the image (this takes 5 to 20 minutes; don't remove the card)"
  $device = "\\.\PhysicalDrive$n"
  $script:keyAdded = $false
  $hook = $null
  if ($ToolsDir -and $PublicKey) {
    . (Join-Path $PSScriptRoot "ext4-inject.ps1")
    $hook = [Action[long, long]]{
      param($off, $size)
      Say "Letting your PC log in to the Pi (adding your SSH key to the card)"
      try {
        Add-RootSshKeyAt -Device $device -Offset $off -Size $size -ToolsDir $ToolsDir -PublicKeyFile $PublicKey -WorkDir $env:TEMP
        $script:keyAdded = $true
        Note "Done. The Pi will accept your PC's key for the 'root' account."
      } catch {
        Warn "Couldn't add the key: $($_.Exception.Message)"
        Warn "The card is written and still good, but the Pi will need a user created on its own screen and keyboard."
      }
    }
  }
  $proc = $null
  if ($isXz) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Extractor
    $psi.Arguments = "e -so -bd -y `"$Image`""
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $source = $proc.StandardOutput.BaseStream
  } else {
    $source = [System.IO.File]::OpenRead($Image)
  }
  try {
    $written = [SdWriter]::Write($source, $device, [long]$target.Size, [long]$imageBytes, $hook)
  } finally {
    $source.Dispose()
  }
  if ($proc) {
    $proc.WaitForExit()
    if ($proc.ExitCode -ne 0) { Fail "Unpacking the image failed (7-Zip exit code $($proc.ExitCode)). The download may be damaged: delete it and run install.cmd again." }
  }
  Note ("Wrote {0:N0} MB." -f ($written / 1MB))

  Say "Finishing"
  try { Update-Disk -Number $n -ErrorAction SilentlyContinue } catch { }
  try { Update-HostStorageCache } catch { }
  Start-Sleep -Seconds 3
  $parts = @(Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue)
  Note ("The card now has {0} partitions." -f $parts.Count)

  Warn "Windows may say it must format some drives on this card. Click Cancel each time: those are Linux partitions it can't read."
  try {
    # Eject the one drive Windows can read, so everything is flushed and the card can be pulled out safely.
    $readable = $parts | Where-Object { $_.DriveLetter } | Select-Object -First 1
    if ($readable) {
      $shell = New-Object -ComObject Shell.Application
      $shell.Namespace(17).ParseName("$($readable.DriveLetter):").InvokeVerb("Eject")
      Start-Sleep -Seconds 2
    }
  } catch { }
  Say "The SD card is written"
  Note "Take it out of the reader. If Windows still shows it, use 'Safely remove hardware' first."
  Read-Host "Press Enter to close this window" | Out-Null
  if ($ToolsDir -and $PublicKey -and -not $script:keyAdded) { exit 2 }
  exit 0
} catch {
  Fail $_.Exception.Message
}
