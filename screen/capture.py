#!/usr/bin/env python3
r"""
Screen-capture companion to record_screen.sh. Joins a Zoom or Google Meet call
in a real (headed, but Xvfb-hosted) Chromium window using a persistent logged-in
profile. Handles host-approval waiting rooms, and auto-leaves when the meeting
ends or most participants have left.

This is the Option 1 driver from the project split:
  - screen/record_screen.sh starts Xvfb + this script + ffmpeg-x11grab.
  - The MP4 is muxed by record_screen.sh once this script exits.
  - The kill sentinel is honored so kill_meeting.sh and Ctrl+\ in the
    recording terminal leave the meeting cleanly.

Sentinel location: when MEETING_BOT_RUN_DIR is set (pipeline.sh always sets
it), the kill/admitted sentinels live in that run's directory rather than in
/tmp. That's what lets two meetings record at once — a shared /tmp path means
killing one recording kills every recording. The /tmp paths remain the default
so a bare `python3 screen/capture.py <url>` still works.

Usage:
    python3 screen/capture.py "<meeting_url>" ["Display Name"]
"""
import sys
import os
import re
import time
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# The persistent Chrome profile, shared with first_time_login.sh. It lives
# under MEETING_BOT_ROOT (not a user's $HOME) so it survives reinstalls and is
# the same profile whichever script opens it.
_BOT_ROOT = os.environ.get("MEETING_BOT_ROOT", "/opt/meeting-bot")
PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR",
                             os.path.join(_BOT_ROOT, "chrome-profile"))
SCREENSHOT_DIR = os.environ.get("RECORDINGS_DIR",
                                os.path.join(_BOT_ROOT, "recordings"))

ADMIT_TIMEOUT_SECONDS = 600      # how long to wait in a waiting room before giving up
POLL_SECONDS = 15                # how often to check participant count / end state
LOW_COUNT_CONFIRMATIONS = 2      # consecutive low readings needed before auto-leaving
DROP_RATIO_THRESHOLD = 0.30      # leave if count falls below 30% of its peak
# Hard max-duration backstop. The mass-exit heuristic above is the primary
# auto-leave trigger; this timeout is the wall-clock safety net so the bot
# never gets stuck in a meeting forever. Override with MAX_MEETING_MINUTES in
# the environment (e.g. MAX_MEETING_MINUTES=120 for a 2-hour cap).
MAX_MEETING_SECONDS = int(os.environ.get("MAX_MEETING_MINUTES", "240")) * 60
# Idle auto-leave: if the meeting has been at "only me + bot" (count==2)
# or "only the bot" (count==1) for IDLE_LEAVE_SECONDS, leave cleanly. This
# catches the "test call with just me" case that the mass-exit rule misses
# (peak is 2, so 30% of peak is 0 — never triggers). Set to 0 to disable.
IDLE_LEAVE_SECONDS = int(os.environ.get("IDLE_LEAVE_MINUTES", "5")) * 60
# Sentinels. Per-run when MEETING_BOT_RUN_DIR is set (the pipeline always sets
# it), so concurrent recordings don't share a kill switch; /tmp otherwise, which
# keeps a standalone `python3 screen/capture.py <url>` working as before.
# Chrome's window has to match the Xvfb head exactly or the recording gets
# black edges; record_screen.sh sets RECORD_GEOMETRY for both.
WINDOW_SIZE = (os.environ.get("RECORD_GEOMETRY", "1920x1080")
               .lower().replace("x", ","))
