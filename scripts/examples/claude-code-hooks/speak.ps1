if (Test-Path 'C:\Users\awang\.claude\tts-muted.flag') { exit }

$raw  = [Console]::In.ReadToEnd()
$data = try { $raw | ConvertFrom-Json } catch { $null }

if ($data -and $data.message) {
    $msg = $data.message.Trim()
} else {
    $msg = "Claude needs your attention."
}

. "C:\Users\awang\.claude\skill-lookup.ps1"

# A generic "Claude is waiting for your input" can actually mean a scheduled
# cron job just fired and finished, not a human sitting idle -- those two
# situations need different messages so they're not mistaken for each other.
if ($msg -match 'waiting for your input|needs your attention' -and (Test-RecentCronActivity -TranscriptPath $data.transcript_path)) {
    $skillName = Get-CurrentSkillName -TranscriptPath $data.transcript_path
    $msg = if ($skillName) {
        "A scheduled job just ran : $($skillName -replace '[-:_]', ' ')"
    } else {
        "A scheduled job just ran"
    }
}

# Always named (not just deliberate renames): this script covers "Claude is
# waiting for your input" / "needs your attention" / "needs your permission",
# which fire from whichever of many concurrent sessions is idle or blocked --
# without a name there's no way to tell which one is actually pinging you.
$sessionName = Get-CurrentSessionNameAlways -TranscriptPath $data.transcript_path
if ($sessionName) {
    $msg = "$msg Session : $sessionName"
}

. "C:\Users\awang\.claude\tts-client.ps1"
$spoken = Invoke-KokoroTts -Text $msg -Gender 'male'

if (-not $spoken) {
    Add-Content -Path 'C:\Users\awang\.claude\tts-failures.log' -Value "$(Get-Date -Format o) speak: $msg"
}
