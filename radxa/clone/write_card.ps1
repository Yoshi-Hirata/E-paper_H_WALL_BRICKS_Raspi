<#
.SYNOPSIS
  Write one cloned Radxa microSD: golden image + per-unit /config + GPT fix.

.EXAMPLE
  .\radxa\clone\write_card.ps1 -Unit 5 -Disk 2
    Writes D:\radxa-golden\radxa-01-golden.img to \\.\PhysicalDrive2 and
    names the card radxa-05 (192.168.50.105 after first boot).

.NOTES
  Needs WSL (Ubuntu, with gdisk installed) for the 16 MB /config patch,
  and raises a UAC prompt for the raw disk write. Windows itself never
  sees the card's partitions (their type GUIDs are not "basic data"),
  and `wsl --mount` refuses USB card readers, so raw access through an
  elevated Python is the only path. Every safety check here exists
  because the same primitive can overwrite an internal drive.
#>
param(
    [Parameter(Mandatory)][ValidateRange(1, 99)][int]$Unit,
    [Parameter(Mandatory)][int]$Disk,
    [string]$Image = 'D:\radxa-golden\radxa-01-golden.img',
    [string]$WslDistro = 'Ubuntu',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$nn = '{0:D2}' -f $Unit

# ---- what are we about to overwrite? ----
$d = Get-Disk -Number $Disk
if ($d.BusType -ne 'USB') { throw "disk $Disk is $($d.BusType), not USB - refusing" }
if ($d.Size -lt 8GB -or $d.Size -gt 512GB) { throw "disk $Disk size $($d.Size) is not a microSD - refusing" }
$imgSize = (Get-Item $Image).Length
if ($imgSize -gt $d.Size) { throw "image ($imgSize B) larger than disk ($($d.Size) B)" }
Write-Host ("target : disk {0}  {1}  {2:N1} GB  ({3})" -f $Disk, $d.FriendlyName, ($d.Size / 1GB), $d.OperationalStatus)
Write-Host ("image  : {0}  {1:N2} GB" -f $Image, ($imgSize / 1GB))
Write-Host ("unit   : radxa-{0}  ->  192.168.50.{1}" -f $nn, (100 + $Unit))
if (-not $Force) {
    $answer = Read-Host "Type YES to overwrite disk $Disk"
    if ($answer -ne 'YES') { throw 'aborted' }
}

# ---- per-unit /config partition (WSL) ----
$wslImage = '/mnt/' + $Image.Substring(0, 1).ToLower() + $Image.Substring(2).Replace('\', '/')
$cfgWin = Join-Path (Split-Path $Image) "cfg-$nn.img"
$cfgWsl = '/mnt/' + $cfgWin.Substring(0, 1).ToLower() + $cfgWin.Substring(2).Replace('\', '/')
$mk = '/mnt/' + $here.Substring(0, 1).ToLower() + $here.Substring(2).Replace('\', '/') + '/mkconfig.sh'
$out = wsl -d $WslDistro -u root -- bash -c "bash '$mk' '$wslImage' '$nn' /tmp/cfg-$nn.img && cp /tmp/cfg-$nn.img '$cfgWsl'" 2>&1
$out | ForEach-Object { Write-Host "  $_" }
$offsetLine = $out | Where-Object { $_ -match '^OFFSET (\d+)$' } | Select-Object -Last 1
if (-not $offsetLine) { throw 'mkconfig.sh did not report the partition offset' }
$offset = [int64]($offsetLine -replace 'OFFSET ', '')

# ---- raw write (elevated) ----
$python = & python -c "import sys; print(sys.executable)"
$log = Join-Path $env:TEMP "write_card_$nn.log"
Remove-Item $log -ErrorAction SilentlyContinue
$args = @("`"$here\rawdisk.py`"", 'write', "$Disk", "`"$Image`"", "`"$log`"", "$offset", "`"$cfgWin`"")
$inner = "Set-Disk -Number $Disk -IsOffline `$true -ErrorAction SilentlyContinue; & `"$python`" $($args -join ' ')"
$p = Start-Process powershell.exe -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $inner -Verb RunAs -PassThru -WindowStyle Hidden
Write-Host "writing (UAC prompt, then a few minutes)..."
$last = ''
while (-not $p.HasExited) {
    Start-Sleep -Seconds 5
    $line = Get-Content $log -Tail 1 -ErrorAction SilentlyContinue
    if ($line -and $line -ne $last) { Write-Host "  $line"; $last = $line }
}
$tail = Get-Content $log -ErrorAction SilentlyContinue
$tail | Select-Object -Last 4 | ForEach-Object { Write-Host "  $_" }
if (-not ($tail -match '^DONE ')) { throw "write failed - see $log" }
Write-Host "radxa-$nn written. Boot it; rsetup applies before.txt, then epaper-firstboot sets 192.168.50.$(100 + $Unit)."
