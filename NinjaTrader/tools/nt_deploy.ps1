<#
  nt_deploy.ps1 -- copy compiled-clean NinjaScript files into the LIVE NinjaTrader Custom folder.

  Refuses to deploy unless nt_compile.ps1 passes first (a single broken file stops NinjaTrader
  from compiling ANY script). Any live file it replaces is backed up to NinjaTrader\backups\<stamp>\.
  NinjaTrader picks the change up and recompiles; if it doesn't, press F5 in the NinjaScript Editor.

  Usage:  .\nt_deploy.ps1 -Files ..\AyniOrderFlowStrategy.cs
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Files,
    [string]$NtCustom = "$env:USERPROFILE\Documents\NinjaTrader 8\bin\Custom"
)
$ErrorActionPreference = 'Stop'
$here = Split-Path $MyInvocation.MyCommand.Path

& (Join-Path $here 'nt_compile.ps1') -Files $Files -NtCustom $NtCustom
if ($LASTEXITCODE -ne 0) { Write-Host "NOT DEPLOYED: fix compile errors first."; exit 1 }

$liveDll   = Join-Path $NtCustom 'NinjaTrader.Custom.dll'
$dllBefore = (Get-Item $liveDll).LastWriteTime

$backup = Join-Path (Split-Path $here) ("backups\" + (Get-Date -Format 'yyyyMMdd_HHmmss'))
foreach ($f in $Files) {
    $full = (Resolve-Path $f).Path
    $src  = Get-Content $full -Raw
    $folder = switch -Regex ($src) {
        'namespace\s+NinjaTrader\.NinjaScript\.Strategies'   { 'Strategies'; break }
        'namespace\s+NinjaTrader\.NinjaScript\.Indicators'   { 'Indicators'; break }
        'namespace\s+NinjaTrader\.NinjaScript\.DrawingTools' { 'DrawingTools'; break }
        'namespace\s+NinjaTrader\.NinjaScript\.AddOns'       { 'AddOns'; break }
        'namespace\s+NinjaTrader\.NinjaScript\.BarsTypes'    { 'BarsTypes'; break }
    }
    $target = Join-Path $NtCustom "$folder\$(Split-Path $full -Leaf)"
    if (Test-Path $target) {
        New-Item -ItemType Directory -Force (Join-Path $backup $folder) | Out-Null
        Copy-Item $target (Join-Path $backup "$folder\") -Force
        Write-Host "backed up live $folder\$(Split-Path $target -Leaf) -> $backup"
    }
    Copy-Item $full $target -Force
    Write-Host "DEPLOYED -> $target"
}

# NinjaTrader only recompiles on an external file change while a NinjaScript Editor window is open
# (verified 2026-10-07: rebuilt ~6s after the change). Without the editor the new script stays invisible.
if (-not (Get-Process NinjaTrader -ErrorAction SilentlyContinue)) {
    Write-Host "NinjaTrader is not running -- it will compile on next start."
    exit 0
}
$waited = 0
while ($waited -lt 30 -and (Get-Item $liveDll).LastWriteTime -eq $dllBefore) { Start-Sleep 2; $waited += 2 }
if ((Get-Item $liveDll).LastWriteTime -ne $dllBefore) {
    Write-Host "NINJATRADER RECOMPILED (${waited}s) -- script is available in NT."
} else {
    Write-Host "NOT RECOMPILED BY NINJATRADER -- open New > NinjaScript Editor (leave it open) and press F5."
    exit 2
}
