# Client examples

Sample scripts showing how to call the running server's `POST /tts`
endpoint from different languages/shells. See the top-level [README](../../README.md)
for the API shape and how to start the server.

- **`tts-client.ps1`** / **`call-tts.ps1`** — the real PowerShell client used
  by Claude Code's voice hooks (permission requests, questions, completion
  sounds), trimmed of hook-specific bits (Claude Code transcript lookups,
  markdown stripping). `tts-client.ps1` has the reusable `Invoke-KokoroTts`
  function; `call-tts.ps1` is a one-line usage example.
- **`call-tts.sh`** — curl-based example for Linux/macOS.
- **`call_tts.py`** — Python example using `requests`.

All of them do the same three things: POST `{"text", "voice_gender"}` as
JSON, get back raw `audio/wav` bytes, and play or save them.

See **`claude-code-hooks/`** for the actual, unmodified production hook
scripts these were trimmed from, including the transcript-reading and
priority-queue logic left out here for clarity.
