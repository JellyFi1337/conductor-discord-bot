#Requires -Version 5.1
# Запуск Conductor через Docker Desktop: postgres + bot, затем логи в этой консоли.
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

Write-Host "Conductor: запуск через Docker Desktop (PostgreSQL)."
Write-Host "Запускается бот из BOT_MODE этого .env (live или test/beta), не оба сразу."
Write-Host "Оба вместе: скопируйте .env в .env.test, там BOT_MODE=test, затем docker compose --profile test up -d."
Write-Host ""

& (Join-Path $PSScriptRoot "docker-up.ps1")
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Логи бота ниже. Ctrl+C закрывает только просмотр, контейнеры продолжают работать."
Write-Host "Полная остановка: .\stop.ps1 или stop.bat"
Write-Host ""
docker compose logs -f --tail=80 bot
exit $LASTEXITCODE