# The Chrome command line, in one place so screen/browser_smoke.py can launch
# an identical browser without a live meeting — a flag that breaks recording
# should break the smoke test too.
#
# --kiosk launches Chrome in true fullscreen with no chrome (no tab bar, no
# address bar, no system UI). The window fills the Xvfb head, so ffmpeg's
# x11grab captures only the meeting UI - no black edges. --kiosk also disables
# F11-toggle, which keeps the unattended recording from accidentally leaving
# fullscreen. --window-size pins the drawable area to the head size; without it
# some Xvfb/Chrome combinations leave a few px of margin on the right and
# bottom (Chrome auto-sizes the window slightly smaller than the display in
# --kiosk mode). The matching Xvfb geometry and ffmpeg -video_size come from
# RECORD_GEOMETRY in screen/record_screen.sh.
#
# --no-sandbox is required because the pipeline runs as root; without it Chrome
# aborts with "Running as root without --no-sandbox is not supported" before
# the page ever loads.
#
# --disable-features=ScreenCapture is layer 1 of the screen-share defense: it
# disables the getDisplayMedia API entirely. The bot has no legitimate reason
# to share its screen; if Chrome ever renames this feature flag, layers 2 and 3
# (in wait_until_meeting_ends) are the runtime catch-nets.
CHROME_ARGS = [
    "--kiosk",
    f"--window-size={WINDOW_SIZE}",
    # Without this Chrome places its window at (10,10) even in kiosk mode, and
    # every recording gets a 10px black band down the left and top edges —
    # found by verify_e2e.sh --browser-smoke, which measures the captured
    # frame rather than trusting the window size.
    "--window-position=0,0",
    "--no-sandbox",
    "--use-fake-ui-for-media-stream",
    "--disable-features=ScreenCapture",
]

RUN_DIR = os.environ.get("MEETING_BOT_RUN_DIR", "")
if RUN_DIR:
    ADMITTED_MARKER = os.path.join(RUN_DIR, "admitted")
    KILL_SENTINEL = os.path.join(RUN_DIR, "kill")
    # Failed-join screenshots belong with the run they came from, not in a
    # shared directory where the next run silently overwrites them.
    SCREENSHOT_DIR = RUN_DIR
else:
    ADMITTED_MARKER = "/tmp/meeting_bot_admitted"  # touched once inside the call
    # When this appears, the bot leaves the meeting cleanly and exits. Touched
    # by record_screen.sh's signal trap (Ctrl+\) or by kill_meeting.sh.
    KILL_SENTINEL = "/tmp/meeting_bot_kill"


def kill_requested():
    r"""True when an external signal (Ctrl+\ or kill_meeting.sh) asked us to stop."""
    return os.path.exists(KILL_SENTINEL)


def click_first_match(page, labels, timeout=3000):
    for label in labels:
        try:
            btn = page.get_by_role("button", name=label)
            if btn.is_visible(timeout=timeout):
                btn.click()
                print(f"Clicked '{label}'")
                return True
        except PWTimeout:
            continue
    return False


def go_fullscreen(page):
    """Kept as a no-op for backward compatibility with older callers.

    Chrome is now launched with --kiosk, which already puts the window in
    true fullscreen from the start. Pressing F11 at runtime would TOGGLE
    Chrome out of --kiosk fullscreen (back to a windowed state), and the
    Fullscreen API is redundant when --kiosk is in effect, so both are
    harmful. The function remains so external callers (and any future
    test imports) don't break.
    """


