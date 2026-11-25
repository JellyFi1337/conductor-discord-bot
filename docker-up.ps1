#Requires -Version 5.1
$ErrorActionPreference = "Stop"
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

function Test-DockerReady {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        Write-Error "Docker не найден. Установите Docker Desktop и перезапустите терминал. Инструкция: docs/DEPLOY.md"
    }
    docker info 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker не отвечает. Запустите Docker Desktop и дождитесь зеленого статуса, затем повторите."
    }
    docker compose version 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Нет плагина docker compose. Обновите Docker Desktop."
    }
}

function Get-EnvValue {
    param(
        [string] $Path,
        [string] $Key
    )
    if (-not (Test-Path $Path)) {
        return $null
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trim = $line.Trim()
        if ($trim.StartsWith("#") -or -not $trim.Contains("=")) {
            continue
        }
        $pair = $trim.Split("=", 2)
        if ($pair[0].Trim() -eq $Key) {
            return $pair[1].Trim()
        }
    }
    return $null
}

function Add-EnvKeyIfMissing {
    param(
        [string] $Path,
        [string] $Key,
        [string] $Value
    )
    $current = Get-EnvValue -Path $Path -Key $Key
    if ($null -eq $current) {
        Add-Content -LiteralPath $Path -Value "$Key=$Value" -Encoding UTF8
        return $true
    }
    return $false
}

if ($PSScriptRoot -match "OneDrive|[\u0400-\u04FF]") {
    Write-Warning "Репозиторий лежит в OneDrive или в пути с кириллицей. Если сборка падает, склонируйте проект в C:\src\Conductor. См. docs/DEPLOY.md"
}

Test-DockerReady

$envPath = Join-Path $PSScriptRoot ".env"
$examplePath = Join-Path $PSScriptRoot ".env.example"
if (-not (Test-Path $envPath)) {
    Copy-Item -LiteralPath $examplePath -Destination $envPath
    Write-Host "Создан .env из .env.example. Заполните BOT_MODE, оба токена, списки серверов и POSTGRES_PASSWORD, затем запустите скрипт снова."
    exit 1
}

$added = $false
$added = (Add-EnvKeyIfMissing -Path $envPath -Key "POSTGRES_USER" -Value "conductor") -or $added
$added = (Add-EnvKeyIfMissing -Path $envPath -Key "POSTGRES_PASSWORD" -Value "change-me") -or $added
$added = (Add-EnvKeyIfMissing -Path $envPath -Key "POSTGRES_DB" -Value "conductor") -or $added
$added = (Add-EnvKeyIfMissing -Path $envPath -Key "POSTGRES_PORT" -Value "5432") -or $added
$added = (Add-EnvKeyIfMissing -Path $envPath -Key "TZ" -Value "Europe/Moscow") -or $added
if ($added) {
    Write-Host "В .env добавлены ключи Postgres. Смените POSTGRES_PASSWORD и запустите скрипт снова."
    exit 1
}

$token = Get-EnvValue -Path $envPath -Key "DISCORD_TOKEN"
$testToken = Get-EnvValue -Path $envPath -Key "TEST_DISCORD_TOKEN"
$guildIds = Get-EnvValue -Path $envPath -Key "GUILD_IDS"
$testGuildIds = Get-EnvValue -Path $envPath -Key "TEST_GUILD_IDS"
$publicId = Get-EnvValue -Path $envPath -Key "PUBLIC_GUILD_ID"
$testId = Get-EnvValue -Path $envPath -Key "TEST_GUILD_ID"
$botMode = Get-EnvValue -Path $envPath -Key "BOT_MODE"
$password = Get-EnvValue -Path $envPath -Key "POSTGRES_PASSWORD"
$placeholders = @("your-bot-token", "changeme", "token", "xxx", "")
$modeNorm = if ($null -eq $botMode) { "live" } else { $botMode.Trim().ToLower() }
$testMode = @("test", "testing", "dev", "beta") -contains $modeNorm
$processToken = if ($testMode -and -not [string]::IsNullOrWhiteSpace($testToken)) { $testToken } else { $token }
$tokenNorm = if ($null -eq $processToken) { "" } else { $processToken.ToLower() }
$hasGuildList = if ($testMode) {
    -not [string]::IsNullOrWhiteSpace($testGuildIds) -or -not [string]::IsNullOrWhiteSpace($testId) -or -not [string]::IsNullOrWhiteSpace($guildIds)
} else {
    -not [string]::IsNullOrWhiteSpace($guildIds) -or -not [string]::IsNullOrWhiteSpace($publicId)
}

if ($placeholders -contains $tokenNorm) {
    if ($testMode) {
        Write-Host "В .env нет токена тестового бота: TEST_DISCORD_TOKEN или DISCORD_TOKEN."
    } else {
        Write-Host "В .env нет настоящего DISCORD_TOKEN."
    }
    exit 1
}
if (-not $hasGuildList) {
    Write-Host "В .env нужен список серверов выбранного BOT_MODE: GUILD_IDS / TEST_GUILD_IDS или запасные id."
    exit 1
}
if ([string]::IsNullOrWhiteSpace($password) -or $password -eq "change-me") {
    Write-Warning "POSTGRES_PASSWORD все еще 'change-me'. Для своего компьютера можно продолжить, на сервере задайте длинный пароль."
}

Write-Host "Останавливаю старые копии бота..."
& (Join-Path $PSScriptRoot "stop.ps1")
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Собираю образы и поднимаю postgres + bot..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Стек запущен. Полезные команды:"
Write-Host "  docker compose logs -f bot"
Write-Host "  docker compose --profile test up -d"
Write-Host "  docker compose logs -f bot-test"
Write-Host "  docker compose ps"
Write-Host "  docker compose stop"
Write-Host "  docker compose down"
Write-Host "Подробности: docs/DEPLOY.md"
exit 0
