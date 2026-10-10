<#
  nt_screenshot.ps1 -- capture open NinjaTrader chart windows to PNG so Claude/the agent can look at them.

  NT charts render with DirectX, so PrintWindow returns a black plot area; this copies the window's
  rectangle from the SCREEN instead. That means the chart must be visible (not covered by another
  window) -- pass -Activate to bring each chart to the front first (briefly steals focus).

  Usage:
    .\nt_screenshot.ps1                          # list chart windows
    .\nt_screenshot.ps1 -Match "MNQ" -Activate   # capture charts whose title contains MNQ
    .\nt_screenshot.ps1 -All -Activate
  Output: %TEMP%\claude\ntshots\<title>_<handle>.png
#>
param([string]$Match = '', [long]$Handle = 0, [switch]$All, [switch]$Activate, [string]$Out = "$env:TEMP\claude\ntshots")
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Drawing @"
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public class NtWin {
  public delegate bool P(IntPtr h, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct R { public int L, T, Rr, B; }
  [DllImport("user32.dll")] static extern bool EnumWindows(P p, IntPtr l);
  [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public static List<KeyValuePair<long,string>> Charts(uint pid) {
    var r = new List<KeyValuePair<long,string>>();
    EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) { var s = new StringBuilder(256); GetWindowText(h, s, 256);
        if (s.ToString().StartsWith("Chart")) r.Add(new KeyValuePair<long,string>(h.ToInt64(), s.ToString())); }
      return true; }, IntPtr.Zero);
    return r; }
  public static System.Drawing.Bitmap Grab(long hw) {
    R r; GetWindowRect(new IntPtr(hw), out r);
    var b = new System.Drawing.Bitmap(r.Rr - r.L, r.B - r.T);
    using (var g = System.Drawing.Graphics.FromImage(b)) g.CopyFromScreen(r.L, r.T, 0, 0, b.Size);
    return b; }
}
"@
[void][NtWin]::SetProcessDPIAware()
$p = Get-Process NinjaTrader -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { Write-Host "NinjaTrader is not running."; exit 1 }
$charts = [NtWin]::Charts([uint32]$p.Id)
if (-not $Match -and -not $All -and -not $Handle) {
    $charts | ForEach-Object { "{0}  {1}  minimized={2}" -f $_.Key, $_.Value, [NtWin]::IsIconic([IntPtr]$_.Key) }
    exit 0
}
New-Item -ItemType Directory -Force $Out | Out-Null
foreach ($c in $charts) {
    if ($Handle -and $c.Key -ne $Handle) { continue }
    if (-not $Handle -and -not $All -and $c.Value -notlike "*$Match*") { continue }
    $h = [IntPtr]$c.Key
    # Minimized charts are skipped unless explicitly named via -Match; restoring them rearranges
    # the user's desktop, so any window we restore is minimized again right after the capture.
    $wasMin = [NtWin]::IsIconic($h)
    # Only restore a minimized chart when it was picked by -Handle; "-Match NQ" also hits MNQ etc.
    if ($wasMin -and -not ($Handle -and $Activate)) { Write-Host "skip (minimized): $($c.Value) $($c.Key)"; continue }
    if ($wasMin) { [void][NtWin]::ShowWindow($h, 9) }
    if ($Activate) { [void][NtWin]::SetForegroundWindow($h); Start-Sleep -Milliseconds 600 }
    $bmp = [NtWin]::Grab($c.Key)
    if ($wasMin) { [void][NtWin]::ShowWindow($h, 6) }
    $file = Join-Path $Out (($c.Value -replace '[^\w\-]+', '_') + "_$($c.Key).png")
    $bmp.Save($file); $bmp.Dispose()
    Write-Host $file
}