def prejoin_mute_and_join_google_meet(page, display_name):
    """Tab through Meet's pre-join screen, identifying buttons by accessible name.

    Strategy:
      1) Fill the display-name field (same selector as the legacy flow).
      2) Press Tab once, then read document.activeElement's accessible name
         (aria-label or innerText) and check it against our label sets.
      3) When we identify one of the target buttons, press Enter to click
         it (the pre-join buttons are real <button>s, so Enter activates).
      4) Stop once the Join button has been clicked, OR after MAX_TABS.

    Why Tab-scan and not fixed Tab counts: Meet reorders the pre-join DOM
    frequently. Identifying by accessible name is the only durable approach.
    See "Things future Claude MUST NOT change" in CLAUDE.md.

    English + Thai label set, same convention as the rest of capture.py:
      - Camera off / already off: "Turn off camera" / "Turn on camera" /
        "Camera is off" / "ปิดกล้อง" / "เปิดกล้อง"
      - Mic off / already off: "Mute microphone" / "Unmute" /
        "Microphone is off" / "ปิดไมโครโฟน" / "เปิดไมโครโฟน"
      - Join: "Join now" / "Ask to join" / "ขอเข้าร่วม" / "เข้าร่วมเลย" /
        "เข้าร่วมตอนนี้"

    Best-effort: a miss is a warning, not a hard failure. The post-admission
    mute_av() is the safety net for camera/mic; the join click falls back to
    the click_first_match path in join_google_meet() on a False return.

    Returns True if Join was clicked via Tab, False otherwise.
    """
    try:
        name_field = page.locator("input[type='text']").first
        if name_field.is_visible(timeout=2000):
            name_field.fill(display_name)
    except PWTimeout:
        pass

    camera_off = {"Turn off camera", "ปิดกล้อง"}
    camera_already_off = {"Turn on camera", "เปิดกล้อง", "Camera is off"}
    mic_off = {"Mute microphone", "ปิดไมโครโฟน"}
    mic_already_off = {"Unmute", "เปิดไมโครโฟน", "Microphone is off"}
    join_labels = {
        "Join now", "Ask to join",
        "ขอเข้าร่วม", "เข้าร่วมเลย", "เข้าร่วมตอนนี้",
    }
    handled = set()  # kinds ("camera" / "microphone") we've already handled

    MAX_TABS = 20
    for _ in range(MAX_TABS):
        try:
            focused_name = page.evaluate(
                "() => (document.activeElement && ("
                "  document.activeElement.getAttribute('aria-label') || "
                "  document.activeElement.innerText || ''"
                ")).trim()"
            ) or ""
            # Collapse whitespace so multi-line innerText still matches.
            focused_name = " ".join(focused_name.split())

            if focused_name in camera_already_off and "camera" not in handled:
                print("  Pre-join: camera already off (Tab scan).")
                handled.add("camera")
            elif focused_name in camera_off and "camera" not in handled:
                page.keyboard.press("Enter")
                print("  Pre-join: clicked camera-off (Tab scan).")
                handled.add("camera")
            elif focused_name in mic_already_off and "microphone" not in handled:
                print("  Pre-join: mic already off (Tab scan).")
                handled.add("microphone")
            elif focused_name in mic_off and "microphone" not in handled:
                page.keyboard.press("Enter")
                print("  Pre-join: clicked mic-off (Tab scan).")
                handled.add("microphone")
            elif focused_name in join_labels:
                page.keyboard.press("Enter")
                print("  Pre-join: clicked Join (Tab scan).")
                return True
        except Exception:
            # Element went away or evaluate failed; just keep Tabbing.
            pass

        page.keyboard.press("Tab")
        # Brief settle so the focused element has time to update.
        time.sleep(0.05)

    print(
        "  WARNING: pre-join Tab scan reached MAX_TABS without seeing "
        "Join. Falling back to click_first_match."
    )
    return False


def join_google_meet(page, url, display_name):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    # Diagnostic: log the actual viewport / screen size. If --window-size
    # plus --kiosk aren't matching the Xvfb head (1920x1080), this prints
    # the real numbers so we can see why black borders appear in the
    # recording. Best-effort: a failure here just logs a warning.
    try:
        sizes = page.evaluate(
            "() => ({"
            "  inner: { w: window.innerWidth, h: window.innerHeight },"
            "  screen: { w: screen.width, h: screen.height }"
            "})"
        )
        print(
            f"Viewport diagnostic: window.inner={sizes['inner']['w']}x"
            f"{sizes['inner']['h']}, screen={sizes['screen']['w']}x"
            f"{sizes['screen']['h']}"
        )
    except Exception as e:
        print(f"WARNING: viewport diagnostic failed ({e}) - continuing.")
    # Go fullscreen BEFORE any click, per user request. Fills the Xvfb
    # display so participants see the same layout a human would.
    go_fullscreen(page)
    # Tab-scan pre-join: fill name, mute camera+mic, click Join by accessible
    # name. Falls through to the aria-label click below if the scan didn't
    # see Join (e.g. lobby screen, unusual DOM order).
    if prejoin_mute_and_join_google_meet(page, display_name):
        return True
    # Meet's own UI is in whichever language locale="th-TH" forces it to. Since
    # we pass that locale (so Thai participant names render correctly in the
    # chat), we have to know BOTH the English and Thai button labels - the
    # English ones are kept as a safety net for when locale ever falls back.
    return click_first_match(
        page,
        [
            "Join now", "Ask to join",               # English
            "ขอเข้าร่วม", "เข้าร่วมเลย", "เข้าร่วมตอนนี้",  # Thai
        ],
        timeout=4000,
    )


