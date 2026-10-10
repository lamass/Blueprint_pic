<#
  nt_compile.ps1 -- compile NinjaScript files OUTSIDE NinjaTrader, against the real NT8 DLLs.

  Mirrors Documents\NinjaTrader 8\bin\Custom into a scratch folder, drops the candidate .cs
  files in (replacing any live copy of the same name), registers them in the csproj (NT keeps
  an explicit <Compile Include> list), and runs MSBuild. Never touches the live Custom folder
  or the live NinjaTrader.Custom.dll, so it is safe while NinjaTrader is running.

  Usage:
    .\nt_compile.ps1 -Files ..\AyniOrderFlowStrategy.cs
    .\nt_compile.ps1 -Files a.cs,b.cs

  Exit code 0 = compiled clean; 1 = compile errors (printed as file(line,col): error CSxxxx ...).
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Files,
    [string]$NtCustom = "$env:USERPROFILE\Documents\NinjaTrader 8\bin\Custom",
    [string]$Work     = "$env:TEMP\claude\ntbuild"
)
$ErrorActionPreference = 'Stop'

$msbuild = @(
    "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
    "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $msbuild) { throw "MSBuild not found" }

$custom = Join-Path $Work 'Custom'
$out    = Join-Path $Work 'out'
New-Item -ItemType Directory -Force $custom, $out | Out-Null

# Fresh mirror of the live project (skip build outputs and the live DLL).
robocopy $NtCustom $custom /MIR /XD bin obj /XF NinjaTrader.Custom.dll NinjaTrader.Custom.pdb NinjaTrader.Custom.xml /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }

$csproj = Join-Path $custom 'NinjaTrader.Custom.csproj'
$projText = Get-Content $csproj -Raw
$newItems = @()

foreach ($f in $Files) {
    $full = (Resolve-Path $f).Path
    $src  = Get-Content $full -Raw
    # Pick the NinjaScript folder from the namespace the file declares.
    $folder = switch -Regex ($src) {
        'namespace\s+NinjaTrader\.NinjaScript\.Strategies'    { 'Strategies'; break }
        'namespace\s+NinjaTrader\.NinjaScript\.Indicators'    { 'Indicators'; break }
        'namespace\s+NinjaTrader\.NinjaScript\.DrawingTools'  { 'DrawingTools'; break }
        'namespace\s+NinjaTrader\.NinjaScript\.AddOns'        { 'AddOns'; break }
        'namespace\s+NinjaTrader\.NinjaScript\.BarsTypes'     { 'BarsTypes'; break }
        default { throw "Can't tell NinjaScript type from namespace in $f" }
    }
    $name = Split-Path $full -Leaf
    Copy-Item $full (Join-Path $custom "$folder\$name") -Force
    $rel = "$folder\$name"
    if ($projText -notmatch [regex]::Escape("Include=`"$rel`"")) { $newItems += "    <Compile Include=`"$rel`" />" }
    Write-Host "candidate: $rel"
}

if ($newItems) {
    $block = "  <ItemGroup>`r`n" + ($newItems -join "`r`n") + "`r`n  </ItemGroup>`r`n</Project>"
    $projText = $projText -replace '</Project>\s*$', $block
    Set-Content -Path $csproj -Value $projText -Encoding UTF8
}

$log = Join-Path $Work 'build.log'
& $msbuild $csproj -restore -nologo -v:q -clp:ErrorsOnly -p:Configuration=Debug "-p:OutputPath=$out\" "-flp:LogFile=$log;Verbosity=minimal" | Out-Host
$code = $LASTEXITCODE

if ($code -eq 0) { Write-Host "COMPILE OK -> $out\NinjaTrader.Custom.dll" } else { Write-Host "COMPILE FAILED (see $log)" }
exit ([int]($code -ne 0))
