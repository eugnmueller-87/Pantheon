# Pantheon OS — Dashboard Dev Launcher
# Starts backend (port 8081) + frontend (port 3001) for local development
# Usage: .\dashboard\start_dev.ps1
# Note: frontend runs on 3001 — port 3000 is used by the Aushang/DIGITnews app.

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

Write-Host "`n⚡ PANTHEON OS Dashboard — Dev Mode`n" -ForegroundColor Cyan

# Backend
Write-Host "Starting dashboard backend on http://localhost:8081 ..." -ForegroundColor Yellow
$backend = Start-Process -FilePath "python" `
    -ArgumentList "-m", "uvicorn", "dashboard.backend.server:app", "--host", "0.0.0.0", "--port", "8081", "--reload" `
    -WorkingDirectory $root `
    -PassThru -NoNewWindow

# Frontend
Write-Host "Starting React frontend on http://localhost:3001 ..." -ForegroundColor Yellow
$frontend = Start-Process -FilePath "npm" `
    -ArgumentList "start" `
    -WorkingDirectory "$root\dashboard\frontend" `
    -PassThru -NoNewWindow

Write-Host "`n Dashboard: http://localhost:3001" -ForegroundColor Green
Write-Host " API:       http://localhost:8081/api/status" -ForegroundColor Green
Write-Host " WebSocket: ws://localhost:8081/ws`n" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop both processes." -ForegroundColor Gray

try {
    Wait-Process -Id $backend.Id
} finally {
    if (!$backend.HasExited)  { Stop-Process -Id $backend.Id  -Force }
    if (!$frontend.HasExited) { Stop-Process -Id $frontend.Id -Force }
    Write-Host "`nShutdown complete." -ForegroundColor Gray
}
