# CLAUDE.md — Project context for future Claude sessions

This file is the canonical reference for architectural decisions, conventions,
and non-obvious facts about this project. Read it before exploring the source
so you don't re-derive context that's already settled. After every edit, update
it so it stays accurate. **Always ask the user questions first.**

User-facing docs (what this does, how to run it) live in `README.md`. The
one other Markdown file is `DESIGN.md`, the specification of the summary
PDF's look (added at the operator's request 2026-09-29; `pdf.py`'s `_css()`
implements it, and the two change together). This file is for the things
that aren't obvious from reading code or git history.

### Note from AssemblyAI
Before writing AssemblyAI code, read https://www.assemblyai.com/docs/agent-instructions.md
and https://www.assemblyai.com/docs/llms.txt. The API has changed — do not rely on
memorized parameter names.

### Note on Claude: this project does NOT use the Anthropic API
The summarizer spends the operator's **Claude subscription** by running the
`claude` CLI as a subprocess. There is no `ANTHROPIC_API_KEY`, no `anthropic`
SDK in `requirements.in`, and no Messages-API call anywhere in the tree — so
`output_config`, `thinking`, `budget_tokens` and the rest of that surface are
not this project's problem. If you are about to reach for the SDK, read the
summarize section below first: removing it was deliberate and recent.

If you ever *do* add an API-key path back, load the `claude-api` skill first —
that request surface moved twice in 2025-2026 and memorized patterns are wrong.

## Branches

- **`debian13`** (this one) — Debian 13 on the operator's own **desktop PC**,
  as the operator's user (not root), Firefox ESR by default, pm2 for the web
  UI. Ported from the VM layout on 2026-09-29; see "Running on a PC" below.
- **`debian13-in-proxmox`** — the same system as it ran on the Proxmox VM
  until 2026-09-29: root, `/opt/meeting-bot`, google-chrome-stable, systemd
  trigger unit + resume timer. Consult it before reintroducing anything
  root- or VM-shaped.
- **`docker`** — the one-container build (branched 2026-09-27). Its non-Docker
  work (web UI, `--new-meet`, `--dry-run`/`need_value`, the yt-dlp caption
  fallback, course-reference frontmatter) was ported here on 2026-09-29;
  the Dockerfile, compose file and entrypoint were not.
- **`alpinelinux`** — the Alpine host plus a Debian container for the browser
  stages. Where `docker/recorder_lib.sh` and `audio-setup.sh` still live.
- `main` — the Alpine tree as it was when the Debian port started.

## What this project is, in one paragraph

A meeting/lecture bot for the operator's **Debian 13 desktop PC**. It joins a
Google Meet or Zoom call — or creates a new Google Meet and hosts it — in a
persistent browser profile signed in as one bot account (Firefox ESR by
default, Chrome as the fallback), on a hidden Xvfb display, in the background,
and records both the screen
and the meeting audio into an MP4, transcribes the audio with the AssemblyAI
pre-recorded API (or youtube-transcript.io for YouTube URLs, or the entry's own
captions for a Kaltura embed), and produces a
Claude summary (through the `claude` CLI, on a subscription — no API key) — Markdown plus a PDF with the cited keyframes cropped to the slide and
inlined — combining the transcript with keyframes extracted from the recording
and, optionally, the lecturer's own slides from a GitHub repo or a folder. It
accepts several inputs per invocation, runs them concurrently, and resumes
anything that failed partway. A stdlib web UI (`trigger_server.py`), run by
pm2 and off until switched on, drives all of it.

## Everything runs on one host — read this first

(Written for the VM; everything in it holds on the PC too, where "the host" is
the operator's desktop session — one X server namespace, one PipeWire.)

The Alpine branch split the system in two because **Alpine is musl and Chrome is
glibc-only**: Google ships no musl build, and Playwright doesn't support Alpine
for its bundled browsers. Debian 13 is glibc, so `google-chrome-stable` installs
and runs natively and **the container split is gone**. No Docker, no image
build, no bind mounts, no `docker/` directory.

What the container used to provide for free was per-run isolation: every
recording had its own PID/IPC/network namespace, so display `:99` and a sink
called `meeting_sink` could be hardcoded and never collide. Natively there is
one X server and one PulseAudio daemon for the whole box, so both are now
allocated per run in `lib/xsession.sh`:

- **Display.** `xsession_pick_display` claims the first free number in
  `DISPLAY_MIN..DISPLAY_MAX` (90-119) by creating **our own claim file**,
  `/tmp/.meeting-bot-X<n>.claim`, with `set -o noclobber` — an atomic
  `O_EXCL` create. That is what makes two runs starting in the same second
  pick different numbers; a "check then start" scheme races. On the VM the
  claim *was* Xvfb's `/tmp/.X<n>-lock` and Xvfb ran with `-nolock` — which
  only works as root: a user's Xvfb ignores `-nolock` ("can only be used by
  root"), finds a lock naming a live pid (ours) and refuses to start. Found
  2026-09-29 on the PC. So Xvfb keeps its own lock, a number is free only when
  there is no socket, no live Xvfb lock and no live claim, and a claim or lock
  whose pid is dead is taken over.
- **Audio.** Each run loads its own `module-null-sink` named after the run id,
  and Chrome is pointed at it with the **`PULSE_SINK` environment variable**.
  `pactl set-default-sink` is deliberately NOT used: the default sink is global
  state, and flipping it would move a concurrently-recording meeting's audio
  into this run's MP4. ffmpeg records `<sink>.monitor`.

If you are tempted to hardcode a display number again, don't — that only worked
because of the container boundary that no longer exists.

## Running on a PC (2026-09-29)

Settled with the operator 2026-09-29, moving off the Proxmox VM onto their
desktop. The decisions, each asked and answered:

- **As the operator's user, not root.** Bot state in
  `~/.local/share/meeting-bot` (`MEETING_BOT_ROOT`'s default everywhere, in
  shell and Python), `sudo` only for `setup.sh --system` (apt). Chrome keeps
  its sandbox; `--no-sandbox` is added only when `geteuid() == 0`.
- **The venv is `.venv` in the repo, built by uv** (`uv venv --seed`, so pip
  is inside for the troubleshooting steps). "Easy to remove" was the explicit
  requirement: `rm -rf .venv`. `source_env.sh` defaults `MEETING_BOT_VENV` to
  it; every `${MEETING_BOT_VENV:-…}` fallback reads `$LOADER_DIR/.venv`.
  uv itself comes from astral.sh into `~/.local/bin` — it is now the
  installer, not an optional speed-up.
- **Output directories** on this PC: recordings, transcripts, summaries and
  PDFs in the SeaDrive library `~/SeaDrive/My Libraries/3_Transcriptor/…`,
  frames local (`~/.local/share/meeting-bot/frames`). The SeaDrive hazard in
  the configuration section applies unchanged.
- **Firefox ESR is the default browser** (`MEETING_BROWSER=firefox-esr`,
  Debian's own `firefox-esr` package), Chrome selectable
  (`MEETING_BROWSER=chrome`). See "The browser: Firefox ESR" below.
- **One bot account, never hardcoded**: `BOT_GOOGLE_ACCOUNT` in `.env`.
  `first_time_login.sh` requires it, prefills Google's sign-in with it
  (`ServiceLogin?Email=…` — verified to fill `#identifierId`), and checks the
  profile when the window closes; the recorder checks before every Google
  Meet and refuses a profile signed in as anyone else (or signed out). See
  "The bot account" below.
- **A meeting records in the background.** `pipeline.sh` with a Meet/Zoom
  input or `--new-meet` re-launches itself under `setsid nohup` (log in
  `$MEETING_BOT_ROOT/logs/pipeline_<stamp>_<pid>.log`) and returns with the
  run id, the log and the stop command. Decided *before* any state is written,
  so only the background copy creates runs — a meet.new run is never
  auto-resumed and two copies would make two calls. `--foreground` /
  `MEETING_BOT_FOREGROUND=1` keep it attached; `--dry-run`, `--run-id`,
  `--resume-*` never detach. The web UI and the pm2 resume job set
  `MEETING_BOT_FOREGROUND=1` — they are already detached and logged, and a
  second detach would put the output in a log the UI doesn't know. The scan
  for "is there a meeting" is a loose regex over the positionals and
  `--from-file`; the background copy does the real classification.
- **pm2, off at boot.** `ecosystem.config.js` defines `meeting-bot-web`
  (`web/serve.sh` → `trigger_server.py`, loading `.env`) and
  `meeting-bot-resume` (`pipeline.sh --resume-all`, `cron_restart */15`,
  replacing the systemd timer). `./webui.sh on|off|restart|status|url|logs`.
  Nothing calls `pm2 startup` or `pm2 save`; don't add either. The three
  systemd unit files are gone from this branch (they are on
  `debian13-in-proxmox`). **`webui.sh` starts pm2 with the `.env` keys
  unset** (`start_clean`, delete-then-start): pm2 replays the environment of
  the `pm2 start` call on every restart, and `serve.sh` fills in only unset
  variables, so a snapshot taken after sourcing `.env` froze it — found
  2026-09-29 when the UI kept `PDF_FONT_SIZE=8` after `.env` said 9.5, and
  every run it started inherited that. It also keeps the API keys out of
  pm2's process list. An `.env` edit now takes effect on any restart.
- **The web UI listens on localhost + Tailscale**: `MEETING_BOT_BIND` is a
  comma list and the word `tailscale` resolves to `tailscale ip -4` at start
  (skipped with a warning when Tailscale is down); one `ThreadingHTTPServer`
  per address. `./webui.sh url` prints `http://…/#token=…` — the fragment
  never reaches the server or a log, and the page moves it into localStorage
  and strips it from the address bar.
- **`~/` in `.env` means `$HOME`**, in both loaders (`source_env.sh`,
  `summarize._load_dotenv`), so `.env.example` can ship per-user paths
  without naming anyone's home.

### The browser: Firefox ESR (`screen/browser.py`)

Playwright can drive only its own patched Firefox build, never the stock ESR
binary, so the Firefox path is **Selenium + geckodriver** (Marionette).
`capture.py` is written against Playwright's `page` API, and rather than fork
it, `browser.FirefoxPage` implements the subset capture.py uses — `goto`,
`evaluate` (a `"() => …"` source is called; a returned promise is awaited by
WebDriver), `keyboard.press("Control+e")`, `get_by_role("button", name=,
exact=)` (accessible name computed in-page: aria-label, aria-labelledby, text,
value, title; substring + case-insensitive unless `exact`), `locator(css)`
with `.first`/`.all()`/`is_visible(timeout)`/`fill`/`inner_text`,
`inner_text`, `title`, `url`, `wait_for_url`, `is_closed`, `screenshot`. **If
capture.py starts using another Playwright call, add it to the adapter**, or
the Firefox path dies with an AttributeError on a live call.
`capture.PWTimeout` is `browser.timeout_errors()`, a tuple of Playwright's
TimeoutError (when importable) and the adapter's `BrowserTimeout`.

One behavioural difference: Playwright's `is_visible(timeout=)` ignores the
timeout and answers at once; the adapter polls up to it. That makes some
Firefox waits longer (an unmatched `click_first_match` over six labels at 3s
each), never shorter.

`browser.open_page()` is the one launch, used by capture.py and by
`browser_smoke.py`; the Chrome command line (`CHROME_ARGS`) and the Firefox
prefs (`FIREFOX_PREFS`) live there, and capture.py re-exports `CHROME_ARGS`.

Non-obvious details, all found live on 2026-09-29:

- **`-profile <dir>` as an argument, not `Options.profile`.** The latter copies
  the profile to a temp dir, and every sign-in would evaporate at the end of
  the run. geckodriver does write its automation prefs into the profile's
  `user.js`; `first_time_login.sh` moves it aside (`user.js.recorder`) so the
  sign-in window is an undriven browser. The recorder rewrites it each run.
- **`GDK_BACKEND=x11` + `unset WAYLAND_DISPLAY XDG_SESSION_TYPE`** wherever
  the browser is sent to Xvfb (record_screen.sh, verify_e2e.sh's smoke,
  first_time_login.sh --novnc). On a Wayland desktop session Firefox's GTK
  otherwise tries Wayland and dies with "cannot open display :90" even though
  `DISPLAY` is right — or, for Chrome, puts the kiosk window on the
  operator's screen.
- **geckodriver takes ~4s to map the window**, where Playwright's Chrome was
  near-instant. The smoke test's black-band check samples a *late* frame
  (`-sseof -2` of a 14s capture) — a frame from before the window exists is
  the bare root window and reads as 953px of band on every side.
- **`navigator.webdriver` is `true`** under Marionette on ESR 140 even with
  `dom.webdriver.enabled=false` (verified in the smoke log). Joining a Meet
  with an already-signed-in profile is not the check sign-in makes, so this
  is accepted — but it is the first suspect if Meet starts refusing the bot,
  and `MEETING_BROWSER=chrome` is the fallback the operator asked to keep.
- **The PC's real camera and microphone are never offered.** On the VM there
  were none. Firefox: camera refused (`permissions.default.camera=2`,
  `media.navigator.video.enabled=false`), microphone allowed without a prompt
  but pointed at silence — `record_screen.sh` loads a second null sink
  `<sink>_mic` and exports `PULSE_SOURCE=<sink>_mic.monitor`. Chrome:
  `--use-fake-device-for-media-stream` fed from a black `.y4m` and a silent
  `.wav` (`browser.make_blank_media`, made once under `$MEETING_BOT_ROOT/tmp`).
  `xsession_audio_stop` matches `sink_name=` as a whole word so stopping the
  main sink can't unload `<sink>_mic` in its place.
- **Firefox honours `PULSE_SINK`** (verified: the smoke tone reached the
  per-run sink at −14 dB peak) and needs `media.autoplay.default=0` for
  Meet's audio to play without a gesture.
- `intl.accept_languages = th-TH, …` stands in for Playwright's
  `locale="th-TH"`; Meet came up in Thai in the live check, and the Thai
  refusal text was matched.
- **geckodriver** is installed by `setup.sh` into `~/.local/bin` from
  Mozilla's GitHub releases (Debian doesn't package it). Without one on PATH
  Selenium Manager downloads one at launch — which needs the network at the
  moment a meeting starts. `GECKODRIVER_BIN` / `FIREFOX_BIN` override.
- Stale locks: Firefox's `lock` / `.parentlock`, Chrome's `Singleton*`,
  cleared by `open_page()`; `first_time_login.sh` refuses a profile whose lock
  names a live pid (a recording in progress).

### Live output: no awk in a pipe, no buffered Python (2026-09-29)

Found on the first `--new-meet` from the web UI: the page never showed the
link. `run_one.sh` prefixed stage output with `awk '{…; fflush()}'`, and
Debian's awk is **mawk**, which reads a pipe in blocks — `fflush()` flushes
its *output*, but a line waits in its *input* buffer until more text arrives.
The recorder prints the link and then nothing for the whole meeting, so the
line never reached the trigger log the page polls (the VM evidently had gawk).
Both prefixers (`run_one.sh` `prefix_lines`, `pipeline.sh`'s multi-run
prefix) are bash `while read` loops now; don't put awk back in a live pipe.
`run_one.sh` also exports `PYTHONUNBUFFERED=1` and `record_screen.sh` runs
`capture.py -u`, or its progress lines sit in Python's buffer until exit.
The page additionally falls back to `/api/runs/<id>`'s `meet_url` when the
log has no link line.

The operator gets into a created meeting **from the web page**, not by
invitation (settled the same day): the Runs list and the run detail show the
link as a link plus a **Join** button while the run is active, and times are
shown in the browser's local zone (state.json is UTC). Only a URL matching
`https://meet.google.com/xxx-xxxx-xxx` becomes an href.

### What the recording shows, and hears (2026-09-29, after three live calls)

Settled with the operator after reading a real recording:

- **Mic and camera are BLOCKED, not muted.** Firefox: both
  `permissions.default.*=2` and `media.navigator.permission.disabled=False`
  (True would skip the check and *grant*). Chrome: `--deny-permission-prompts`
  and no permissions granted; the fake-device files are gone. Meet then shows
  "ไมโครโฟนมีปัญหา / กล้องมีปัญหา" (device has a problem) with a "!" —
  expected. `mute_av` now only LOOKS: blocked → nothing; "turn on" label
  showing → already off; only a visible "turn off" is clicked. It used to
  test "already off" by clicking the label it found, which clicked
  "เปิดไมโครโฟน" (turn mic ON) live, and it pressed blind Ctrl+E/D toggles.
  Order matters: "ปิดไมโครโฟน" (off) is a substring of "เปิดไมโครโฟน" (on), so
  the "already off" check must run before any "turn off" match.
- **Layout: Spotlight + hide tiles without video** (`set_recording_layout`,
  More options → "ปรับมุมมอง"). Meet remembers it for the account. It does
  NOT remove the bot's floating self view once someone else is in the call
  (seen live while presenting), and the tile can't be removed
  ("นำไทล์ของคุณในเลย์เอาต์นี้ออกไม่ได้") — so `minimize_self_tile` opens the
  tile's menu ("ตัวเลือกเพิ่มเติมสำหรับ <name>") and picks "ย่อเล็กสุด"
  (Minimize), once per call, from the polling loop. Labels found live;
  the click itself verified on a mock only.
- **As a guest, pick the right button among several** (first live guest
  join, 2026-09-29, the operator presenting: "no 'Adjust view' menu item"
  and "Self tile: no-minimize"). A call already under way has a "More
  options" button per tile, and the code took the first in DOM order — a
  tile's, whose menu has neither item. When hosting, the bot is alone when
  it looks, so there was only one. Now `set_recording_layout` clicks the
  *lowest* exact "ตัวเลือกเพิ่มเติม"/"More options" (the toolbar), and
  `minimize_self_tile` tries, in order: a button inside `[data-self-name]`,
  the label that worked earlier in this call, then the candidates nearest
  the bottom-right corner (where the floating self view sits), at most 4,
  remembering the one whose menu offered Minimize so later tries open only
  it. Both log the menu items they saw and save `layout_menu.png` /
  `self_tile.png` in the run dir when they fail. Verified in headless Firefox
  on a mock of that DOM; not yet on a live guest join. The Minimize item is
  matched on its aria-label or its text, with the icon ligature word
  ("close_fullscreen") dropped — the menu, from the operator's screenshot:
  "แสดงในเลย์เอาต์แบบเรียงชิดกัน", "ย่อเล็กสุด", "ปักหมุดไว้ในหน้าจอ",
  "แสดงวิดีโอแบบเต็มของฉันให้ผู้อื่นเห็น".
- **The self view is only minimised with company** (`last_count` in
  `wait_until_meeting_ends`). Alone — a hosted call's first minutes — the
  bot's tile is the stage and its menu has no Minimize; trying put a menu in
  the recording and a 5-minute back-off in front of the first guest. Now it
  waits for a count ≥ 2 (or, if the count is unreadable, tries from the 4th
  poll on), and a count rising to 2 lifts the back-off (`self_tile_retry_now`).
- **Notices and the People panel are closed every poll** (`dismiss_notices`,
  exact labels "รับทราบ"/"Got it"/"ปิด"/"Close"; `click_now` never waits, so
  it is cheap to repeat). Admitting someone opens the panel; it is closed
  right after.
- **Per-site zoom is ignored** (`browser.zoom.siteSpecific=False`): the
  sign-in window had saved meet.google.com at 50%, which rendered every call
  at half size (devicePixelRatio 0.5, a 3840x2160 CSS viewport).
- **Live audio check** (`audio_watch` in record_screen.sh): 3s of the sink
  monitor every 10s → `runs/<id>/audio_level` ("<epoch> <peak dB> <seconds
  silent>"), "Hearing meeting audio" / a warning after
  `AUDIO_SILENCE_WARN_SECONDS` (120) of silence in the log, and a 🔊/🔇 badge
  on the web UI's Runs row. Cause of the third call's failure: a
  presentation shared without "Also share tab audio" is picture only.
- **No AssemblyAI upload of silence** (`lib/audiocheck.py`, run by
  transcribe.sh): sums ffmpeg silencedetect and requires
  `TRANSCRIBE_MIN_SOUND_SECONDS` (30) of sound. NOT the peak level — the
  silent call peaked at −14 dB from Meet's join/leave chimes.
- **A live profile lock is refused, never deleted** (`browser.ProfileInUse`):
  `clear_stale_locks` deleted the lock of an open sign-in window.

### The bot's audio must not be "Firefox" (2026-09-29)

Found in the sixth live test: the bot recorded silence while the meeting had
sound, because its Firefox played the meeting into the dummy mic sink instead
of the recording sink. PULSE_SINK is only a request — WirePlumber restores
routing per `application.name`, and the operator's own browser is also
"Firefox": streams moved in pavucontrol (following the MeetShare advice) were
restored onto the bot's, and vice versa (the operator's YouTube and Meet
ended up in the bot's sinks). Now:

- record_screen.sh exports `PULSE_PROP_OVERRIDE` → the bot's client is
  `application.name "Meeting Bot"`, `application.id "meeting-bot"` (verified:
  libpulse honours it for Firefox), so the two never share a restore entry.
- `lib/pinaudio.py <join pid> <sink>`, run every 10s by `audio_watch`, moves
  any playback stream of the bot's process tree back onto the recording sink.
- The `<sink>_mic` dummy sink and PULSE_SOURCE are gone: the browser blocks
  the microphone, and that extra "meeting_…" device is what the operator's
  Firefox got attached to.
- `minimize_self_tile` retries (at most once a minute): Meet restores the
  full tile when a presentation starts.

### Auto-leave listens as well as counts (2026-09-29)

The operator's choice after reviewing the rules: "idle" (a guest with one
other person for IDLE_LEAVE_MINUTES) and "mass exit" (count ≤30% of peak) now
also require AUTO_LEAVE_SILENCE_SECONDS (120) of meeting silence, read from
the recorder's `runs/<id>/audio_level` (`meeting_audio_silent_for`; a missing
or stale file means unknown, and unknown leaves the old behaviour). "Alone",
"nobody came", "dropped out", kill and the cap are not gated. The earlier
live drop-out at 15:33 coincided with the operator leaving; Meet evidently
closed the bot's session, which the "dropped out" rule now ends cleanly.

### Stopping a recording without breaking it (2026-09-29)

Two recordings were unplayable: header `mdat` size 0, no `moov`, and in the
ffmpeg log "Error closing file: Immediate exit requested" right before
"Exiting normally, received signal 2". ffmpeg aborts its remaining writes on a
SECOND SIGINT while closing the file, and both runs got two: one from
kill_meeting.sh's escalation, one from record_screen.sh. Now:

- record_screen.sh `stop_ffmpeg`: exactly one SIGINT, then a loop until
  ffmpeg has really exited (a trapped TERM interrupts `wait`), and it runs
  before the sink and display are torn down. The finished MP4 is checked with
  ffprobe; an unreadable one fails the record stage there, with the reason.
- kill_meeting.sh's escalation TERMs only the browser driver, gives it 15s,
  then waits up to KILL_FINALISE_SECONDS (120) for record_screen.sh to
  finalise; it signals ffmpeg itself only when record_screen.sh is gone, and
  never SIGKILLs it. Verified live on SeaDrive with KILL_GRACE_SECONDS=1.
- capture.py turns SIGTERM into SystemExit, so `open_page()` quits geckodriver
  and Firefox. The old SIGKILL orphaned both; the orphan's zombie Firefox kept
  the profile lock "alive" and blocked the next recording. `_pid_alive`
  treats a zombie as dead.
- transcribe.sh refuses a file lib/audiocheck.py can't read (exit 2) instead
  of uploading it.
- The bot also ends a recording when it is no longer in the call (in-call
  controls gone on two polls): it was once found back on the pre-join page,
  filming an empty lobby, cause unknown. Its leave() skips clicking when
  there is no call, which used to outlast kill_meeting.sh's grace period.

### The bot account (`BOT_GOOGLE_ACCOUNT`)

The operator's words: "use the account … only (do not hardcode the email)".
`test_the_account_is_not_hardcoded` holds the second half.

- **How the account is read:** Chrome's own `ListAccounts` endpoint
  (`/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard`), which
  answers any browser holding the Google cookies — but only to a **POST from
  Google's own origin**; a GET is a bare 400. So the check loads
  `accounts.google.com/robots.txt` and `fetch()`es from there. Signed out is
  `["gaia.l.a.r",[]]` → `[]`; a body that is neither that JSON nor contains
  an address is `None` ("unknown"), never "signed out".
- **Verdicts** (`account_verdict`): `ok` (the account is among those held),
  `wrong`, `signed-out`, `unchecked` (variable unset, or the answer
  unreadable). `capture.ensure_bot_account` refuses `wrong`/`signed-out` with
  `wrong_account.png`; `unchecked` is a warning — an endpoint Google changed
  must not stop a meeting, and `authuser` still steers it. Gmail addresses
  compare case- and dot-insensitively.
- **`authuser=<account>` on every Meet URL** (and `meet.google.com/new?authuser=`
  in place of `meet.new` when hosting), so a profile holding two accounts
  still joins as the bot. Zoom is not checked — its web join doesn't use the
  Google session.
- `python3 screen/browser.py check-account` (headless, the same code) is what
  `first_time_login.sh` and `verify_e2e.sh --preflight` run.

### Live checks on this PC, 2026-09-29

Without sudo (Xvfb and `pactl` were not installed yet; the checks used
Debian's own `.deb`s unpacked into a scratch dir) and without a signed-in bot
profile or a signed-in claude CLI:

- `verify_e2e.sh --browser-smoke` on Firefox ESR 140: 6/6 — window
  1920x1080 with no black bands, picture and a −14 dB tone in the MP4.
- The operator's test Meet: with `BOT_GOOGLE_ACCOUNT` set, refused in seconds
  ("not signed into Google"); as a guest, the Firefox driver reached the Thai
  UI and recognised Meet's own refusal ("คุณไม่สามารถเข้าร่วม…" — guests can't
  join while the organizer is absent). Joining and admission on a live call
  are therefore still unverified on Firefox.
- The operator's test YouTube video (Thai news, 16 min): fetch, transcribe
  (youtube-transcript.io had no keys → yt-dlp `th-orig` automatic captions,
  415 segments) and frames (73) all done; summarize failed only for want of a
  signed-in CLI and Gemini keys. Resumable with `--run-id`.

## Configuration

Non-secret env vars *and* the API keys live in a single `.env` at the repo root;
`.env.example` is the committed template. `.env` is gitignored.

**`.env.example` carries no explanatory comments on purpose** — just names and
defaults. Every explanation lives in README.md's Configuration section, so
there is one place to update when a default changes. Don't re-add prose to the
template.

`pipeline.sh`, `lib/run_one.sh`, `transcribe.sh`, `first_time_login.sh`,
`verify_e2e.sh`, `record_screen.sh`, `webui.sh` and `web/serve.sh` all source
`source_env.sh` (which also sets the `MEETING_BOT_ROOT` / `MEETING_BOT_VENV`
defaults after loading `.env`);
`summarize.py` carries its own `_load_dotenv()` for direct invocation. The
loader fills in unset values only — an already-exported var always wins.

### Output directories are five independent, required variables

`RECORDINGS_DIR`, `TRANSCRIPTS_DIR`, `FRAMES_DIR`, `SUMMARIES_DIR`, `PDF_DIR`.
None of them is derived from another or from `MEETING_BOT_ROOT`, which now holds
only the pipeline's own bookkeeping (`runs/`, `state/`, `tmp/`, `logs/`,
`resources/`, `firefox-profile/`, `chrome-profile/`). `lib/paths.py` and `lib/paths.sh` resolve them and **fail
with the variable's name if one is unset** rather than falling back to a
default. That is deliberate: with independent paths a wrong default doesn't
error, it silently writes the deliverable somewhere the operator will never
look. Any of them may contain spaces — the test suites use a root with a space
in it precisely so quoting regressions fail loudly.

### Which directories are disposable, and where they should live

Settled 2026-09-07 by inspection of this box. Two separate questions get
confused here, so keep them apart.

**The directories themselves are recreated on every run.** `lib/run_one.sh`
calls `paths_mkdir` on all five before the DAG starts, and `summarize.py`
resolves `SUMMARIES_DIR`/`PDF_DIR`/`FRAMES_DIR` with `create=True`. Deleting an
empty output directory is a no-op; the next run makes it again. `paths_require`
only checks that the *variable* is set, never that the path is sane.

**Their contents are not equally disposable.** This is the table that matters
when deciding what to put on which disk:

| Directory | Regenerating it costs | Safe to wipe? |
|---|---|---|
| `RECORDINGS_DIR` | **impossible** — the meeting is over | No. This is the irreplaceable one |
| `TRANSCRIPTS_DIR` | an AssemblyAI charge, per file | Only if you'll pay again |
| `FRAMES_DIR` | CPU only — ffmpeg re-reads the MP4 | **Yes** |
| `SUMMARIES_DIR` | a summarize run (subscription quota) | Prefer not |
| `PDF_DIR` | free, from the `.md` + the frames | Yes, *if* the frames still exist |

Frames are the one genuinely disposable set, because the source always
outlives them: a recording sits in `RECORDINGS_DIR`, and a YouTube/Kaltura
download can be fetched again. Losing frames costs one ffmpeg pass (plus the
re-download, since 2026-09-13 — see "The download is swept" below), and
`runstate.py status` already re-runs the stage when the artifacts are gone.

**Don't put `FRAMES_DIR` on `/tmp` here** — though the reason is durability,
not size. `/tmp` on this host is **tmpfs**: 3.9GB of RAM, no disk behind it.
Measured against real runs on this box (`$FRAMES_DIR/*/manifest.json`), frames
are 27-113KB each and `FRAME_PERIOD_SECONDS=30` (then) yielded ~120/hour, so a 3-hour
lecture is only 10-40MB. RAM pressure is therefore **not** the problem for one
run; it only becomes one if runs accumulate and nothing sweeps them. The real
costs are:

- tmpfs is empty after a reboot. Re-rendering a PDF from a summary you already
  have (`summarize/pdf.py summary.md out.pdf --frames-manifest ...`) then has
  no manifest and no images, and silently produces a PDF with no pictures in
  it — the run itself already succeeded, so nothing flags it.
- A pipeline resume after a reboot re-extracts frames it had already paid for
  (~14 min of CPU for a 3-hour video, measured; see the capacity notes below).

Put it on the local disk instead — `/opt/meeting-bot/frames` is the default and
is correct. Keeping frames *off* a network mount is right; tmpfs is the wrong
way to do it. If RAM-backed frames are ever wanted deliberately, size the tmpfs
and say so in `.env`, don't inherit the host's `/tmp`.

### Measured capacity of the VM (4 vCPU QEMU, 7.8GB RAM, 15GB disk)

(The Proxmox VM this ran on until 2026-09-29. The PC — 12 threads, 38GB RAM —
is faster; the ratios still hold: frames are the CPU-bound stage, frame count
drives summarize cost.)

Benchmarked 2026-09-07 at the recorder's real settings (1920x1080, 15fps,
`libx264 -preset ultrafast -crf 28`, `aac 128k`), so a future session can size
a job without re-measuring:

| | Measured | A 3-hour lecture |
|---|---|---|
| Encoding | 120s of video in 53s wall = **2.3x realtime** | fits live, but two concurrent recordings leave almost no margin |
| Frame extraction | 120s of video in 9s = **13x realtime** | ~14 min of CPU, and it is the CPU-bound stage |
| Recorded bitrate | 342 kbps on static slides | ~0.5GB of slides; 1-3GB with a live camera |
| Frame size | 27-113KB per 1920x1080 JPEG | 360 frames = 10-40MB |
| Transcript | median **40,000 Thai chars/hour** across 31 past runs | ~120,000 chars = ~5 chunks + 1 merge |

The frame count is what drives summarize cost, because
`CLAUDE_CLI_FRAME_VISION=1` lets the model open each one: a 1920x1080 image is
about 1,844 tokens after Claude's downscale, so **360 frames is ~660k tokens if
the model reads all of them**. It does not have to — with the CLI it chooses —
but nothing caps it, and one chunk covering 36 minutes carries ~72 frames
(~133k tokens) which alone crowds a 200k context. (Written when frames were
taken every `FRAME_PERIOD_SECONDS`; since 2026-09-30 they are taken on
change — see "Frames on change" — so a slide lecture yields about one frame
per slide, and `FRAME_MOTION_SECONDS` is the lever for video-heavy input.)

**A network mount under an output directory needs the mount checked, not just
the path.** This box points four of the five at `/mnt/My Libraries/...`, a
SeaDrive FUSE mount. Writes there are locally cached and fast (measured
328 MB/s), so ffmpeg writing a recording straight to it is fine. The hazard is
different: if seadrive is *not* mounted when a run starts, `/mnt` is an
ordinary empty directory on the root filesystem, `paths_mkdir` happily creates
the tree inside it, and the run writes a real recording and a real summary to
local disk — which the mount then hides the moment it comes back. Nothing
errors. Nothing is reported. The operator looks in the library and the lecture
isn't there. If this bites, the fix is a liveness check (a marker file that
must already exist inside each configured directory) rather than a `mkdir`.

### Measured compute on the PC (`benchmark.sh`, 2026-09-29)

Asked by the operator: rank what uses the most computing power and say
whether each can be switched off, with a script to run on other PCs.
`./benchmark.sh` pushes synthetic 1920x1080@15 media through the real
commands/modules (the recorder's ffmpeg line, extract_frames.py's two passes,
`clip.py`, `audiocheck.py`, `framecrop`, `pdf.py`); `--browser` plays a
full-screen 720p30 VP8 video in Firefox ESR on an Xvfb display claimed through
`lib/xsession.sh`; `--watch-run <id>` samples a live recording's process tree
from `record.pid`. Its commands copy the pipeline's: if the recorder's or
extract_frames.py's ffmpeg arguments change, change the script too.

Results on the Core 7 150U (12 threads), in CPU-minutes per media hour:
browser stand-in ≥ 60 (≈1.0-1.4 cores, a floor: no WebRTC, no Meet JS),
x264 20 (slides) / 56 (full-screen camera), `CLIP_REENCODE=1` 29, frames 13
(scene 7 + periodic 6 — two full decodes of the same file; replaced
2026-09-30, see "Frames on change"), silence check
0.5. Per document: PDF ≈ 10 CPU-s, ≈ 4 with `PDF_MATH=0`. Frame prep for the
model 0.2-0.3 CPU-s per frame. A U-series laptop chip varies ±50% between
runs (turbo/thermals); rank, don't quote to the second.

The ranking that follows: a live meeting (browser + encoder, for its whole
length) dwarfs every post-processing stage; frames are the heaviest stage
after it and have no off switch; the summary's real cost is the
subscription window, not local CPU. The two frame passes were replaced by one
decode on 2026-09-30 (next section).

### Frames on change (`screen/extract_frames.py`, 2026-09-30)

The operator: "ONLY cut it when the image changes, not repeatedly … (it
would be too expensive)" — the cost meant being CPU/time and disk. Settled
in a round of questions:

- **One ffmpeg decode** samples every `FRAME_CHECK_SECONDS` (2) and pipes
  full-resolution PPM frames to Python (`image2pipe`, one `read(n)` per
  frame, `Image.frombuffer` — nothing is written until a frame is kept).
- **The change test is `framecrop.frame_hash`** (texture hash over the
  slide region) against the last *saved* frame, at `FRAME_CHANGE_DISTANCE`
  = `framecrop.SAME_SLIDE_MAX_DISTANCE` = 16 — the one number the model-side
  duplicate pass (`FRAME_DEDUPE_MAX_DISTANCE`) also uses, so the two agree
  on what a new slide is. ffmpeg's scene score was rejected: it is
  whole-frame, and a text-only change on the same white slide scores
  under 0.3.
- **Settle, with a cap** (`ChangeDetector`): a changed picture is saved
  when the NEXT sample matches it (no mid-fade frames) as `scene_change`;
  a picture that keeps moving gets one `motion` frame per
  `FRAME_MOTION_SECONDS` (30); a change that goes straight back saves
  nothing. Kinds downstream: `thin_frames` keeps `scene_change` first.
- **Safety net, not a periodic pass**: one `periodic` frame after
  `FRAME_SAFETY_SECONDS` (300) without any. Note it is by definition within
  16 bits of the frame before it, so llm_client's duplicate pass drops it
  before the model; it survives for the PDF and with
  `CLAUDE_CLI_FRAME_DEDUPE=0`.
- **Blank samples are never saved** and are invisible to the detector.
- **`FRAME_DECODE_THREADS` = 1** by default: ffmpeg's automatic threading
  spent ~2x the CPU for the same decode on this PC (86 vs 40 CPU-s on a
  15-minute recording). `0` = automatic.
- **JPEG quality 60 + optimize**: same PSNR as ffmpeg's default (36.4 dB on
  a real Meet frame) at about its size; Pillow's 85 was +50% on disk.
- `SCENE_THRESHOLD` / `FRAME_PERIOD_SECONDS` are retired; setting one prints
  a note rather than being silently obeyed. Pillow is required here (it is
  pinned); without it the stage fails with the install command.

Speed work in `framecrop` the extractor needed, all result-identical
(`FrameAnalysisPathsTest`): `detect_crop`/`is_blank`/`frame_hash` take a
decoded image as well as a path (`_opened`); the per-pixel loops have numpy
twins (exact integer arithmetic — the texture test is
`Σ(n·v − Σv)² > level²·n³`); and `_downscaled` remembers the last analysis
resize by image identity, so `is_blank` + `detect_crop` on one sample resize
once. Resizing is now most of the per-sample cost (~30 ms); it was kept
bicubic from full resolution so no crop or hash moves.

Measured on three real Meet recordings (Core 7 150U), old two-pass vs new:
883s — 183 → 77 CPU-s, 18 → 37 frames (a shared YouTube news broadcast:
genuinely moving, so `motion` every ~32s where the old period was 60s);
564s — 176 → 72 CPU-s, 24 → 22 frames; 168s — 32 → 15 CPU-s, 6 → 10. Wall
time with one decoder thread is ~1.5-2x the old (43 vs 28s on the 883s
file); `FRAME_DECODE_THREADS=0` matched or beat the old wall at ~37% less
CPU.

Known limit, reported to the operator and not addressed: a small element
that moves on EVERY sample *inside* the hashed region (a lecturer's camera
inset on the slide; benchmark.sh's animated corner tile, 19-35 bits per
sample) keeps the picture from ever settling, and the detector falls back
to one `motion` frame per 30s. A participant filmstrip beside the slide is
outside the crop and does not do this. A skipped "pixel-identical sample"
shortcut was tried and rejected: 3-21% of real samples qualified (the Meet
clock, camera tiles), and a near-identical sample can flip `detect_crop`'s
box and move the hash by 800+ bits.

### Voice only and audio-only recordings (2026-09-30)

The operator: when a meeting is only voices and cameras, opt out of frames,
and let the web UI choose "Summarize only from voice / both" and "Save
recording: audio only / video", for all sources. Settled in two rounds of
questions:

