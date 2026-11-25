<#
.SYNOPSIS
  Keeps a pymobiledevice3 RemoteServiceDiscovery tunnel (tunneld) running so
  mpi can reach iOS 17+ developer (DVT) services automatically.

.DESCRIPTION
  iOS 17+ (and iOS 26) require a RemoteXPC tunnel for `developer dvt` commands
  (launch / sysmon) used by the live iOS capture. The tunnel needs Administrator
  rights because it creates a WinTun virtual network adapter.

  Once tunneld is running on its default address, pymobiledevice3 -- and
  therefore mpi -- auto-discovers it. No environment variables and no
  app changes are required.

  Modes:
    -Install    Register a scheduled task that runs this script (daemon mode) at
                every logon with highest privileges, then start it now.
                Self-elevates (one UAC prompt).
    -Uninstall  Remove that scheduled task. Self-elevates.
    -Status     Show whether the task is registered / running and tail the log.
    (default)   Run the tunneld daemon loop. This is what the scheduled task
                launches; you can also run it directly from an elevated terminal.

.PARAMETER Python
  Interpreter that has pymobiledevice3 installed. Defaults to
  C:\Python314\python.exe, then falls back to `python` on PATH.

.EXAMPLE
  # One-time setup (approve the UAC prompt):
  powershell -ExecutionPolicy Bypass -File scripts\ios-tunnel.ps1 -Install

.EXAMPLE
  # Remove the automatic tunnel:
  powershell -ExecutionPolicy Bypass -File scripts\ios-tunnel.ps1 -Uninstall
#>
[CmdletBinding(DefaultParameterSetName = 'Run')]
param(
    [Parameter(ParameterSetName = 'Install')]   [switch]$Install,
    [Parameter(ParameterSetName = 'Uninstall')] [switch]$Uninstall,
    [Parameter(ParameterSetName = 'Status')]    [switch]$Status,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$TaskName = 'MPI iOS Tunnel'
$LegacyTaskName = 'PerfInspect iOS Tunnel'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Resolve-Python {
    param([string]$Preferred)
    foreach ($cand in @($Preferred, 'C:\Python314\python.exe')) {
        if ($cand -and (Test-Path $cand)) { return (Resolve-Path $cand).Path }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "No Python interpreter with pymobiledevice3 found. Pass -Python <path>."
}

function Invoke-Elevated {
    param([string[]]$Extra)
    $argList = @(
        '-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`""
    ) + $Extra
    # Wait for the elevated transaction. Electron keeps one shared promise
    # alive until this wrapper exits, preventing another measurement window
    # from dispatching an overlapping migration.
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs `
        -ArgumentList $argList -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "Elevated tunnel migration failed with exit code $($process.ExitCode)."
    }
}

$LogDir = Join-Path $env:LOCALAPPDATA 'MPI'
$LegacyLogDir = Join-Path $env:LOCALAPPDATA 'perfinspect'
$LogPath = Join-Path $LogDir 'ios-tunnel.log'
$TunneldUrl = 'http://127.0.0.1:49151/'

function Write-Log {
    param(
        [string]$Level,
        [string]$Message
    )
    "$(Get-Date -Format o) [$Level] $Message" | Add-Content $LogPath
}

function Test-TunneldApi {
    try {
        $null = Invoke-WebRequest -UseBasicParsing -Uri $TunneldUrl -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

function Assert-ScheduledTaskRunning {
    param([string]$Name)
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        if ($task.State -eq 'Running') { return }
        Start-Sleep -Milliseconds 250
    }
    throw "restored task '$Name' did not enter the Running state"
}

function Get-ScheduledTaskEnginePids {
    param([string]$Name)
    try {
        $service = New-Object -ComObject 'Schedule.Service'
        $service.Connect()
        $task = $service.GetFolder('\').GetTask($Name)
        return @($task.GetInstances(0) | ForEach-Object { [int]$_.EnginePID })
    } catch {
        return @()
    }
}

$TunneldOutLog = Join-Path $LogDir 'tunneld.out.log'
$TunneldErrLog = Join-Path $LogDir 'tunneld.err.log'

function Stop-TunneldPortOwner {
    param([switch]$Strict)
    # Kill whatever still LISTENS on tunneld's port before starting a fresh
    # child. Covers orphaned tunneld processes this wrapper does not own (e.g.
    # left behind by a previous wrapper instance after a task restart): a
    # wedged orphan would otherwise hold 49151 forever while every new child
    # fails to bind and exits, putting the loop into a restart spin.
    try {
        $owners = Get-NetTCPConnection -LocalPort 49151 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($ownerPid in $owners) {
            if ($ownerPid -and $ownerPid -ne $PID) {
                Write-Log 'warn' "killing stale listener on 49151 (pid $ownerPid) before restart"
                Stop-Process -Id $ownerPid -Force -ErrorAction Stop
            }
        }
        if ($Strict -and $owners) {
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                $remaining = @(
                    Get-NetTCPConnection -LocalPort 49151 -State Listen `
                        -ErrorAction SilentlyContinue
                )
                if ($remaining.Count -eq 0) { break }
                Start-Sleep -Milliseconds 100
            }
            if ($remaining.Count -gt 0) {
                throw 'port 49151 still has a listener after cleanup'
            }
        }
    } catch {
        Write-Log 'error' "could not clear port 49151: $($_.Exception.Message)"
        if ($Strict) { throw }
    }
}