def join_zoom(page, url, display_name):
    if "zoom.us/wc/" not in url and "/j/" in url:
        url = url.replace("/j/", "/wc/join/")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    click_first_match(page, ["I Agree", "Accept Cookies", "OK"], timeout=1500)
    try:
        name_field = page.locator("#inputname, input[type='text']").first
        if name_field.is_visible(timeout=4000):
            name_field.fill(display_name)
    except PWTimeout:
        pass
    return click_first_match(page, ["Join", "Join from Your Browser"], timeout=4000)


def is_admitted(page):
    """True once we're actually inside the call (not a waiting/lobby screen)."""
    # English + Thai: see join_google_meet() for why both.
    admitted_markers = [
        "Leave call", "Leave meeting", "Leave", "End",  # English
        "ออกจากการโทร", "ออกจากการประชุม",                  # Thai
    ]
    for label in admitted_markers:
        try:
            if page.get_by_role("button", name=label).is_visible(timeout=1000):
                return True
        except PWTimeout:
            continue
    return False


def is_waiting_for_admission(page):
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return False
    # Substring match against body text. .lower() doesn't affect Thai chars,
    # so Thai phrases work as-is.
    waiting_phrases = [
        "waiting for the host", "will let you in soon",
        "someone will let you in", "please wait", "ask to join",
        "กำลังรอ", "ขอเข้าร่วม",  # Thai: "waiting", "ask to join"
    ]
    return any(p in text for p in waiting_phrases)


def join_rejection_reason(page):
    """Return a human-readable reason if the page is a terminal refusal.

    These are the pages where waiting cannot help: the meeting code is bad,
    the organizer isn't there to admit anyone, the room is locked, the
    request to join was declined. Detecting them is what lets an
    unconfirmed join click fall through to wait_for_admission() safely —
    the only cost of waiting is time, and here we know waiting is futile.

    English + Thai, same convention as the rest of capture.py.
    """
    try:
        text = " ".join(page.inner_text("body").split()).lower()
    except Exception:
        return None
    # (substring to match, message to print)
    rejections = [
        ("you can't join this video call",
         "Google Meet refused the join: nobody can enter unless the "
         "organizer is in the call or the bot's account was invited."),
        ("คุณไม่สามารถเข้าร่วม",
         "Google Meet refused the join (Thai UI): nobody can enter unless "
         "the organizer is in the call or the bot's account was invited."),
        ("check your meeting code",
         "Google Meet rejected the meeting code."),
        ("ตรวจสอบรหัสการประชุม",
         "Google Meet rejected the meeting code (Thai UI)."),
        ("no one responded to your request",
         "Nobody admitted the bot from the waiting room."),
        ("ไม่มีใครตอบรับคำขอ",
         "Nobody admitted the bot from the waiting room (Thai UI)."),
        ("you can't join this meeting",
         "The meeting refused the join."),
        ("invalid meeting id",
         "Zoom rejected the meeting ID."),
        ("this meeting has been locked",
         "The Zoom meeting is locked."),
        ("meeting has been ended",
         "The meeting has already ended."),
        ("this meeting id is not valid",
         "Zoom rejected the meeting ID."),
        ("removed you from the meeting",
         "The bot was removed from the meeting."),
    ]
    for needle, message in rejections:
        if needle in text:
            return message
    return None


def wait_for_admission(page, timeout_seconds=ADMIT_TIMEOUT_SECONDS):
    print("Waiting for host approval (if a waiting room applies)...")
    start = time.time()
    while time.time() - start < timeout_seconds:
        if kill_requested():
            print("Kill signal received - abandoning wait and exiting.")
            return False
        if is_admitted(page):
            print("Admitted into the meeting.")
            return True
        # A refusal can also arrive mid-wait ("no one responded to your
        # request"). Waiting out the full timeout on those wastes ten
        # minutes and buries the actual reason.
        reason = join_rejection_reason(page)
        if reason:
            print(f"Cannot join: {reason}")
            return False
        time.sleep(5)
    print(f"Not admitted within {timeout_seconds}s - giving up.")
    return False


