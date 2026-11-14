# gpuq.ps1 - single-GPU arbiter for this laptop.
# Written 2026-11-13. PURE ASCII ON PURPOSE:
#   PS 5.1 reads BOM-less files as ANSI; any CJK here would turn into mojibake
#   (that exact bug already created a garbage folder on the Desktop on 2026-11-10).
# Also avoids PS7-only syntax such as "if (...) {...} else {...}" used as an expression.
# Spec (Chinese, same folder): the dated .md next to this script.
# NOTE: the CJK filename above is the one non-ASCII string; keep the rest ASCII only.
param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet('status','acquire','heartbeat','release','log','doctor','run','init')]
    [string]$Verb,
    [string]$Label = 'unnamed',
    [int]$MemMiB = 13000,
    [int]$EtaMin = 240,
    [string]$Id = '',
    [string]$Owner = '',
    [string]$Command = '',
    [int]$Tail = 20,
    [string]$Day = '',
    [switch]$Try,
    [switch]$Override,
    [switch]$Fast,
    [switch]$Fix
)

$ErrorActionPreference = 'Stop'
$Root      = Join-Path $env:USERPROFILE '.gpuq'
$StatePath = Join-Path $Root 'state.json'
$MutexPath = Join-Path $Root 'mutex.lock'
$QueueDir  = Join-Path $Root 'queue'
$LogPath   = Join-Path $Root 'log.jsonl'
$CfgPath   = Join-Path $Root 'config.json'

$Defaults = [ordered]@{
    blackout_start_hour = 20
    blackout_end_hour   = 1
    reserve_mib         = 1500
    gpu_max_util        = 10
    confirm_samples     = 3
    confirm_interval_s  = 60
    lease_min           = 10
    grace_min           = 5
}

function Ensure-Dirs {
    if (-not (Test-Path $Root))     { New-Item -ItemType Directory -Path $Root -Force | Out-Null }
    if (-not (Test-Path $QueueDir)) { New-Item -ItemType Directory -Path $QueueDir -Force | Out-Null }
    if (-not (Test-Path $CfgPath))  {
        ($Defaults | ConvertTo-Json) | Out-File -FilePath $CfgPath -Encoding utf8
    }
}

function Get-Cfg {
    Ensure-Dirs
    $c = $null
    try { $c = Get-Content $CfgPath -Raw -Encoding utf8 | ConvertFrom-Json } catch { }
    $o = [ordered]@{}
    foreach ($k in $Defaults.Keys) {
        if (($null -ne $c) -and ($null -ne $c.$k)) { $o[$k] = $c.$k } else { $o[$k] = $Defaults[$k] }
    }
    return [pscustomobject]$o
}

