# pipeline.sh Verification

I don't have network/sudo/Xvfb/a real Google/Zoom account in this sandbox, so
"verify" here means two things:

1. **Static trace** of the routing logic against all 4 input types from the
   diagram.
2. **Mocked dry-run**: real `pipeline.sh`, with `screen/record_screen.sh`,
   `transcribe/transcribe.sh`, and `summarize/summarize.py` swapped for stubs
   that honor the same argument/output-file contract (write the same
   filenames pipeline.sh looks for, in the same directories), so the actual
   shell logic — routing, `ls -1t` file discovery, stage chaining, exit codes
   — runs for real, just without the actual recording/AssemblyAI/Gemini calls.

**This does not replace testing on the real VM.** It proves the orchestration
logic is correct; it does not prove Playwright can actually join a Meet call,
that ffmpeg's x11grab settings work on your hardware, or that your
AssemblyAI/Gemini keys are valid.

## Bug found and fixed

`pipeline.sh`'s input router only recognized Meet/Zoom/YouTube URLs. A direct
local file (the 4th input type in your diagram — `.mp4 Video`) fell through
to `ERROR: Unrecognized URL` and exited 1, even though `transcribe.sh` and
`summarize.py` both already handle local files fine on their own. Proof
before the fix:

```
$ bash -x -c '
INPUT_URL="/tmp/fake_video.mp4"
... (same regex checks pipeline.sh used) ...
'
IS_MEETING_URL=0 IS_YOUTUBE_URL=0
==> pipeline.sh would exit 1 here: ERROR Unrecognized URL
```

**Fix**: added an `IS_LOCAL_FILE` branch (`[ -f "$INPUT_URL" ]`) alongside
the existing Meet/Zoom/YouTube checks. A local file now skips Stage 1 (like
YouTube does) but sets `MP4_FILE="$INPUT_URL"` directly (unlike YouTube,
which leaves it empty and lets `transcribe.sh`/`summarize.py` pull the video
themselves). If no meeting name is given, the name is derived from the
filename instead of the generic "meeting" default, so repeated local-file
runs don't collide on the same basename.

## Dry-run results

All 4 diagram inputs, plus one deliberately-bad input to confirm the error
path still works:

| # | Input | Stage 1 | Stage 2 input | Result |
|---|---|---|---|---|
| 1 | `https://meet.google.com/abc-defg-hij` | Runs (recorder stub) | recorded `.mp4` | ✅ exit 0, all 3 output files created |
| 2 | `https://zoom.us/j/1234567890` | Runs (recorder stub) | recorded `.mp4` | ✅ exit 0, all 3 output files created |
| 3 | `https://www.youtube.com/watch?v=dQw4w9WgXcQ` | Skipped | YouTube URL itself | ✅ exit 0, transcript+summary created, `yt_<id>` basename |
| 4 | `/tmp/test_video.mp4` (local file) | Skipped (was: crash before fix) | the file itself | ✅ exit 0, transcript+summary created, basename from filename |
| 5 | `not-a-real-input` | — | — | ✅ exit 1, clean error message, no partial files |

Full output log and the stub scripts used are reproducible — see the shell
history in this conversation if you want to re-run the same harness.

## What's still unverified (needs the real VM)

- Playwright actually joining/being admitted to a live Meet/Zoom call
- `audio-setup.sh`'s PulseAudio sink creation and ffmpeg's `x11grab`/`pulse`
  capture on your actual hardware
- Real AssemblyAI upload/poll/transcript round-trip with a valid API key
- Real Gemini call via `llm_client.py` with a valid API key, including how it
  handles the frame `manifest.json` + prompt
- `kill_meeting.sh`'s signal handling against real running processes
- The `trigger_server.py` → `pipeline.sh` background-execution path end to end

Recommend running one real end-to-end test per input type from `RUNBOOK.md`
once this is deployed, ideally with a short/low-stakes meeting or a short
YouTube video first.
