# Reproducible GUI-to-window benchmark: Start-Process -> MainWindowHandle.
# Run from any directory: powershell -File tools\bench_gui_shell.ps1
# Uses the same launch method as the original A/B benchmark so the number is
# comparable with the kensaku reference.

param(
    [string]$Base = (Join-Path $env:TEMP "opencode\afbench"),
    [string]$ShellExe = (Join-Path $PSScriptRoot "..\shell-app\target\release\autofind-shell.exe"),
    [string]$KensakuExe = (Join-Path $env:TEMP "opencode\afbench\portable\kensaku.exe"),
    [int]$Runs = 3
)

function Measure-Launch {
    param([string]$Label, [string]$Exe, [string]$DataDir, [hashtable]$EnvOverrides)

    foreach ($key in $EnvOverrides.Keys) {
        [Environment]::SetEnvironmentVariable($key, $EnvOverrides[$key], "Process")
    }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $proc = Start-Process -FilePath $Exe -WorkingDirectory (Join-Path $PSScriptRoot "..") -PassThru
    $ms = -1
    while ($sw.ElapsedMilliseconds -lt 25000) {
        Start-Sleep -Milliseconds 10
        try { $proc.Refresh() } catch { break }
        if ($proc.HasExited) { break }
        if ($proc.MainWindowHandle -ne [IntPtr]::Zero) {
            $ms = [math]::Round($sw.Elapsed.TotalMilliseconds, 1)
            break
        }
    }
    if (-not $proc.HasExited) { & taskkill /PID $proc.Id /T /F 2>$null | Out-Null }
    foreach ($key in $EnvOverrides.Keys) {
        [Environment]::SetEnvironmentVariable($key, $null, "Process")
    }
    return $ms
}

# A console-subsystem exe makes ShellExecute start conhost first; that alone
# used to cost ~900 ms. Fail loudly here instead of silently benchmarking it.
function Assert-GuiSubsystem {
    param([string]$Path)
    $bytes = [IO.File]::ReadAllBytes($Path)
    $pe = [BitConverter]::ToInt32($bytes, 0x3C)
    $subsystem = [BitConverter]::ToUInt16($bytes, $pe + 24 + 68)
    if ($subsystem -ne 2) {
        throw "$Path has PE subsystem $subsystem (GUI=2). Rebuild the shell with #![windows_subsystem = `"windows`"]."
    }
    Write-Output ("PE subsystem OK (GUI=2): " + $Path)
}
Assert-GuiSubsystem -Path $ShellExe

$af = @()
$ks = @()
for ($i = 1; $i -le $Runs; $i++) {
    $dir = Join-Path $Base ("gui-shell-" + $i)
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $af += Measure-Launch -Label "shell" -Exe $ShellExe -DataDir $dir `
        -EnvOverrides @{ AUTOFIND_HOME = $dir; AUTOFIND_PORT = (18770 + $i) }
    Start-Sleep -Milliseconds 200

    $dir = Join-Path $Base ("gui-ks-" + $i)
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $ks += Measure-Launch -Label "kensaku" -Exe $KensakuExe -DataDir $dir `
        -EnvOverrides @{ KENSAKU_DATA_DIR = $dir }
    Start-Sleep -Milliseconds 200
}

$sortedAf = @($af | Sort-Object)
$sortedKs = @($ks | Sort-Object)
$mid = [int][math]::Floor($Runs / 2)
Write-Output ("AutoFind shell GUI->window ms: " + ($af -join ", ") + " median=" + $sortedAf[$mid])
Write-Output ("Kensaku      GUI->window ms: " + ($ks -join ", ") + " median=" + $sortedKs[$mid])

[pscustomobject]@{
    autofind_ms = $af
    autofind_median = $sortedAf[$mid]
    kensaku_ms = $ks
    kensaku_median = $sortedKs[$mid]
} | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $Base "gui_shell_result.json")