function Now-Iso { (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz') }

function Write-Event([string]$ev, [string]$eid, [string]$lab, [string]$own, [string]$detail) {
    $rec = [ordered]@{ ts = (Now-Iso); event = $ev; id = $eid; label = $lab; owner = $own; detail = $detail }
    ($rec | ConvertTo-Json -Compress) | Out-File -FilePath $LogPath -Encoding utf8 -Append
}

function Read-State {
    if (-not (Test-Path $StatePath)) { return $null }
    try { return (Get-Content $StatePath -Raw -Encoding utf8 | ConvertFrom-Json) } catch { return $null }
}

function Write-State($obj) {
    if ($null -eq $obj) {
        if (Test-Path $StatePath) { Remove-Item $StatePath -Force }
    } else {
        ($obj | ConvertTo-Json) | Out-File -FilePath $StatePath -Encoding utf8
    }
}

function Lock-Acquire([int]$timeoutSec = 120) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $fs = [System.IO.File]::Open($MutexPath, [System.IO.FileMode]::CreateNew,
                                         [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            $fs.Close()
            return $true
        } catch { Start-Sleep -Milliseconds 250 }
    }
    if (Test-Path $MutexPath) {
        $age = (Get-Date) - (Get-Item $MutexPath).LastWriteTime
        if ($age.TotalSeconds -gt 60) { Remove-Item $MutexPath -Force -ErrorAction SilentlyContinue }
    }
    return $false
}
function Lock-Release { if (Test-Path $MutexPath) { Remove-Item $MutexPath -Force -ErrorAction SilentlyContinue } }

function Get-Gpu {
    $usedRaw = & nvidia-smi --query-gpu=memory.used       --format=csv,noheader,nounits 2>$null
    $totRaw  = & nvidia-smi --query-gpu=memory.total      --format=csv,noheader,nounits 2>$null
    $utilRaw = & nvidia-smi --query-gpu=utilization.gpu   --format=csv,noheader,nounits 2>$null
    if (-not $usedRaw -or -not $totRaw -or -not $utilRaw) { return $null }
    try {
        $u = [int]((@($usedRaw)[0]).ToString().Trim())
        $t = [int]((@($totRaw)[0]).ToString().Trim())
        $p = [int]((@($utilRaw)[0]).ToString().Trim())
    } catch { return $null }
    return [pscustomobject]@{ UsedMiB = $u; TotalMiB = $t; FreeMiB = ($t - $u); Util = $p }
}

function In-Blackout {
    $c = Get-Cfg
    $h = (Get-Date).Hour
    $s = [int]$c.blackout_start_hour
    $e = [int]$c.blackout_end_hour
    if ($s -gt $e) { return (($h -ge $s) -or ($h -lt $e)) }
    return (($h -ge $s) -and ($h -lt $e))
}

function Test-Stale($st) {
    if ($null -eq $st) { return $false }
    $c = Get-Cfg
    $exp = [datetime]::Parse($st.expires)
    return ((Get-Date) -gt $exp.AddMinutes([int]$c.grace_min))
}

function Show-Status {
    $c = Get-Cfg
    $g = Get-Gpu
    $st = Read-State
    Write-Host "=== GPU ==="
    if ($null -eq $g) {
        Write-Host "  nvidia-smi unreadable"
    } else {
        Write-Host ("  vram {0}/{1} MiB (free {2})   util {3}%" -f $g.UsedMiB, $g.TotalMiB, $g.FreeMiB, $g.Util)
    }
    $winTxt = 'free'
    if (In-Blackout) { $winTxt = 'IN BLACKOUT (grants denied)' }
    Write-Host ("  blackout window {0}:00-{1}:00   now: {2}" -f $c.blackout_start_hour, $c.blackout_end_hour, $winTxt)
    Write-Host "=== holder ==="
    if ($null -eq $st) {
        Write-Host "  (idle)"
    } else {
        $rem = ([datetime]::Parse($st.expires) - (Get-Date)).TotalMinutes
        Write-Host ("  id={0}  label={1}  owner={2}  pid={3}" -f $st.id, $st.label, $st.owner, $st.pid)
        $tail = ''
        if (Test-Stale $st) { $tail = '   [EXPIRED - reclaimable]' }
        Write-Host ("  since {0}   lease left {1:N1} min{2}" -f $st.since, $rem, $tail)
        if ($st.note) { Write-Host ("  note: " + $st.note) }
    }
    Write-Host "=== queue ==="
    $q = @()
    if (Test-Path $QueueDir) { $q = @(Get-ChildItem $QueueDir -File | Sort-Object Name) }
    if ($q.Count -eq 0) { Write-Host "  (empty)" }
    else { foreach ($f in $q) { Write-Host ("  " + (Get-Content $f.FullName -Raw -Encoding utf8).Trim()) } }
}

function Invoke-Acquire {
    $c = Get-Cfg
    $g = Get-Gpu
    if ($null -eq $g) { Write-Host "nvidia-smi unreadable; refusing"; exit 3 }
    $need = $MemMiB + [int]$c.reserve_mib

    if ($Try) {
        $st = Read-State
        if (($null -ne $st) -and (-not (Test-Stale $st))) { Write-Host ("BUSY label=" + $st.label + " id=" + $st.id); exit 2 }
        if ((In-Blackout) -and (-not $Override)) { Write-Host "DENY: blackout window"; exit 2 }
        if ($g.FreeMiB -lt $need) { Write-Host ("DENY: vram free=" + $g.FreeMiB + " need=" + $need); exit 2 }
        if ($g.Util -ge [int]$c.gpu_max_util) { Write-Host ("DENY: util=" + $g.Util + "%"); exit 2 }
        Write-Host "OK: would be granted"
        exit 0
    }

    # [long], not [int]: a yyyyMMddHHmmss stamp is a 14-digit number and overflows Int32, so
    # this cast threw before the queue file was ever written. The throw aborted Invoke-Acquire
    # with no message, so blocking `acquire` and `run` (which is built on it) had never granted
    # a token -- only `acquire -Try`, `status` and `doctor` worked. Fixed 2026-11-13.
    $myseq = [long](Get-Date -Format 'yyyyMMddHHmmss')
    $safeLabel = ($Label -replace '[^A-Za-z0-9_\-]', '_')
    $reqFile = Join-Path $QueueDir ("{0:D14}_{1}.json" -f $myseq, $safeLabel)
    if (-not (Test-Path $reqFile)) {
        $req = [ordered]@{ seq = $myseq; label = $Label; owner = $Owner; mem_mib = $MemMiB; eta_min = $EtaMin; since = (Now-Iso) }
        ($req | ConvertTo-Json -Compress) | Out-File -FilePath $reqFile -Encoding utf8
    }

    $streak = 0
    while ($true) {
        if (Lock-Acquire) {
            try {
                $st = Read-State
                $busy = ($null -ne $st) -and (-not (Test-Stale $st))
                $g2 = Get-Gpu
                $okMem  = ($null -ne $g2) -and ($g2.FreeMiB -ge $need)
                $okUtil = ($null -ne $g2) -and ($g2.Util -lt [int]$c.gpu_max_util)
                $okWin  = (-not (In-Blackout)) -or $Override

                if ($okWin -and $okMem -and $okUtil -and (-not $busy)) { $streak++ } else { $streak = 0 }

                $needConfirm = [int]$c.confirm_samples; if ($Fast) { $needConfirm = 1 }
    if ($streak -ge $needConfirm) {
                    if (($null -ne $st) -and (Test-Stale $st)) {
                        Write-Event 'steal' $st.id $st.label $st.owner ('stale lease stolen by ' + $Label)
                    }
                    $chars = @(48..57) + @(97..102)
                    $newId = -join ($chars | Get-Random -Count 6 | ForEach-Object { [char]$_ })
                    $obj = [ordered]@{
                        id      = $newId
                        label   = $Label
                        owner   = $Owner
                        pid     = 0
                        since   = (Now-Iso)
                        expires = (Get-Date).AddMinutes([int]$c.lease_min).ToString('yyyy-MM-ddTHH:mm:sszzz')
                        mem_mib = $MemMiB
                        eta_min = $EtaMin
                        note    = ''
                    }
                    Write-State $obj
                    $freeTxt = 'n/a'
                    $utilTxt = 'n/a'
                    if ($null -ne $g2) { $freeTxt = $g2.FreeMiB; $utilTxt = $g2.Util }
                    Write-Event 'grant' $newId $Label $Owner ("mem={0} need={1} free={2} util={3}" -f $MemMiB, $need, $freeTxt, $utilTxt)
                    if (Test-Path $reqFile) { Remove-Item $reqFile -Force }
                    Write-Host ("ACQUIRED id=" + $newId + " label=" + $Label)
                    exit 0
                } else {
                    $why = @()
                    if (-not $okWin) { $why += 'blackout' }
                    if ($busy) { $why += ('busy:' + $st.label) }
                    if (-not $okMem) {
                        $f = '?'; if ($null -ne $g2) { $f = $g2.FreeMiB }
                        $why += ('vram:' + $f)
                    }
                    if (-not $okUtil) {
                        $u2 = '?'; if ($null -ne $g2) { $u2 = $g2.Util }
                        $why += ('util:' + $u2)
                    }
                    Write-Host ("queued ({0}/{1} confirm) [{2}]" -f $streak, [int]$c.confirm_samples, ($why -join ','))
                }
            } finally { Lock-Release }
        }
        Start-Sleep -Seconds ([int]$c.confirm_interval_s)
    }
}

function Invoke-Heartbeat {
    if (-not (Lock-Acquire)) { Write-Host "mutex timeout"; exit 3 }
    try {
        $st = Read-State
        if ($null -eq $st) { Write-Host "no holder"; exit 2 }
        if ($Id -and ($st.id -ne $Id)) { Write-Host ("id mismatch (current " + $st.id + ")"); exit 2 }
        $c = Get-Cfg
        $st.expires = (Get-Date).AddMinutes([int]$c.lease_min).ToString('yyyy-MM-ddTHH:mm:sszzz')
        Write-State $st
        Write-Event 'heartbeat' $st.id $st.label $st.owner ('expires=' + $st.expires)
        Write-Host ("renewed " + $st.id + " until " + $st.expires)
    } finally { Lock-Release }
}

function Invoke-Release {
    if (-not (Lock-Acquire)) { Write-Host "mutex timeout"; exit 3 }
    try {
        $st = Read-State
        if ($null -eq $st) { Write-Host "(already idle)"; exit 0 }
        if ($Id -and ($st.id -ne $Id)) { Write-Host ("id mismatch (current " + $st.id + "); refusing"); exit 2 }
        Write-State $null
        Write-Event 'release' $st.id $st.label $st.owner 'released'
        Write-Host ("released " + $st.id)
    } finally { Lock-Release }
}

function Invoke-Run {
    if (-not $Command) { Write-Host "run requires -Command"; exit 2 }
    $args1 = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath,'acquire','-Label',$Label,'-MemMiB',$MemMiB,'-EtaMin',$EtaMin)
    if ($Owner) { $args1 += @('-Owner',$Owner) }
    $out = & powershell.exe @args1
    $out | ForEach-Object { Write-Host $_ }
    $line = $out | Where-Object { $_ -match '^ACQUIRED id=' } | Select-Object -First 1
    if (-not $line) { Write-Host "not granted; abort"; exit 2 }
    $myid = (($line -replace '^ACQUIRED id=', '') -split '\s+')[0]
    Write-Host ("running: " + $Command)
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-Command',$Command) -PassThru -NoNewWindow
    try {
        while (-not $p.HasExited) {
            Start-Sleep -Seconds 60
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath heartbeat -Id $myid | Out-Null
        }
    } finally {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath release -Id $myid | Out-Null
    }
    Write-Host ("child exit code: " + $p.ExitCode)
    exit $p.ExitCode
}

