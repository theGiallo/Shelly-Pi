using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class SdWriter {
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern SafeFileHandle CreateFile(string fileName, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);

  const int Head = 4 * 1024 * 1024;    // the partition table is written last, so Windows doesn't mount half a card
  const int Chunk = 4 * 1024 * 1024;

  static int Fill(Stream s, byte[] buf) {
    int got = 0;
    while (got < buf.Length) {
      int n = s.Read(buf, got, buf.Length - got);
      if (n <= 0) break;
      got += n;
    }
    return got;
  }

  // The root partition of a GPT disk, found from the first sectors of the image: the biggest one.
  static bool FindLargestPartition(byte[] head, out long offset, out long size) {
    offset = 0; size = 0;
    if (head.Length < 2048) return false;
    string sig = System.Text.Encoding.ASCII.GetString(head, 512, 8);
    if (sig != "EFI PART") return FindLargestMbr(head, out offset, out size);
    long entLba = BitConverter.ToInt64(head, 512 + 72);
    int count = BitConverter.ToInt32(head, 512 + 80);
    int esz = BitConverter.ToInt32(head, 512 + 84);
    if (esz < 128 || count <= 0 || count > 1024) return false;
    for (int i = 0; i < count; i++) {
      long p = entLba * 512 + (long)i * esz;
      if (p + 128 > head.Length) break;
      bool empty = true;
      for (int j = 0; j < 16; j++) if (head[p + j] != 0) { empty = false; break; }
      if (empty) continue;
      long first = BitConverter.ToInt64(head, (int)p + 32), last = BitConverter.ToInt64(head, (int)p + 40);
      long len = (last - first + 1) * 512;
      if (len > size) { size = len; offset = first * 512; }
    }
    return size > 0;
  }

  // Old-style (MBR) partition table: four entries at byte 446; the biggest one.
  static bool FindLargestMbr(byte[] head, out long offset, out long size) {
    offset = 0; size = 0;
    if (head[510] != 0x55 || head[511] != 0xAA) return false;
    for (int i = 0; i < 4; i++) {
      int p = 446 + i * 16;
      int type = head[p + 4];
      if (type == 0 || type == 0xEE || type == 0x05 || type == 0x0F) continue;
      long first = BitConverter.ToUInt32(head, p + 8), count = BitConverter.ToUInt32(head, p + 12);
      long len = count * 512;
      if (len > size) { size = len; offset = first * 512; }
    }
    return size > 0;
  }

  // Set when the partition table in the image could not be read (so beforeHead was not called).
  public static string Problem = "";

  // beforeHead (optional) is called once everything except the first 4 MB is on the card, with the offset and size of
  // the biggest partition. At that moment the card has no partition table yet, so Windows hasn't mounted anything
  // and raw access to the partition is allowed even on removable media.
  public static long Write(Stream src, string device, long diskBytes, long expected, Action<long, long> beforeHead) {
    uint GENERIC_READ_WRITE = 0xC0000000, SHARE_READ_WRITE = 3, OPEN_EXISTING = 3, WRITE_THROUGH = 0x80000000;
    SafeFileHandle h = CreateFile(device, GENERIC_READ_WRITE, SHARE_READ_WRITE, IntPtr.Zero, OPEN_EXISTING, WRITE_THROUGH, IntPtr.Zero);
    if (h.IsInvalid)
      throw new IOException("Can't open " + device + " for writing (Windows error " + Marshal.GetLastWin32Error() + "). Close programs that use the card and try again.");
    long total = 0, lastTotal = 0;
    double speed = 0, lastSec = 0;   // speed: bytes per second, smoothed over the last half minute or so
    System.Diagnostics.Stopwatch clock = System.Diagnostics.Stopwatch.StartNew();
    using (FileStream dst = new FileStream(h, FileAccess.ReadWrite, 1, false)) {
      byte[] head = new byte[Head];
      int got = Fill(src, head);
      if (got < 1024 * 1024) throw new IOException("The image ended after " + got + " bytes. It is damaged.");
      total = got;
      byte[] buf = new byte[Chunk];
      dst.Position = Head;
      while (got == Head) {
        int n = Fill(src, buf);
        if (n == 0) break;
        int w = (n + 511) / 512 * 512;
        if (w > n) Array.Clear(buf, n, w - n);
        if (Head + (total - Head) + w > diskBytes) throw new IOException("The image is bigger than the card.");
        dst.Write(buf, 0, w);
        total += n;
        double sec = clock.Elapsed.TotalSeconds;
        if (sec - lastSec >= 2) {
          double inst = (total - lastTotal) / (sec - lastSec);
          speed = speed <= 0 ? inst : speed * 0.85 + inst * 0.15;
          lastSec = sec; lastTotal = total;
          string eta = "";
          if (expected > total && speed > 0) {
            int left = (int)((expected - total) / speed);
            eta = string.Format("  about {0}:{1:00} left", left / 60, left % 60);
          }
          if (expected > 0) Console.Write("\r    {0:N0} of {1:N0} MB written ({2}%)  {3:N1} MB/s{4}          ", total / 1048576, expected / 1048576, total * 100 / expected, speed / 1048576, eta);
          else Console.Write("\r    {0:N0} MB written  {1:N1} MB/s          ", total / 1048576, speed / 1048576);
        }
        if (n < Chunk) break;
      }
      dst.Flush(true);
      Console.WriteLine();
      long partOffset, partSize;
      if (beforeHead != null) {
        if (FindLargestPartition(head, out partOffset, out partSize)) {
          beforeHead(partOffset, partSize);
          dst.Flush(true);
        } else {
          Problem = "I couldn't read the partition table at the start of the image.";
        }
      }
      dst.Position = 0;
      dst.Write(head, 0, Head);
      dst.Flush(true);
    }
    return total;
  }
}
