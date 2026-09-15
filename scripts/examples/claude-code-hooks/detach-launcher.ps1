param([Parameter(Mandatory)][string]$ScriptPath)

$raw = [Console]::In.ReadToEnd()
$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tmp, $raw, (New-Object System.Text.UTF8Encoding($false)))

# Unique per invocation: this launcher runs concurrently across many Claude Code
# sessions on this machine, so a shared redirect target would collide.
$tmpOut = [System.IO.Path]::GetTempFileName()
$tmpErr = [System.IO.Path]::GetTempFileName()

Start-Process powershell -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $ScriptPath
) -RedirectStandardInput $tmp -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
