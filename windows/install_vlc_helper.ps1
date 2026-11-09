# Installs the EPG VLC helper on Windows: registers the epgvlc:// link type so the
# EPG Manager dashboard can open videos in VLC with one click.  Run once per user.
# Needs no administrator rights (it writes to HKEY_CURRENT_USER).
$ErrorActionPreference = 'Stop'

$vlc = @("$env:ProgramFiles\VideoLAN\VLC\vlc.exe", "${env:ProgramFiles(x86)}\VideoLAN\VLC\vlc.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $vlc) {
    Write-Error 'VLC was not found. Install VLC first, then run this again.'
    exit 1
}

$dir = Join-Path $env:LOCALAPPDATA 'EPGVLC'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$handler = Join-Path $dir 'open-in-vlc.ps1'

$script = @'
param([string]$link)
$prefix = 'epgvlc://play/'
if ($link -and $link.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    $encoded = $link.Substring($prefix.Length).TrimEnd('/')
    $media = [System.Uri]::UnescapeDataString($encoded)
    Start-Process -FilePath VLCPATH -ArgumentList ('"' + $media + '"')
}
'@
$script = $script.Replace('VLCPATH', "'" + $vlc.Replace("'", "''") + "'")
Set-Content -Path $handler -Value $script -Encoding UTF8

$key = 'HKCU:\Software\Classes\epgvlc'
New-Item -Path $key -Force | Out-Null
Set-ItemProperty -Path $key -Name '(Default)' -Value 'URL:EPG VLC'
New-ItemProperty -Path $key -Name 'URL Protocol' -Value '' -Force | Out-Null
New-Item -Path "$key\shell\open\command" -Force | Out-Null
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$command = '"' + $powershell + '" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $handler + '" "%1"'
Set-ItemProperty -Path "$key\shell\open\command" -Name '(Default)' -Value $command

Write-Host ''
Write-Host "Installed. VLC: $vlc"
Write-Host 'Test it (VLC should open and play): press Win+R and paste:'
Write-Host '  epgvlc://play/http%3A%2F%2F192.168.1.176%3A5001%2Fepg-web%2Fapi%2Fplay%2Fproxy%3Fchannel_id%3D247.andygriffithshow'
