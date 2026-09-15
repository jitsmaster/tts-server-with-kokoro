if (Test-Path 'C:\Users\awang\.claude\tts-muted.flag') { exit }

$raw  = [Console]::In.ReadToEnd()
$data = try { $raw | ConvertFrom-Json } catch { $null }

if ($data -and $data.last_assistant_message) {
    $msg = $data.last_assistant_message.Trim()
} else {
    $msg = "Claude is done."
}

function Clean-ForSpeech([string]$text) {
    $text = $text -replace '\*\*([^*]+)\*\*', '$1'
    $text = $text -replace '\*([^*]+)\*',     '$1'
    $text = $text -replace '`([^`]+)`',       '$1'
    $text = $text -replace '#{1,6}\s*',       ''
    $text = $text -replace '\[([^\]]+)\]\([^)]+\)', '$1'
    $text = $text -replace '[—–]',            ' '
    $text = $text -replace '\s+-\s+',         ' '
    $text = $text -replace '[|~^_]',           ' '
    $text = $text -replace '[^\x20-\x7E\r\n一-鿿㐀-䶿豈-﫿]','  '
    return $text
}

# Pull out an "Insights" section so it always survives truncation below,
# and is always spoken (with the word "Insights") at the end of the message.
$insightsText = $null
if ($msg -match '(?ms)(^#{1,6}\s*Insights\s*\r?\n(.*?)(?=^#{1,6}\s|\z))') {
    $fullBlock = $Matches[1]
    $insightsText = Clean-ForSpeech ($Matches[2].Trim())
    $insightsText = ($insightsText -replace '\s+', ' ').Trim()
    $msg = $msg.Remove($msg.IndexOf($fullBlock), $fullBlock.Length).Trim()
}

$msg = Clean-ForSpeech $msg

. "C:\Users\awang\.claude\skill-lookup.ps1"
$skillName = Get-CurrentSkillName -TranscriptPath $data.transcript_path
if ($skillName) {
    $msg = "$msg Done by skill : $($skillName -replace '[-:_]', ' ')"
}

$sessionName = Get-CurrentSessionName -TranscriptPath $data.transcript_path
if ($sessionName) {
    $msg = "$msg Session : $sessionName"
}

if ($insightsText) {
    $msg = "$msg Insights. $insightsText"
}

. "C:\Users\awang\.claude\tts-client.ps1"
$spoken = Invoke-KokoroTts -Text $msg -Gender 'male'

if (-not $spoken) {
    Add-Content -Path 'C:\Users\awang\.claude\tts-failures.log' -Value "$(Get-Date -Format o) stop-speak: $msg"
}