def get_participant_count(page):
    """Best-effort scrape of the participant count from Zoom/Meet UI.

    Tries a targeted selector first (Google Meet's top-right chip,
    ``div.fs3avc`` in the th-TH locale — a class Google rotates, so we keep
    the body-text regexes as a fallback for when the class name changes or
    the selector doesn't render).
    """
    # Targeted: the participant-count chip in Google Meet's top-right corner.
    try:
        chip = page.locator("div.fs3avc").first
        if chip.is_visible(timeout=500):
            txt = (chip.inner_text() or "").strip()
            if txt.isdigit():
                return int(txt)
    except Exception:
        pass

    # Fallback: scan the body text for "N participants" / "Participants (N)".
    try:
        text = page.inner_text("body")
    except Exception:
        return None
    patterns = [
        r'(\d+)\s*participants?',
        r'Participants\s*\((\d+)\)',
        r'People\s*\((\d+)\)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return int(m.group(1))
    return None


def leave_meeting(page):
    print("Leaving the meeting.")
    click_first_match(
        page,
        [
            "Leave call", "Leave meeting", "Leave", "End",  # English
            "ออกจากการโทร", "ออกจากการประชุม",                 # Thai
        ],
        timeout=3000,
    )
    # Confirm dialogs sometimes follow (e.g. "Leave meeting" -> confirm "Leave")
    time.sleep(1)
    click_first_match(
        page,
        [
            "Leave meeting", "Leave",        # English
            "ออกจากการประชุม", "ออกจากการโทร",  # Thai
        ],
        timeout=2000,
    )


def mute_av(page, platform):
    """Turn off camera and microphone so other participants can't see/hear the bot.

    Strategy:
      1) Try keyboard shortcuts (Meet: Ctrl+E / Ctrl+D, Zoom: Alt+V / Alt+A).
         The user explicitly suggested these as a fast path.
      2) Fall back to clicking aria-label buttons: "Turn off camera" /
         "Mute microphone" (also accept the inverse "Turn on camera" /
         "Unmute" — if those are showing, the device is already off).

    Best-effort: on any failure we log a warning and continue. Failing the
    recording because the UI heuristic missed would block real meetings; the
    bot's screen capture is the real product here.
    """
    if platform == "google_meet":
        shortcuts = [("camera", "Control+e"), ("microphone", "Control+d")]
        # English + Thai button labels.
        # NOTE: Meet's already-off buttons are "Turn on camera" / "Unmute" —
        # if we see those, the device is already off (we leave it alone).
        camera_off = ["Turn off camera", "ปิดกล้อง"]
        mic_off = ["Mute microphone", "ปิดไมโครโฟน"]
        camera_already_off = ["Turn on camera", "เปิดกล้อง", "Camera is off"]
        mic_already_off = ["Unmute", "เปิดไมโครโฟน", "Microphone is off"]
    elif platform == "zoom":
        shortcuts = [("camera", "Alt+v"), ("microphone", "Alt+a")]
        camera_off = ["Stop video", "Mute video"]
        mic_off = ["Mute", "Mute microphone"]
        camera_already_off = ["Start video"]
        mic_already_off = ["Unmute"]
    else:
        print(f"WARNING: unknown platform {platform!r} - skipping mute.")
        return

    print(f"Muting camera + mic on {platform}...")
    for kind, shortcut in shortcuts:
        try:
            page.keyboard.press(shortcut)
            print(f"  Tried shortcut {shortcut} for {kind}.")
        except Exception as e:
            print(f"  Shortcut {shortcut} for {kind} failed: {e}")

    # Brief settle, then verify + fall back to clicking aria-labels.
    time.sleep(1)
    for kind, off_labels, already_off_labels in [
        ("camera", camera_off, camera_already_off),
        ("microphone", mic_off, mic_already_off),
    ]:
        # If the already-off label is visible, we're done for this device.
        if click_first_match(page, already_off_labels, timeout=800):
            print(f"  {kind} already off (via already-off label).")
            continue
        if click_first_match(page, off_labels, timeout=1500):
            print(f"  Clicked {kind} off (via aria-label).")
            continue
        print(f"  WARNING: could not confirm {kind} is off - check the recording.")


def block_screen_share_dialog(page):
    """Layer 2 of the screen-share defense.

    Chrome's native permission dialog ("Allow [site] to see your screen?")
    could in principle appear if a malicious page bypasses Layer 1. This
    layer kills it by clicking any button whose label contains 'Block'.
    """
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return False
    share_phrases = [
        "see your screen", "share your screen",
        "want to share", "screen share",
        # Common non-English versions we might encounter; substring match.
    ]
    if not any(p in text for p in share_phrases):
        return False

    # Scan all visible buttons for a "Block" label and click the first.
    try:
        buttons = page.get_by_role("button").all()
    except Exception:
        return False
    for btn in buttons:
        try:
            label = (btn.inner_text() or "").strip().lower()
            if "block" in label or "deny" in label or "cancel" in label:
                btn.click()
                print("WARNING: screen-share permission dialog appeared - clicked Block.")
                return True
        except Exception:
            continue
    return False


def stop_unwanted_presenting(page):
    """Layer 3 of the screen-share defense.

    Even with the Chrome flag set (Layer 1) and dialogs killed (Layer 2),
    some page-level actions could trigger the in-Meet "Stop presenting"
    banner. If we see it, click it and log a loud warning so the user can
    investigate the underlying cause.
    """
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return False
    presenting_phrases = [
        "you are presenting", "stop presenting",
        "หยุดนำเสนอ",  # Thai: stop presenting
    ]
    if not any(p in text for p in presenting_phrases):
        return False

    # Try the aria-label button first; fall back to text-based click.
    if click_first_match(page, ["Stop presenting", "หยุดนำเสนอ"], timeout=1000):
        print("WARNING: Bot accidentally started presenting - clicked Stop presenting.")
        return True
    return False


def wait_until_meeting_ends(page, poll_seconds=POLL_SECONDS):
    print("In meeting. Monitoring participant count and end state...")
    peak_count = None
    low_streak = 0
    idle_since_ts = None    # first poll at which count was in (1, 2)
    start_ts = time.time()  # for the hard max-duration timeout backstop

    while True:
        try:
            time.sleep(poll_seconds)

            if kill_requested():
                print("Kill signal received - leaving the meeting cleanly.")
                try:
                    leave_meeting(page)
                except Exception as e:
                    print(f"Clean leave failed ({e}) - exiting anyway.")
                return

            if page.is_closed():
                print("Page closed - meeting ended.")
                return

            title = page.title().lower()
            if any(k in title for k in ["meeting has ended", "call ended", "left the meeting"]):
                print(f"Detected end via page title: {title}")
                return

            # Hard max-duration timeout. Runs AFTER the kill/page/title checks
            # (which are quicker + always-fatal) but BEFORE the participant
            # count check (which can be slow on busy meetings).
            elapsed = time.time() - start_ts
            if elapsed > MAX_MEETING_SECONDS:
                minutes = int(elapsed // 60)
                cap = MAX_MEETING_SECONDS // 60
                print(f"Hard timeout reached ({minutes}m elapsed, cap {cap}m) - leaving.")
                try:
                    leave_meeting(page)
                except Exception as e:
                    print(f"Clean leave failed ({e}) - exiting anyway.")
                return

            # Screen-share defenses (Layers 2 and 3). Layer 1 is the Chrome
            # flag set at launch; these two are the runtime catch-nets.
            block_screen_share_dialog(page)
            stop_unwanted_presenting(page)

            count = get_participant_count(page)
            if count is not None:
                peak_count = count if peak_count is None else max(peak_count, count)
                is_alone = count <= 1
                is_mass_exodus = peak_count and count <= max(1, int(peak_count * DROP_RATIO_THRESHOLD))

                # Idle auto-leave: only the bot (count==1) or only the bot
                # + one other person (count==2) for IDLE_LEAVE_SECONDS.
                # Independent of mass-exit; both can fire on the same call.
                if IDLE_LEAVE_SECONDS > 0 and count in (1, 2):
                    if idle_since_ts is None:
                        idle_since_ts = time.time()
                    elif time.time() - idle_since_ts >= IDLE_LEAVE_SECONDS:
                        mins = int((time.time() - idle_since_ts) // 60)
                        print(
                            f"Idle threshold reached ({mins}m, count={count}, "
                            f"peak={peak_count}) - leaving."
                        )
                        leave_meeting(page)
                        return
                else:
                    idle_since_ts = None

                if is_alone or is_mass_exodus:
                    low_streak += 1
                    print(f"Low participant count ({count}, peak {peak_count}) - streak {low_streak}")
                else:
                    low_streak = 0

                if low_streak >= LOW_COUNT_CONFIRMATIONS:
                    print("Confirmed most/all participants have left - leaving.")
                    leave_meeting(page)
                    return

        except KeyboardInterrupt:
            print("Manual stop.")
            return
        except Exception as e:
            print(f"Page unreachable ({e}) - assuming meeting ended.")
            return


def main():
    if len(sys.argv) < 2:
        print("Usage: capture.py <meeting_url> [display_name]")
        sys.exit(1)

    url = sys.argv[1]
    display_name = sys.argv[2] if len(sys.argv) > 2 else "Meeting Bot"
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    if os.path.exists(ADMITTED_MARKER):
        os.remove(ADMITTED_MARKER)
    # Clear any stale kill sentinel left over from a previous run that may
    # have been killed before it could clean up.
    if os.path.exists(KILL_SENTINEL):
        os.remove(KILL_SENTINEL)

    # Chrome's SingletonLock encodes the hostname and pid of whoever last held
    # the profile. A run killed before its cleanup ran (SIGKILL, reboot,
    # Ctrl+C racing the trap) leaves one behind, and the next Chrome refuses to
    # launch at all — "the profile appears to be in use by another computer".
    # Any lock still present here is stale by definition: this process is about
    # to be the only user of the profile.
    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = os.path.join(PROFILE_DIR, lock_name)
        if os.path.exists(lock_path) or os.path.islink(lock_path):
            os.remove(lock_path)

    with sync_playwright() as p:
        # channel="chrome" forces real Google Chrome (installed by setup.sh)
        # rather than Playwright's bundled unbranded Chromium. Google blocks
        # sign-in on the unbranded build ("This browser or app may not be
        # secure"), and Meet itself is more reliable on the real browser.
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            channel="chrome",
            # --no-sandbox is required because record_screen.sh runs
            # the whole pipeline as root (sudo -H, matching setup.sh). Without
            # it, Chrome aborts with "Running as root without --no-sandbox is
            # not supported" before the page ever loads.
            args=CHROME_ARGS,
            permissions=["camera", "microphone"],
            no_viewport=True,
            locale="th-TH",  # renders Thai participant names/chat correctly
        )
        page = context.new_page()

        if "meet.google.com" in url:
            clicked = join_google_meet(page, url, display_name)
        elif "zoom.us" in url:
            clicked = join_zoom(page, url, display_name)
        else:
            print("Unrecognized meeting URL (expected zoom.us or meet.google.com)")
            clicked = False

        if not clicked:
            # An unconfirmed click is NOT the same as a failed join. Zoom's
            # web client swallows the button behind its "Joining Meeting..."
            # interstitial, so click_first_match times out while the join is
            # in fact under way; exiting here abandoned a call we were about
            # to be in. Only a terminal refusal page is fatal — anything
            # else falls through to wait_for_admission(), which already has
            # its own timeout and screenshot.
            page.screenshot(path=os.path.join(SCREENSHOT_DIR, "join_failed.png"))
            reason = join_rejection_reason(page)
            if reason:
                print(f"Cannot join: {reason} (screenshot saved)")
                context.close()
                return
            print(
                "Could not confirm the join click, but the page shows no "
                "refusal - screenshot saved, waiting for admission anyway."
            )

        if not wait_for_admission(page):
            page.screenshot(path=os.path.join(SCREENSHOT_DIR, "not_admitted.png"))
            context.close()
            return

        # Mute camera + mic before recording starts so other participants
        # don't see/hear the bot. We do this AFTER admission (otherwise we
        # haven't necessarily reached the in-call UI yet) and BEFORE touching
        # the admitted marker (which signal record_screen.sh to start ffmpeg).
        # Best-effort: a failure here just logs a warning and continues.
        platform = "google_meet" if "meet.google.com" in url else (
            "zoom" if "zoom.us" in url else "unknown"
        )
        try:
            mute_av(page, platform)
        except Exception as e:
            print(f"WARNING: mute_av raised {e} - continuing anyway.")

        # Signal the orchestrator (record_screen.sh) that it's safe to start
        # recording now - we're actually in the call, not a lobby.
        with open(ADMITTED_MARKER, "w") as f:
            f.write(str(time.time()))

        try:
            wait_until_meeting_ends(page)
        finally:
            if os.path.exists(ADMITTED_MARKER):
                os.remove(ADMITTED_MARKER)
        context.close()


if __name__ == "__main__":
    main()