function Start-Tunneld {
    param([string]$PythonPath)
    # Run tunneld in the FOREGROUND of its own child process -- NOT with
    # --daemonize. pymobiledevice3's --daemonize relies on the unix-only
    # `daemonize`/`fcntl` modules and aborts immediately on Windows (the child
    # exits with 0xC0000142 before tunneld ever binds the port), so the API
    # never comes up. This PowerShell loop is itself the long-running daemon
    # (kept alive by the scheduled task); it owns the tunneld child directly and
    # restarts it whenever it exits.
    $argList = @(
        '-m', 'pymobiledevice3', 'remote', 'tunneld',
        '--host', '127.0.0.1', '--port', '49151'
    )
    Write-Log 'info' "starting tunneld: `"$PythonPath`" $($argList -join ' ')"
    return Start-Process -FilePath $PythonPath -ArgumentList $argList `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $TunneldOutLog `
        -RedirectStandardError $TunneldErrLog
}

function Write-TunneldChildLog {
    # Fold the most recent tunneld child output into the main log so a crash or
    # an "admin required" message is visible in one place.
    foreach ($f in @($TunneldErrLog, $TunneldOutLog)) {
        if (Test-Path $f) {
            foreach ($line in (Get-Content $f -Tail 20 -ErrorAction SilentlyContinue)) {
                if ($line) { Write-Log 'pmd3' $line.ToString() }
            }
        }
    }
}

# ---------------------------------------------------------------- Install ----
if ($Install) {
    if (-not (Test-Admin)) {
        $elevatedArgs = @('-Install')
        if ($Python) {
            $elevatedArgs += @('-Python', "`"$Python`"")
        }
        Invoke-Elevated -Extra $elevatedArgs
        return
    }

    $migrationMutex = [System.Threading.Mutex]::new(
        $false,
        'Global\MPI.iOS.Tunnel.Migration'
    )
    $ownsMigrationMutex = $false
    try {
        try {
            $ownsMigrationMutex = $migrationMutex.WaitOne(
                [TimeSpan]::FromMinutes(3)
            )
        } catch [System.Threading.AbandonedMutexException] {
            $ownsMigrationMutex = $true
        }
        if (-not $ownsMigrationMutex) {
            throw 'Another MPI iOS tunnel migration is still in progress.'
        }

        $py = Resolve-Python -Preferred $Python
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $daemonArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass " +
                      "-File `"$PSCommandPath`" -Python `"$py`""

        $action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $daemonArgs
        $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $me
        $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest
        $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                        -DontStopIfGoingOnBatteries -StartWhenAvailable `
                        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                        -ExecutionTimeLimit ([TimeSpan]::Zero)

        $currentTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $currentTaskXml = if ($currentTask) {
            Export-ScheduledTask -TaskName $TaskName
        } else {
            $null
        }
        $currentWasRunning = $currentTask -and $currentTask.State -eq 'Running'
        $legacyTask = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
        $legacyWasRunning = $legacyTask -and $legacyTask.State -eq 'Running'
        $movedLogs = @()

        try {
            if ($currentTask -and $currentTask.State -eq 'Running') {
                Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
            }
            if ($legacyTask -and $legacyTask.State -eq 'Running') {
                Stop-ScheduledTask -TaskName $LegacyTaskName -ErrorAction Stop
            }
            New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
            # A scheduled-task host can exit while leaving its tunneld child
            # alive. Clear the dedicated listener before the MPI task starts;
            # otherwise an orphan legacy API can make the new task look healthy.
            Stop-TunneldPortOwner -Strict

            if (Test-Path -LiteralPath $LegacyLogDir -PathType Container) {
                foreach ($legacyLog in Get-ChildItem -LiteralPath $LegacyLogDir -File -Force) {
                    $targetName = $legacyLog.Name
                    $target = Join-Path $LogDir $targetName
                    if (Test-Path -LiteralPath $target) {
                        $target = Join-Path $LogDir ("legacy-" + $targetName)
                    }
                    Move-Item -LiteralPath $legacyLog.FullName -Destination $target
                    $movedLogs += [pscustomobject]@{
                        Source = $legacyLog.FullName
                        Target = $target
                    }
                }
            }

            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                -Principal $principal -Settings $settings -Force | Out-Null
            Start-ScheduledTask -TaskName $TaskName
            Assert-ScheduledTaskRunning -Name $TaskName

            $ready = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                if (Test-TunneldApi) {
                    $ready = $true
                    break
                }
                Start-Sleep -Seconds 1
            }
            if (-not $ready) {
                throw "The new '$TaskName' task did not expose the tunneld API."
            }
            Assert-ScheduledTaskRunning -Name $TaskName

            if ($legacyTask) {
                Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false
            }
        } catch {
            $migrationError = $_
            $rollbackErrors = [System.Collections.Generic.List[string]]::new()

            # Deleting a scheduled-task definition does not stop a running task
            # instance. Stop the failed MPI task first so its tunneld child cannot
            # retain port 49151 while the previous task is restored.
            if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
                $failedTaskPids = @(Get-ScheduledTaskEnginePids -Name $TaskName)
                try {
                    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
                } catch {
                    $rollbackErrors.Add("could not stop failed MPI task: $($_.Exception.Message)")
                    foreach ($failedTaskPid in $failedTaskPids) {
                        try {
                            Stop-Process -Id $failedTaskPid -Force -ErrorAction Stop
                        } catch {
                            $rollbackErrors.Add(
                                "could not terminate failed MPI task pid ${failedTaskPid}: $($_.Exception.Message)"
                            )
                        }
                    }
                }
                try {
                    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false `
                        -ErrorAction Stop
                } catch {
                    $rollbackErrors.Add("could not remove failed MPI task: $($_.Exception.Message)")
                }
            }
            # Stop any tunneld child orphaned when its PowerShell task host was
            # terminated before restoring the previous scheduled task.
            Stop-TunneldPortOwner

            if ($currentTaskXml) {
                try {
                    Register-ScheduledTask -TaskName $TaskName -Xml $currentTaskXml `
                        -Force -ErrorAction Stop | Out-Null
                    if ($currentWasRunning) {
                        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
                        Assert-ScheduledTaskRunning -Name $TaskName
                    }
                } catch {
                    $rollbackErrors.Add("could not restore prior MPI task: $($_.Exception.Message)")
                }
            }
            if ($legacyTask -and $legacyWasRunning) {
                try {
                    Start-ScheduledTask -TaskName $LegacyTaskName -ErrorAction Stop
                    Assert-ScheduledTaskRunning -Name $LegacyTaskName
                } catch {
                    $rollbackErrors.Add("could not restart legacy task: $($_.Exception.Message)")
                }
            }
            foreach ($move in $movedLogs) {
                if ((Test-Path -LiteralPath $move.Target) -and
                    -not (Test-Path -LiteralPath $move.Source)) {
                    try {
                        New-Item -ItemType Directory -Force -Path $LegacyLogDir | Out-Null
                        Move-Item -LiteralPath $move.Target -Destination $move.Source
                    } catch {
                        $rollbackErrors.Add("could not restore legacy log '$($move.Source)': $($_.Exception.Message)")
                    }
                }
            }
            if ($rollbackErrors.Count -gt 0) {
                throw "$($migrationError.Exception.Message) Rollback incomplete: $($rollbackErrors -join '; ')"
            }
            throw $migrationError
        }
    } finally {
        if ($ownsMigrationMutex) {
            $migrationMutex.ReleaseMutex()
        }
        $migrationMutex.Dispose()
    }

    Write-Host "[ok] Installed and started '$TaskName'."
    Write-Host "     The iOS tunnel now runs automatically at every logon (using $py)."
    Write-Host "     Keep the iPhone unlocked + connected; then start a capture in the app."
    return
}

# -------------------------------------------------------------- Uninstall ----
if ($Uninstall) {
    if (-not (Test-Admin)) { Invoke-Elevated -Extra @('-Uninstall'); return }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[ok] Removed scheduled task '$TaskName'."
    return
}

# ----------------------------------------------------------------- Status ----
if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "[--] Task '$TaskName' is NOT installed. Run with -Install to set it up."
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "[ok] Task '$TaskName': State=$($task.State) LastRun=$($info.LastRunTime) LastResult=$($info.LastTaskResult)"
    }
    $legacyTask = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
    if ($legacyTask) {
        Write-Host "[!!] Legacy task '$LegacyTaskName' is still installed; run -Install to migrate it."
    }
    if (Test-Path $LogPath) {
        Write-Host "--- tail $LogPath ---"
        Get-Content $LogPath -Tail 15
    } else {
        Write-Host "(no log yet at $LogPath)"
    }
    return
}

# ------------------------------------------------------------ Daemon mode ----
$py = Resolve-Python -Preferred $Python
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Test-Admin)) {
    Write-Log 'warn' 'not elevated -- tunneld will likely fail to create the tunnel adapter'
}

$proc = $null         # the tunneld child we own
$wasReady = $false    # so readiness is logged once per (re)start
$apiDownSince = $null # start of the current continuous API-unresponsive stretch

# Wedge watchdog bounds. tunneld binds its HTTP API at startup (before any
# tunnel forms), so "child alive but API unresponsive" is NOT a normal
# waiting-for-device state: past these bounds the child is wedged (observed in
# the field: process Running, port 49151 dead until manually restarted) and
# must be killed so the loop below respawns it. The steady-state grace rides
# out the momentary unresponsiveness tunneld shows while (re)building tunnels.
$BootGraceSec   = 60  # first startup: interpreter + WinTun adapter creation
$SteadyGraceSec = 30  # was ready once, then went quiet

while ($true) {
    try {
        $childAlive = $proc -and -not $proc.HasExited

        if (Test-TunneldApi) {
            if (-not $wasReady) {
                Write-Log 'info' "tunneld API is ready at $TunneldUrl"
                $wasReady = $true
            }
            $apiDownSince = $null
            Start-Sleep -Seconds 10
            continue
        }

        # API is down.
        if ($childAlive) {
            if (-not $apiDownSince) { $apiDownSince = Get-Date }
            $downSec = [int]((Get-Date) - $apiDownSince).TotalSeconds
            $grace = if ($wasReady) { $SteadyGraceSec } else { $BootGraceSec }
            if ($downSec -lt $grace) {
                # Still within the allowance (booting, or a transient stall
                # while tunnels rebuild) -- keep waiting.
                Start-Sleep -Seconds 3
                continue
            }
            # Wedged: alive but unresponsive past the grace. Kill it; the
            # restart path below brings up a fresh child. Without this the
            # loop waited forever and recovery required a manual task restart.
            Write-Log 'error' "tunneld API unresponsive for ${downSec}s with child alive (pid $($proc.Id)) -- killing wedged tunneld"
            Write-TunneldChildLog
            try {
                Stop-Process -Id $proc.Id -Force -ErrorAction Stop
            } catch {
                Write-Log 'error' "failed to kill wedged tunneld (pid $($proc.Id)): $($_.Exception.Message)"
            }
            $proc = $null
            $wasReady = $false
            $apiDownSince = $null
            Start-Sleep -Seconds 2
            continue
        }

        # No live child -> log why the previous one died, then (re)start it.
        if ($proc) {
            Write-Log 'error' "tunneld exited with code $($proc.ExitCode); restarting"
            Write-TunneldChildLog
        }
        $wasReady = $false
        $apiDownSince = $null
        Stop-TunneldPortOwner
        $proc = Start-Tunneld -PythonPath $py
        Start-Sleep -Seconds 5
    } catch {
        Write-Log 'error' $_.Exception.Message
        Start-Sleep -Seconds 5
    }
}
