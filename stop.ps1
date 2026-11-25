#Requires -Version 5.1
# Останавливает все копии Conductor на этой машине: локальный Python и контейнеры.
# Тома Postgres не удаляет. После этого можно поднять чистый стек: .\start.ps1
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Set-Utf8Console {
    try {
        $utf8 = [System.Text.UTF8Encoding]::new($false)
        [Console]::OutputEncoding = $utf8
        [Console]::InputEncoding = $utf8
        $script:OutputEncoding = $utf8
        $global:OutputEncoding = $utf8
        chcp 65001 | Out-Null
    } catch {
    }
}

Set-Utf8Console

function Get-LocalConductorProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        if (-not $_.CommandLine) {
            return $false
        }
        $cmd = [string]$_.CommandLine
        $name = [string]$_.Name
        if ($name -eq "conductor.exe") {
            return $true
        }
        if ($name -notmatch "^(python|pythonw)\.exe$") {
            return $false
        }
        if ($cmd -match "(?i)pytest|ruff |mypy|alembic") {
            return $false
        }
        if ($cmd -match "(?i)[-\s]m\s+conductor(\s|$)") {
            return $true
        }
        if ($cmd -match "(?i)[\\/]Scripts[\\/]conductor\.exe") {
            return $true
        }
        return $false
    }
}

function Stop-LocalConductorProcesses {
    $found = @(Get-LocalConductorProcesses)
    foreach ($proc in $found) {
        Write-Host ("  process {0}: {1}" -f $proc.ProcessId, $proc.CommandLine)
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    return @($found | ForEach-Object { $_.ProcessId })
}

function Stop-DockerConductor {
    $stopped = New-Object System.Collections.Generic.List[string]
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        Write-Host "Docker not found, skip containers."
        return @()
    }
    docker info 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Docker is not running, skip containers."
        return @()
    }

    docker compose --profile test down --remove-orphans 1>$null 2>$null
    docker compose down --remove-orphans 1>$null 2>$null
    docker compose -p conductor --profile test down --remove-orphans 1>$null 2>$null
    docker compose -p conductor down --remove-orphans 1>$null 2>$null

    $lines = @(docker ps -a --format "{{.ID}}`t{{.Names}}`t{{.Image}}" 2>$null)
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        $parts = $line.Split("`t")
        if ($parts.Count -lt 3) {
            continue
        }
        $id = $parts[0]
        $cname = $parts[1]
        $image = $parts[2]
        if ($cname -notmatch "(?i)conductor" -and $image -notmatch "(?i)^conductor(:|$)") {
            continue
        }
        Write-Host ("  container {0} ({1})" -f $cname, $image)
        docker stop --time 20 $id 1>$null 2>$null
        docker rm -f $id 1>$null 2>$null
        $stopped.Add($cname) | Out-Null
    }
    return @($stopped)
}

Write-Host "Stopping every Conductor copy..."
$procIds = @(Stop-LocalConductorProcesses)
$containers = @(Stop-DockerConductor)

Start-Sleep -Seconds 1
$left = @(Get-LocalConductorProcesses)
if ($left.Count -gt 0) {
    $leftIds = ($left | ForEach-Object { $_.ProcessId }) -join ", "
    Write-Error ("Failed to stop processes: {0}" -f $leftIds)
    exit 1
}

if ($procIds.Count -eq 0 -and $containers.Count -eq 0) {
    Write-Host "No running copies."
} else {
    Write-Host ("Stopped processes: {0}, containers: {1}." -f $procIds.Count, $containers.Count)
}
Write-Host "Postgres volumes kept. Clean start: .\start.ps1"
exit 0
