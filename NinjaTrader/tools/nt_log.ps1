<#
  nt_log.ps1 -- show recent NinjaTrader errors/output relevant to a script, so runtime problems
  (exceptions in OnBarUpdate, failed restores, compile errors inside NT) are visible without the GUI.

  Usage:  .\nt_log.ps1                      # last 40 error lines from today's log + trace
          .\nt_log.ps1 -Name AyniOrderFlow  # also lines mentioning that script
          .\nt_log.ps1 -Minutes 15          # only the last 15 minutes
#>
param([string]$Name = '', [int]$Minutes = 0, [int]$Tail = 40)
$nt = "$env:USERPROFILE\Documents\NinjaTrader 8"
$pattern = 'ERROR|rror on calling|xception|Error compiling|CS\d{4}'
if ($Name) { $pattern += '|' + [regex]::Escape($Name) }

$files = @()
$files += Get-ChildItem "$nt\log\log.*.txt"   | Where-Object Name -notlike '*.en.txt' | Sort-Object LastWriteTime -Desc | Select-Object -First 1
$files += Get-ChildItem "$nt\trace\trace.*.txt" | Sort-Object LastWriteTime -Desc | Select-Object -First 1
$since = if ($Minutes -gt 0) { (Get-Date).AddMinutes(-$Minutes) } else { [DateTime]::MinValue }

foreach ($f in $files) {
    Write-Host "== $($f.Name)"
    # Case-sensitive so "errorCode=NoError" connection chatter doesn't match "ERROR".
    Select-String -Path $f.FullName -Pattern $pattern -CaseSensitive | ForEach-Object {
        $line = $_.Line
        $ts = [DateTime]::MinValue
        [void][DateTime]::TryParseExact($line.Substring(0, [Math]::Min(19, $line.Length)), 'yyyy-MM-dd HH:mm:ss', $null, 'None', [ref]$ts)
        if ($ts -ge $since) { $line.Substring(0, [Math]::Min(400, $line.Length)) }
    } | Select-Object -Last $Tail
}

# Probe files written by scripts (see agent instructions) -- newest first.
$probe = Join-Path $nt 'claude_probe'
if (Test-Path $probe) {
    Write-Host "== probe files"
    Get-ChildItem $probe | Sort-Object LastWriteTime -Desc | Select-Object -First 10 Name, Length, LastWriteTime | Format-Table -AutoSize | Out-String | Write-Host
}
