# Kokoro TTS Server — Mid-Implementation Handoff

**Date:** 2026-09-10
**Plan:** `docs/superpowers/plans/2026-09-10-kokoro-tts-server.md`
**Spec:** `docs/superpowers/specs/2026-09-10-kokoro-tts-server-design.md`
**Worktree:** `D:\dev\TTS\tts-server-with-kokoro\.worktrees\kokoro-tts-server` on branch `feature/kokoro-tts-server`
**SDD ledger (full task-by-task detail):** `.superpowers\sdd\2026-09-10-kokoro-tts-server\progress.md` inside that worktree

## Status in one line

All 10 plan tasks are implemented and individually code-reviewed clean, but **live end-to-end testing (Task 10) found a critical bug that made Kokoro audio effectively never play through the real hooks** — root cause found and partially fixed just now, but not yet re-verified, and two mid-session scope changes from the user are not yet implemented.

## What's actually done

- Tasks 1-5 (server: voice table, language detection, Kokoro engine wrapper, `POST /tts` endpoint, start script + README) are committed on `feature/kokoro-tts-server` up through commit `c708725`. All committed, all reviewed clean (a handful of deferred Minor findings, see ledger).
- Tasks 6-9 (PowerShell side: `tts-client.ps1`, and editing `elicitation-speak.ps1` / `stop-speak.ps1` / `speak.ps1`) are done but live outside the git repo at `C:\Users\awang\.claude\*.ps1` — no commits for these (that directory isn't a git repo), reviewed by reading the files directly.
- Two git mishaps happened during Tasks 1 and 2 (implementer subagents committed straight onto `main` instead of the worktree) — both were caught and fixed (cherry-picked onto the feature branch, `main` reset back to `a9ebf89`). `main` is currently clean and untouched by any of this work.
- Task 3 had a real bug (`.numpy()` called on a possibly-CUDA tensor without `.cpu()` first) — caught by code review, fixed in commit `df256bb`.
- Task 5 did a real end-to-end smoke test against the actual Kokoro model (before the CUDA bug below was discovered) and it worked, but that was on CPU inference, which is why it wasn't obviously slow to the automated check (it doesn't judge audio latency, only that a valid WAV came back eventually with a generous no-timeout `Invoke-WebRequest` call).

## The critical bug found during Task 10 (live verification)

Task 10's live tests (real `AskUserQuestion` triggering the real hook, in the user's actual Claude Code session) went like this:
1. **Question hook, server up:** user confirmed hearing the male Kokoro voice + chime. ✅
2. **Completion hook, server up:** user confirmed hearing the female Kokoro voice + chime. ✅
3. **Fallback test (server stopped):** user heard **nothing at all** — no Kokoro voice (expected, server's down) AND no OS-voice fallback either (not expected — this should have worked, it's unchanged `System.Speech` code that worked before this whole project started).

Debugging that (steps 1 and 2 above having worked made this genuinely confusing) found:
- `torch` was installed as a **CPU-only build** (`pip install torch` with no index URL defaults to CPU-only on Windows) — `torch.cuda.is_available()` returned `False` despite this being a GPU-equipped machine (RTX 3050 Ti Laptop GPU, confirmed via `nvidia-smi`).
- This meant every Kokoro synthesis request ran on CPU. A "warm" request (pipeline already loaded, second+ call) measured at **~11 seconds** — far past `tts-client.ps1`'s `-TimeoutSec 3`. So `Invoke-KokoroTts` almost always times out and returns `$false`, triggering the fallback path — this explains why Kokoro voice was inaudible during the server-stopped fallback test (irrelevant, server actually was down that time) and is a strong hypothesis for why, in *earlier* everyday use, the user may not reliably hear Kokoro at all even when the server is up and working, if requests regularly exceed 3 seconds.
- The **separate, still-unexplained mystery**: why the *OS-voice fallback itself* produced no audible sound during Task 10 step 3, when the exact same `System.Speech` code, called directly in this session immediately afterward for debugging, also produced no complaint of silence from the user (though the user was never explicitly asked "did you hear THIS specific test" for that one — see Immediate next steps below). **This has not been root-caused.** Don't assume it's fixed by the CUDA fix above — it needs its own investigation.

## Fix applied so far (not yet fully verified)

