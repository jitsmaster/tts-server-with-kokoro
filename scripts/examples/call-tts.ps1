# Minimal example: dot-source the client and speak a line.
#
#   .\call-tts.ps1 -Text "The build finished." -Gender male

param(
    [string]$Text = "Hello from Kokoro.",
    [ValidateSet('male', 'female')][string]$Gender = 'female'
)

. "$PSScriptRoot\tts-client.ps1"

if (-not (Invoke-KokoroTts -Text $Text -Gender $Gender)) {
    Write-Error "TTS request failed -- is the server running on http://127.0.0.1:8880?"
    exit 1
}
