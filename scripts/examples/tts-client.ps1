# Reusable PowerShell client for the Kokoro TTS server's POST /tts endpoint.
#
# This is the actual function used by Claude Code's voice hooks
# (permission/notification/question/completion sounds). It shows the full
# real-world usage pattern: fire the HTTP request immediately (the server
# serializes GPU work on its own), then serialize only local *playback*
# through a named mutex so multiple concurrent callers don't talk over each
# other, with a priority flag so an urgent message (e.g. a live question)
# can jump the queue.
#
# Usage:
#   . .\tts-client.ps1
#   Invoke-KokoroTts -Text "Hello there" -Gender 'female'
#   Invoke-KokoroTts -Text "Build failed!" -Gender 'male' -Priority High

function Get-WavDurationSeconds([byte[]]$Bytes) {
    # Walk RIFF chunks looking for "fmt " (byte rate) and "data" (payload size),
    # rather than assuming a fixed 44-byte header -- robust to extra chunks.
    $byteRate = $null
    $dataSize = $null
    $pos = 12
    while ($pos + 8 -le $Bytes.Length) {
        $chunkId = [System.Text.Encoding]::ASCII.GetString($Bytes, $pos, 4)
        $chunkSize = [BitConverter]::ToUInt32($Bytes, $pos + 4)
        if ($chunkId -eq 'fmt ') {
            $byteRate = [BitConverter]::ToUInt32($Bytes, $pos + 16)
        } elseif ($chunkId -eq 'data') {
            $dataSize = $chunkSize
        }
        $pos += 8 + $chunkSize + ($chunkSize % 2)
    }
    if ($byteRate -and $dataSize) { return $dataSize / $byteRate }
    return $null
}

function Invoke-KokoroTts {
    param(
        [Parameter(Mandatory)][string]$Text,
        [Parameter(Mandatory)][ValidateSet('male', 'female')][string]$Gender,
        [ValidateSet('High', 'Normal')][string]$Priority = 'Normal',
        [string]$ServerUrl = 'http://127.0.0.1:8880/tts'
    )

    $flagPath = Join-Path $env:TEMP 'kokoro-tts-priority-pending.flag'
    $client = $null
    $stream = $null
    $mutex = New-Object System.Threading.Mutex($false, 'Global\KokoroTtsPlayback')
    $acquired = $false
    $setFlag = $false
    try {
        # Synthesize right away -- the server serializes actual GPU work on its own,
        # so a client shouldn't sit idle client-side before even asking for audio.
        # Only PLAYBACK (below) is queued, so nothing overlaps audibly.
        Add-Type -AssemblyName System.Net.Http
        $body = @{ text = $Text; voice_gender = $Gender } | ConvertTo-Json -Compress
        $content = New-Object System.Net.Http.StringContent($body, [System.Text.Encoding]::UTF8, 'application/json')

        $client = New-Object System.Net.Http.HttpClient
        $client.Timeout = [TimeSpan]::FromSeconds(30)
        $response = $client.PostAsync($ServerUrl, $content).Result
        if (-not $response.IsSuccessStatusCode) {
            Write-Warning "HTTP $([int]$response.StatusCode) from $ServerUrl"
            return $false
        }
        $bytes = $response.Content.ReadAsByteArrayAsync().Result

        if ($Priority -eq 'High') {
            Set-Content -Path $flagPath -Value (Get-Date -Format o) -Force
            $setFlag = $true
        } else {
            # Yield to a pending high-priority speaker (e.g. a live question) for up to
            # 20s, ignoring a stale flag left behind by a script that crashed before
            # clearing it (staleness judged by the flag file's own write time).
            $waited = 0
            while ((Test-Path $flagPath) -and $waited -lt 20000) {
                $age = (Get-Date) - (Get-Item $flagPath).LastWriteTime
                if ($age.TotalSeconds -gt 20) { break }
                Start-Sleep -Milliseconds 250
                $waited += 250
            }
        }

        try {
            $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds(30))
        } catch [System.Threading.AbandonedMutexException] {
            # Ownership is still granted to this thread when this is thrown;
            # a previous holder just died without releasing it.
            $acquired = $true
        }
        if (-not $acquired) {
            Write-Warning "mutex timeout after 30s (another speak in progress)"
            return $false
        }

        $stream = New-Object System.IO.MemoryStream(, $bytes)
        $player = New-Object System.Media.SoundPlayer
        $player.Stream = $stream
        $player.Load()

        # Play asynchronously and poll, so a caller could add its own "stop" signal
        # (e.g. a flag file) if it needs to interrupt audio mid-playback.
        $duration = Get-WavDurationSeconds -Bytes $bytes
        $player.Play()
        if ($duration) {
            $playSw = [System.Diagnostics.Stopwatch]::StartNew()
            while ($playSw.Elapsed.TotalSeconds -lt ($duration + 0.2)) {
                Start-Sleep -Milliseconds 50
            }
        } else {
            # Couldn't parse duration (unexpected format) -- fall back to blocking play.
            $player.PlaySync()
        }

        return $true
    } catch {
        Write-Warning "$($_.Exception.GetType().FullName): $($_.Exception.Message)"
        return $false
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($client) { $client.Dispose() }
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
        if ($setFlag) { Remove-Item -Path $flagPath -Force -ErrorAction SilentlyContinue }
    }
}
