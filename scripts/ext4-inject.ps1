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

# ---- working on the card: copy the root partition to a file, change it there, write back only what changed ----------

if (-not ("RawPart" -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class RawPart {
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr sec, uint disp, uint flags, IntPtr tmpl);

  static FileStream Open(string device, bool write) {
    uint access = write ? 0xC0000000 : 0x80000000;   // read+write or read
    SafeFileHandle h = CreateFile(device, access, 3, IntPtr.Zero, 3, write ? 0x80000000 : 0u, IntPtr.Zero);
    if (h.IsInvalid) throw new IOException("Can't open " + device + (write ? " for writing" : "") + " (Windows error " + Marshal.GetLastWin32Error() + ").");
    return new FileStream(h, write ? FileAccess.ReadWrite : FileAccess.Read, 1, false);
  }

  static int Fill(Stream s, byte[] buf, int count) {
    int got = 0;
    while (got < count) { int n = s.Read(buf, got, count - got); if (n <= 0) break; got += n; }
    return got;
  }

  // true if an ext2/3/4 superblock starts at this offset
  public static bool HasExt4(string device, long offset) {
    using (FileStream f = Open(device, false)) {
      byte[] b = new byte[4096];
      f.Position = offset;
      if (Fill(f, b, 4096) < 4096) return false;
      return b[1080] == 0x53 && b[1081] == 0xEF;
    }
  }

  public static void CopyOut(string device, long offset, long size, string file) {
    byte[] buf = new byte[4 * 1024 * 1024];
    long done = 0; int last = -1;
    using (FileStream f = Open(device, false))
    using (FileStream o = new FileStream(file, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20)) {
      f.Position = offset;
      while (done < size) {
        int want = (int)Math.Min(buf.Length, size - done);
        int n = Fill(f, buf, want);
        if (n < want) throw new IOException("The card stopped answering after " + (done + n) / 1048576 + " MB.");
        o.Write(buf, 0, n);
        done += n;
        int pct = (int)(done * 100 / size);
        if (pct != last && pct % 5 == 0) { last = pct; Console.Write("\r    reading the partition: {0}%   ", pct); }
      }
    }
    Console.WriteLine();
  }

  // writes back the 1 MiB pieces that differ between the original and the changed copy; returns how many
  public static int WriteBack(string device, long offset, string original, string changed) {
    const int Piece = 1024 * 1024;
    byte[] a = new byte[Piece], b = new byte[Piece], check = new byte[Piece];
    int changedPieces = 0;
    using (FileStream fa = File.OpenRead(original))
    using (FileStream fb = File.OpenRead(changed))
    using (FileStream dev = Open(device, true)) {
      long pos = 0, size = fa.Length;
      while (pos < size) {
        int want = (int)Math.Min(Piece, size - pos);
        int na = Fill(fa, a, want), nb = Fill(fb, b, want);
        if (na != nb) throw new IOException("The changed copy has a different size.");
        bool same = true;
        for (int i = 0; i < na; i++) if (a[i] != b[i]) { same = false; break; }
        if (!same) {
          int w = (na + 511) / 512 * 512;
          if (w > na) Array.Clear(b, na, w - na);
          dev.Position = offset + pos;
          dev.Write(b, 0, w);
          dev.Flush(true);
          dev.Position = offset + pos;
          Fill(dev, check, na);
          for (int i = 0; i < na; i++) if (check[i] != b[i]) throw new IOException("Read-back after writing differs at " + (pos + i) + ".");
          changedPieces++;
        }
        pos += na;
      }
    }
    return changedPieces;
  }
}
'@
}

# Add-RootSshKeyToDisk: finds the Fedora IoT root partition on the disk, adds the key there, writes back the change.
function Add-RootSshKeyToDisk {
  param([Parameter(Mandatory)][int]$DiskNumber, [Parameter(Mandatory)][string]$ToolsDir, [Parameter(Mandatory)][string]$PublicKeyFile,
        [string]$WorkDir = $env:TEMP, [long]$MinSize = 1GB)
  $debugfs = Join-Path $ToolsDir "debugfs.exe"
  $e2fsck = Join-Path $ToolsDir "e2fsck.exe"
  foreach ($f in @($debugfs, $e2fsck)) { if (-not (Test-Path $f)) { throw "Missing $f" } }
  $device = "\\.\PhysicalDrive$DiskNumber"

  $cands = @(Get-Partition -DiskNumber $DiskNumber | Where-Object { $_.Size -gt $MinSize } | Sort-Object Size -Descending |
             Where-Object { [RawPart]::HasExt4($device, $_.Offset) })
  if ($cands.Count -eq 0) { throw "No Linux (ext4) root partition found on disk $DiskNumber." }
  $part = $cands[0]
  $size = [long]$part.Size
  $free = (Get-PSDrive -Name ([IO.Path]::GetPathRoot($WorkDir).Substring(0, 1))).Free
  if ($free -lt ($size * 2 + 200MB)) { throw ("Not enough free space in {0}: this needs about {1:N1} GB for a temporary copy of the card's root partition." -f $WorkDir, (($size * 2 + 200MB) / 1GB)) }

  $orig = Join-Path $WorkDir "shelly-root-original.img"
  $mod = Join-Path $WorkDir "shelly-root-changed.img"
  $wasOnline = -not (Get-Disk -Number $DiskNumber).IsOffline
  try {
    # Windows blocks raw writes into partitions it has mounted; an offline disk has none mounted
    if ($wasOnline) { Set-Disk -Number $DiskNumber -IsOffline $true }
    Write-Host ("    Copying the {0:N1} GB root partition to a temporary file ..." -f ($size / 1GB))
    [RawPart]::CopyOut($device, [long]$part.Offset, $size, $orig)
    Copy-Item -Force $orig $mod
    $origU = $orig -replace "\\", "/"
    $modU = $mod -replace "\\", "/"
    $before = & $e2fsck -fn $origU 2>&1 | ForEach-Object { "$_" }; $codeBefore = $LASTEXITCODE
    Add-RootSshKey -Debugfs $debugfs -Target $modU -PublicKeyFile $PublicKeyFile
    $after = & $e2fsck -fn $modU 2>&1 | ForEach-Object { "$_" }; $codeAfter = $LASTEXITCODE
    # only a check that gets worse than before our change stops us (the stock image may not be perfectly clean)
    if ($codeAfter -gt $codeBefore) { throw ("The changed copy didn't pass the filesystem check, so nothing was written to the card: " + ($after -join " | ")) }
    Write-Host "    Filesystem check passed. Writing the changes to the card ..."
    $n = [RawPart]::WriteBack($device, [long]$part.Offset, $orig, $mod)
    Write-Host ("    Done: {0} MB of changes written." -f $n)
  } finally {
    Remove-Item -Force $orig, $mod -ErrorAction SilentlyContinue
    if ($wasOnline) { try { Set-Disk -Number $DiskNumber -IsOffline $false } catch { } }
  }
}
