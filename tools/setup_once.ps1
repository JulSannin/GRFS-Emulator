# What this machine needs once before tools/rig.py can run the real game against a local server,
# and which only an administrator can do:
#   - the hosts line sending the game's configuration request here (127.0.0.1);
#   - Ubisoft Connect, from the installer the game ships with, if it is not installed yet.
# Run it again any time: it changes nothing that is already right.
#
#   powershell -ExecutionPolicy Bypass -File tools\setup_once.ps1
#
# It asks for elevation itself. Signing in to Ubisoft Connect is left to you: nothing here
# touches the account.

# -HostsFile edits another file instead, and does nothing else: for the tests.
param([string]$Game = "", [string]$Address = "127.0.0.1", [string]$HostsFile = "")

$ErrorActionPreference = "Stop"
$hostName = "onlineconfigservice.ubi.com"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin -and -not $HostsFile) {
    $passOn = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
                "-Address", $Address)
    if ($Game) { $passOn += @("-Game", "`"$Game`"") }
    Start-Process powershell -Verb RunAs -ArgumentList $passOn -Wait
    exit
}

# ----- hosts ---------------------------------------------------------------------------------
$hosts = if ($HostsFile) { $HostsFile } else { Join-Path $env:SystemRoot "System32\drivers\etc\hosts" }
$text = [IO.File]::ReadAllText($hosts)
$lines = $text -split "`r?`n"
$mine = $lines | Where-Object {
    $fields = ($_ -split "#", 2)[0].Trim() -split "\s+"
    $fields.Count -ge 2 -and ($fields[1..($fields.Count - 1)] -contains $hostName)
}
if ($mine | Where-Object { (($_ -split "#", 2)[0].Trim() -split "\s+")[0] -eq $Address }) {
    Write-Host "hosts: $hostName -> $Address already"
} else {
    if ($mine) {
        # Another address for it: commented out, not deleted, so it can be put back by hand.
        $lines = $lines | ForEach-Object { if ($mine -contains $_) { "# (grfs) $_" } else { $_ } }
        $text = ($lines -join "`r`n")
    }
    if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $text += "`r`n" }
    $text += "$Address $hostName`r`n"
    [IO.File]::WriteAllText($hosts, $text, (New-Object Text.UTF8Encoding $false))
    if (-not $HostsFile) { ipconfig /flushdns | Out-Null }
    Write-Host "hosts: added $Address $hostName"
}
if ($HostsFile) { exit }

# ----- Ubisoft Connect -----------------------------------------------------------------------
$installed = @("${env:ProgramFiles(x86)}\Ubisoft\Ubisoft Game Launcher",
               "$env:ProgramFiles\Ubisoft\Ubisoft Game Launcher") | Where-Object { Test-Path $_ }
if ($installed) {
    Write-Host "Ubisoft Connect: installed in $($installed[0])"
} else {
    if (-not $Game) {
        $Game = & python -c "import sys; sys.path.insert(0, sys.argv[1]); import launch_game; print(launch_game.find_game() or '')" $PSScriptRoot 2>$null
    }
    $installer = if ($Game) { Join-Path $Game "UbisoftConnectInstaller.exe" } else { "" }
    if ($installer -and (Test-Path $installer)) {
        Write-Host "Ubisoft Connect: running $installer"
        Start-Process $installer -Wait
    } else {
        Write-Host "Ubisoft Connect: not installed, and no installer found - pass -Game <folder>"
    }
}
Write-Host ""
Write-Host "Left for you: start Ubisoft Connect and sign in to the account that owns the game."
Write-Host "Then: python tools\rig.py check"
Read-Host "Enter to close"