function Show-Log {
    if (-not (Test-Path $LogPath)) { Write-Host "(no log)"; return }
    $lines = @(Get-Content $LogPath -Encoding utf8)
    if ($Day) { $lines = @($lines | Where-Object { $_ -match [regex]::Escape($Day) }) }
    if ($lines.Count -eq 0) { Write-Host "(nothing)"; return }
    $lines | Select-Object -Last $Tail
}

function Invoke-Doctor {
    $c = Get-Cfg
    $g = Get-Gpu
    $st = Read-State
    Write-Host ("=== doctor " + (Now-Iso) + " ===")
    if ($null -eq $g) {
        Write-Host "[FAIL] nvidia-smi unreadable"
    } else {
        Write-Host ("[INFO] vram {0}/{1} MiB free={2} util={3}%" -f $g.UsedMiB, $g.TotalMiB, $g.FreeMiB, $g.Util)
        if ($g.FreeMiB -lt (13000 + [int]$c.reserve_mib)) { Write-Host "[WARN] free vram below the big-job threshold" }
    }
    if ($null -ne $st) {
        if (Test-Stale $st) {
            Write-Host ("[WARN] stale lease id={0} label={1} expires={2}" -f $st.id, $st.label, $st.expires)
            if ($Fix) {
                Write-State $null
                Write-Event 'steal' $st.id $st.label $st.owner 'doctor -Fix cleaned stale lease'
                Write-Host "       cleaned"
            }
        } else {
            Write-Host ("[INFO] live lease id={0} label={1}" -f $st.id, $st.label)
        }
    } else { Write-Host "[INFO] no holder" }
    if (In-Blackout) { Write-Host "[WARN] inside blackout window" }

    $reboot = @()
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
        'HKLM:\SOFTWARE\Microsoft\Windows\WindowsUpdate\Auto Update\RebootRequired',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
    )
    foreach ($k in $keys) { if (Test-Path $k) { $reboot += $k } }
    $sm = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager'
    if (Test-Path $sm) {
        $pfro = Get-ItemProperty $sm -Name PendingFileRenameOperations -ErrorAction SilentlyContinue
        if ($pfro) { $reboot += 'PendingFileRenameOperations' }
    }
    if ($reboot.Count -gt 0) {
        Write-Host "[WARN] pending reboot (this is what killed the 2026-11-12 run):"
        foreach ($r in $reboot) { Write-Host ("       " + $r) }
    } else { Write-Host "[INFO] no pending reboot" }

    $q = @(); if (Test-Path $QueueDir) { $q = @(Get-ChildItem $QueueDir -File) }
    Write-Host ("[INFO] queue length " + $q.Count)
    $drv = Get-PSDrive C
    Write-Host ("[INFO] C: free {0:N0} GB" -f ($drv.Free / 1GB))
    if (($drv.Free / 1GB) -lt 30) { Write-Host "[WARN] low disk" }
}

Ensure-Dirs
switch ($Verb) {
    'init'      { Write-Host ("state dir: " + $Root); Get-Cfg | ConvertTo-Json }
    'status'    { Show-Status }
    'acquire'   { Invoke-Acquire }
    'heartbeat' { Invoke-Heartbeat }
    'release'   { Invoke-Release }
    'run'       { Invoke-Run }
    'log'       { Show-Log }
    'doctor'    { Invoke-Doctor }
}