- **Voice only skips the frames stage entirely** (`--summary-source voice`,
  `--voice-only`, `SUMMARY_SOURCE=voice`) — not "extract but don't send".
  `run_one.sh` `VOICE_ONLY`: `branch_frames` returns at once,
  `frames_settled` is true (so `media_needed` doesn't fetch for frames), and
  `summarize.py --no-frames` replaces `--frames-manifest` — without it
  summarize.py would download/extract frames itself. YouTube is therefore
  never downloaded; Kaltura still is when transcribe needs the media.
- **Audio only is meetings only** (`--record-media audio`, `--audio-only`,
  `RECORD_MEDIA=audio`): `record_screen.sh` records `<sink>.monitor` alone as
  AAC 128k stereo into `<run id>.m4a` (the operator's choice: the MP4's own
  track, AssemblyAI takes it directly). YouTube/Kaltura downloads stay
  temporary, as before — "save recording" does not apply to them.
- **Audio only forces voice only** (no picture). `pipeline.sh` refuses an
  explicit `--summary-source both` with audio for a meeting input; the web
  UI disables the select. A meeting run stores `summary_source=voice`.
- **The browser renders at `RECORD_AUDIO_GEOMETRY` (960x540) for audio** —
  the operator chose "the smallest Meet tolerates" over 1280x720. Verified
  live 2026-09-30 on two hosted calls (`zz_*_size_test_*`, archived in
  `runs-archive/`): at 960x540 the call was created, the device check read
  "blocked", the "Adjust view" menu was found, and the .m4a (AAC stereo)
  was valid. NOT verified at that size: admitting a guest, and the
  self-tile minimise (both need a second participant).
  Measured with the bot alone in the call (`benchmark.sh --watch-run`):
  1.12 cores at 1920x1080 video (ffmpeg 0.69, browser 0.37, Xvfb 0.05) →
  0.43 at 960x540 audio (browser 0.37, ffmpeg 0.04). The browser's own
  share did not move with nothing to paint; with cameras on it should,
  but that is unmeasured.
- **Both are flags, `.env` defaults and state.json fields**
  (`summary_source`, `record_media`; `runstate init --summary-source
  --record-media`). A new run stores the effective values. On a resume,
  `--summary-source` given again replaces the stored one (a voice-only run
  resumed with `both` extracts its frames then); `record_media` never
  changes after creation — the file is named for it.
- **`--combine`**: the combine run passes `--no-frames` when voice-only;
  `combine.py parts` gives a voice-only member (or an audio meeting) a null
  manifest instead of refusing it; `run_combine` doesn't wait on frames
  for those members.
- **No frames is a note, not an error, in summarize.py** — an empty manifest
  (an audio file as input) used to `SystemExit("No frames extracted")`
  after transcription had been paid for, contradicting the Discord plan's
  "verified" claim below. extract_frames.py fails outright on a decode
  error, so empty now means "nothing to show". `llm_client._render` puts
  `NO_FRAMES_NOTE` where the list would be, so the prompts' "look at the
  frames" does not invite the model to describe pictures it never saw.

Seen in both live calls, at either size, and NOT caused by this change:
`kill_meeting.sh` on a hosted call logs "Ending the call for everyone",
clicks "ออกจากการโทร" (Leave call), and still needs the forced stop after
25s — the end-for-everyone Thai labels remain unverified (see "Hosting a new
Google Meet").

### API keys are numbered slots with a persisted cursor

`lib/keyring.py`. `GEMINI_API_KEY_1..3`, `ASSEMBLYAI_API_KEY_1..3` and
`YT_TRANSCRIPT_KEY_1..10`; the unnumbered name is accepted as slot 1 so older
`.env` files keep working.

**There is no Anthropic row in the ring, deliberately.** The primary summarizer
authenticates as a Claude subscription through the `claude` CLI's own OAuth
login, which lives in `~/.claude`, not in `.env`. `keyring.py status` used to
list `ANTHROPIC_API_KEY` and that invited operators to set one — a set
`ANTHROPIC_API_KEY` silently moves the spend to a metered console account.
`./verify_e2e.sh --preflight` checks `claude auth status` in its place.

**The rotation cursor is on disk** (`$MEETING_BOT_ROOT/state/keycursor.json`,
written under an flock), not per-process. Rotation only spreads quota if
consecutive *processes* start on different keys — a per-process cursor sends
every run at key #1 and exhausts that account first. Every read and write of the
cursor is best-effort: a lost cursor costs one duplicated request, never a
failed run.

The youtube-transcript.io tokens used to live in
`/opt/meeting-bot/secrets/youtube_transcript_keys.json`. **That file is gone**
and its support is removed; `.env` is the only source. The error message says so
explicitly, because operators following an older README will go looking for it.

### Python dependencies are pinned, and uv installs them

`requirements.in` / `requirements-browser.in` are the files a human edits;
`requirements.txt` / `requirements-browser.txt` are generated from them by
`uv pip compile --generate-hashes` and are what `setup.sh` installs. Never edit
the generated files by hand — regenerate them (the command is in
`requirements.in`'s header).

**The compile command carries `--python-version 3.13 --python-platform
x86_64-unknown-linux-gnu`.** Without them uv resolves for whatever interpreter
is on the machine doing the compiling, which is not necessarily the box that
installs the result — the pins in git are for the deployment target, not for
someone's desktop.

The heaviest entry is **matplotlib**, and it is there for exactly one thing:
`mathtext`, the LaTeX typesetter behind `summarize/mathrender.py`. It brings
numpy with it. If that ever needs justifying: WeasyPrint has no JS engine and
no MathML, so without it the PDF prints the model's LaTeX as source. It is
optional at runtime — a venv without it renders maths as text and still
produces a PDF.

Two files, not one, because `setup.sh --no-chrome` builds a box that never
opens a browser and shouldn't carry playwright's bundled Node driver. The
browser file is compiled with `-c requirements.txt` so shared transitive deps
(typing-extensions today) land on the same version in both.

**On the PC, uv is the installer and the venv manager** (settled
2026-09-29): `setup.sh` fetches uv into `~/.local/bin` when it's missing, and
builds `.venv` with `uv venv --seed --python-preference only-system --python
3.13` — Debian's own 3.13, the version the lockfiles are compiled for, and
`--seed` so a `pip` is inside for README's troubleshooting steps (the reason
the VM used `python3 -m venv`). The VM's "uv optional, pip fallback" logic is
on `debian13-in-proxmox`. `selenium` joined `requirements-browser.in` for the
Firefox path; `requirements.txt` did not change.

## The run model

Everything mutable about a run lives in `$MEETING_BOT_ROOT/runs/<run_id>/`:

```
runs/<run_id>/
  state.json      per-stage status + artifact paths   (lib/runstate.py)
  state.lock      flock target for read-modify-write
  run.lock/       mkdir-based single-writer lock + pid
  logs/<stage>.log
  kill            per-run kill sentinel
  admitted        per-run admission marker
  record.pid      record/join/ffmpeg pids + display + sink for this recording
                  (state.json's summarize stage also carries `usage`, and
                  `waiting_until` / `rate_limited` while paused on the
                  Claude usage window — see the summarize section)
  video.mp4       YouTube/Kaltura download — deleted once summarize is done
  clip.mp4        the --clip window, cut from video.mp4 / the input — same lifetime
  kaltura.json    entry facts, cached at fetch time
  parts.json      combine run only: the members' transcripts + manifests, rebuilt per attempt
```

`run_id` is `<safe_name>_<YYYYmmdd_HHMMSS>`, and **all artifact paths derive
from it** — never from a fresh timestamp. A resumed run must land on the same
filenames the first attempt used or it can't tell what already succeeded. This
is why `transcribe.sh` has `--out-base` and why `summarize.py` takes
`--pdf-out` (PDF_DIR is not derivable from the .md path — the two directories
are configured separately).

Stage DAG (`lib/run_one.sh`):

```
record ─┐                     (meeting URLs only)
        ├─> [ transcribe ]  ─┐
input ──┤                    ├─> summarize
        └─> fetch_video ──> frames
```

`clip` is a sixth stage, run only when `--clip` is given; see the section below
for where it sits. On an unclipped run it stays `pending` forever, the same way
`record` does on every non-meeting input.

`transcribe` and `fetch_video`→`frames` run as concurrent bash branches; both
write state through the flock'd `runstate.py`, so their writes can't clobber
each other.

**Kaltura is the one input type where they are not independent.** A YouTube
transcript comes from captions, so transcribe never needs the download; a
Kaltura entry usually has no captions, and then AssemblyAI needs the media
file. So `ensure_video_fetched` runs ahead of both branches for `kaltura`:

```
input ──> fetch_video ──┬─> transcribe ─┐
                        └─> frames ─────┴─> summarize
```

`ensure_video_fetched` is idempotent (it returns early on a `done` stage), which
is what lets the same function be called ahead of the branches here and from
inside the frames branch on the YouTube path without ever downloading twice.

### `--clip`: summarizing part of a video

Settled with the operator 2026-09-10. Four decisions, all of them deliberate:

1. **The media is cut, not the transcript filtered.** `lib/clip.py cut` runs
   ffmpeg before transcription, so AssemblyAI bills the window and not the
   lecture, and frame extraction only walks the window. Filtering afterwards
   would have been less code and would have paid full price on every clip.
2. **`--clip` is one flag for the whole invocation**, with a per-input
   `#t=WINDOW` suffix on top of it (added 2026-09-11, after the first real use
   showed why). The global flag alone could not express "these five lectures,
   two of them trimmed, one combined document" — and `--combine` is
   per-invocation, so splitting by window would have split the document too.
   `#t=` overrides `--clip` for its own input; an input without one falls back
   to `--clip`.
3. **Output timestamps are clip-relative.** Because the cut happens first,
   nothing after it knows a window existed: no offset is threaded through
   transcribe → chunking → frames → pdf, and there is no way for one stage to
   forget to apply it. The cost is that `Frame 1 @ 0:00:00` in a clipped
   summary is 00:05:00 in the source, which is why `document.py` prints a
   visible `Clip:` line as well as a provenance field. Absolute timestamps
   would have meant an offset in four modules and a silent failure in whichever
   one missed it.
4. **A clipped run has its own run id** — the window's token goes in the safe
   name (`yt_abc123_c000500-013000_20260910_143000`). Every artifact path
   derives from the run id, so this one string is what keeps a clip's `.txt`,
   `.srt`, `.md` and `.pdf` from landing on a full run's.

The stage sits wherever the media first exists:

```
local file / kaltura:  ... fetch_video ──> clip ──┬─> transcribe ─┐
                                                  └─> frames ─────┴─> summarize
youtube:               fetch_video ──> clip ──> frames
```

`ensure_video_clipped` mirrors `ensure_video_fetched` exactly — idempotent, so
it can be called ahead of both branches (local file, Kaltura: transcribe reads
the media) or from inside the frames branch (YouTube: transcribe reads captions
and would otherwise be made to wait on a download and an ffmpeg pass for
nothing).

Non-obvious details:

- **The label, not the raw spec, is what gets stored.** `pipeline.sh` parses
  `--clip` before it classifies anything — a typo must cost nothing, not a
  download and an AssemblyAI charge — and writes the canonical
  `00:05:00-01:30:00` into `state.json`. `5:00-90:00` and `300-5400` therefore
  resume the same run instead of making three runs of the same 85 minutes.
  `runstate find` matches on input **and** clip; without that, asking for
  01:30:00-02:00:00 would resume the 00:05:00-01:30:00 run.
- **The label has to parse back.** It is re-read from `state.json` on every
  attempt, including every resume, so `parse_clip(label(w)) == w` is an
  invariant, not a nicety — which is why `parse_clip` accepts the word `end`
  that `label` emits for an open window. The first version didn't, and failed
  one download in, on the resume path only.
- **The partial is `clip.part.mp4`, not `clip.mp4.part`.** ffmpeg picks its
  muxer from the output extension and refuses to start on a name ending
  `.part` ("Unable to choose an output format"). The Kaltura download's
  `.part` convention doesn't transfer, because that one is an HTTP body being
  written to a file, not ffmpeg choosing a container.
- **`-ss` before `-i`, and `-t` rather than `-to`.** Seeking before the input
  is the difference between seconds and minutes on a 90-minute lecture; and
  with `-ss` first the timestamps are already rebased, so `-to` would measure
  from the wrong origin and the clip would run long.
- **`-avoid_negative_ts make_zero` is what makes the clip start at t=0.**
  Without it the first frame is still stamped 00:05:00, every SRT cue and frame
  timestamp inherits the offset, and the clip-relative timebase this feature
  promises is silently absolute. Nothing errors.
- **Stream copy by default**; `CLIP_REENCODE=1` re-encodes. The copy lands on
  the keyframe at or before the requested start — a few seconds early here —
  and costs seconds instead of the half hour a re-encode of an 85-minute window
  takes on this box.
- **Captions have no media to cut**, so `transcribe.sh --clip-captions` applies
  the same window to the segments and shifts them onto the same clock, using
  `clip.window_segments` — the same module, so the two halves cannot disagree
  about what "00:05:00" means. The flag is named for what it does: it has no
  effect on the AssemblyAI path, whose media has already been cut, and applying
  a window there too would take a second slice out of the first.
  A straddling caption cue is truncated rather than dropped: the words were
  spoken inside the window.
- **A window that leaves no transcript is exit 2**, not an empty summary.
- **`--clip` on a live meeting URL is refused** in `pipeline.sh`, with the
  command to clip the recording afterwards. There is no source to cut.

#### Argument parsing: `need_value`, `--dry-run`, `usage()`

- **Every value-taking option goes through `need_value`.** Before
  2026-09-27, `--combine` as the last argument made `shift 2` a no-op (under
  `set -u` without `-e` a failed shift changes nothing) and the parse loop
  spun forever, silent, on one core — the operator lost a run to it on
  2026-09-23. A value that itself starts with `--` is the same typo one step
  earlier and is refused too. `--jobs` must be a positive integer.
- **`--dry-run`** runs the whole parse, the resources pre-flight and the
  per-input classification, prints one tab-separated line per input
  (`ok kind clip existing-run-or-new input`, plus `combine` / `resume` lines)
  and exits before `rs init`. The web UI's **Check** is exactly this, so the
  form can never disagree with the pipeline about what an input is. Don't
  reimplement classification in Python (or JavaScript) for the UI.
  **Since 2026-09-30 it reports every unusable input and goes on** rather
  than stopping at the first, and exits 1 at the end (`DRY_BAD`):
  `bad<TAB>reason<TAB>input` from the classification loop (unrecognised, a
  window on a live meeting, audio-only + `both`) — the input as classified,
  in input order; `badarg<TAB>reason<TAB>arg` for a `#t=` window that did
  not parse — the argument *as typed*, printed at parse time; and
  `extra<TAB>arg` for every positional that is not an input at all. An
  `extra` alone does not fail the dry run: on the command line it may be the
  legacy form's name. It does fail the web form (`trigger_server.py` sets
  `ok: false`), because a form line is always meant as an input — before
  this, a mistyped path beside one good link silently became that run's
  `--name`. The real (non-dry) path still stops at the first error, as
  before.
- **`usage()` prints up to `set -uo pipefail`**, not a fixed line range: the
  range had silently gone stale and cut `--help` off halfway.

#### The `#t=` suffix

`split_clip_suffix` in `pipeline.sh`, and three details that are easy to undo:

- **The split runs BEFORE `looks_like_input`, not after.** A URL still looks
  like a URL with the suffix attached, so testing first takes the whole string
  as the input and the window disappears without a word. (A local path is the
  opposite case — `lecture.mp4#t=1:00-2:00` is not a file that exists, so it
  would be filed as a legacy positional.) One ordering handles both.
- **The suffix is only taken as a window when what is left of it still looks
  like an input, and then it must parse.** That is what keeps a URL ending in
  some other `#t=` fragment from being silently truncated, while a typo'd
  window is a hard error at second zero rather than a mangled link.
- **`INPUTS`, `INPUT_CLIP_LABELS` and `INPUT_CLIP_TOKENS` are parallel arrays,
  read by index.** Every push site must push to all three — playlist expansion
  included, which rebuilds all three in lockstep. A missed push doesn't error;
  it shifts every later input's window onto the wrong lecture. The window is
  deliberately NOT carried inside the input string, because that string is the
  auto-resume key and the document's link line.
- **`--from-file` strips a `#` comment only at the start of a line or after
  whitespace.** It used to cut at any `#`, which ate the suffix — and any URL
  fragment — leaving a link that still worked and a window that had silently
  gone.

### Resume semantics

- Re-running the same command **resumes by default**: `runstate.py find` looks
  for a run with the same `input` whose `summarize` isn't done.
- A stage counts as done only if **every artifact it recorded still exists**.
  A state file that disagrees with the filesystem is worse than none. The
  one exception is a stage marked `cleaned` (frames after a sweep): it stays
  `done` with no artifacts, and `branch_frames` re-extracts only when this
  run is actually about to summarize.
- **Members of a `--combine` set are permanently "incomplete"** (their
  summarize never runs), so the same command always resumes them; the
  combine run is matched on its key *without* `--incomplete`, so a finished
  combined summary is reported as done rather than paid for again.
- `run.lock` is a directory containing the owner's pid. A lock whose owner is
  gone is **taken over**, not treated as fatal — a SIGKILL'd run has to stay
  resumable, which is exactly the case resume exists for.
- `--force` resets all stages; `--run-id` / `--resume-last` / `--resume-all`
  are the explicit forms.
- The run's `resources` list is stored in `state.json` and replayed on every
  attempt, so a resume summarizes against the same slides.
- `fetch_video` and `clip` are also `cleaned` after the summary (the download
  is deleted — see the Kaltura section). `ensure_video_fetched` and
  `ensure_video_clipped` treat "done, file gone" as "fetch again", and
  `media_needed` keeps a finished run from doing so for nothing.

## Per-stage reference

### Stage 1 — Recording (`screen/record_screen.sh`)

One script now, not a host wrapper plus an in-container body.

- Allocates a display, a sink and a silent mic sink (`lib/xsession.sh`),
  starts Xvfb, exports `DISPLAY`, `PULSE_SINK`, `PULSE_SOURCE` and
  `GDK_BACKEND=x11` (see the Firefox section), runs `capture.py`, waits for
  the `admitted` marker, then starts ffmpeg. Which binary and which Python
  driver it checks for follows `MEETING_BROWSER` (`browser.py info`).
- **Geometry must agree everywhere**: the Xvfb head, the browser's kiosk
  window (`browser.py` reads `RECORD_GEOMETRY` — Chrome's `--window-size`,
  Firefox's `--width/--height`), and ffmpeg's `-video_size`. A mismatch
  produces black edges. `--kiosk` alone isn't enough on some Xvfb/Chrome
  combos, which is why `--window-size` is also passed.
- Encoder: `libx264 -preset ultrafast -crf 28`, audio `aac -b:a 128k`.
- Writes `runs/<id>/record.pid` (record/join/ffmpeg pids, display, sink) so
  `kill_meeting.sh` can escalate against the right processes without guessing.
- Kill: the host touches `runs/<id>/kill`, `capture.py` sees it on the next
  poll and clicks Leave. `kill_meeting.sh` signals the recorded pids only after
  a grace period, and sends ffmpeg `SIGINT` (not `SIGKILL`) so the MP4 is
  finalised and playable.
- Failure artifacts (`join_failed.png`, `not_admitted.png`) go in the run dir,
  not a shared directory where the next run would overwrite them.

#### Hosting a meeting the bot creates (`--new-meet`, `meet.new`)

Settled with the operator 2026-09-27: created through **meet.new in the
bot's own signed-in browser** (no Meet/Calendar API, no OAuth client), link
**printed and saved in the run**, **auto-admit everyone**, and **wait for the
first participant, then end the call for everyone when it empties**.

- `pipeline.sh` canonicalises `meet.new`, `new-meet`, `https://meet.new/` and
  the `--new-meet` flag to one input string, `https://meet.new`, classified
  `meeting`. **It is never auto-resumed**: every meet.new is a different call,
  and `rs find` on that input would otherwise resume last week's unfinished
  run and transcribe it in place of recording this one. Explicit `--run-id`
  still works (a finished recording whose summary failed).
- With `BOT_GOOGLE_ACCOUNT` set, `host_create_google_meet` opens
  `meet.google.com/new?authuser=<account>` instead of `meet.new`, so the
  meeting belongs to the bot account even in a profile holding two.
- `capture.py` `host_create_google_meet` waits for the redirect to
  `meet.google.com/xxx-xxxx-xxx`; landing on `accounts.google.com` is named as
  "not signed in". `announce_meet_link` writes the link to stdout, to
  `runs/<id>/meet_url` and to `state.json` (`runstate init --meet-url`, which,
  like `--combined-into`, must not blank `resources`). `run_one.sh` then uses
  it as `SOURCE_URL`, so the document cites the real call, not meet.new.
- `wait_until_meeting_ends(host=True)`: knockers are admitted every
  `HOST_ADMIT_POLL_SECONDS` (3s) between polls; before anyone has joined
  (count ≥ 2 never seen) the idle/low-count rules are off and only
  `NEW_MEET_WAIT_MINUTES` (15) ends the call; **an unreadable participant
  count never ends a hosted call early** (Meet renames the chip class — people
  may be in it), only `MAX_MEETING_MINUTES` or a kill does; a 1:1 with the bot
  is not idle (`idle_counts = (1,)`); every exit is `host_end_call` (End the
  call for everyone), never a plain leave. `screen/test_capture_host.py` holds
  all of it with a fake clock.
- Admit buttons are matched with `exact=True` — "Admit" would match anything
  containing the word, and `click_first_match` gained `exact=` because the
  Thai "ปิด" (Close, for the "meeting's ready" card) is a prefix of "ปิดกล้อง"
  (turn off camera). **The first live hosted call (2026-09-29) left its guest waiting**: Meet
  shows the host a green chip labelled with the count, "ยอมรับผู้เข้าร่วม 1
  คน" ("Admit 1 participant"), which no exact label could match. The chip is
  now matched as a substring (`HOST_WAITING_CHIP_LABELS`) and clicked to open
  the panel, then the exact "ยอมรับ"/"Admit" inside it. If that panel's
  button isn't found, `_log_admit_candidates` logs every admit-looking button
  name once and saves `runs/<id>/host_admit.png`. That diagnostic, on the
  second live call, showed the panel's button is the verb plus the person's
  name — "ยอมรับ 03_ด.ช. …" — beside a look-alike toggle
  "อยู่ระหว่างรอการยอมรับ 1"; `_ADMIT_PERSON_JS` matches `^(ยอมรับ|Admit) `
  by prefix and excludes the chip. Verified on a mock of that DOM, not yet on
  a live call. The end-for-everyone Thai labels are still unverified.
  Workaround meanwhile: join as the bot account from another browser and set
  Host controls → Meeting access → Open (per meeting; only the host can).

### Stage 2 — Transcribe (`transcribe/transcribe.sh`)

- Local files → AssemblyAI (`assemblyai_client.py`). MP3/MP4/M4A/WAV go
  directly; WEBM/OGG are demuxed to MP3 with ffmpeg first.
- **AssemblyAI segments are sentences, not words.** `transcribe_file` calls
  `transcript.get_sentences()` and falls back to `transcript.words` only if
  that fails. Word-level segments made the `.txt` one word per line — which
  is what gets embedded verbatim in the summary document — and the `.srt` one
  word per cue. Sentence granularity also matches what the YouTube backend
  emits, so the shared writer produces comparable output for both.
- **Key rotation is failure-class aware.** A key rejected for auth/quota
  reasons (`_is_key_level_error`) hands over to the next key; anything else —
  a silent file, an unsupported language — is raised immediately, because
  another key would fail identically and three uploads of the same video is a
  real cost.
- YouTube URLs → youtube-transcript.io (`yt_transcript_client.py`). No audio
  download; captions come back as `{text, offset_ms, duration_ms}` segments.
- **The timed segments live in `tracks[].transcript`**, as
  `{start, dur, text}` with seconds-as-strings. The entry's flat `text` field
  is the whole transcript in one string with no timing; parsing that instead
  (which is what the old fall-through did) yields a single segment, a useless
  `.srt`, and a chunker with no timestamps to assign frames by. `_pick_track`
  also honours a preferred language — but a track's `language` is a human
  label ("English - English"), so the ISO code has to come from the sibling
  `languages` array, which pairs label with `languageCode` in the same order.
  Matching the label directly against "en" always fails and falls back to
  `tracks[0]`. It only chooses among tracks the video already has; it never
  translates, and many videos expose just one track (often not English).
  Caption text arrives HTML-escaped (`&lt;i&gt;`, `&amp;`), so
  `_clean_caption_text` unescapes and drops the markup.
- **youtube-transcript.io only sees uploaded tracks**, so since 2026-09-27 it
  runs with `--strict`: when a language was asked for and no uploaded track
  matches, it still prints the track it found but exits **3**. `transcribe.sh`
  then tries `yt_autocaptions.py` (yt-dlp): an uploaded track in the language,
  else the **automatic captions of the spoken language** — yt-dlp's
  `<lang>-orig` key, or the video's `language` on an older yt-dlp — and
  **never** one of YouTube's machine translations (every other
  `automatic_captions` key). The other-language track is the last resort, with
  a loud warning. The same fallback covers the API failing outright (no keys,
  all exhausted). `YT_AUTOCAPTIONS=0` disables it; `YT_DLP_BIN` is the test
  seam. Both calls capture their status with `|| RC=$?` — `transcribe.sh` is
  `set -e`, and a bare `RC=$?` on the next line never runs.
- Both feed one shared writer producing `.txt` + `.srt`.
- `--clip-captions WINDOW` trims a *caption-derived* transcript to a window and
  rebases it, immediately before that shared writer. It is a no-op on the
  AssemblyAI path by design — see the `--clip` section above.
- `--out-base PATH` overrides the timestamped default (see the run model).
- Language default `th`, override via arg or `ASSEMBLYAI_LANGUAGE`.
- `ASSEMBLYAI_BASE_URL` and `YT_TRANSCRIPT_API_URL` exist so
  `lib/test_media_e2e.sh` can run the real clients against local stub servers.
  They are test seams, not features — but they are also the only way to
  exercise these clients without spending money, so don't remove them.

### Kaltura embeds (`lib/kaltura.py`)

Lecture-capture entries pasted out of an LMS, as either the whole `<iframe>`
tag or just its `src`. Both forms are accepted and **both must produce the same
run id** (`kal_<entry id>`), or pasting the tag once and the URL later would
duplicate the run instead of resuming it.

**Not yt-dlp.** yt-dlp ships a Kaltura extractor and it fails on these entries:
it sends no `Referer`, and a university tenant's access-control answers a
referer-less `playManifest` with a bare 404 — `No video formats found!`.
Verified 2026-09-09 against partner 2910381 / entry `1_y9jay9sw`, which
downloads fine through the api_v3 calls this module makes. Don't "simplify"
this back to yt-dlp.

**The `Referer` is the whole trick**, and it is the thing that will break for
someone else's tenant. Measured on that entry: no referer → 404,
`https://example.com/` → 404, the LMS's own domain → 302, and the *Kaltura CDN's
own domain* → 302. The CDN domain is therefore the default, because it needs no
per-institution configuration; `KALTURA_REFERER` overrides it. A 404 during the
download names that variable in the error, because nothing else about the
failure suggests a header.

The sequence: `session.startWidgetSession` for an anonymous KS (what the
embedded player itself does), then `baseEntry.getPlaybackContext` for the
sources — take the progressive `format=url` MP4, not HLS, since both downstream
stages want a file — with the KS appended to the URL, which access-control also
requires. `baseEntry.get` supplies the title, since there is no yt-dlp to ask.

Non-obvious details:

- **`requests` is imported lazily.** `pipeline.sh` runs `kaltura.py parse` on
  every input it classifies; an `ImportError` there would silently reclassify a
  perfectly good embed as "unrecognized input" rather than failing loudly.
  `test_parse_does_not_even_import_requests` holds it.
- **Kaltura reports failures inside a 200 body** (`KalturaAPIException`), so the
  status code proves nothing — same shape as the claude CLI's exit code.
- **Captions are tried before AssemblyAI**, exactly like the YouTube path, and
  "no usable track" is exit code **3**, not 1: `transcribe.sh` reads 3 as
  "fall through to the media file" and anything else as a real failure. Collapse
  them and an outage becomes three silent uploads to AssemblyAI. Only SRT and
  WebVTT assets are used; a DFXP/TTML track is skipped rather than half-parsed,
  because a mangled transcript is worse than paying for a good one.
- **The download writes `<dest>.part` and renames**, and a transfer that ends
  short of `Content-Length` is an error rather than a short file. An
  interrupted download must never look like a finished artifact to the resume
  logic, and a truncated MP4 would otherwise only fail two stages later, in
  ffmpeg.
- **Every request is retried through `summarize/retry.py`**, not a local copy —
  the project's backoff policy (exponential, full jitter, `Retry-After`) is
  documented and shouldn't drift. Transient statuses are re-raised as
  `HTTPError` so `is_retryable` classifies on the status rather than on the
  wording of a `KalturaError`. This was added 2026-09-09 after the first live
  run on the deployment box died on a single 60s read timeout on
  `getPlaybackContext`, seconds after the same host had answered `baseEntry.get`
  fine. It is the one place `lib/` imports from `summarize/`; that import is
  lazy, so the offline `parse` path is unaffected.
- **The `<iframe>` blob never reaches summarize.** `run_one.sh` normalises it to
  a canonical embed URL (`SOURCE_URL`) first; otherwise 900 characters of HTML
  would land in the document's provenance comment and its link line.
  `document.py` prints that as `Video Link:` under a `kaltura` source kind.
- Entry facts are cached in `runs/<id>/kaltura.json` at fetch time, so summarize
  makes no network call of its own and a resume makes none either.

**Measured on the deployment box, 2026-09-09**, on the entry above (1h29m,
1920x1080, no captions), as a live `./verify_e2e.sh --kaltura` run:

| Stage | Wall | Note |
|---|---|---|
| `fetch_video` | ~10s | 446MB, ~45MB/s from the CDN |
| `transcribe` | ~3 min | AssemblyAI, 132,488 Thai chars in **19** cues |
| `frames` | ~25 min | 149 keyframes; slower than the 13x-realtime benchmark because transcribe and summarize were competing for the same 4 vCPU |
| `summarize` | ~9 min | claude-cli/opus, chunked |

Two things that table is worth keeping for: the transcript arrives as 19 cues
for 89 minutes, so this input type leans hard on
`chunking.split_long_segments` (the AssemblyAI-Thai problem documented under
Stage 3), and the 446MB download used to stay in the run dir after the frames
were swept. Since 2026-09-13 it is swept with them (next section); a resume
that needs the frames back re-downloads, ~10s here.

### The download is swept after the summary; the recording never is

Settled with the operator 2026-09-13: the box was filling with
`runs/<id>/video.mp4` from YouTube and Kaltura runs that had long since been
summarized. `sweep_run_media` in `lib/run_one.sh` deletes `video.*`,
`clip.mp4` and any `.part` from the run dir right after `cleanup_frames`,
and a combine run does the same for its members (`cleanup_member_videos`)
after the combined PDF. The rules:

- **Only files inside the run dir.** A meeting's recording is in
  `RECORDINGS_DIR` and a local-file input is the operator's own file; neither
  is ever at these paths, and `sweep_run_media` also returns early on
  `input_type = meeting` so nobody has to trust the path argument alone.
- **Whatever happened to the PDF.** `cleanup_frames` keeps the frames when a
  PDF was asked for and did not render, because re-rendering needs them. It
  never needs the media, so the video goes regardless.
- **`KEEP_FRAMES=1` keeps the video too.** The operator chose one switch
  over a second `KEEP_VIDEO`: it is the "keep what this run used" flag, and
  the video is what the frames come from.
- **The stages are marked `cleaned`**, exactly like frames, so
  `fetch_video` and `clip` stay `done` instead of sliding back to `pending`
  and making a finished run look half-broken in `--status`.
- **`ensure_video_fetched` / `ensure_video_clipped` re-fetch on a swept
  stage.** Both used to return early on `done`; now a `done` stage whose
  file is gone is reset and run again. That is what makes a `--combine
  --force` (which re-extracts the members' swept frames) and a `--force` on
  a finished single run work after the sweep.
- **`media_needed` guards the ahead-of-branches fetch and cut.** The Kaltura
  fetch and the non-YouTube clip run *before* the branches, unconditionally
  until now. With the file gone after every summary, re-invoking a finished
  run with `--run-id` would have pulled 446MB back for a run with nothing to
  do. `frames_settled` (frames done, and either the manifest exists or
  nothing here will summarize — `--skip-summarize`, or summarize already
  done) is the same test `branch_frames` uses to decide whether to
  re-extract; before this it re-extracted (and now would re-download) on a
  finished run too. Both are asserted in `test_pipeline_e2e.sh` ("A finished
  run re-invoked by --run-id neither downloads nor extracts again", and the
  Kaltura twin).
- A failed or paused run keeps its download — the sweep is after
  `mark_done summarize`, same as the frames.

**An entry that needs a real LMS login fails loudly** — `getPlaybackContext`
returns no sources, and the error names the partner and entry id. Browser
recording it is deliberately NOT implemented: the login lives on the LMS page,
not on the iframe src, so opening the embed in the persistent Chrome profile
would hit the same access-control refusal. Settled with the operator
2026-09-09; if it is ever built, the decision taken then was to reuse
`record_screen.sh` as-is (a Kaltura-specific capture driver in place of the
Meet/Zoom join logic), taking a `record` queue slot, and to require the *LMS
page* URL rather than the iframe src.

### Stage 3 — Summarize (`summarize/`)

Split across eight modules:

- `summarize.py` — entry point and orchestration.
- `llm_client.py` — backend dispatch + the fallback chain.
- `language.py` — `SUMMARY_LANGUAGE` and the `{language_rule}` the prompts carry.
- `retry.py` — transient-failure policy (503/429/5xx).
- `chunking.py` — splitting long transcripts, assigning frames to chunks.
- `mapreduce.py` — parallel chunk summarization + the merge call.
- `document.py` — the course-note document wrapper (single and multi-video).
- `pdf.py` + `framecrop.py` + `mathrender.py` — the PDF export, its frame
  cropping and its LaTeX typesetting.

Backends: `claude-cli` (default, aliases `claude`/`anthropic`/`cli`/`fcc`) and
`gemini`. `SUMMARY_BACKEND=fallback` is the default mode and walks
`SUMMARY_FALLBACK_CHAIN` (default `claude-cli,gemini`). **NVIDIA NIM and Ollama
were removed** in the Debian 13 port — neither had been on a configured path,
and both carried env surface and untested code. **The API-key `anthropic`
backend was removed** when the summarizer moved to the subscription; the name
survives only as an alias of `claude-cli`, so an existing `.env` whose chain
reads `anthropic,gemini` keeps working.

### The summarizer spends a subscription, not an API key

`summarize_claude_cli` runs `claude -p` as a subprocess and reads the JSON
envelope back. A Claude Pro/Max subscription has no API key — `api.anthropic.com`
bills a separate console account — and the CLI is the supported way to spend a
subscription non-interactively. That is the whole reason for the subprocess.

The invocation is fixed in one place and asserted in two test suites:

```
claude -p --output-format stream-json --verbose --model <CLAUDE_CLI_MODEL> --effort <SUMMARY_EFFORT>
       --safe-mode --no-session-persistence
       [--append-system-prompt-file <sha>.md --exclude-dynamic-system-prompt-sections]
       --input-format stream-json --tools ""            (frames inline; the default)
     | --tools Read --allowedTools Read --add-dir <dir>  (CLAUDE_CLI_FRAME_INLINE=0)
     | --tools ""                                        (no frames / vision off)
```

- **The prompt goes in on stdin, never in argv.** An 80KB transcript in an
  argument is over `ARG_MAX` on any normal box.
- **`--effort` is where `SUMMARY_EFFORT` lands.** Same scale the Messages API
  spells `output_config.effort`. There is no token-budget knob and no CLI
  spelling for one, which is a more durable fix than remembering not to send
  `budget_tokens`. **`SUMMARY_MAX_TOKENS` is Gemini-only and always was**;
  `_max_tokens()` is called from `summarize_gemini` and nowhere else. The
  operator set it to 100000 in 2026-09 expecting it to bound the
  subscription spend and saw no change, which is what led to the usage
  window section below.
- **`--output-format stream-json --verbose`, not `json`.** The result
  envelope is the same object, arriving as the last line; what the streaming
  format adds is a `rate_limit_event` line carrying the subscription's own
  meters (`rate_limit_info.unifiedWindows.five_hour.{utilization, resetsAt}`)
  and, on an exhausted window, `status: "rejected"` plus the reset time. That
  event is the only machine-readable form of either fact. `--verbose` is
  what print mode requires before it will stream. Verified 2026-09-12 on
  v2.1.263 with two Haiku calls; `parse_cli_output` still accepts a bare
  envelope so the unit tests and an older stub keep working. The cost is
  that the tool results (the frame images the model Reads) are echoed into
  stdout as base64 — a few MB per chunk, held in memory once, discarded.
- **`--model` defaults to the alias `opus`, not a pinned id.** The CLI resolves
  aliases to the current model, so a rename doesn't 404 a box nobody has
  touched in a year. `ANTHROPIC_MODEL` is still read as a fallback name.
- **`--safe-mode` plus a scratch cwd** (`$MEETING_BOT_ROOT/tmp/claude-cli-cwd`).
  Either alone would do it; both are cheap. The CLI auto-discovers `CLAUDE.md`
  from its working directory, and *this* file is 38KB of architecture notes
  that have nothing to do with summarizing a lecture — it would be pulled into
  the context of every single summary. Controlling the cwd doesn't depend on a
  flag name staying put; `--safe-mode` also drops hooks, plugins and MCP
  servers.
- **`--no-session-persistence`**, or every summary leaves a full transcript in
  `~/.claude/projects` on a 15GB disk.

**`CLAUDE_CLI_BIN` should be set, not left to `PATH`.** The CLI installs into
`~/.local/bin`, which root's minimal `.profile` does not add to `PATH` and a
systemd unit does not inherit. `_claude_cli_bin()` returning `None` is not an
error anyone sees: it raises `BackendUnavailable`, the chain advances, and
Gemini answers. Found 2026-09-08 on the deployment box with the variable
commented out in `.env` — 51 summaries had been billed to Gemini keys while
the operator believed the subscription was paying. The symptom is in every
document's provenance header (`model: gemini/...`) and nowhere else.

**The subprocess environment is scrubbed** (`_claude_cli_env`): `ANTHROPIC_API_KEY`,
`ANTHROPIC_API_KEY_1`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are
removed before launch. This is the most important line in the file. If any of
them survives, the CLI switches from subscription auth to API-key billing and
*says nothing* — the summary is identical and the charge lands on an account
the operator thought was unused. An empty `ANTHROPIC_API_KEY` is worse: it fails
auth in a way that reads like a broken subscription. `lib/fake_claude_cli.py`
records what reached the child process precisely so this regression is visible.

**Frames are inlined as image blocks, in one turn** (settled 2026-09-12,
the token-usage pass). `--input-format stream-json` makes stdin a JSON user
message whose content is blocks, and the CLI accepts `image` blocks there
exactly as the Messages API does — verified on v2.1.263 with Haiku
(`build_inline_input`). The prompt text goes first, then for each offered
frame its manifest label and the image, so the number beside the picture is
the number the model cites. `--tools ""`: nothing to open.

The previous delivery — absolute paths in the manifest, `--tools Read
--allowedTools Read --add-dir <frame dir>`, the model Reads each file — is
kept behind `CLAUDE_CLI_FRAME_INLINE=0` for a CLI too old to take stream-json
input, and for nothing else. It was replaced because **each Read is a turn,
and each turn re-sends the whole context**: `usage.iterations` in the
envelope showed it, and on a 12-frame chunk over a ~30k context that is
300k+ cache-read tokens — more than the frames themselves cost. The ledger's
`cache_read_input_tokens` against `input_tokens` is the tell in any old
`state.json`. `CLAUDE_CLI_FRAME_VISION=0` still sends the manifest as text
only, and then the model cites frames it has never seen.

**Frames are filtered before they are offered** (`drop_uninformative`, same
pass). Two kinds cost tokens and teach nothing: blank frames — the PDF already
drops them (`framecrop.is_blank`) but the model was still being shown them,
and the old scene-change pass *preferred* them — and consecutive samples of
a slide that has not changed. (Since 2026-09-30 extraction saves neither;
the pass still matters for older manifests and the safety-net frames.) The repeat test is `framecrop.frame_hash`: a
**texture** hash (per-cell pixel spread, 64x64 cells) over the *slide region*
found by `detect_crop`, so a participant filmstrip beside the slide never
enters it. Measured on synthetic 1920x1080 frames: a moved cursor flips 2 of
4096 bits, a shade change 5, a changed title 58, changed body text 176;
`FRAME_DEDUPE_MAX_DISTANCE` is 16. Only *consecutive* repeats go — a slide
returned to later is a moment the notes may cite — and a frame is never
matched against one from another video of a `--combine` set. Numbers are
untouched; the PDF resolves every citation as before. Two hashes were tried
and rejected first, and the reasons matter if anyone revisits this: a
difference hash encodes only the *sign* of the gradient between neighbours,
so a text line that grew or shrank left it unchanged; an average hash
compares cells against the frame's mean, which the dark chrome drags so low
that every cell on a white slide reads "bright" whether it holds text or not
— it deduplicated two slides with different titles in the live check.
`CLAUDE_CLI_FRAME_DEDUPE=0` offers everything.

The order is fixed: drop blanks and repeats, *then* `thin_frames` to the cap,
*then* crop and downscale. Thinning first would spend the cap on twelve
copies of one slide. `test_dedupe_runs_before_the_cap` holds it.

**The static half of the prompt is a system prompt file, so the prefix can
cache.** Claude's prompt caching keys on a byte-identical prefix; the CLI has
no `cache_control` flag and no caching flag of any kind (checked against
`claude -p --help`, v2.1.259) — caching is automatic, and the only lever we
have is keeping the prefix still. Before this, nothing was still: `mapreduce`
prepends the part label ("Part 2 of 5, 0:12:00-0:24:00") to the *front* of the
template, so three parallel chunks of one lecture shared no prefix at all, and
the CLI's own system prompt carries the date and cwd, which move on their own.

A prompt template may now fence the half that never varies between runs with

```
<!-- static-prompt: begin -->   ... role, instructions, output format, example
<!-- static-prompt: end -->
```

`llm_client.split_static_prompt` lifts that block out, writes it to a
**content-addressed** file (`$MEETING_BOT_ROOT/tmp/claude-cli-prompts/<sha>.md`
— same instructions, same path, same bytes), and passes
`--append-system-prompt-file` plus `--exclude-dynamic-system-prompt-sections`,
which moves the CLI's cwd/env/date/git sections out of the system prompt and
into the first user message. Everything that varies — the chunk label, the
reference material, the transcript, the frame paths — stays in the piped user
turn. Both flags exist but are undocumented in `--help`; they were verified by
invocation (an unknown flag errors immediately, these don't).

The split is **opt-in per prompt file**. All four shipped prompts
(`video`, `meeting`, `lecture`, `tutorial`) carry the markers; a template
without them splits to `(None, itself)` and is sent exactly as written.
`CLAUDE_CLI_STATIC_PROMPT=0` turns the whole thing off for a CLI too old to
know the flags. The markers are stripped in `_render` so they never reach any
model, gemini included.

On a chunked lecture every chunk after the first reuses the whole
instruction set — role, structure, callout vocabulary, rules and (in
`lecture.md`) the course-reference section.

Note the trap this design avoids: if the varying part label ended up inside the
static block, every chunk would write a *different* system prompt file, the
cache would never hit, and **nothing would look wrong** — the summaries would
be identical. `test_a_prepended_chunk_label_stays_dynamic` is what holds it.

**`load_prompt_template` cuts a template at its first `# Input`** and returns
only the tail — unless the static-prompt markers are present, in which case the
file is returned whole. That legacy path is a trap, because the cut lands on
the first *substring* match rather than on a heading:

- The old `prompts/summarize.md` lost its entire role/format/rules section —
  its first match was the real `# Input` at line 46.
- The old `prompts/lecture-claude.md` used to lose its opening role sentence and start
  the prompt with the orphaned word `Data`, because its first match was the
  *"# Input Data"* heading near the top. Adding the static-prompt markers
  fixed that as a side effect: the marker path returns the file whole, so
  "You are an expert academic tutor and note-taker…" now actually reaches the
  model for the first time. Verified 2026-09-08 by diffing what the old split
  produced against the new one.

Prefer the markers over relying on the cut. If you write a new prompt file
without them, check what `load_prompt_template` actually returns.

### Four prompts, no timestamps, callouts (2026-09-29)

Settled with the operator 2026-09-29, after the 09-13 pass had removed frame
citations from the lecture prompts only:

- **Four prompts, one file each, shared by every backend**: `video`
  (the in-code default — talks, news, interviews), `meeting`, `lecture`,
  `tutorial`. The claude/gemini pairs, the `-old` archives, `summarize.md`,
  `summarize-v2.md` and `lecture-reference.md` are gone; the course-reference
  rules are a section of `lecture.md` that the model is told to ignore when
  there is no `<course_reference>` block. **The old names still resolve**
  (`promptnames.PROMPT_ALIASES`: `lecture-*` → lecture, `tutorial-*` →
  tutorial, `meeting-*`/`summarize`/`summarize-v2` → meeting), because
  `.env`, `state.json` of unfinished runs and phone shortcuts carry them.
  `promptnames.py` is stdlib-only so `trigger_server.py` can use it.
- **No timestamps and no frame citations in any prompt** — the tutorial's
  `[mm:ss]` chapter list and the meeting's "Context Timestamp" / visuals
  table went too (the operator: "update ALL the rest of the prompt to follow
  the no-timestamp and frame"). A time that is content (a deadline) stays.
  The chunk preamble tells the model the part label is orientation only, and
  `_merge.md` no longer asks to preserve timestamp citations.
  `PromptSetTest` holds all of it. The PDF's citation fade and
  `PDF_FRAMES=contact|inline` still work for older documents.
- **Callouts**: every prompt asks for `> [!CONCEPT|EXAMPLE|WARNING|IMPORTANT|NOTE] Title`
  blockquotes, which `pdf._extract_callouts` turns into the coloured boxes of
  DESIGN.md (and which GitHub/Obsidian still render). The vocabulary is in
  each prompt's static half. Every formula and maths symbol is asked for
  inside `$…$`; code in language-tagged fences.
- **The `video` prompt gets the wrapper** (link + transcript), like lecture
  and tutorial; `meeting` stays plain. `wants_wrapper` is now called with the
  *resolved* prompt stem, so an alias decides like its target.
- **`--instructions` / `SUMMARY_INSTRUCTIONS`**: the operator's free text for
  one run (the web UI's "Extra instructions"). `inject_instructions` puts it
  right after the static end marker — above the reference material, which
  `inject_resources` put there first — framed as the operator's instructions
  (not data), taking precedence over the default structure but not over the
  language rule or the no-timestamp rule unless it says so. Braces doubled
  for `.format()`. Never in the static half: it varies per run.

### The reality prompt: the one that cites times (2026-09-30)

`--prompt reality`, for competition reality-show episodes (tuned on The
Face; generic wording for Drag Race, MasterChef, Survivor …). The operator
asked for "what happened, highlight timestamps, quotes from the mentors and
contestants, which team won, who is eliminated" and settled, when asked:

- **Timestamps, accurate and clickable.** The deliberate exception to the
  no-timestamp rule, for this prompt only (`promptnames.
  TIMED_TRANSCRIPT_PROMPTS`). Before this the model never saw *when*
  anything was said — it reads the `.txt`; only frame labels carried times,
  one per ~30s on a moving picture. Now `summarize.model_transcript` gives a
  timed prompt `chunking.timed_transcript`: the `.srt` grouped into lines of
  ~`TIMED_LINE_SECONDS` (10), each opening `[mm:ss]` (`[h:mm:ss]` past the
  hour), long ASR segments first cut to 30s so AssemblyAI-Thai still gets
  dense marks. Measured on a 1h54m episode: 73k → 78k chars (+7%), 587
  lines; a mark on every caption cue would have been ~+30%. The marks ride
  inside the segment text, so `build_chunks(..., timed=True)` chunks and
  frame-windows them like any other. The document's `<details>` transcript
  stays the plain `.txt`. No `.srt` → plain text and a warning; the prompt
  then writes no timestamps.
- **The code, not the model, makes the links** (`document.link_timestamps`,
  run in `main`/`main_parts` for timed prompts, before the wrapper). YouTube
  only (`youtube_id`), `watch?v=<id>&t=<s>s`; `--clip` start is added to the
  link, the visible text stays clip-relative (the document's clock). A
  combined set writes `[Video N, mm:ss]`; a bare mark among several videos
  stays text. Code spans/fences and marks already followed by `(` are
  skipped — except a code span holding only a timestamp, which Gemini wrote
  in the live run's Highlights table and is unwrapped and linked.
  Kaltura/recordings: plain text.
- **Live run, 2026-09-30** (Take Hormones Thailand EP.2, 1h54m, Thai auto
  captions, on the operator's Gemini-only chain): 220 frames, 2 chunks +
  the reality merge, 8 segments, 78 linked marks, results last ("no one
  eliminated — a walkout"). Speaker attributions were not checked against
  the video.
- **Speakers from the frames.** The transcript has no names (no
  diarization; YouTube captions never have it). The operator: the names
  "will be stated there" — on-screen name captions — so the prompt ranks
  captions, then names said aloud, then context, and says never guess a
  name. No cast file, no AssemblyAI speaker labels.
- **Results at the end** (the operator's choice over a results box first):
  opening paragraph reveals nothing; sections in episode order with a
  `[mm:ss]` in each heading and an `[!EXAMPLE] Quotes` box per section
  (`* **Name** (role, team) [mm:ss]: "…"`, original language + translation
  when it differs from SUMMARY_LANGUAGE); `## Highlights` table; `## Results`
  last, `[!IMPORTANT]` table (winner, prize, nominees, eliminated, saved,
  who decided); "not shown" rather than a guessed result.
- **Its own merge prompt**, `prompts/_merge-reality.md`
  (`mapreduce.load_merge_template(prompt)` picks `_merge-<stem>.md` when it
  exists; `summarize_chunked(merge_template=)`). The shared `_merge.md` says
  "do not add timestamps" and would scatter partial results; this one keeps
  every mark and quote byte for byte and builds one Highlights and one
  Results section, last.
- `inject_instructions` now says "the prompt's rules on timestamps", not
  "the rule against timestamps". PDF: `KIND_LABELS` "Episode recap"; a link
  in an H2 banner is pale blue (DESIGN.md) — the body blue was unreadable on
  navy.

### The output language is a setting, not a property of the transcript

Settled with the operator 2026-09-15. `SUMMARY_LANGUAGE` (`summarize/language.py`)
is `th` by default and `en` is the switch; anything else raises
`UnknownLanguage`, which `summarize.py main()` turns into `SystemExit` before
either path runs — a typo must fail at second zero, not after the first
chunk was billed. `ASSEMBLYAI_LANGUAGE` is a different question (what the
*audio* is in) and stays `th` independently.

How it reaches the model: all six shipped templates and `_merge.md` carry a
`{language_rule}` placeholder in their numbered rules — the old hard-coded
"Write in English even when…" sentence (lecture/tutorial) and "same language
as the transcript" (meeting) are gone. `language.apply()` fills it in
`load_prompt_template` and `load_merge_template`, so both backends see the
same rule; a template without the placeholder is returned untouched. The
rule for `th` asks for Thai prose with the English term in parentheses on
first use (การแปลงฟูเรียร์ (Fourier transform)) — the operator's choice over
"English terms only" and "everything translated" — and forbids translating
code, LaTeX, commands and on-screen identifiers. `_default_merge_template`
(the in-code fallback for a missing `_merge.md`) carries the placeholder
too.

**The placeholder sits inside the static-prompt block on purpose.** The
setting is per box, not per run, so filling it there changes the
content-addressed system prompt file exactly once when the operator flips
it. `test_the_rule_lands_in_the_cacheable_half` asserts that the two
languages produce different static halves and identical dynamic halves.

Three things deliberately stay English: the wrapper `document.py` builds
(`Youtube Link:`, `View Transcript`, `Clip:`), the PDF's appendix headings
and frame captions, and the provenance keys. The `.md` has to drop into the
operator's existing course files. `build_document` records the code as
`language:` in the provenance comment so `pdf.py` can pick the body face
from the document itself (next section).

`meeting-gemini.md` had no language rule at all before this; it got one
appended as rule 4. The `*-old.md` templates are archives and were not
touched.

**Frames sent to the CLI are cropped to the slide and downscaled; the saved
frames are not.** `framecrop.fit_for_llm` runs `detect_crop` in the PDF's own
`PDF_FRAME_CROP` mode (one knob, deliberately: the model looks at the picture
the PDF prints, and the detector's decline-rather-than-guess rule protects
both) and then fits the result to `FRAME_MAX_DIMENSION` (default **768** since
2026-09-12, was 1024) on the long edge. Cropping first is what makes 768
enough: on a Meet recording the slide is maybe two thirds of the frame, and
the dark chrome was paying for pixels that said nothing. Live check with
Haiku: 60px slide titles read correctly off the 768px cropped copy. A whole
1920x1080 frame is ~1,844 tokens, ~790 at 1024px, ~440 at 768px, and the crop
takes it lower still. The copy goes to `<frame dir>/llm-<px>-<mode>/<same
name>.jpg` — the mode is in the directory name so flipping `PDF_FRAME_CROP`
doesn't reuse copies cut the old way — and is reused on the next run. The
originals never move, because `pdf.py` crops and embeds them and needs the
resolution — `_downscale_frames` builds new `FrameMeta` objects with
`dataclasses.replace` rather than touching the ones `summarize.py` passes on to
the PDF. Pillow is optional: without it the originals are sent with one
warning. The frame-extraction settings are untouched — this changes
resolution, never which frames exist.

**The chunk label is appended, not prepended, and the reference material sits
at the top of the dynamic half.** Both in the same pass. Caching keys on a
prefix, and the label is the one thing that differs between the chunks of a
run; in front of everything it meant the `--resources` block behind it —
identical on every chunk, up to `RESOURCE_MAX_CHARS` = 40k characters — never
cached. Now `inject_resources` inserts the block right after the static-prompt
end marker (a template without markers is appended to, as before, so its
instructions still come first) and `mapreduce` puts the label after the frame
manifest. `PromptOrderForCachingTest` holds both. In the inline-image message
the text block comes before the images for the same reason: the images differ
on every chunk.

**The merge call may run on a cheaper model.** `mapreduce` passes
`role="merge"` through `summarize` → `summarize_with_fallback` → the backend;
`summarize_claude_cli` swaps in `CLAUDE_CLI_MERGE_MODEL` / `SUMMARY_MERGE_EFFORT`
for that call only (default: the chunk's model and effort, so an existing
`.env` changes nothing). The merge reads every partial and — by `_merge.md`'s
own rule, "do not compress" — writes them all out again, so its output is
about the size of everything the chunks produced; output tokens are the
expensive kind, and this was the single most expensive call of a chunked run.
The chunk summaries, where the reading of noisy Thai ASR happens, stay on
Opus. `.env.example` recommends `sonnet` / `low` for the merge and
`SUMMARY_CHUNK_CHARS=60000`, so most lectures under ~90 minutes are one call
and have no merge at all; the code defaults (24000, same model) are
unchanged. `gemini` accepts and ignores `role`.

### The usage window: metered, waited for, never handed to Gemini

Settled with the operator 2026-09-12. A Pro/Max subscription has a rolling
5-hour window and a 7-day one; before this, running out looked like *"claude
CLI is not logged in"* (the CLI's own error classifier files "usage limit
reached" beside the auth errors) and the chain quietly handed the summary to
Gemini. Four decisions:

1. **Every call's usage is recorded, with the meter.** `UsageLedger`
   (`llm_client.USAGE`) takes the envelope's `usage` (input, output, cache
   read/creation, thinking), `total_cost_usd` (list price — a decent proxy
   for how much of the window a call took), and the `rate_limit_event`'s
   `unifiedWindows`. `summarize.py` writes `USAGE.summary()` to
   `stages.summarize.usage` in `state.json` on *every* exit path — the calls
   that completed before a failure were spent too. `utilization_delta` on
   `five_hour` is "what fraction of the window this stage cost", which is
   the number the operator asked for and the only honest way to size a
   lecture to a plan. Per-call lines go to the stage log.
2. **A hit window waits in-process, on Claude.** `_rate_limit_from` turns a
   rejected event (or `api_error_status` 429, or the legacy
   `usage limit reached|<epoch>` wording) into `ClaudeCliRateLimited`, which
   is `retryable = False` — it must not burn the backoff schedule — and
   which `summarize_with_fallback` re-raises instead of advancing to Gemini.
   `summarize_claude_cli` loops: `_wait_for_window` sleeps until the reset
   the CLI named plus `RATE_LIMIT_MARGIN_SECONDS` (60), or polls every
   `CLAUDE_CLI_RATE_LIMIT_POLL_SECONDS` when no time was given, bounded in
   total by `CLAUDE_CLI_MAX_WAIT_SECONDS` (6h — a full 5h window plus margin;
   anything longer is the weekly limit and no stage should sit through
   that). Sleeps go through `llm_client._sleep` so the tests can patch the
   clock. `WAIT_HOOK` mirrors the wait into `stages.summarize.waiting_until`
   so `--status` can distinguish "waiting" from "hung".
3. **Past the cap, the stage pauses rather than merging around the hole.**
   `mapreduce` re-raises a chunk error carrying `pause_run = True` instead
   of writing "*(this part could not be summarized)*" and marking the stage
   done — nothing would ever fill that gap. `summarize.py` exits **75**
   (`EX_TEMPFAIL`) with `stages.summarize.rate_limited = {window, resets_at,
   resets_at_iso}` annotated; `run_one.sh` marks the stage failed as usual
   and passes 75 up; `pipeline.sh` reports `PAUSED` (not `FAIL`) and exits
   75. `runstate.start` and `done` clear the field — a stale reset time
   would make the next point skip a run that could go.
4. **`--resume-all` is the resume, and a timer is its backstop.** It skips a
   run whose `rate_limited.resets_at` is still in the future (and one whose
   `run.lock` owner is alive), so firing it every 15 minutes never retries
   into the same wall. On the PC that is pm2's `meeting-bot-resume`
   (`ecosystem.config.js`, `cron_restart */15`, only while `./webui.sh on`);
   the VM's systemd `meeting-bot-resume.{service,timer}` (with
   `OnBootSec=5min` for a reboot mid-wait) are on `debian13-in-proxmox`. The
   in-process wait is the primary path; the timer exists for the process
   being gone. With pm2 off at boot, a reboot mid-wait needs `./webui.sh on`
   or a manual `./pipeline.sh --resume-all`.

`CLAUDE_CLI_MAX_FRAMES` came out of the same conversation: frames are the
bulk of a call's input, so `thin_frames` caps what the model is *offered* —
scene changes first, periodic frames at an even stride so the cap still
covers the whole window. It runs after the blank/duplicate pass, so the cap
counts distinct slides. Numbers are untouched (they were assigned over the
whole manifest), so the PDF resolves whatever the model cites. Code default 0
(= off): turning it on by default would silently change every existing run's
summaries. **The recommended Pro-plan values live in `.env.example` instead**
(settled with the operator 2026-09-12): `CLAUDE_CLI_MAX_FRAMES=20` (raised
from 12 the same day — with duplicates gone and each frame a third of its old
cost, 20 distinct slides per chunk is cheaper than 12 samples were),
`FRAME_PERIOD_SECONDS=60` (retired 2026-09-30 with the periodic pass),
`SUMMARY_EFFORT=medium`, `SUMMARY_MAX_PARALLEL=1`,
`SUMMARY_CHUNK_CHARS=60000`, `CLAUDE_CLI_MERGE_MODEL=sonnet`,
`SUMMARY_MERGE_EFFORT=low`. The README's table still lists the code defaults;
the two differing on purpose is documented there.

The fixed overhead the CLI itself adds was measured while sizing this
(v2.1.263, Haiku, trivial prompt): ~4.9k tokens of system prompt with the
Read tool, ~3.5k with `--tools ""`, cached as `ephemeral_1h` automatically.
Small next to a chunk; not a lever worth chasing.

markitdown (microsoft/markitdown) was evaluated the same day as a way to cut
resource tokens and **rejected**: it converts to Markdown, which carries more
markup than the words-only extraction `resources.py` already does, and its
image path *spends* an LLM call per picture. Its real benefit is structure
(tables, headings) for slides; the operator chose not to take on pdfminer +
python-pptx + mammoth + magika for that. Don't re-propose it as a token saver.

**The CLI exits 0 when it is not logged in.** The only signal is the body:
`is_error: true` with `result: "Not logged in · Please run /login"`. So
`_run_claude_cli` parses the JSON rather than trusting the status, and maps
auth wording to `BackendUnavailable` so the chain advances to Gemini
immediately. The marker list has to keep up with the CLI's wording: an expired
OAuth session says *"Failed to authenticate: OAuth session expired and could
not be refreshed"*, which matched none of the original markers and so burned
the full retry schedule before falling through. `failed to authenticate` and
`oauth session expired` were added 2026-09-08 after seeing it live. Everything else becomes `ClaudeCliError` carrying the CLI's own
words, because the CLI has no HTTP status and `retry.py` classifies on wording.

**Gemini key rotation happens outside `with_retries`.** retry.py handles "the
provider is busy"; the rotation loop handles "this key is exhausted or
revoked". Inside the retry wrapper, a dead key would burn the full backoff
schedule before the chain ever advanced.

**`GEMINI_MODEL` is a chain, walked keys-first** (settled with the operator
2026-09-13). `gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash` means: try
every key on 3.8, and only when all of them are out move to 3.7. Quota is
per key *and* per model, so a rate-limited key says nothing about its
neighbours, and the operator wanted the keys exhausted before a weaker model
is used. Two exceptions carry `retryable = False` so `with_retries` raises
them at once: `GeminiQuotaExhausted` (429 / `RESOURCE_EXHAUSTED`) → next key,
no backoff — a 429 used to sit through the whole schedule first; and
`GeminiModelUnavailable` (404 / "not found" / "not supported for
generateContent") → next model on the first key, since the name is dead for
all of them. The "model" test is deliberately narrow: an *unsupported
request* (a mime type) is a 400 and stays a real error. A 503 still gets the
backoff. `_record_used` names the model that actually answered, so the
provenance header says which link of the chain paid. `GeminiModelChainTest`
drives all of it through a fake `google.genai`.

**`BackendUnavailable` carries `retryable = False`, and `retry.is_retryable`
honours that before every other check.** It is raised from *inside*
`with_retries` (the CLI only reveals "not logged in" once it has run), and
`is_retryable`'s type-name heuristic reads the "Unavailable" in the class name
as a busy server. Without the opt-out, a signed-out CLI sat through the full
backoff schedule before the chain ever reached Gemini. Caught by
`test_not_logged_in_is_never_retried`.

**Retry policy.** Every backend's network call goes through
`retry.with_retries`. Retryable: 408/409/425/429/500/502/503/504, connection and
timeout errors, and provider wording like "overloaded" / "UNAVAILABLE" /
"server is busy" that some SDKs raise without a usable status code. Not
retryable: 400/401/403/404/405/422 — a bad key fails identically forever, and
retrying only delays the fallback to a backend that would have worked. Backoff
is exponential with **full** jitter (not ±10%) specifically because parallel
chunk requests must not retry in lockstep against the server that just said it
was overloaded. `Retry-After` wins when present and ≤ 300s; a longer one means
give up and let the chain advance.

**Segment granularity.** A segment is the atom for both chunk boundaries and
frame windows, so `chunking.split_long_segments` cuts any segment over
`SUMMARY_SEGMENT_MAX_SECONDS` (120) or `SUMMARY_SEGMENT_MAX_CHARS` (2000) into
pieces with linearly interpolated timestamps, before chunking. Segments under
the caps are returned untouched, so a normal transcript behaves exactly as it
did.

This exists because **AssemblyAI returns almost no sentence boundaries for
Thai**: Week01, a 2.6-hour lecture, came back as *three* cues, the first a
single 29-minute "sentence". A segment can't be split by `chunk_by_segments`,
so chunks came out at 60k and 70k characters against a 40k limit, chunk 1's
window ran 0-6966s while chunk 0's was 0-1782s, and every frame in a half-hour
had an equal claim on every sentence in it. After the split: 3 segments become
79, the longest drops from 5184s to 127s, chunks land at 39.6k/39.3k/8.7k, and
the windows are sequential. Interpolating on character offset assumes an even
speaking rate — wrong in detail, and enormously closer than one 29-minute atom.

**Chunking.** Above `SUMMARY_CHUNK_CHARS` (24000), the transcript is split and
chunks are summarized concurrently, then merged by one more LLM call using
`prompts/_merge.md`. Chunking prefers the `.srt` sibling of the `.txt`, because
that is the only place segment timestamps live — and timestamps are what let
each chunk carry the frames that were on screen while those words were spoken.
Without an `.srt` it falls back to splitting text and dividing frames
proportionally. A chunk that fails does not discard the others: the merge
proceeds over what succeeded and the document says which parts are missing.

**Frames.** `screen/extract_frames.py` saves a frame each time the picture
changes and settles (see "Frames on change"), into
`$FRAMES_DIR/<run_id>/manifest.json`.
The pipeline runs this as its own stage and passes `--frames-manifest` to
`summarize.py`.

**Reference material** (`lib/resources.py`). `--resources` takes a GitHub URL
(optionally `@branch`, or a `/tree/<branch>/<subdir>` URL pasted from the
browser) or a local file/folder. Text from `.md/.txt/.pdf/.pptx/.docx` is
appended to the prompt template, capped at `RESOURCE_MAX_CHARS`; slide images
(pdftoppm, LibreOffice for pptx) go into the PDF's Appendix B.

Two non-obvious details:
- **Braces in the material are doubled before injection.** The prompt template
  is later run through `str.format()` for `{transcript}` and
  `{frame_manifest}`; an unescaped `{x}` in someone's slides raises KeyError
  and takes down a run that had already paid for transcription.
- **A missing *local* path is fatal; an unreachable GitHub repo is not.** A bad
  local path is always a typo, and finding out after paying for a summary is
  worse than failing in the first second. A repo that won't clone only degrades
  the summary, so it becomes a note.
- OOXML text is extracted with `zipfile` + a regex, not python-pptx/python-docx:
  we want the words, not the layout, and that is two fewer dependencies.
- **Checked in `pipeline.sh` before anything is paid for** (`resources.py
  check`, offline; GitHub specs are only parsed). A missing local path, or a
  text-named file whose bytes are `%PDF`, `PK\x03\x04` or contain a NUL,
  fails at second zero ("this looks like a binary document; convert it to
  Markdown first"). A real `.pdf` passes. `--from-file` gets the same binary
  check (`resources.py is-binary`) — the operator lost an hour to a PDF read as
  a line list.

#### Frontmatter: the course reference (`--resources` + the `lecture` prompt)

Settled 2026-09-27, replacing the 09-26 `--context` spec: **frontmatter on
`--resources`, not a second flag** (one channel, one state field, one prompt
block), and **the block sits after the static prompt, verified through the
usage ledger** — no API-key backend. A `.md` whose first line is `---` may
carry `course`, `source`, `citation_label`, `coverage` (hand-parsed, flat
`key: value`, quotes and `# comments`; unknown keys ignored; malformed lines
reported, never fatal; a missing `course` falls back to the file stem,
`citation_label` to `course`). That file's text is wrapped in
`<course_reference course=… source=… citation_label=… coverage=…
lecture_language=…>` — `lecture_language` from `MEETING_BOT_LANGUAGE`, which
`run_one.sh` exports from the run's state, so no language pair is hardcoded.

**The metadata is never substituted into the prompt's instructions.** The
spec asked for template slots; that would give every course its own static
system-prompt file, so the cache would never be shared — and it would put
per-run data in the static block, which the rule below forbids. So the
"Using the course reference" section of `prompts/lecture.md` (static half)
refers to "the block's `citation_label`" generically; since 2026-09-29 it is
part of the one lecture prompt rather than a separate `lecture-reference.md`,
and book-only additions go in a `[!NOTE] From <citation_label>` box. A file
without frontmatter produces byte-identical output to before
(`test_a_reference_without_frontmatter_is_unchanged`).


## Output document format

`document.py` builds the wrapper **in code**, not via the prompt:

```
<!-- meeting-transcriber ... source / model / prompt / run_id / generated -->
# <the model's own title; the video's from yt-dlp only if the body has none>
Youtube Link: `<url>`
<details><summary> View Transcript </summary>  ...4-space indented...  </details>
<br>
...the rest of the model's body...
<br><br>
```

Shaped to match the user's course files (`2_Transcripts/chapter1.md`,
`chapter2.md`) so output drops straight in. Decisions behind it:

- **Code builds the wrapper, the model writes only the body.** The link,
  transcript and provenance can then never be hallucinated or truncated, and an
  ~80KB transcript doesn't round-trip through the model just to be echoed back.
- **A recorded call's link is a `Meeting Link:`** (`document.looks_like_meeting`:
  Meet, Zoom, Teams), `source_type: meeting`. It used to fall through to
  `Source File:` — seen on the first live guest recording, 2026-09-29.
- **Provenance is an HTML comment**, so it survives being pasted into a bigger
  chapter file without adding visual noise. Values are escaped so a `-->` in a
  source can't terminate the comment early.
- **The heading is the model's** (settled 2026-09-13). The lecture and
  tutorial prompts ask for a `# Title` (they used to forbid one — that line
  was flipped the same day, or the wrapper's H1 would always have been the
  video's); `split_leading_heading` lifts it above the link lines, and
  the video title is only the fallback for a body without one — "Signals and
  Transformations" names the material where "2110203 L01" names the file.
  Until then the wrapper put a `Chapter N — <topic> (<date>)` placeholder and
  the video title above the body; both went at the operator's request.
  `pdf._drop_legacy_header` strips them from the older files it re-renders
  (the placeholder always; the video-title H1 only when a second H1 follows
  the link lines, in which case that one moves up to head the page), so a
  document with a single heading is never touched.
- **The 4-space indent inside `<details>` is deliberate**, reproducing what the
  existing chapter files do (most renderers show it as a code block). Don't
  "fix" it.
- Applies to the `lecture`, `tutorial`, `video` and `reality` prompts
  (`document.wants_wrapper`, on the resolved name); `meeting` keeps the plain
  executive format. Override with `--format always|never`.
- `--combine` produces one such document for several videos: one title, one
  link line per video tagged `(Video N)`, one transcript block, one body. See
  the `--combine` section below.

### `--combine`: several videos summarized as ONE (`--combine-pdf`, `--no-combine-pdf`)

Reworked 2026-09-11 with the operator. Until then `--combine` summarized every
input on its own and stapled the `.md` files together, renumbering frame
citations per section so the combined PDF's pictures lined up. That whole
mechanism — `document.combine_documents`, `shift_frame_citations`,
`pdf.merge_manifests` and the offsets they passed around — is **gone**. Don't
bring it back as a "concat mode": the operator chose to replace it, not to
keep both.

What it does now: the members run transcribe + frames and stop
(`run_one.sh --skip-summarize`), and a **combine run** — `input_type:
combine`, run id `combine_<n>x_<sha1[:10] of the member ids>_<time>` — runs
one summarize stage over all of them via `summarize.py --parts parts.json`.
The model reads every transcript, in input order, and writes one body.
Decisions, each settled explicitly:

1. **One summary, not a merge of per-video summaries.** N+1 calls would have
   been cheaper to build and weaker across video boundaries; one summarize
   spend is also the point — the members deliberately get **no individual
   summary** (their `summarize` stays `pending`, and `combined_into` in their
   state says why).
2. **Per-video clocks, never a running total.** The transcript the model
   reads is fenced per video (`chunking.part_transcript`:
   `=== video 2 of 3: <title> ===`), every frame label carries its video
   (`[frame 12 @ video 2 410.0s (periodic)]` — `FrameMeta.part`), chunk
   headers name the video, the PDF caption reads `Frame 12 — Video 2, 6:50`,
   and the document says under its links that timestamps are relative to the
   video they cite. A continuous clock would have meant an offset in four
   modules and a silent failure wherever one was missed — the same reasoning
   as `--clip`'s relative timestamps, one level up.
3. **Frame numbers are global across the set, assigned once.**
   `pdf.load_part_manifests` tags each manifest's frames with its part and
   runs `assign_numbers` over the lot, which now sorts by
   `FrameMeta.sort_key = (part, timestamp_s)`. A video with no manifest still
   consumes a part number. This is the multi-video form of "Frame numbers are
   global, and assigned exactly once" below, and the failure mode is the same
   silent one.
4. **Each video is chunked on its own** (`chunking.build_part_chunks`): a
   chunk never spans two videos, because its time window and its frames
   belong to one clock, and a short video is never folded into a neighbour's
   chunk because the chunk label is what the model cites frames against. If
   the whole set fits under `SUMMARY_CHUNK_CHARS`, it goes in one call with
   every frame — that is the case where cross-video context actually helps.
5. **The combine run is a real run**, resumable through the same
   `runstate find --input` auto-resume as everything else: its `input` is
   `combine:<id1>+<id2>+…` (`lib/combine.py run-key`), so the same members in
   the same order land on the same run. Its `members`, `output_md` and
   `output_pdf` live in `state.json` and `init` refreshes them on every
   invocation, so re-running with a different `--combine` path writes there.
6. **Flat wrapper**: one title (the first video's, or `--title`), one link
   line per video tagged `(Video N)`, one `<details>` block holding the fenced
   transcript, one body. `document.build_document(videos=[...])`.

Non-obvious details:

- **`run_one.sh` handles the combine run before the per-input DAG** (the
  `INPUT_TYPE = combine` branch). Before summarizing it checks every member
  has its transcript and manifest on disk; a member whose frames were swept
  (`done` with no artifacts — the `cleaned` state) gets `rs reset --stage
  frames` and is re-run with `--skip-summarize`. That is what makes
  `--run-id combine_… --force` work after the sweep, and it is why
  `parts.json` is rebuilt on every attempt rather than cached.
- **The frame sweep belongs to the combine run** (`cleanup_member_frames`),
  after its PDF, under the same rules as a single run. `pipeline.sh` no
  longer exports `KEEP_FRAMES=1` to the children or sweeps anything — members
  never reach `cleanup_frames` under `--skip-summarize`.
- **`--resume-all` skips members** (anything with `combined_into` set) and
  resumes the combine run instead. Without that it would bill an individual
  summary nobody asked for. `--run-id <member>` still does run one to the
  end — that is an explicit ask.
- **`--combine` with `--run-id`/`--resume-last`/`--resume-all` is refused**;
  the combine run is resolved from the inputs.
- **A failed member leaves the combine run untouched** — no summarize is
  attempted, exit 1, and the same command resumes both.
- **`summarize.py --parts` looks up YouTube titles itself** (one yt-dlp call
  per video, best-effort) because the labels the model reads need them;
  Kaltura titles come from each member's `kaltura.json` through
  `combine.py parts`. The `<iframe>` blob is normalised there too.
- **`pdf.py`'s CLI takes repeated `--frames-manifest`** to re-render a
  combined document: the Nth is video N's, `-` holds an empty slot.

### The PDF (`summarize/pdf.py`)

WeasyPrint, markdown→HTML→PDF. Chosen over headless Chrome (which would couple
stage 3 to the browser half) and over pandoc/LaTeX (a gigabyte of texlive, and
Thai in LaTeX is genuinely painful).

Reworked 2026-09-13 after the operator read a real 71-page combined sheet
(six lectures, Gemini backend). Settled then, each with a reason:

- **The sheet is the summary alone.** `PDF_FRAMES=none`,
  `PDF_TRANSCRIPT=none` and the new `PDF_RESOURCES=none` are the code
  defaults; the 71 pages were 19 of notes and 52 of keyframe thumbnails,
  reference slides and white-on-white transcript. The `.md` keeps its
  `<details>` transcript — that was an explicit condition. Every appendix is
  still there on request (`contact`/`inline`, `hidden`/`appendix`,
  `appendix`), and `test_media_e2e.sh` exports `PDF_FRAMES=contact` because
  it asserts on frames being cropped and embedded.
- **The body face follows the document's language** (2026-09-15;
  `DEFAULT_FONT_STACKS` in `pdf.py`). English: CMU Serif (`fonts-cmu`) —
  real Computer Modern, the face mathtext already sets the maths in, so
  `PDF_MATH_SCALE` is 1.0 (the 1.15 nudge was for a sans body's taller
  x-height). The old stack named Adwaita Sans and Arial, and neither is
  installable on this box — `fonts-adwaita*` is not in Debian 13's archive
  and Arial needs the contrib `ttf-mscorefonts-installer` — so every PDF had
  silently been Liberation Sans. The operator chose CMU Serif over CMU Sans
  and Arial. Thai: **Bai Jamjuree, then Sarabun** — the operator's order —
  leading the stack so Latin words inside Thai sentences stay in the same
  face. Neither is in Debian's archive (checked: only TLWG, Noto and
  Arundina are), so four styles of each are **vendored under `fonts/`**
  with their OFL licences and `setup.sh` installs them into
  `/usr/local/share/fonts/meeting-bot/` + `fc-cache`; no network needed at
  setup time. The maths is unaffected: mathtext renders to SVG paths, so
  `pdffonts` on a Thai sheet lists Bai Jamjuree and CMU Typewriter (code)
  and nothing for the maths, which is correct. Noto Serif Thai stays in
  both stacks as the last-resort Thai face.
  `_document_language()` reads the provenance's `language:` first, then
  `SUMMARY_LANGUAGE`, then the default, and never raises — a re-render of a
  Thai sheet on a box since switched to English keeps its face, and a typo in
  the variable is summarize.py's to report. `PDF_FONT_FAMILY`, when set,
  applies to both languages; **`.env.example` no longer sets it** (it used
  to pin the CMU stack, which would have silently overridden the Thai
  face — the operator's live `.env` had the same line and it was commented
  out the same day).
- **Nested bullets are re-indented before conversion**
  (`_normalize_list_indent`). Gemini indents sub-items two spaces, Claude
  often does; python-markdown nests only at four and folds anything less
  into the parent item, which flattened every outline. Levels are read from
  the indents seen so far and rewritten to four a level; continuation lines
  (a display formula under a bullet) follow their item; fenced code is left
  alone. It runs *after* `mathrender.extract`, so a multi-line `$$` block is
  one token by then and can't be cut mid-matrix. The same pass inserts the
  blank line python-markdown needs between a paragraph and a list that
  follows it directly — CommonMark and the model both consider that a list.
- **Frame citations are faded, not removed** (`.cite { opacity: 0.3 }`,
  `_fade_citations` on the rendered HTML — "70% transparent" was the
  operator's spec). `CITATION_RE` covers every spelling seen in real output:
  `(Video 1, Frame 52 @ 0:08:52)`, `(Frame 280 @ Video 1 [02:21:00])`,
  `(Frames 20–26 @ …)` and the frameless `(Video 6, [02:51:30])`.
- **The provenance line and the source go under the title**, not over it.
- **Environments are composed in `mathrender`** — see the LaTeX section.

#### The design and the per-run font (2026-09-29)

Settled with the operator 2026-09-29, with their exercise sheet
(`Signal_Exercise_2110203`, headless-Chrome + KaTeX) as the style guide —
**for styling only**, not its content structure. `DESIGN.md` is the spec;
the decisions behind it:

- **Title block + navy H2 banners + colour-coded callouts + tinted table
  headers**, for all four prompt types. The wrapper's link lines
  (`Youtube Link: …`, `Clip: …`) are lifted out of the body
  (`_take_link_lines`) and printed grey under the title; model/prompt/run/
  font go to a small colophon at the end instead of a line under the title.
  `---` is not drawn (the banners separate sections).
- **Callouts before maths.** `_extract_callouts` runs before
  `mathrender.extract`: a `$$` block inside a quote has `> ` on every line
  until the quote is gone. Top-level blockquotes only (≤3 spaces), fenced
  code skipped; the result is `<div class="callout …" markdown="1">`, which
  needs the `md_in_html` extension. A plain quote is an untitled grey box.
- **Maths is always Computer Modern**: mathtext SVG as before, and
  `_wrap_math_symbols` puts any ω/≤/⇒/²/ℝ typed into the prose into
  `<span class="msym">` (CMU Serif), skipping `pre`/`code` and tag
  attributes. It runs before `mathrender.restore` so it never scans a
  data: URI.
- **Code is JetBrains Mono on a dark "editor window"** — Pygments `one-dark`
  via codehilite (Pygments is now a pinned dependency), a window bar with the
  fence's language (`_fence_languages`, matched to the blocks by position;
  labels dropped if the counts disagree), inline code as a dark chip. Font
  from `fonts-jetbrains-mono` (setup.sh `--system`) or the operator's
  JetBrainsMono Nerd Font in `~/.local`.
- **The body font is a per-run choice from a fixed list** (`fontchoice.py`):
  Thai — Bai Jamjuree, Sarabun; English — CMU Serif, Sarabun, Bai Jamjuree.
  Defaults `PDF_FONT_TH` / `PDF_FONT_EN`. Precedence: the document's
  provenance `font:` > `PDF_FONT` (run_one.sh, from state) > `PDF_FONT_FAMILY`
  (legacy whole stack, only when nothing chose a font) > `PDF_FONT_<LANG>` >
  built-in. An invalid choice at render time warns and falls back — the PDF
  never fails a run; `pipeline.sh` refuses it up front instead.
- **"The font size should be similar to all three" = x-height matching.**
  `PDF_FONT_SIZE` (now **9.5**, the sheet's size) is the size *as Computer
  Modern*; a face is set at `nominal × 0.431 / its x-height` (measured from
  the font files: CMU 0.431, Sarabun 0.500, Bai Jamjuree 0.499, JetBrains
  Mono 0.550), so Sarabun is 8.19pt. Maths stays at the nominal size and so
  matches whatever face the text is in. `.msym`/`.math-fallback` are
  `1/factor` em for the same reason.
- **Summary language, font and instructions are per run** — pipeline.sh
  `--summary-language`, `--pdf-font`, `--instructions`, validated before the
  background detach and before anything is paid for; stored in state.json
  (`summary_language`, `pdf_font`, `instructions`); replayed by run_one.sh as
  `SUMMARY_LANGUAGE`, `PDF_FONT` and `--instructions`. **On a resume,
  explicitly given ones replace the stored ones** (the summary has not been
  written yet); `rs init` now blanks `resources` only when it names `--input`,
  so that update can't wipe them. The web UI has all three on both tabs; the
  font list follows the chosen language.

The 2026-09-08 rework (after a real 39-page output) still stands underneath:

- **Keyframes are an appendix, not illustrations.** `PDF_FRAMES=contact`
  leaves every citation as the model wrote it and puts the frames it names
  into a thumbnail contact sheet in Appendix A. Inline figures were the
  export's worst feature: a keyframe is a screenshot of a video call, so most
  of them are a face, a half-drawn slide, or solid black — and the
  scene-change pass is *drawn to* the black ones, because black-to-content is
  the largest scene change in the video. `inline` restores the old behaviour.
- **Only cited frames are cropped, and blank ones are dropped.**
  `_cited_frame_numbers` scans the rendered HTML with a looser regex than
  `FRAME_CITE_RE` so the second and third number of a compound citation
  ("Frame 33 @ ..., Frame 15 @ ...") count too, and `_prepare_frames` takes a
  `wanted` set. A three-hour manifest is hundreds of frames and cropping is
  the expensive part of this file; this made a real render 34s instead of
  minutes. `framecrop.is_blank` is the black-frame filter.
- **The transcript, when asked for, is an invisible layer, not an appendix.**
  `PDF_TRANSCRIPT=hidden`: white, 1pt, between `BEGIN_TRANSCRIPT` and
  `END_TRANSCRIPT` markers, in normal flow — *not* `display: none`, which
  would put nothing in the PDF at all. The reader never sees it; `pdftotext`
  always finds it.
  **It is cut into 40,000-character pieces on purpose.** Poppler silently
  stops returning text after roughly 50,000 characters on a single page:
  measured here, one 85k-character block came back 60% complete from
  `pdftotext` while pypdf read all of it off the same page. Since this repo's
  own `resources.py` shells out to `pdftotext`, a silent 40% loss was not an
  option. The cost is a couple of blank-looking pages at the back.
  `PDF_TRANSCRIPT=appendix` prints it as Appendix C.
- **Every size is an `em` of the body.** `PDF_FONT_SIZE` therefore rescales
  headings, tables, captions and code together. (8pt until 2026-09-29, 9.5pt
  since — see the design section below.) **A Thai face must stay in any
  custom stack** — Computer Modern has no Thai glyphs, and a Thai lecture
  then renders as tofu.

The older decisions still hold:

- **In `inline` mode, the first citation of each frame becomes the image** and
  later ones stay text — a lecture that refers back to one diagram eight times
  should not print it eight times.
- **Figures are hoisted out of the block they were cited in** (`_end_of_block`)
  so a `<figure>` never lands inside a `<p>`, `<td>` or `<li>`, which produces
  invalid nesting and wrecks table layout.
- **A PDF failure is a warning, not a failed stage.** The markdown is already
  written by then and is what everything downstream depends on. Turning off
  *both* outputs is an error rather than a run that writes nothing.
- `run_one.sh` records whichever of the two files actually exists as the
  stage's artifacts — see the `--no-pdf` / `--no-markdown` paths.
- **`render()` owns the crop scratch directory unless the caller names one**,
  and everything after the `mkdir` runs inside the `try` whose `finally`
  deletes it. `summarize.py` must *not* pass `work_dir`: it used to, which
  made every run leave a `.frames` tree of cropped intermediates beside the
  deliverable — re-uploaded on every run of a synced `PDF_DIR`, and read by
  nothing, because WeasyPrint copies the image bytes into the PDF itself.
  `test_media_e2e.sh` asserts both halves of this.

### Frame numbers are global, and assigned exactly once

`llm_client.assign_numbers()` numbers every frame by its position in the whole
recording, and `summarize.load_manifest()` is the only caller — because it is
the only place that sees the entire manifest. Everything downstream gets
slices.

This is not a style preference. `_render()` numbers whatever list it is handed
and is called **once per chunk**, so before the fix chunk 3's fifth frame was
announced to the model as "frame 5" and so was chunk 1's. The model cited what
it was shown, faithfully; `pdf.py` numbers across the whole recording, so it
resolved half the citations to a picture of a completely different moment —
which is what the operator saw as "the image is completely unrelated to the
content". The tell, in a finished summary, is the same frame number carrying
two timestamps:

```
$ grep -o 'Frame 4 @ [0-9:]*' Week01.md
Frame 4 @ 0:03:39      <- chunk 1's fourth frame
Frame 4 @ 1:31:24      <- chunk 2's fourth frame
```

Found 2026-09-08 in `Week01_20260907_230838.md`: 10 of 40 cited numbers named
two different moments. `_render` still falls back to the position in its list
when a frame has no number, which is only correct when that list is the whole
manifest — that fallback exists for direct callers and the unit tests, not for
the pipeline.

### LaTeX in the PDF (`summarize/mathrender.py`)

The model writes maths; markdown renderers show it and WeasyPrint printed the
backslashes, because it has no JavaScript engine (so no KaTeX/MathJax) and no
MathML support. matplotlib's `mathtext` closes that gap: a self-contained
LaTeX-subset typesetter that ships **Computer Modern** (`fontset = "cm"`),
needs no TeX installation, and renders to SVG which WeasyPrint embeds happily.
That is the whole reason matplotlib is in `requirements.in`.

Non-obvious parts, all of them regression-tested:

- **Environments are composed, not parsed** (2026-09-13). mathtext has no
  `\begin` at all, and a signals lecture writes `\begin{cases}` and
  `\begin{bmatrix}` in every other formula — the operator's sheet had 18
  of them printed as source. `_segments` cuts the expression around each
  environment (nesting-aware; a `\left(` … `\right)` around one becomes its
  delimiters), every plain piece and every cell goes through mathtext on its
  own, `_grid` lays the cells out with the environment's column alignment
  (`c` for matrices, `l` for cases, alternating `rl` for aligned, the
  `{spec}` for array) on shared row baselines, and `_delim_box` draws the
  brace/bracket/paren as a stroked SVG path stretched to the grid — at 8pt
  indistinguishable from CM's extensible glyphs, and no glyph table needed.
  The pieces are stacked on one baseline with the grid centred on the maths
  axis (0.25em) and shipped as **one** SVG, so the page sees exactly what a
  plain expression produces. The cell SVGs are matplotlib's own, inlined as
  `<g transform>` with their glyph `<defs>` deduplicated by id and the
  `figure_1`/`patch_1`/`text_1` ids stripped. Expressions without an
  environment still ship matplotlib's file untouched — the two paths report
  the same metrics. Nested environments recurse; an unknown one falls back
  to text like before.
- **Display formulas use `\dfrac`.** mathtext sets `\frac` in text style
  everywhere, so every `$$` block had running-text fractions; `_prepare`
  promotes `\frac` in display mode, and `_try_engine`'s last candidate
  demotes every `\dfrac` for a mathtext too old to know it. Cells of
  matrices and cases stay text style, as in LaTeX.
- **`\le`, `\ge`, `\ne` are mapped to the long names** in `_COMPAT`.
  mathtext knows only `\leq`; every remaining fallback in the real sheet was
  one of these.
- **Extraction runs on the markdown, before the HTML conversion.** Convert
  first and python-markdown has already eaten `_{trans}` into emphasis and
  dropped the backslashes. The maths comes out into opaque alphanumeric tokens
  (`MTHX3Z`) that markdown has no reason to touch, and goes back in *after*
  the citation passes so those never step over a base64 data: URI.
- **Baseline alignment is computed, not guessed.** `MathTextParser` reports
  width, height and depth; depth becomes a negative `vertical-align` in
  points, so inline maths sits on the text baseline instead of floating.
- **A top-level `\\` still stacks a display block into lines**, but only
  outside any environment (`_split_top_level`); inside one it is the grid's
  row break.
- **Digits are wrapped in `\mathrm{}` outside `\text{}` groups.** With
  `mathtext.default = "it"` matplotlib italicises digits, which LaTeX does
  not, so `2 \times 10^8` came out visibly wrong. The rewrite is cosmetic, so
  a failed parse retries with the author's own spelling before falling back.
- **Nothing here may fail the render.** matplotlib is optional and its parser
  rejects real LaTeX (`\begin{cases}`, `\substack`); every failure degrades
  to cleaned-up text in a serif face. Same rule as the rest of the export.
- Identical expressions render once — a lecture writing `$L$` forty times pays
  for one SVG.

### Frame cropping (`summarize/framecrop.py`)

`slide` mode looks for the largest bright rectangle (slides are overwhelmingly
light on dark UI) and accepts it only if it passes **all** of: minimum side
(240px), minimum area (18% of the frame), aspect ratio in 0.9-3.2, brighter
than the frame as a whole, and not the whole frame. Otherwise it falls back to
a mechanical border trim, and then to no crop at all.

Every one of those guards is load-bearing, and two of them were written after
the tests caught real failures: without the size/area floor a white logo or a
cursor highlight becomes "the slide" and the PDF gets a 30-pixel thumbnail;
without the full-frame check, rounding in the downscaled analysis pass reports
a 2-pixel "crop" on every frame and re-encodes the lot for nothing. **A
confidently wrong crop is worse than an uncropped frame** — that is the whole
design rule here.

`is_blank()` lives here too, and is the same idea one step blunter: a frame
whose downscaled grayscale copy is within a few levels of one shade carries no
picture, so it never reaches the PDF. Recordings are full of solid-black
frames — a screen share stopping, a slide mid-fade — and the old scene-change
pass collected them preferentially, since black-to-content is the biggest
scene change in the video. extract_frames.py now never saves one.

Analysis runs on a 200px-wide grayscale copy, so cost is a few milliseconds per
frame regardless of source resolution. Pillow is optional: without it frames
are copied through uncropped with one warning, and nothing is blank.

## Cross-session queueing (`lib/slotqueue.py`)

`--jobs` throttles concurrency *within one* `pipeline.sh` invocation. It does
nothing about several invocations running at once, which is the normal case
here (multiple terminals, or the trigger server firing repeatedly). The slot
queue is the machine-wide coordination those separate processes share, via
files under `$MEETING_BOT_ROOT/queue/`.

- One FIFO queue per component: `record`, `fetch_video`, `transcribe`,
  `frames`, `summarize`. Limit is `QUEUE_SLOTS_<COMPONENT>`, falling back to
  `QUEUE_SLOTS_DEFAULT`.
- **Unlimited unless configured.** Unset means the acquire path returns
  immediately and touches no files at all — the queue is opt-in, and an
  unconfigured box behaves exactly as it did before the queue existed.
- **A slot is held by the calling shell's PID**, not by a supervising process.
  That's what lets a bash stage hold a slot for an hour without a babysitter.
  Holders whose PID is gone are pruned by the next caller, so a SIGKILL'd run
  or a reboot releases its slot with no cleanup daemon — the queue cannot wedge
  permanently.
- A waiter whose own holder PID dies aborts instead of taking a slot nobody
  will use or release.
- `run_stage` in `run_one.sh` reads `$BASHPID` **into a variable before** the
  `$( ... )` command substitution. Inside the substitution, `$BASHPID` is the
  substitution's own throwaway subshell, which exits immediately — the queue
  would see a dead holder and reclaim the slot instantly, serializing nothing.
  This was a real bug; don't inline it back.
- `QUEUE_SLOTS_RECORD` is supported but dangerous and documented as such:
  recording is the only time-sensitive stage, so a queued meeting isn't delayed,
  it's missed. It stays unlimited by default.

## Things future Claude MUST NOT change

Decisions with a specific reason behind them. If you want to change one, stop
and confirm with the user first — they're deliberate trade-offs, not laziness.

- **Firefox ESR is the default browser, Chrome the kept fallback.** The
  operator chose "switchable, Firefox default" over replacing Chrome
  (2026-09-29). Don't delete the Chrome path; don't make Chrome the default
  again without asking.
- **Firefox is driven through Selenium + geckodriver and `browser.FirefoxPage`,
  not Playwright** — Playwright cannot drive the stock ESR binary. capture.py
  stays written against the Playwright page API; extend the adapter rather
  than forking the Meet/Zoom logic per browser.
- **Firefox gets its profile as `-profile <dir>`**, never `Options.profile`
  (which copies it and loses every sign-in).
- **The PC's real camera and microphone never reach the browser.** Camera
  refused / fake black device; microphone = the run's silent `<sink>_mic`
  monitor (`PULSE_SOURCE`) or a silent fake file. Dropping this on a PC
  broadcasts the operator's room when a mute click misses.
- **`GDK_BACKEND=x11` and no `WAYLAND_DISPLAY` wherever the browser goes to
  Xvfb.** On a Wayland session the browser otherwise fails to open the
  display, or opens on the operator's screen.
- **Playwright uses `channel="chrome"`** (on the Chrome path), NOT the bundled
  Chromium. The bundled build gets Google's "This browser or app may not be
  secure" block on sign-in.
- **Login uses a direct launch of the browser binary, never Selenium or
  Playwright.** Both inject automation flags (`navigator.webdriver=true`) that
  Google's sign-in detects. `first_time_login.sh` also moves geckodriver's
  `user.js` aside first.
- **Chrome must be `google-chrome-stable`, not Debian's `chromium`.** The
  branded build is what gets through the sign-in flow.
- **`--no-sandbox` only as root.** The PC runs as the operator; Chrome keeps
  its sandbox. `browser._open_chrome` adds the flag when `geteuid() == 0`.
- **The bot account is `BOT_GOOGLE_ACCOUNT`, never a literal.** Refuse a Meet
  from a profile that holds a different account or none; never treat an
  unreadable answer as "signed out"; keep `authuser=` on Meet URLs.
- **Everything runs as the operator's user.** No root requirement outside
  `setup.sh --system`; the user step refuses root (it would leave root-owned
  files in `~/.local` and `.venv`).
- **The venv is `.venv` in the repo, built by uv.** Removable by deleting it —
  the operator's stated reason for uv.
- **A meeting input detaches before any state is written**, and the web UI /
  pm2 resume job run with `MEETING_BOT_FOREGROUND=1`. Detaching after
  `rs init`, or twice, makes duplicate runs (two calls, for meet.new).
- **pm2 never starts anything at boot.** No `pm2 startup`, no `pm2 save` in
  any script. The operator asked for the web UI to be off by default.
- **The bot's mic and camera stay blocked at the browser**, and mute_av
  never clicks an "already off" label. See "What the recording shows".
- **Silence is measured by duration, not peak**, before any AssemblyAI upload.
- **The bot's audio client is never called "Firefox"** (PULSE_PROP_OVERRIDE
  in record_screen.sh), and lib/pinaudio.py keeps it on the recording sink.
- **ffmpeg gets exactly one SIGINT, and is never SIGKILLed.** A second one
  while it closes the file leaves an unplayable MP4.
- **No awk (mawk) in a pipe that must be live.** It holds lines in its
  input buffer; the meeting link never reached the web UI. Use a bash
  `while read` loop.
- **Display numbers are claimed with our own claim file, not Xvfb's lock.**
  A non-root Xvfb ignores `-nolock` and refuses a lock that names a live pid.
- **`--window-position=0,0` stays in `CHROME_ARGS`.** Without it Chrome places
  its kiosk window at (10,10) and every recording carries a 10px black band
  down the left and top edges. Found by `verify_e2e.sh --browser-smoke`, which
  measures the recorded frame rather than trusting the reported window size —
  a 1px band at the right and bottom is Chrome's viewport rounding and is fine.
- **`browser.open_page()` is the single launch** (`CHROME_ARGS` /
  `FIREFOX_PREFS` in `screen/browser.py`, `CHROME_ARGS` re-exported by
  capture.py), used by capture.py and `screen/browser_smoke.py`. A flag that
  breaks recording has to break the smoke test too, or the smoke test is
  testing a different browser.
- **Display numbers and sink names are allocated per run, never hardcoded.**
  The container boundary that made `:99` safe is gone. See the isolation
  section above, including why `pactl set-default-sink` must not be used.
- **Locale is `th-TH`**, so Thai participant names render in chat. Side effect:
  Meet's UI labels come back in Thai, which is why `capture.py` carries both
  English and Thai labels for every selector.
- **An unconfirmed join click is not a failed join.** `capture.py` falls
  through to `wait_for_admission()` whenever the click can't be confirmed but
  `join_rejection_reason()` finds no refusal on the page. Zoom's web client
  hides the button behind its "Joining Meeting..." interstitial, so
  `click_first_match` times out *while the join is succeeding* — the old
  fail-fast path abandoned calls the bot was seconds from entering. The
  refusal detector (English + Thai) is what keeps this from costing the full
  600s `ADMIT_TIMEOUT_SECONDS` on a genuinely dead link; it also runs inside
  the admission wait loop, so "no one responded to your request" fails fast.
- **The kill switch routes through the in-Meet Leave button**, not by killing
  the browser process, so other participants see the bot leave cleanly.
  `kill_meeting.sh` signals pids only as post-grace escalation, and gives
  ffmpeg `SIGINT` so the MP4 stays playable.
- **H.264 is `libx264 -preset ultrafast -crf 28`.** Keeps CPU low on a 4-vCPU
  VM with no GPU; visually fine for talking heads and slides.
- **Frames are taken on change, from one decode — not on a clock.** The
  operator replaced the scene-change + periodic passes on 2026-09-30. Keep:
  the change test = the model dedupe's hash and distance (one constant,
  `SAME_SLIDE_MAX_DISTANCE`); save on settle, never mid-transition; the
  `motion` cap so a played video is not invisible; the 5-minute safety net;
  blanks never saved. Don't bring back a fixed-period pass without asking.
- **framecrop's numpy paths must stay result-identical to the Python loops**
  (`FrameAnalysisPathsTest`). A crop or hash that moves changes the PDF, the
  dedupe and the frame detector at once.
- **`--disable-features=ScreenCapture` is intentional** — the bot has no reason
  to share its screen. Layers 2 and 3 in `capture.py` (dialog killer, "Stop
  presenting" monitor) are the catch-nets if Chrome renames the flag.
- **EXIT traps in the `set -e` scripts end every line with `|| true`.**
  `record_screen.sh`, `first_time_login.sh` and `xsession_stop_xvfb` all kill
  pids that are usually already gone. Under errexit a failing command in an
  EXIT trap aborts the trap *and becomes the script's exit status* — which made
  every successful recording exit 1, so `pipeline.sh` marked `record` failed and
  never transcribed the MP4 it had just produced. Verified 2026-09.
- **Camera/mic mute is best-effort (log warning + continue), not abort.** A
  failed UI heuristic must not block a real meeting.
- **Google Meet pre-join uses a Tab-scan, not fixed Tab counts.** The pre-join
  DOM reorders frequently; identifying buttons by accessible name is the only
  durable approach.
- **YouTube URLs auto-route to youtube-transcript.io**, not AssemblyAI. We
  already have free captions there and they return in seconds.
- **Empty YouTube transcripts fail loudly**, not silently. Every key hits the
  same upstream captions, so retrying won't help. Placeholder-only text (e.g.
  `[เสียงพากย์ไทย]`) is written through so the operator can see it in the
  `.txt`; only a genuinely empty response is an error.
- **Multi-key rotation is round-robin with an on-disk cursor.** Don't collapse
  any of the numbered key sets to a single variable, and don't make the cursor
  per-process — see the keyring section.
- **The YouTube download never merges streams.** The format chain is
  `best[ext=mp4]/best/bv*[ext=mp4][vcodec^=avc1][height<=720]/bv*[ext=mp4][height<=720]/bv*[height<=720]/bv*`
  — muxed first, then **video-only**. NOT `bestvideo+bestaudio
  --merge-output-format mp4`: the merge path needs a JS runtime for YouTube
  extraction and a clean postprocess merge. Dropping audio is free here because
  this file exists *only* to extract frames from — the YouTube transcript comes
  from captions and never touches it. `avc1` is preferred over AV1 so a 4-vCPU
  box decodes frames cheaply (AV1 works, just slower).
  History: format 18 (muxed 360p) resolved fine in 2026-08, but by 2026-09
  YouTube exposes no muxed format at all on many videos and `best[ext=mp4]/best`
  alone fails with "Requested format is not available". Keep any future fix
  runtime-agnostic rather than re-enabling the merge.
  The same string appears in `lib/run_one.sh` and `summarize/summarize.py` —
  change both.
- **Kaltura is reached through `lib/kaltura.py`, never yt-dlp.** Its extractor
  sends no `Referer` and 404s on exactly the entries this project exists for.
- **Every Kaltura request carries a `Referer`.** Dropping it turns a working
  entry into a bare 404 with nothing to explain it. The default is the CDN's own
  domain so no tenant-specific configuration is needed; `KALTURA_REFERER`
  overrides it.
- **`kaltura.py parse` stays offline and dependency-free.** `classify_input`
  runs it on every pipeline input; an import error or a network call there
  breaks classification for inputs that have nothing to do with Kaltura.
- **"No captions" is exit code 3, distinct from failure.** `transcribe.sh`
  routes on it. Merging it into 1 turns a Kaltura outage into three paid
  AssemblyAI uploads of a video whose captions were fine.
- **`fetch_video` runs before both branches on the Kaltura path.** Its
  transcribe branch needs the media file; leaving the download inside the frames
  branch races it.
- **The pasted `<iframe>` is normalised before it reaches summarize.** The raw
  tag in the provenance comment and the link line is not a document.
- **`summarize.py` does not download the video when `--frames-manifest` is
  given.** The video exists only to produce frames; once a manifest exists
  there's nothing to download. The pipeline always passes one, so re-adding the
  download would make every YouTube run fetch the same video twice.
- **Artifact paths derive from the run id, not the clock.** Resume depends on
  it. This is why `--out-base` and `--pdf-out` exist.
- **The `#t=` suffix never reaches the stored input.** It is the auto-resume
  key and the document's link line; a window left inside it breaks both.
- **A clipped run gets its own run id.** Dropping the window from the run id
  makes a clip overwrite the full summary of the same lecture, and two windows
  overwrite each other — silently, because every artifact path is a function of
  the run id and nothing checks what is already there.
- **`--clip` cuts the media; it does not filter the transcript afterwards.**
  Filtering pays AssemblyAI for the whole video on every clip. The one
  exception is captions, which have no media to cut and are free anyway.
- **Clip timestamps are relative, and the document says so.** The `Clip:` line
  in `document.py` is not decoration: without it every timestamp in a clipped
  summary points at the wrong moment of the source video and looks correct.
- **`clip.label()` must round-trip through `clip.parse_clip()`.** The label is
  what `state.json` stores and what every resume parses back. A label that
  doesn't parse fails one download in, on the resume path only.
- **`-avoid_negative_ts make_zero` stays in the ffmpeg cut**, and `-ss` stays
  before `-i`. Dropping the first makes the clip-relative timebase silently
  absolute; moving the second turns a few seconds of seeking into minutes of
  decoding.
- **The clip's partial file keeps the destination extension**
  (`clip.part.mp4`). ffmpeg chooses its muxer from the extension and refuses to
  start without one.
- **The five output directories are required, with no defaults.** See the
  configuration section: a silent default is worse than an error here.
- **`runstate.py status` verifies artifacts exist on disk** before reporting
  `done`, and `mark_done` in `run_one.sh` refuses to record an absolute
  artifact path that doesn't exist yet. Don't "optimize" either away — they
  are what turns "the stage lied about succeeding" into an error naming the
  missing file, instead of a traceback two stages later.
- **A stale `run.lock` is taken over, not fatal.** Otherwise a killed run could
  never be resumed.
- **Missing credentials raise `BackendUnavailable`, not `SystemExit`.**
  `SystemExit` doesn't inherit from `Exception`, so the fallback chain didn't
  catch it and one unset key killed the whole run. Keep it a normal exception.
- **Retry jitter is full, not proportional.** Parallel chunk requests must not
  retry in lockstep.
- **The summarizer runs the `claude` CLI; it does not call the Messages API.**
  No `ANTHROPIC_API_KEY`, no `anthropic` package. Adding one back moves the
  spend off the subscription the operator is paying for. If a run has to be
  billed to a console account, that is a new backend beside `claude-cli`, not a
  change to it.
- **`CLAUDE_CLI_BIN` is set to an absolute path in `.env`.** Relying on `PATH`
  makes a signed-in, working CLI invisible to the pipeline, and the only
  evidence is which model the provenance header names.
- **`ANTHROPIC_*` must stay scrubbed from the CLI's environment.** See the
  summarize section: leaving one set redirects billing silently, and an empty
  one breaks auth in a way that looks like a broken subscription.
- **The CLI runs with `--safe-mode` in a scratch cwd.** Otherwise this file
  gets loaded into the context of every summary.
- **Nothing that varies per run may enter the static prompt block.** The chunk
  label, the reference material, the transcript and the frame paths all belong
  in the piped user turn. A leak costs the cache and reports nothing.
- **The static-prompt markers are opt-in and stripped before send.** A template
  without them must be sent byte-for-byte as it was before the split existed.
- **`FRAME_MAX_DIMENSION` downscales a copy, never the saved frame.** `pdf.py`
  crops and embeds the original; overwriting it degrades every PDF and is not
  recoverable without re-running ffmpeg.
- **Frames go to the CLI as image blocks in one turn, not as paths to Read.**
  The Read path re-sends the whole context once per frame opened. It stays
  only as `CLAUDE_CLI_FRAME_INLINE=0` for an old CLI; don't make it the default
  again.
- **Blank and repeated frames are dropped before the cap, and the cap before
  the crop.** Reordering spends the cap on copies of one slide. The repeat
  test is a texture hash over the slide region — not a difference hash (blind
  to a line that grew) and not an average hash (blind to text on a white slide
  next to dark chrome); both were tried and both merged distinct slides.
- **The chunk label goes after the frame manifest; the reference material goes
  right after the static-prompt end marker.** Anything constant across a run's
  chunks must precede anything that varies, or it never caches — and nothing
  reports the miss.
- **`role="merge"` reaches every backend's signature.** A backend that doesn't
  take it breaks the fallback chain on the merge call with a TypeError.
- **The CLI's JSON body decides success, not its exit code.** It exits 0 when
  signed out. Parsing the envelope is the only way to tell.
- **`--output-format stream-json --verbose` stays.** `json` drops the
  `rate_limit_event`, and with it the meter and the reset time; the window
  then goes back to looking like a login failure.
- **`SUMMARY_MAX_TOKENS` is not a Claude lever, and must not be wired to
  one.** There is no output cap on the CLI, and output is not what spends
  the window. The levers are `SUMMARY_CHUNK_CHARS` (whether there is a merge),
  `CLAUDE_CLI_MERGE_MODEL`, `CLAUDE_CLI_MAX_FRAMES`, `FRAME_MOTION_SECONDS`,
  `FRAME_MAX_DIMENSION`, `SUMMARY_EFFORT` and `CLAUDE_CLI_MODEL`.
- **`ClaudeCliRateLimited` is `retryable = False` and the chain re-raises
  it.** Retrying it burns the backoff schedule; advancing hands the summary to
  Gemini, which the operator chose not to pay for. It waits, or it pauses.
- **A paused chunk fails the stage; it is never merged around.** `pause_run`
  in `mapreduce.py`. A document with "*this part could not be summarized*"
  and a `done` stage is a hole nothing will ever fill.
- **Exit 75 means paused, and `--resume-all` honours `rate_limited.resets_at`.**
  Collapsing 75 into 1 turns a timer-driven resume into a call against the
  same wall every fifteen minutes.
- **`runstate.start` clears `rate_limited` and `waiting_until`.** Otherwise a
  run that resumed and succeeded still looks paused to the next `--resume-all`.
- **`CLAUDE_CLI_MAX_FRAMES` thins what is offered, never renumbers.** The
  numbers come from `assign_numbers()` over the whole manifest; a thinned
  list that renumbered would recreate the per-chunk numbering bug above.
- **`BackendUnavailable.retryable = False` stays.** Without it a signed-out CLI
  burns the whole retry schedule before falling back to Gemini.
- **No `thinking.budget_tokens`, ever** — and now no CLI spelling for one
  either. Effort is `--effort`; thinking is adaptive.
- **The document wrapper is built in code, not requested in the prompt.** See
  the output-format section above.
- **A failed PDF render must not fail the run.** The markdown is the artifact.
- **Unparseable LaTeX degrades to text; it never fails the render.** matplotlib
  is optional and its parser rejects plenty of real LaTeX.
- **Maths is extracted before the markdown→HTML conversion, not after.**
  Convert first and there is no LaTeX left to typeset.
- **The hidden transcript is chunked under poppler's per-page extraction
  limit**, and is white text in normal flow rather than `display: none`.
  Either mistake — one big block, or a display rule that emits nothing —
  turns "the transcript travels with the PDF" into a silent half-truth.
- **The PDF look is DESIGN.md.** Change the spec and `_css()` together;
  don't restyle one without the other.
- **Callouts are extracted before `mathrender.extract`**, and maths symbols
  are wrapped before `mathrender.restore`. Either order reversed breaks
  formulas in boxes or corrupts data: URIs.
- **Body faces are size-matched on x-height; `PDF_FONT_SIZE` is Computer
  Modern's size.** Setting every face to the same point size is exactly what
  the operator asked not to have.
- **Computer Modern is never offered for Thai.** No Thai glyphs.
- **No prompt asks for timestamps or frame citations** — except the
  timestamps of `reality` (`TIMED_TRANSCRIPT_PROMPTS`), the operator's
  explicit exception of 2026-09-30; no prompt cites frames. One file per
  prompt for every backend; don't add a sixth without asking. The old names
  stay as aliases in `promptnames.py` — unfinished runs and `.env` files
  carry them.
- **A timed prompt's timestamps come from the transcript's marks, and the
  links from `document.link_timestamps`.** Never ask the model for URLs (it
  cannot know the clip offset, and a wrong link looks right), and never
  put the marks in the document's embedded transcript.
- **Per-run instructions go after the static end marker**, never inside the
  static half.
- **Keep a Thai face in `PDF_FONT_FAMILY`.** Computer Modern has no Thai
  glyphs.
- **Don't set `PDF_FONT_FAMILY` in `.env.example`.** Set, it overrides the
  per-language stacks and every Thai PDF silently goes back to Computer
  Modern. The per-language default is the feature.
- **`{language_rule}` stays in every shipped prompt, inside the static
  block.** Hard-coding a language back into a template makes
  `SUMMARY_LANGUAGE` a no-op for that prompt with nothing to report it;
  moving the placeholder into the dynamic half costs nothing today but
  would if the rule ever became per-run.
- **The document wrapper's labels stay English whatever `SUMMARY_LANGUAGE`
  says.** Settled with the operator 2026-09-15: the `.md` drops into existing
  course files that use those labels.
- **The vendored fonts under `fonts/` are the install source, not a
  download.** They are OFL; keep the `OFL.txt` beside each family.
- **The PDF defaults are the summary alone** — `PDF_FRAMES`,
  `PDF_TRANSCRIPT` and `PDF_RESOURCES` all `none`. The `.md` keeps the
  transcript. Don't turn an appendix back on by default; the operator read
  the 71-page version.
- **`_normalize_list_indent` runs after `mathrender.extract`.** Before it, a
  multi-line `$$` matrix under a bullet is cut in half.
- **Environments never reach mathtext whole.** `\begin{cases}` is composed
  from cells in `mathrender._layout`; handing the whole expression to the
  parser is the text fallback the operator complained about.
- **The chapter placeholder and the fixed video-title H1 are gone from the
  wrapper.** The heading is the model's. Don't put them back for the course
  files' sake — the operator chose this for the markdown too.
- **`summarize.py` must not pass `work_dir` to `pdf.render()`.** Naming one
  transfers ownership and leaves the cropped intermediates beside the
  deliverable.
- **`--combine` is one summary over every video, not a concatenation of
  per-video summaries.** The members' `summarize` stage stays pending on
  purpose; giving them individual summaries doubles the spend. See the
  `--combine` section.
- **Timestamps in a combined document are per video, and every place a
  timestamp appears names the video.** The transcript fences, the frame
  labels, the chunk headers, the PDF captions. Dropping the video from any
  one of them makes "410.0s" ambiguous with nothing to show for it.
- **Frames of a combined set are numbered once, across all videos, in
  `(part, timestamp)` order.** Numbering per manifest gives two videos the
  same "Frame 4" and the PDF resolves both to the first.
- **A chunk never spans two videos.** Its window and its frames belong to
  one clock.
- **The combine run owns the members' frame sweep**, after its PDF.
  `run_one.sh --skip-summarize` never sweeps, and `pipeline.sh` no longer
  does either.
- **`--resume-all` must skip runs with `combined_into` set.**
- **The post-summary media sweep touches only `runs/<id>/video.*` and the
  clip.** Never `RECORDINGS_DIR`, never the local-file input. A meeting
  recording is the one irreplaceable artifact; `sweep_run_media` returns
  early on `input_type = meeting` on top of never being pointed there.
- **The sweep runs after `mark_done summarize`, never before.** A failed or
  paused run keeps its download so the resume doesn't pay for it twice.
- **`ensure_video_fetched` / `ensure_video_clipped` re-fetch a `done` stage
  whose file is gone**, and the ahead-of-branches calls are behind
  `media_needed`. Drop the first and every post-sweep re-extraction fails
  with "no video available"; drop the second and every `--run-id` on a
  finished Kaltura run downloads the entry again.
- **Frame numbers come from `assign_numbers()` over the whole manifest, never
  from a per-chunk enumeration.** See the section above: the failure mode is
  silent, survives every unit test that looks at one chunk, and produces a PDF
  full of confidently mislabelled pictures.
- **Frame cropping declines rather than guesses.** See the framecrop section.
- **Reference material is escaped before it enters the prompt template**, and
  framed as data rather than instructions — it is untrusted input exactly like
  the transcript.
- **Queue slots default to unlimited.** Turning any of them on by default
  would silently serialize existing setups, and for `record` would silently
  start missing overlapping meetings. Opt-in only.
- **The queue holder is the calling shell's PID, read before the command
  substitution.** See the queueing section above — inlining `$BASHPID` into
  `$( ... )` breaks serialization in a way that looks like it works.
- **`whisper.cpp` is gone.** It hadn't been on any pipeline path since the
  AssemblyAI switch. Don't re-add a `TRANSCRIBE_BACKEND=whisper` escape hatch
  without explicit sign-off.
- **Alpine support was dropped in the Debian 13 port.** `setup.sh` is apt-only
  and fails fast elsewhere with a pointer to the `alpinelinux` branch. Don't
  reintroduce dual-target detection without asking.
- **This branch is native; Docker lives on the `docker` branch.** The PC port
  deliberately took the docker branch's features and not its packaging. If the
  two are ever merged, the docker branch's own rules (one container, the
  entrypoint's PID cleanup) come with it.
- **Every value-taking `pipeline.sh` option goes through `need_value`.**
  Without it a trailing option hangs the script silently.
- **The web UI validates with `pipeline.sh --dry-run`, not its own parser.**
  Its per-line ✓/✗ come from the dry run's `ok`/`bad`/`badarg`/`extra`
  lines; the page's own hints check shapes, never what an input is. An
  `extra` line is a failure in the form (a typo'd path would become the
  run's name).
- **A meet.new run is never auto-resumed**, and a hosted call is never ended
  on an unreadable participant count.
- **Captions are never taken from a YouTube machine translation.** Spoken
  language first; another language only as the announced last resort.
- **Voice only means no frames stage, and summarize.py gets `--no-frames`.**
  Dropping the flag while skipping the stage makes summarize.py extract (or,
  for YouTube, download) the frames itself.
- **An audio-only recording is always voice only**, and its medium is fixed
  at run creation (the recording's file name depends on it).
- **Course-reference metadata stays in the dynamic half** of the prompt, as
  attributes — never substituted into the static instructions.

## Tests

Run them with the project venv (`.venv/bin/python3 <suite>`, and
`MEETING_BOT_VENV=$PWD/.venv bash lib/test_*_e2e.sh`). All of these run
without API keys or network, against temp directories
— including one whose path contains a space, so quoting regressions fail loudly.
`verify_e2e.sh` is the exception: it is the live checklist.

| File | Covers | Count |
|---|---|---|
| `lib/test_runstate.py` | state transitions, stale artifacts, concurrent writes, CLI, `annotate` and the pause fields | 19 |
| `lib/test_slotqueue.py` | FIFO order, dead-holder reclaim, timeout, CLI | 23 |
| `lib/test_keyring.py` | numbered slots, gaps, duplicates, cursor persistence | 22 |
| `lib/test_resources.py` | spec parsing, text extraction, GitHub fetch, budgets, frontmatter, binary files | 36 |
| `lib/test_kaltura.py` | iframe/URL parsing, the Referer, the KS, caption selection, download, retries | 51 |
| `lib/test_clip.py` | window parsing, the label round-trip, the ffmpeg invocation, caption windowing | 33 |
| `summarize/test_summarize_units.py` | the Gemini model chain (keys first, 429 without backoff, 404 skips the model), retry classification/backoff, chunking, segment granularity, map-reduce, global frame numbering, document, the multi-video wrapper and per-video chunking for `--combine`, the claude-cli command line + envelope parsing (plain and stream-json), inline image blocks vs the Read path, the merge role, the cacheable static prompt and the label/resources order, frame crop + downscale, blank/duplicate dropping and the texture hash, the usage ledger, the hit-window wait/pause and the chain not advancing, frame thinning, the model's title heading the document, the output language (default, aliases, the rule in every template and the merge, the cacheable half, the provenance field), the `<course_reference>` block, the five prompts (old names resolve, no timestamps outside `reality`, the callout vocabulary), `--instructions` placement, the no-frames note, the reality prompt (timed lines and their chunking, `[mm:ss]` → YouTube links with the clip offset, `[Video N, …]`, its own merge, end to end through `main()`) | 221 |
| `summarize/test_pdf_units.py` | crop geometry, framecrop on decoded images / numpy vs Python identical / the shared downscale, citation rewriting and fading, blank-frame detection, LaTeX extraction/fallback, environment composition (cases/matrices/aligned, nesting, one glyph table), display fractions, nested-list re-indent, the legacy header, the summary-only defaults, the hidden transcript on request, part-tagged manifests and captions for `--combine`, the per-language body face (provenance over env, `PDF_FONT_FAMILY` override, the CSS), the per-run font (lists, aliases, defaults, precedence, x-height matching, CLI check), the design markup (callouts, code window, maths symbols, link lines in the title block, colophon), real PDF render | 107 |
| `transcribe/test_yt_transcript_client.py` | key rotation, retry, and the `tracks[]` response shape | 16 |
| `transcribe/test_yt_autocaptions.py` | the yt-dlp fallback: track choice (never a translation), json3, the CLI against a stub yt-dlp | 11 |
| `screen/test_extract_frames.py` | frames on change: settle, a transient change, blanks, the motion cap, the safety net, the last sample, the shared distance; the PPM reader; real ffmpeg (black lead-in skipped, audio-only empty, retired settings named) | 19 |
| `screen/test_capture_host.py` | hosting a created Meet: the wait for the first participant, ending when empty, an unreadable count, 1:1 not idle, the guest path unchanged; which tile menu is the bot's own, minimising only with company | 19 |
| `screen/test_browser.py` | browser choice and aliases, per-browser profiles, no real camera/mic, sandbox only as root, Firefox stale locks, ListAccounts parsing (signed out vs unknown), verdicts, gmail normalisation, `authuser`, the account not hardcoded, capture's account gate | 16 |
| `test_trigger_server.py` | the web UI's API against a stub pipeline: body → argv, token, `/api/check` = `--dry-run`, every refused line reported (`bad`/`badarg`/`extra`, an `extra` failing the form), run/log path refusal, summary language/font/instructions, the options, record media / summary source | 12 |
| `lib/test_pipeline_e2e.sh` | full orchestration with stubbed stages, output dirs, PDF/markdown toggles, `--resources`, the combine run (members skip summarize, parts.json in input order, resume, `--force` re-extraction, failed member, `--resume-all`, the frame sweep), the Kaltura DAG, the `--clip` DAG and run-id separation, the per-input `#t=` suffix, a summarize paused on the usage window (exit 75, `PAUSED`, `--resume-all` skipping until the reset, then finishing), the post-summary media sweep (download and clip gone, recording and local input kept, `cleaned` stages, `KEEP_FRAMES=1`, re-download on `--force` / combine `--force` / a swept clip, no re-download on a finished `--run-id`), options without values, `--help` complete, binary `--resources`/`--from-file` refused, a frontmatter reference, `--dry-run` (plan lines, creates nothing, every unusable input reported as `bad`/`badarg`/`extra` and exit 1, a legacy name still passing), `--new-meet` / `meet.new` (link stored and cited, never auto-resumed, clip refused), a meeting detached into the background (returns at once, names its log and run, finishes on its own; `--foreground`, `--dry-run` and non-meeting inputs stay attached), per-run summary language/font/instructions (refusals, storage, export to summarize, replaced on a resume without touching resources), voice only per source (no frames stage, no YouTube download, Kaltura still fetched, `--no-frames`), audio-only meetings (.m4a, RECORD_MEDIA to the recorder, stored voice), refusals, `.env` defaults, a resume switching frames back on, a voice-only `--combine` | 448 |
| `lib/test_media_e2e.sh` | real MP4 + real SDKs against local stub servers, the real llm_client against a stub `claude` binary (single run and `--parts`), the usage ledger landing in state.json, a hit window waited out then retried against the stub (`rate-limited-once`), a pause past the cap (exit 75, reset time recorded, Gemini untouched), and a real ffmpeg clip probed for duration and rebased timestamps, the YouTube caption fallback through a stub yt-dlp (spoken-language auto captions; the other-language track as last resort), `--no-frames` sending no image and the no-frames note, an audio file's empty manifest summarized | 131 |
| `verify_e2e.sh --browser-smoke` | the real browser (Firefox ESR or Chrome) under Xvfb, recorded and measured for black edges | 6 |

`test_pipeline_e2e.sh` runs the real `pipeline.sh` and `run_one.sh` and stubs
only the four expensive stages, behind the same argument/output contract. It has
caught seven real bugs so far (`--from-file` with no positionals, an unhelpful
unrecognized-input error, the double YouTube download, the
`$BASHPID`-in-substitution queue bug, a stage exiting 0 without writing its
artifacts, a `--clip` label that could not be parsed back on a resume, and a
resumed run failing its `frames` branch because the frames had been swept —
`mark_done` refused the missing manifest on a stage that had nothing to do).
Add to it when you touch orchestration.

The `--clip` split between the two shell suites is worth knowing: the pipeline
suite stubs ffmpeg and asserts on the argv and the DAG, and `test_media_e2e.sh`
runs the real thing against a real MP4 and probes the result with ffprobe. Only
the second could see that ffmpeg refuses to write a file named `.part`, and
only the first can afford to exercise nine different windows.

The Kaltura network seam is **not** in `test_media_e2e.sh`. `lib/test_kaltura.py`
drives a fake `requests.Session` instead, which asserts on what we send — the
`Referer`, the `widgetId`, the KS on the media URL — without needing a stub HTTP
server, and is the place to add assertions when the invocation changes.
`test_pipeline_e2e.sh` stubs only `kaltura.py`'s network half and delegates
`parse` to the real module, because the orchestration under test depends on what
the parser returns (the run id, and the canonical URL that replaces the blob).

`test_media_e2e.sh` is the counterpart: real media, real SDKs, stub servers
(`lib/fake_api_server.py`) speaking the providers' HTTP protocols. It is the
only place that can assert on **what we actually send**. For the summarizer
that seam is no longer HTTP — it is `lib/fake_claude_cli.py`, a stub
*executable* that `CLAUDE_CLI_BIN` points at. It records the argv, the piped
prompt and the auth vars that reached the child, so the test asserts that
`--effort` carries `SUMMARY_EFFORT`, that the frames arrive as JPEG image
blocks in a stream-json user message with `--tools ""` and no `--add-dir`,
that no filesystem path is in the prompt, that the copies sent are the
cropped ≤768px ones and the originals are untouched, and that
`ANTHROPIC_API_KEY` — deliberately exported by the test — did *not* reach the
CLI. The stub unpacks the stream-json line so its `prompt` field is still the
text the model read, and records the image blocks under `images`. It also drives `FAKE_CLAUDE_MODE=not-logged-in` to prove a signed-out CLI
fails as `BackendUnavailable` rather than being retried, and
`rate-limited-once` / `rate-limited` (with `FAKE_CLAUDE_RESET_IN` and
`CLAUDE_CLI_MAX_WAIT_SECONDS`) to drive the real wait-then-retry and the
pause through the real `summarize.py`, state.json included. The stub answers
in stream-json when asked to, with a `rate_limit_event` at a fixed 42% so
the ledger's numbers can be asserted exactly. When you change the
invocation, assert it here. Note the wait test really sleeps: the stub's
reset is 2s away and the margin is 60s, so that block takes about a minute.
`KEEP_TESTROOT=1` keeps the test root for inspection.

One flaky failure seen on the author's desktop in the past is worth knowing
so it is not mistaken for a regression: "language not sent" in the
transcribe section (it did not reproduce on 2026-09-29: 125/125 with the
project `.venv`). The media
test's synthetic slides are flat colour, so the duplicate pass collapses all
three to one image block; that is the fixture, not a bug — real slides carry
text, which is what the texture hash keys on.

**What no test here covers:** the browser actually joining a live Meet/Zoom
call (Firefox has reached Meet's pre-join page and its refusal page, not a
call), a
real Kaltura tenant's access-control (`./verify_e2e.sh --kaltura` is the live
check, and needs no key), and
real AssemblyAI/Claude/Gemini/youtube-transcript.io round-trips — including
whether the subscription behind `claude auth` has quota left.
`./verify_e2e.sh` runs those on the real box. Its `--preflight` and
`--browser-smoke` need no keys and no meeting: between them they cover the
display/audio/capture chain and Chrome rendering under Xvfb with the recorder's
own flags, which is everything about stage 1 except the call itself.

## File layout

```
.
├── README.md                     <- all user-facing docs
├── DESIGN.md                     <- the summary PDF's design spec; pdf.py implements it
├── CLAUDE.md                     <- this file
├── .env.example                  <- names and defaults only; prose lives in README
├── requirements.in               <- edit this
├── requirements.txt              <- generated, hash-pinned; setup.sh installs it
├── requirements-browser.in       <- playwright only, for the browser stages
├── requirements-browser.txt      <- generated
├── fonts/                        <- vendored Bai Jamjuree + Sarabun (OFL); setup.sh installs them
├── source_env.sh
├── setup.sh                      <- `sudo … --system` (apt) + the user step (uv .venv, geckodriver, pm2, .env)
├── first_time_login.sh           <- desktop-window login as BOT_GOOGLE_ACCOUNT (or --novnc), then checks it
├── kill_meeting.sh               <- per-run or global, pid-file based
├── pipeline.sh                   <- multi-input orchestrator
├── verify_e2e.sh                 <- live checks: preflight + mp4/YouTube/Kaltura/Meet/Zoom
├── benchmark.sh                  <- CPU/memory per local stage (synthetic media); --browser, --watch-run
├── trigger_server.py             <- web UI + /trigger + /api/* (stdlib only)
├── ecosystem.config.js           <- pm2: meeting-bot-web + meeting-bot-resume (every 15 min)
├── webui.sh                      <- ./webui.sh on|off|restart|status|url|logs (nothing at boot)
├── test_trigger_server.py
├── web/
│   ├── index.html                <- the UI page (no external scripts)
│   └── serve.sh                  <- pm2 entry: loads .env, runs trigger_server.py
├── lib/
│   ├── runstate.py               <- run state + locking + CLI
│   ├── slotqueue.py              <- machine-wide component queue
│   ├── keyring.py                <- numbered API keys + rotation cursor
│   ├── paths.py / paths.sh       <- the five required output directories
│   ├── xsession.sh               <- per-run Xvfb display + PulseAudio sink
│   ├── resources.py              <- slides/notes from GitHub or a folder
│   ├── kaltura.py                <- Kaltura embeds: parse, media URL, captions
│   ├── clip.py                   <- --clip: window parsing + the ffmpeg cut
│   ├── combine.py                <- --combine: parts.json from the member runs, the run key
│   ├── run_one.sh                <- the per-run stage DAG
│   ├── fake_api_server.py        <- stub AssemblyAI/YouTube servers
│   ├── fake_claude_cli.py        <- stub `claude` binary for the media test
│   ├── test_runstate.py
│   ├── test_slotqueue.py
│   ├── test_keyring.py
│   ├── test_resources.py
│   ├── test_kaltura.py
│   ├── test_clip.py
│   ├── test_pipeline_e2e.sh
│   └── test_media_e2e.sh
├── screen/
│   ├── record_screen.sh          <- stage 1, native (no container)
│   ├── browser.py                <- MEETING_BROWSER, profiles, launch, FirefoxPage adapter, the account check
│   ├── capture.py                <- join/host driver (Playwright page API)
│   ├── browser_smoke.py          <- the same browser, without a meeting
│   ├── test_browser.py
│   ├── test_capture_host.py
│   ├── test_extract_frames.py
│   └── extract_frames.py         <- one decode; a frame when the picture changes
├── transcribe/
│   ├── transcribe.sh
│   ├── assemblyai_client.py
│   ├── yt_transcript_client.py
│   ├── yt_autocaptions.py        <- yt-dlp fallback: YouTube's own captions
│   ├── test_yt_transcript_client.py
│   └── test_yt_autocaptions.py
└── summarize/
    ├── summarize.py
    ├── llm_client.py
    ├── retry.py
    ├── chunking.py
    ├── mapreduce.py
    ├── document.py
    ├── language.py               <- SUMMARY_LANGUAGE: the {language_rule} every prompt carries
    ├── fontchoice.py             <- per-language PDF fonts, defaults, x-height size matching (+ CLI check)
    ├── promptnames.py            <- the five prompts, the old names' aliases, which read timed lines (stdlib only)
    ├── pdf.py                    <- markdown -> PDF (DESIGN.md)
    ├── framecrop.py              <- slide-region detection, blank frames
    ├── mathrender.py             <- LaTeX -> Computer Modern SVG
    ├── test_summarize_units.py
    ├── test_pdf_units.py
    └── prompts/
        ├── video.md              <- the default: talks, news, interviews
        ├── meeting.md
        ├── lecture.md            <- study sheet + the course-reference rules
        ├── tutorial.md
        ├── reality.md            <- reality-show episode recap: [mm:ss], quotes, results last
        ├── _merge.md             <- internal; leading _ keeps it off the menu
        └── _merge-reality.md     <- the reality prompt's own merge
```

## The web UI (`trigger_server.py`, `web/index.html`)

Settled 2026-09-27: **a page served by the existing trigger server** — stdlib
`http.server`, no framework, no new dependency, same bearer token — reachable
on **localhost or the Tailscale address only** (`MEETING_BOT_BIND`, see
"Running on a PC"). Run by pm2 (`./webui.sh on`), never at boot. The page itself is unauthenticated (it holds no
data); every `/api/*` call and `/trigger` carries the token, which the page
keeps in `localStorage`.

- `build_args(body)` turns a JSON body into `pipeline.sh` arguments, and is
  shared by `/trigger` and `/api/check`, so what was checked is what runs.
  `/api/check` is `pipeline.sh --dry-run` with those arguments. **Start** is
  only enabled for the exact form state that last passed a check.
- The "New Google Meet" tab triggers `{"new_meet": true}` and polls that
  trigger's log (`/api/log`) for the `New Google Meet: <link>` line
  `capture.py` prints — the log is the one place the link appears before any
  run id is known to the page. It also reads `Run: <id>` from the same log for
  the **End & stop recording** button (`/api/runs/<id>/stop` →
  `kill_meeting.sh --run-id`, which leaves through the UI as always).
- Run ids and log names are matched against strict regexes before they touch
  a path; nothing from a request reaches the filesystem otherwise.
- A meeting started from the command line detaches with its own
  `pipeline_*.log` in the same `logs/` directory; `LOG_NAME_RE` accepts both
  `trigger_*` and `pipeline_*`, so the UI can show either.
- `launch()` passes `MEETING_BOT_FOREGROUND=1`: the UI's child is already
  detached and logged, and pipeline.sh must not detach a second time.
- Both tabs carry the **Summary** fieldset (2026-09-29): style, "written
  in" (th/en), PDF font (repopulated from `/api/options` `fonts[lang]` when
  the language changes), and **Extra instructions**. `build_args` maps them
  to `--summary-language`, `--pdf-font`, `--instructions` (one argv item,
  never a shell). Since 2026-09-30 also **Save recording** / **Summarize
  from** (`record_media`, `summary_source`; preselected from `.env` via
  `/api/options` `default_record_media` / `default_summary_source`), with
  audio disabling the source select at "voice". The default prompt shown is `canonical_prompt_name` of
  `SUMMARY_PROMPT`, so an old name in `.env` still preselects.
- `/trigger`'s original body contract is unchanged (phone shortcuts keep
  working); `new_meet`, `clip`, `no_combine_pdf` and `playlist` were added.

### The page's design (2026-09-30)

The operator asked for "clearly toggle and input settings, with clear
warnings and tick symbols", DESIGN.md as the reference, and chose in a round
of questions:

- **Segmented button groups for 2-3-way choices** (written in, save
  recording, summarize from, spoken language) with a ✓ on the chosen one and
  🔒 when locked (audio only locks "Summarize from" at voice); **switches for
  on/off** (playlists, Markdown only, start over) with an ON/OFF word;
  dropdowns only for the long lists (style, with a one-line description of
  each; PDF font).
- **Live hints plus an automatic check.** Every field has a one-line
  ✓/⚠/✗/ℹ feedback slot (`fb-<id>`) filled as you type — *format only*
  (clip shape, jobs, a name with several inputs, a non-.md combine path,
  start over, playlists). ~900 ms after the last change `/api/check` runs
  the dry run, and **each input line gets ✓ (kind, new/resume, window) or
  ✗ (the pipeline's reason)**. A sequence number drops stale answers.
  Mapping lines to the plan (`mapPlan`): `badarg`/`extra` entries by the
  exact line, the rest in order; if the counts disagree (playlist
  expansion) the plan is listed as it came. A refusal before any input was
  looked at (bad `--clip`, font, jobs) marks the lines "not checked" and
  shows the pipeline's words. Start reads "Start N runs" and unlocks only
  for the exact form state that passed; warnings are repeated beside it.
- **DESIGN.md's palette and callouts**: navy top bar and numbered section
  banners, the five callout colours as the page's states (ok, info/running,
  warn, bad with the red frame, note). Dark mode kept (the page follows the
  system; tokens redefined under `prefers-color-scheme` and
  `[data-theme=dark]`). Body face Bai Jamjuree → Sarabun → system, code
  JetBrains Mono, as installed; a phone falls back to its own.
- **Runs**: cards with a status badge and **labelled stage pills**; a stage
  that never runs for that input (record on non-meetings, download on a
  meeting or a voice-only YouTube run, clip without a window, frames on
  voice only, summarize on a combine member) is hidden while pending
  (`expected()`; `run_summary` now carries `summary_source` and
  `combined_into` for it). The detail lists settings, output paths with
  Copy, and logs whose open/closed state survives the 10 s refresh.
- **Inline confirmation bars** (`confirmInline`) replace `confirm()` for
  Stop and End & stop recording; the refresh does not rebuild a detail that
  is waiting on one.
- The last tab is remembered in `localStorage` (a convenience; the page
  works without it).

## Planned: a Discord voice source (not built)

Researched 2026-09-23, confirmed "plan only" 2026-09-27. The operator's
decisions: **audio only** (bots cannot receive Go Live / camera video at all —
a user-account "self-bot" in Chrome would violate Discord's ToS, so video is
a hard wall, not a follow-up; `--resources` is the substitute for slides);
**one bot instance serving many servers** (not per-organisation installs);
**email only the summary PDF** (Gmail SMTP with an app password is the first
version; a recording is 50-500MB, over every provider's attachment limit, so
it is a DM to the operator when ready — Discord's bot upload limit is 25MiB,
enough for <~90 min of 32kbps Opus, with a Drive/Seafile link beyond that).
No second VM and no domain are needed: the gateway is outbound-only, and the
bot must share this host's `~/.claude` login, key cursor and queue.

The shape, when it is built:

- `discord_bot.py` (discord.py + `discord-ext-voice-recv`, or Pycord), a third
  pm2 app beside the web UI, enabled when `DISCORD_BOT_TOKEN` is set.
  Commands `/record`, `/summarize`, `/record-and-summarize`, `/stop`; an email
  modal; stops on `/stop` or when the channel empties. Posts "🔴 Recording
  started by @user" and renames itself while recording (bots can't show the
  red dot; PDPA and Discord's developer policy both want the notice).
- **The risky part is the sink.** Discord sends per-user Opus with nothing
  during silence and no reference clock; both libraries' silence padding is
  broken (Pycord's `sync_start` is ignored since 2.7; voice-recv's README says
  its silence generation is "pretty broken"). A custom sink must pad each
  user's stream by wall-clock arrival in 20ms frames, then ffmpeg `amix` the
  users into one `.ogg`. Needs a live 3-person test.
- **No new input type is needed in the pipeline.** The bot hands the mixed
  `.ogg` to `pipeline.sh` as a local file with `--prompt meeting-claude`;
  an audio-only file goes through `frames` as an empty manifest, and (since
  2026-09-30 — before that summarize.py refused an empty manifest) the
  summary is text-only; `--voice-only` skips frames outright. A per-user speaker map
  (Discord user per track) is free attribution — a v2, not v1.
- Delivery is a notify hook after `summarize`: SMTP (the PDF to the email the
  command collected) and a DM to the operator about the recording.
- Tests: unit tests for the sink's padding against synthetic packet timings;
  a `test_pipeline_e2e.sh` case for the hand-off; the live test by hand.

Estimated 4-5 working days. Recurring cost is AssemblyAI only (~$0.15-0.21
per meeting-hour).

## Things future Claude might want to add

- Auto-upload summaries to Slack / Notion / Obsidian after `pipeline.sh`.
- Real-time incremental summary during a meeting (needs a long-running agent;
  the pipeline is post-meeting only).
- Speaker diarization, so summaries can attribute quotes without inferring.
- A web UI for re-summarizing a past run with different settings.
- Slide-to-transcript alignment: match rendered slide images against extracted
  frames so the PDF can show the *source* slide rather than a screen capture of
  it.
