$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root 'logs'

# Already serving? Do nothing (a second server would load the model onto the GPU again, then fail to bind).
if (Get-NetTCPConnection -LocalPort 8880 -State Listen -ErrorAction SilentlyContinue) { exit 0 }

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8880', '--app-dir', $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'server.log') `
    -RedirectStandardError (Join-Path $logDir 'server.error.log')
