# Shared helper: find the most recently invoked Skill in a session transcript.
# Dot-sourced by stop-speak.ps1 and elicitation-speak.ps1 so the spoken
# notification can say which skill is running (identifies the window when
# several Claude Code sessions are talking at once).

function Get-CurrentSkillName {
    param([string]$TranscriptPath)

    if (-not $TranscriptPath -or -not (Test-Path $TranscriptPath)) { return $null }

    $lines = Get-Content -Path $TranscriptPath -Tail 400 -ErrorAction SilentlyContinue
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        $entry = try { $lines[$i] | ConvertFrom-Json -ErrorAction Stop } catch { $null }
        if (-not $entry -or -not $entry.message -or -not $entry.message.content) { continue }
        foreach ($item in @($entry.message.content)) {
            if ($item.type -eq 'tool_use' -and $item.name -eq 'Skill' -and $item.input.skill) {
                return $item.input.skill
            }
        }
    }
    return $null
}

# Shared helper: with many concurrent Claude Code sessions on this machine all
# narrating over the same voice, an explicitly-renamed session's friendly name
# (set via a rename skill, e.g. "PR 6426") is the only way to tell which session
# is actually talking. Only announced when the name was a deliberate user rename
# ("user" nameSource) -- an auto-derived default name (e.g. "dsh-2e") isn't
# meaningful enough to be worth adding to every single narration.
function Get-SessionInfo {
    param([string]$TranscriptPath)

    if (-not $TranscriptPath) { return $null }
    $sessionId = [System.IO.Path]::GetFileNameWithoutExtension($TranscriptPath)
    if (-not $sessionId) { return $null }

    foreach ($file in Get-ChildItem -Path 'C:\Users\awang\.claude\sessions\*.json' -ErrorAction SilentlyContinue) {
        $info = try { Get-Content -Path $file.FullName -Raw | ConvertFrom-Json -ErrorAction Stop } catch { $null }
        if ($info -and $info.sessionId -eq $sessionId) { return $info }
    }
    return $null
}

function Get-CurrentSessionName {
    param([string]$TranscriptPath)
    $info = Get-SessionInfo -TranscriptPath $TranscriptPath
    if ($info -and $info.nameSource -eq 'user' -and $info.name) { return $info.name }
    return $null
}

# Unconditional variant: returns whatever name the session has (derived or user),
# for notifications where knowing WHICH of several idle/background sessions is
# pinging you matters more than whether the name is a deliberate rename.
function Get-CurrentSessionNameAlways {
    param([string]$TranscriptPath)
    $info = Get-SessionInfo -TranscriptPath $TranscriptPath
    if ($info -and $info.name) { return $info.name }
    return $null
}

# A CronCreate job doesn't spawn a new session -- it fires as a scheduled prompt
# INSIDE an already-running interactive session, using one of these literal
# sentinels as the resumed prompt text. Detecting one near the end of the
# transcript is how we tell "this notification came from a cron firing" apart
# from a real human sitting there waiting.
function Test-RecentCronActivity {
    param([string]$TranscriptPath)

    if (-not $TranscriptPath -or -not (Test-Path $TranscriptPath)) { return $false }

    $lines = Get-Content -Path $TranscriptPath -Tail 20 -ErrorAction SilentlyContinue
    foreach ($line in $lines) {
        $entry = try { $line | ConvertFrom-Json -ErrorAction Stop } catch { $null }
        if (-not $entry -or $entry.type -ne 'user' -or -not $entry.message -or -not $entry.message.content) { continue }
        $text = if ($entry.message.content -is [string]) {
            $entry.message.content
        } else {
            (@($entry.message.content) | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join ' '
        }
        if ($text -match '<<autonomous-loop(-dynamic)?>>') { return $true }
    }
    return $false
}