- Uninstalled the CPU-only `torch`, reinstalled `torch==2.6.0+cu124` via `pip install torch --index-url https://download.pytorch.org/whl/cu124` in the worktree's `.venv`. Confirmed: `torch.cuda.is_available()` → `True`, device → `NVIDIA GeForce RTX 3050 Ti Laptop GPU`.
- Full test suite re-run after the reinstall: 12/12 passing (these are all mocked-`synthesize` tests, so this only confirms nothing broke structurally, not that GPU inference itself is fast).
- **NOT yet done:** re-measuring synthesis latency with CUDA actually enabled, to confirm the timeout problem is resolved. The user explicitly asked: *"profile and see what is taking all the delay"* — this is the immediate next step, see below.
- **NOT yet done:** `requirements.txt` currently just says `torch` (unpinned). A fresh `pip install -r requirements.txt` on a clean venv would silently reinstall the CPU-only build again, silently regressing this fix. `requirements.txt` and/or `README.md` need to document the correct CUDA install command (`--index-url https://download.pytorch.org/whl/cu124`, or whatever CUDA version is current when this is picked back up — check `nvidia-smi`'s reported CUDA version first).

## Mid-session scope changes from the user (not yet implemented)

The user gave two new instructions directly, overriding the originally-approved spec's design:

1. **"Hooks should only play kokoro voice, no fallback"** — the approved spec and Tasks 7-9 built in silent OS-voice fallback on any Kokoro failure (this was an explicit, deliberate design choice the user approved during brainstorming). The user has now reversed that: hooks should ONLY use Kokoro, with no `System.Speech` fallback at all. This needs:
   - Removing the `if (-not $spoken) { ...System.Speech... }` blocks from `elicitation-speak.ps1`, `stop-speak.ps1`, and `speak.ps1` (all three, at `C:\Users\awang\.claude\`).
   - Deciding what SHOULD happen instead when Kokoro fails (silently do nothing? Still play the mp3 chime? Log something? — needs a quick clarifying question to the user, don't assume).
2. **"don't write to file for the tts, return the actual voice stream to be played by the hooks"** — `tts-client.ps1`'s `Invoke-KokoroTts` currently does `Invoke-WebRequest -OutFile $tempFile` then opens that file with `System.Windows.Media.MediaPlayer`. The user wants this changed to stream the HTTP response directly into playback, without an intermediate temp `.wav` file. This likely means switching to something like reading the response into a memory stream (`Invoke-WebRequest`'s raw content bytes into a `System.IO.MemoryStream`, or use `HttpClient` for true streaming) and feeding that to a player that can play from a stream rather than a file path (`System.Windows.Media.MediaPlayer` only takes a `Uri`, so a different playback API may be needed — e.g. `System.Media.SoundPlayer.Stream` works with WAV streams directly and might be a simpler fit than `MediaPlayer`). This needs its own small design pass, not a blind edit.

Neither of these is implemented yet. Both invalidate parts of Task 6's approved design (spec section "PowerShell hook integration") and Tasks 7-9's reviewed code — expect to redo those three hook files and `tts-client.ps1` once the new design is settled.

## Immediate next steps (in order)

1. **Profile the delay** (user's explicit ask, not yet done): with CUDA now enabled, start the server fresh and time a `/tts` request — both cold (first request after startup) and warm (second+ request) — and if still slow, add rough timing instrumentation around pipeline construction vs. the actual `pipeline(text, voice=...)` inference call vs. `sf.write`/WAV encoding, to see where the time actually goes. Compare against expectations for an 82M-param model on a 4GB laptop GPU (should be well under a second per short sentence once warm, if CUDA is actually being used for the forward pass and not just present-but-unused).
2. **Root-cause the OS-fallback silence** from Task 10 step 3 — this is separate from the CUDA issue and still unexplained. Worth checking: was the user's system audio actually working at that moment (e.g., a muted output device, a different default playback device than expected)? Was `detach-launcher.ps1`'s process-detach mechanism somehow suppressing audio for a detached/backgrounded process in a way that direct interactive PowerShell calls don't hit? This needs a targeted, isolated test — e.g., trigger the *exact* hook path (through the actual Claude Code hook mechanism, not a manual PowerShell invocation) with the Kokoro server intentionally down, and ask the user specifically "did you hear the OS fallback voice for THIS one."
3. **Design and implement the no-fallback + streaming-playback changes** the user requested — probably worth a short clarifying exchange with the user first (what should happen on Kokoro failure if not a silent OS fallback?), then a small design note, then redo `tts-client.ps1` and the three hook files.
4. **Fix `requirements.txt`/`README.md`** to pin/document the CUDA torch install so this doesn't silently regress for the next person (or the next fresh venv).
5. Once the above are settled and re-verified live (repeat Task 10's three checks, including asking the user to confirm by ear each time), resume the subagent-driven-development finish sequence: final whole-branch code review, then `superpowers:finishing-a-development-branch` to decide how this merges.

## State to preserve

- Kokoro server is currently **stopped** (no lingering process — confirmed via `Get-Process python`).
- The worktree's `.venv` currently has the CUDA-enabled `torch==2.6.0+cu124` installed (this is AHEAD of what `requirements.txt` would produce on a fresh install — see step 4 above).
- No uncommitted changes in the worktree beyond what's already in the SDD ledger's task history (check `git status` in the worktree to confirm before doing anything else).
- The three PowerShell files at `C:\Users\awang\.claude\` (`elicitation-speak.ps1`, `stop-speak.ps1`, `speak.ps1`) plus `tts-client.ps1` currently reflect the OLD (fallback-enabled, file-based) design from Tasks 7-9/6 — they still need the scope-change edits from this handoff's "Mid-session scope changes" section.
