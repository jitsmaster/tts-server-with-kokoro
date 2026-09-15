if (Test-Path 'C:\Users\awang\.claude\tts-muted.flag') { exit }

$raw  = [Console]::In.ReadToEnd()
$data = try { $raw | ConvertFrom-Json } catch { $null }

if ($data -and $data.tool_input -and $data.tool_input.questions) {
    $msg = ($data.tool_input.questions | ForEach-Object { $_.question }) -join '. '
    if ([string]::IsNullOrWhiteSpace($msg)) {
        $msg = "We have a question for you"
    }
} elseif ($data -and $data.message) {
    $msg = $data.message.Trim()
} elseif ($data -and $data.last_assistant_message) {
    $msg = $data.last_assistant_message.Trim()
} else {
    $msg = "Claude has a question for you."
}

# Strip markdown formatting for cleaner speech
$msg = $msg -replace '\*\*([^*]+)\*\*', '$1'
$msg = $msg -replace '\*([^*]+)\*',     '$1'
$msg = $msg -replace '`([^`]+)`',       '$1'
$msg = $msg -replace '#{1,6}\s*',       ''
$msg = $msg -replace '\[([^\]]+)\]\([^)]+\)', '$1'
$msg = $msg -replace '[—–]',            ' '
$msg = $msg -replace '\s+-\s+',         ' '
$msg = $msg -replace '[|~^_]',           ' '
$msg = $msg -replace '[^\x20-\x7E\r\n一-鿿㐀-䶿豈-﫿]','  '

. "C:\Users\awang\.claude\skill-lookup.ps1"
$skillName = Get-CurrentSkillName -TranscriptPath $data.transcript_path
if ($skillName) {
    $msg = "$msg Question by skill : $($skillName -replace '[-:_]', ' ')"
}

$sessionName = Get-CurrentSessionName -TranscriptPath $data.transcript_path
if ($sessionName) {
    $msg = "$msg Session : $sessionName"
}

. "C:\Users\awang\.claude\tts-client.ps1"
$spoken = Invoke-KokoroTts -Text $msg -Gender 'female' -Priority High

if (-not $spoken) {
    Add-Content -Path 'C:\Users\awang\.claude\tts-failures.log' -Value "$(Get-Date -Format o) elicitation-speak: $msg"
}
