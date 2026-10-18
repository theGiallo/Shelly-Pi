<#
.SYNOPSIS
  Puts this PC's SSH key into the root account of a Fedora IoT card, so the Pi can be set up without a login.

.DESCRIPTION
  Windows can't write the card's Linux (ext4) partition, so this uses debugfs.exe from e2fsprogs, the same code
  Linux uses to make and repair ext4. It adds /root/.ssh/authorized_keys with the right owner, permissions and
  SELinux label. Fedora IoT's sshd then accepts the key for root.

  Dot-source it:   . scripts\ext4-inject.ps1 ; Add-RootSshKey -Debugfs ... -Target ... -PublicKeyFile ...
#>

function Invoke-Debugfs {
  param([string]$Debugfs, [string]$Target, [string]$Request = "", [string]$CommandFile = "", [switch]$Write)
  $a = @()
  if ($Write) { $a += "-w" }
  if ($CommandFile) { $a += @("-f", ($CommandFile -replace "\\", "/")) } else { $a += @("-R", $Request) }
  $a += $Target
  $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { $out = & $Debugfs @a 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $old }
  return ($out -join "`n")
}

# Add-RootSshKey: Target is what debugfs opens: a file, or "//./PhysicalDrive10?offset=<bytes of the root partition>".
function Add-RootSshKey {
  param([Parameter(Mandatory)][string]$Debugfs, [Parameter(Mandatory)][string]$Target, [Parameter(Mandatory)][string]$PublicKeyFile)
  $key = ([System.IO.File]::ReadAllText($PublicKeyFile)).Trim()
  if ($key -notmatch "^(ssh-|ecdsa-|sk-)") { throw "That doesn't look like a public key: $PublicKeyFile" }

  $listing = Invoke-Debugfs -Debugfs $Debugfs -Target $Target -Request "ls -p /ostree/deploy"
  $names = @()
  foreach ($line in ($listing -split "`n")) {
    $f = $line.Trim().Split("/")
    if ($f.Count -ge 6 -and $f[5] -and $f[5] -ne "." -and $f[5] -ne "..") { $names += $f[5] }
  }
  if ($names.Count -ne 1) { throw "Expected one OS state under /ostree/deploy on the card, found $($names.Count). Is this the Fedora IoT root partition?" }
  $home_ = "/ostree/deploy/$($names[0])/var/roothome"

  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("shelly-ext4-" + [Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  try {
    [System.IO.File]::WriteAllText((Join-Path $tmp "key.pub"), $key + "`n", (New-Object System.Text.UTF8Encoding($false)))
    # SELinux labels are stored with a trailing NUL byte, as libselinux does
    $nul = [byte[]]@(0)
    foreach ($t in @("ssh_home_t", "admin_home_t")) {
      $bytes = [System.Text.Encoding]::ASCII.GetBytes("system_u:object_r:${t}:s0") + $nul
      [System.IO.File]::WriteAllBytes((Join-Path $tmp "$t.ctx"), $bytes)
    }
    $t = $tmp -replace "\\", "/"
    # debugfs "mkdir" on a name that exists still uses up an inode, which leaves the filesystem inconsistent,
    # so only create what is missing
    function Test-Exists([string]$path) {
      return ((Invoke-Debugfs -Debugfs $Debugfs -Target $Target -Request "stat $path") -match "Inode:\s+\d+")
    }
    $cmds = @()
    if (-not (Test-Exists $home_)) {
      $cmds += @("mkdir $home_", "sif $home_ mode 040700", "sif $home_ uid 0", "sif $home_ gid 0",
                 "ea_set -f $t/admin_home_t.ctx $home_ security.selinux")
    }
    if (-not (Test-Exists "$home_/.ssh")) { $cmds += "mkdir $home_/.ssh" }
    if (Test-Exists "$home_/.ssh/authorized_keys") { $cmds += "rm $home_/.ssh/authorized_keys" }
    $cmds += @(
      "sif $home_/.ssh mode 040700", "sif $home_/.ssh uid 0", "sif $home_/.ssh gid 0",
      "ea_set -f $t/ssh_home_t.ctx $home_/.ssh security.selinux",
      "write $t/key.pub $home_/.ssh/authorized_keys",
      "sif $home_/.ssh/authorized_keys mode 0100600", "sif $home_/.ssh/authorized_keys uid 0", "sif $home_/.ssh/authorized_keys gid 0",
      "ea_set -f $t/ssh_home_t.ctx $home_/.ssh/authorized_keys security.selinux"
    )
    $cf = Join-Path $tmp "commands.txt"
    [System.IO.File]::WriteAllText($cf, (($cmds -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
    [void](Invoke-Debugfs -Debugfs $Debugfs -Target $Target -CommandFile $cf -Write)

    # read it back; "mkdir" of something that already exists is fine, a wrong result is not
    $back = Invoke-Debugfs -Debugfs $Debugfs -Target $Target -Request "cat $home_/.ssh/authorized_keys"
    if (-not $back.Contains($key)) { throw "The key was not written to the card (read-back differs)." }
    $ea = Invoke-Debugfs -Debugfs $Debugfs -Target $Target -Request "ea_list $home_/.ssh/authorized_keys"
    if ($ea -notmatch "ssh_home_t") { throw "The SELinux label was not set on the key file." }
    $mode = Invoke-Debugfs -Debugfs $Debugfs -Target $Target -Request "stat $home_/.ssh/authorized_keys"
    if ($mode -notmatch "Mode:\s+0600") { throw "The key file doesn't have mode 0600." }
  } finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  }
}
