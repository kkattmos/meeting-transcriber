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

It can also HOST: given "meet.new" (or https://meet.new) as the URL, it creates
a new Google Meet in the signed-in profile, announces the link (stdout, the
run dir's meet_url file, and state.json), admits everyone who knocks, and ends
the call for everyone once they have all left — see host_* below.

Usage:
    python3 screen/capture.py "<meeting_url>" ["Display Name"]
    python3 screen/capture.py meet.new ["Display Name"]
"""
import json
import subprocess
import sys
import os
import re
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser  # noqa: E402  (screen/browser.py: which browser, how launched)

# Playwright's TimeoutError and the Firefox adapter's, as one `except` target.
PWTimeout = browser.timeout_errors()

# The persistent browser profile, shared with first_time_login.sh. It lives
# under MEETING_BOT_ROOT so it survives reinstalls and is the same profile
# whichever script opens it. One per browser (MEETING_BROWSER).
_BOT_ROOT = os.environ.get("MEETING_BOT_ROOT", os.path.expanduser("~/.local/share/meeting-bot"))
PROFILE_DIR = browser.profile_dir()
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
# The softer auto-leave rules (idle with one other person, and "most people
# have left") only fire once the meeting has also been silent this long —
# settled with the operator 2026-09-29, so a 1:1 or the end of a lecture with
# a few students left isn't cut off while someone is still talking.
AUTO_LEAVE_SILENCE_SECONDS = int(os.environ.get("AUTO_LEAVE_SILENCE_SECONDS", "120"))
# A meeting the bot created itself: how long to hold it open for the first
# participant before ending it. Once somebody has joined, the ordinary
# auto-leave rules take over.
NEW_MEET_WAIT_SECONDS = int(os.environ.get("NEW_MEET_WAIT_MINUTES", "15")) * 60
# As host, how often to look for people knocking. Much shorter than
# POLL_SECONDS: a knocker left waiting 15s assumes nobody is there.
HOST_ADMIT_POLL_SECONDS = 3
NEW_MEET_RE = re.compile(r"^(https?://)?meet\.new/?$", re.I)
MEET_LINK_RE = re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}")
# Sentinels. Per-run when MEETING_BOT_RUN_DIR is set (the pipeline always sets
# it), so concurrent recordings don't share a kill switch; /tmp otherwise, which
# keeps a standalone `python3 screen/capture.py <url>` working as before.
# The browser window has to match the Xvfb head exactly or the recording gets
# black edges; record_screen.sh sets RECORD_GEOMETRY for both. The command
# line lives in screen/browser.py (CHROME_ARGS / FIREFOX_PREFS) and is
# re-exported here for screen/browser_smoke.py, so a flag that breaks
# recording breaks the smoke test too.
CHROME_ARGS = browser.CHROME_ARGS

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


def click_first_match(page, labels, timeout=3000, exact=False):
    # exact=True for short labels that are a substring of something else — the
    # Thai "ปิด" (Close) is the start of "ปิดกล้อง" (turn off camera).
    for label in labels:
        try:
            btn = page.get_by_role("button", name=label, exact=exact)
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


def join_google_meet(page, url, display_name, navigate=True):
    # navigate=False when host_create_google_meet has already landed on this
    # meeting's pre-join page; loading it a second time only costs seconds.
    if navigate:
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


# --- Hosting a meeting the bot created (meet.new) ----------------------------
# The bot's own Google account owns the call, so there is no organizer to wait
# for and nobody to admit the bot — but everybody else now knocks on the bot.
# All of these labels are English + Thai like the rest of this file. The Thai
# admit/end labels are best-effort and are the first thing to check against a
# live call if knockers are left waiting.

# Exact matches: "Admit" would otherwise match any button containing the word.
HOST_ADMIT_LABELS = [
    "Admit all", "Admit",
    "ยอมรับทั้งหมด", "ยอมรับ", "อนุญาตทั้งหมด", "อนุญาต", "รับเข้าทั้งหมด", "รับเข้า",
]
# Several people knocking at once collapse into "View all"; the Admit all
# button is inside the panel that opens.
HOST_VIEW_ALL_LABELS = ["View all", "ดูทั้งหมด"]
# The green chip Meet shows the host at the top right while someone waits.
# Its label carries the count ("ยอมรับผู้เข้าร่วม 1 คน" — "Admit 1
# participant", verified live 2026-09-29), so it can only be matched as a
# substring; clicking it opens the panel with the per-person Admit buttons.
# Missing this chip is why the first live hosted call left its guest waiting.
HOST_WAITING_CHIP_LABELS = [
    "ยอมรับผู้เข้าร่วม", "Admit 1", "Admit 2", "Admit 3", "Admit guest",
    "Admit people", "people waiting", "someone wants to join",
]
# Inside the panel, each knocker gets their own button whose label is the verb
# PLUS the person's name — "ยอมรับ 03_ด.ช. ขัตติยะ …" (verified live
# 2026-09-29), so neither an exact nor a substring match on "ยอมรับ" works
# (the substring also hits "อยู่ระหว่างรอการยอมรับ 1", the waiting-list
# toggle). Matched by prefix-with-a-space in the page instead; the chip
# ("ยอมรับผู้เข้าร่วม…", "Admit 1 guest") is excluded by the second pattern.
_ADMIT_PERSON_JS = r"""() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const isPerson = n => /^(ยอมรับ|Admit) \S/.test(n) && !/^Admit \d/.test(n)
                        && !/^(Admit all|ยอมรับทั้งหมด)/.test(n);
  const btn = Array.from(document.querySelectorAll('button,[role=button]'))
    .filter(b => b.getBoundingClientRect().width > 0)
    .find(b => isPerson(norm(b.getAttribute('aria-label') || b.innerText)));
  if (!btn) return null;
  btn.click();
  return norm(btn.getAttribute('aria-label') || btn.innerText);
}"""

# Notices that sit over the call and should just be acknowledged.
HOST_DISMISS_LABELS = ["Got it", "Dismiss", "รับทราบ"]
HOST_END_FOR_ALL_LABELS = [
    "End the call for everyone", "End call for everyone", "End call for all",
    "สิ้นสุดการโทรสำหรับทุกคน", "วางสายสำหรับทุกคน", "ปิดการโทรสำหรับทุกคน",
]


def is_new_meet(url):
    return bool(NEW_MEET_RE.match(url.strip()))


def announce_meet_link(link):
    """Make the new link findable everywhere the operator might look.

    stdout (the stage log and the terminal), a meet_url file in the run dir
    (what a web UI or a script can poll), and state.json (what --status
    shows, and what run_one.sh cites as the document's source). Each is
    best-effort: failing to record the link must not end a call that exists.
    """
    print("=" * 66)
    print(f"  New Google Meet: {link}")
    print("  Share this link. The bot admits everyone who asks to join.")
    print("=" * 66, flush=True)
    if not RUN_DIR:
        return
    try:
        with open(os.path.join(RUN_DIR, "meet_url"), "w") as fh:
            fh.write(link + "\n")
    except OSError as e:
        print(f"WARNING: could not write meet_url ({e})")
    runstate = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "lib", "runstate.py")
    try:
        subprocess.run([sys.executable, runstate, "init", "--run-dir", RUN_DIR,
                        "--meet-url", link], check=True, timeout=30)
    except Exception as e:
        print(f"WARNING: could not store the link in state.json ({e})")


def host_create_google_meet(page):
    """Open meet.new and return the new meeting's link, or None.

    meet.new redirects a signed-in account straight to a fresh
    meet.google.com/xxx-xxxx-xxx pre-join page. A signed-out profile lands on
    accounts.google.com instead, which is the one failure worth naming.
    """
    print("Creating a new Google Meet (meet.new)...")
    page.goto(browser.with_authuser("https://meet.google.com/new",
                                    browser.expected_account())
              if browser.expected_account() else "https://meet.new",
              wait_until="domcontentloaded")
    try:
        page.wait_for_url(lambda u: bool(MEET_LINK_RE.search(u)), timeout=45000)
    except PWTimeout:
        if "accounts.google.com" in page.url:
            print("Cannot create a meeting: the Chrome profile is not signed "
                  "into Google. Run ./first_time_login.sh and sign in first.")
        else:
            print(f"meet.new did not produce a meeting link (landed on {page.url}).")
        return None
    link = MEET_LINK_RE.search(page.url).group(0)
    announce_meet_link(link)
    return link


def host_dismiss_ready_dialog(page):
    """Close the "Your meeting's ready" card that covers part of the call."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    click_first_match(page, ["Close", "ปิด"], timeout=1500, exact=True)
    # "Use Meet safely" and similar notices ("รับทราบ" = Got it).
    click_first_match(page, HOST_DISMISS_LABELS, timeout=1500, exact=True)


_ADMIT_DIAGNOSED = False


def _log_admit_candidates(page):
    """Once per call: name every visible button that looks like an admit
    control, and save a screenshot — the data needed when Meet renames one."""
    global _ADMIT_DIAGNOSED
    if _ADMIT_DIAGNOSED:
        return
    _ADMIT_DIAGNOSED = True
    try:
        names = page.evaluate(
            "() => Array.from(document.querySelectorAll('button,[role=button]'))"
            ".filter(b => b.getBoundingClientRect().width > 0)"
            ".map(b => (b.getAttribute('aria-label') || b.innerText || '').replace(/\\s+/g, ' ').trim())"
            ".filter(n => /admit|ยอมรับ|อนุญาต|รับเข้า|wait|รอ/i.test(n))") or []
        print(f"  Admit diagnostics: candidate buttons {names!r}")
        if RUN_DIR:
            page.screenshot(path=os.path.join(RUN_DIR, "host_admit.png"))
    except Exception as e:
        print(f"  Admit diagnostics failed ({e})")


def host_admit_waiting(page):
    """Admit anyone knocking. Returns True if a button was clicked."""
    def click_visible(labels):
        for label in labels:
            try:
                btn = page.get_by_role("button", name=label, exact=True).first
                if btn.is_visible():
                    btn.click()
                    return label
            except Exception:
                continue
        return None

    def click_containing(labels):
        for label in labels:
            try:
                btn = page.get_by_role("button", name=label, exact=False).first
                if btn.is_visible():
                    btn.click()
                    return label
            except Exception:
                continue
        return None

    def click_person():
        try:
            return page.evaluate(_ADMIT_PERSON_JS)
        except Exception:
            return None

    # The panel may already be open from the previous poll — clicking the chip
    # again would close it — so try the buttons inside it first.
    clicked = click_visible(HOST_ADMIT_LABELS) or click_person()
    if not clicked:
        opener = (click_visible(HOST_VIEW_ALL_LABELS)
                  or click_containing(HOST_WAITING_CHIP_LABELS))
        if opener:
            print(f"Someone is waiting — opened the admit panel ('{opener}').")
            time.sleep(1.5)
            clicked = click_visible(HOST_ADMIT_LABELS) or click_person()
            if not clicked:
                _log_admit_candidates(page)
    if not clicked:
        return False
    print(f"Admitted waiting participant(s) ('{clicked}').")
    # "Admit all" asks for confirmation with a second "Admit all".
    time.sleep(1)
    click_visible(HOST_ADMIT_LABELS)
    # Opening the chip left the People panel over a third of the recording.
    time.sleep(1)
    dismiss_notices(page)
    return True


def host_end_call(page):
    """Leave as host, ending the call for everyone still in it.

    Meet asks the host whether to end the call for everyone only when
    others are still there; alone, hanging up already ends it.
    """
    print("Ending the call for everyone (the bot is the host).")
    click_first_match(
        page,
        ["Leave call", "Leave meeting", "ออกจากการโทร", "ออกจากการประชุม"],
        timeout=3000,
    )
    time.sleep(1)
    if click_first_match(page, HOST_END_FOR_ALL_LABELS, timeout=2000):
        return
    click_first_match(
        page,
        ["Leave meeting", "Leave", "ออกจากการประชุม", "ออกจากการโทร"],
        timeout=2000,
    )


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


def click_now(page, labels, exact=True):
    """Click the first visible button among labels, without waiting.

    For things polled every few seconds (notices, the side panel): the
    waiting variant, click_first_match, would spend its timeout on every
    label that isn't there, every poll.
    """
    for label in labels:
        try:
            btn = page.get_by_role("button", name=label, exact=exact).first
            if btn.is_visible():
                btn.click()
                return label
        except Exception:
            continue
    return None


# Pop-ups and cards that sit over the meeting and are just acknowledged or
# closed: "Got it" on "Use Meet safely", the "Your meeting's ready" card's
# close, the People panel's close (opened by admitting someone). Exact labels
# only — "ปิด" (close) is also the start of "ปิดกล้อง"/"ปิดไมโครโฟน".
NOTICE_LABELS = ["รับทราบ", "Got it", "Dismiss", "ปิด", "Close"]


def dismiss_notices(page):
    """Close whatever is covering the call. Cheap enough to run every poll."""
    closed = []
    for _ in range(3):
        label = click_now(page, NOTICE_LABELS)
        if not label:
            break
        closed.append(label)
        time.sleep(0.5)
    if closed:
        print(f"Closed {', '.join(repr(c) for c in closed)} over the call.")
    return closed


# The recording's layout: Spotlight (one big tile — the presentation, or the
# person speaking) with "hide tiles without video" on, which takes the bot's
# own tile (it has no camera) out of the picture. Found live 2026-09-29 under
# More options → "ปรับมุมมอง" (Adjust view); Meet remembers the choice for the
# account's later meetings, so this is idempotent.
_LAYOUT_JS = r"""() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const dlg = Array.from(document.querySelectorAll('[role=dialog]')).filter(vis)
    .find(d => /ปรับมุมมอง|เปลี่ยนเลย์เอาต์|Adjust view|Change layout/.test(d.innerText));
  if (!dlg) return 'no-dialog';
  const out = [];
  const label = Array.from(dlg.querySelectorAll('label')).find(l => /^(สปอตไลท์|Spotlight)$/.test(norm(l.innerText)));
  if (label) { label.click(); out.push('spotlight'); } else out.push('no-spotlight');
  const hide = Array.from(dlg.querySelectorAll('label,span,div')).find(l =>
      /^(ซ่อนหน้าต่างโดยไม่มีวิดีโอ|ซ่อนไทล์ที่ไม่มีวิดีโอ|Hide tiles without video|Hide tiles with no video)$/.test(norm(l.innerText)));
  let sw = null;
  if (hide) {
    const forId = hide.getAttribute('for');
    sw = forId ? document.getElementById(forId) : null;
    if (!sw) { let p = hide.parentElement; for (let i = 0; i < 4 && p && !sw; i++, p = p.parentElement) sw = p.querySelector('[role=switch]'); }
  }
  if (sw) {
    if (sw.getAttribute('aria-checked') !== 'true') { sw.click(); out.push('hide-no-video:on'); }
    else out.push('hide-no-video:already');
  } else out.push('no-hide-switch');
  return out.join(',');
}"""


# The bot's own floating tile, shown whenever someone else is in the call —
# "hide tiles without video" does not cover the self view. Its menu
# ("ตัวเลือกเพิ่มเติมสำหรับ <bot name>") offers "ย่อเล็กสุด" (Minimize),
# found live 2026-09-29; the tile itself can't be removed in this layout.
#
# Which "More options for …" button is the bot's? Every tile has one, and the
# first live GUEST join (2026-09-29, with the operator presenting) opened
# someone else's — "Self tile: no-minimize" — because the code took the first
# in DOM order. When hosting, the bot is alone when it looks, so there was
# only one. Now: a button inside Meet's self tile ([data-self-name]) if the
# DOM marks it; else the label that worked before in this call; else the
# candidates nearest the bottom-right corner first (where the floating self
# view sits), opening each menu until one offers Minimize — and that label is
# remembered, so later tries open only the bot's own menu.
_SELF_MENU_CANDIDATES_JS = r"""() => {
  return Array.from(document.querySelectorAll('button'))
    .filter(b => /^(ตัวเลือกเพิ่มเติมสำหรับ|More options for) /.test(b.getAttribute('aria-label') || ''))
    .map(b => { const r = b.getBoundingClientRect();
      return {label: b.getAttribute('aria-label'), self: !!b.closest('[data-self-name]'),
              corner: r.right + r.bottom}; });
}"""
# The item as the operator saw it (screenshot, 2026-09-29): an icon, then
# "ย่อเล็กสุด", among "แสดงในเลย์เอาต์แบบเรียงชิดกัน", "ปักหมุดไว้ในหน้าจอ" and
# "แสดงวิดีโอแบบเต็มของฉันให้ผู้อื่นเห็น". The icon is a ligature whose name
# ("close_fullscreen") is part of innerText, so any leading icon word is
# dropped, and the aria-label and the text are each tried on their own.
_CLICK_MINIMIZE_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().replace(/^[a-z_]+ /, '');
  const isMin = s => /^(ย่อเล็กสุด|Minimi[sz]e)$/.test(clean(s));
  const item = Array.from(document.querySelectorAll('[role=menuitem]')).find(m =>
      isMin(m.getAttribute('aria-label')) || isMin(m.innerText));
  if (!item) return 'no-minimize';
  item.click();
  return 'minimized';
}"""
_MENU_ITEMS_JS = r"""() => Array.from(document.querySelectorAll('[role=menuitem]'))
    .filter(m => m.getBoundingClientRect().width > 0)
    .map(m => (m.getAttribute('aria-label') || m.innerText || '').replace(/\s+/g, ' ').trim())"""
_SELF_TILE_NEXT_TRY = 0.0
_SELF_MENU_LABEL = None
_SELF_TILE_DIAGNOSED = False
# Opening someone else's menu shows in the recording for a second; bound it.
_SELF_MENU_MAX_PROBES = 4


def self_tile_retry_now():
    """Let the next poll try to minimise the self view straight away."""
    global _SELF_TILE_NEXT_TRY
    _SELF_TILE_NEXT_TRY = 0.0


def _click_button_with_label_js(label):
    return ("() => { const b = Array.from(document.querySelectorAll('button'))"
            f".find(b => b.getAttribute('aria-label') === {json.dumps(label)});"
            " if (b) b.click(); return !!b; }")


def _self_menu_order(candidates, remembered=None):
    """The labels to try, best first: the DOM's self tile, the label that
    worked before, then nearest the bottom-right corner."""
    if not candidates:
        return []
    marked = [c["label"] for c in candidates if c.get("self")]
    if marked:
        return marked[:1]
    labels = [c["label"] for c in candidates]
    if remembered in labels:
        return [remembered]
    ordered = sorted(candidates, key=lambda c: -(c.get("corner") or 0))
    return [c["label"] for c in ordered][:_SELF_MENU_MAX_PROBES]


def minimize_self_tile(page):
    """Minimise the bot's floating tile whenever it is showing.

    Not once per call: Meet restores the full tile when a presentation starts
    (seen live 2026-09-29 — minimised while alone, back at full size once the
    operator presented). While the tile is full-size its "more options"
    button is present; when minimised it isn't, so this is a no-op then.
    At most one attempt a minute, so the menu never flickers in the recording.
    """
    global _SELF_TILE_NEXT_TRY, _SELF_MENU_LABEL, _SELF_TILE_DIAGNOSED
    now = time.time()
    if now < _SELF_TILE_NEXT_TRY:
        return
    _SELF_TILE_NEXT_TRY = now + 60
    try:
        candidates = page.evaluate(_SELF_MENU_CANDIDATES_JS) or []
        order = _self_menu_order(candidates, _SELF_MENU_LABEL)
        if not order:
            return
        if _SELF_MENU_LABEL and _SELF_MENU_LABEL not in order:
            # The remembered tile is gone: already minimised. Nothing to do,
            # and no reason to open anyone else's menu.
            return
        result, seen_items = "no-minimize", []
        for label in order:
            if not page.evaluate(_click_button_with_label_js(label)):
                continue
            time.sleep(1)
            result = page.evaluate(_CLICK_MINIMIZE_JS)
            if result == "minimized":
                _SELF_MENU_LABEL = label
                break
            seen_items.append((label, page.evaluate(_MENU_ITEMS_JS) or []))
            page.keyboard.press("Escape")
            time.sleep(0.5)
        if result != "minimized":
            # Look again in five minutes rather than every minute.
            _SELF_TILE_NEXT_TRY = now + 300
            if not _SELF_TILE_DIAGNOSED:
                _SELF_TILE_DIAGNOSED = True
                print(f"  Self tile diagnostics: menus opened {seen_items!r}")
                if RUN_DIR:
                    page.screenshot(path=os.path.join(RUN_DIR, "self_tile.png"))
        print(f"  Self tile: {result}")
    except Exception as e:
        print(f"  WARNING: could not minimise the bot's own tile ({e})")


# The toolbar's "More options" — not a tile's. Several buttons can carry the
# exact label (a presentation's tile has one too), and the first live guest
# join clicked a tile's, whose menu has no "Adjust view". The toolbar sits at
# the bottom of the window, so take the lowest.
_OPEN_TOOLBAR_MORE_JS = r"""() => {
  const b = Array.from(document.querySelectorAll('button'))
    .filter(b => /^(ตัวเลือกเพิ่มเติม|More options)$/.test((b.getAttribute('aria-label') || '').trim()))
    .filter(b => b.getBoundingClientRect().width > 0)
    .sort((a, c) => c.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
  if (!b) return false;
  b.click();
  return true;
}"""
_OPEN_ADJUST_VIEW_JS = r"""() => { const m = Array.from(document.querySelectorAll('[role=menuitem]'))
    .find(e => /ปรับมุมมอง|เปลี่ยนเลย์เอาต์|Adjust view|Change layout/.test(e.innerText));
  if (m) m.click(); return !!m; }"""


def set_recording_layout(page):
    """Spotlight + hide tiles without video. Best-effort; never fails a call."""
    try:
        opened_menu = page.evaluate(_OPEN_TOOLBAR_MORE_JS)
        if not opened_menu and not click_now(page, ["ตัวเลือกเพิ่มเติม", "More options"]):
            print("  Layout: no 'More options' button - left as is.")
            return
        time.sleep(1)
        if not page.evaluate(_OPEN_ADJUST_VIEW_JS):
            items = page.evaluate(_MENU_ITEMS_JS) or []
            page.keyboard.press("Escape")
            print(f"  Layout: no 'Adjust view' menu item - left as is. "
                  f"Menu offered: {items!r}")
            if RUN_DIR:
                page.screenshot(path=os.path.join(RUN_DIR, "layout_menu.png"))
            return
        time.sleep(1.5)
        result = page.evaluate(_LAYOUT_JS)
        print(f"  Layout: {result}")
        time.sleep(0.5)
        # The dialog's close is "Close" / "ปิดกล่องโต้ตอบ"; Escape as backup.
        if not click_now(page, ["Close", "ปิดกล่องโต้ตอบ", "ปิด"]):
            page.keyboard.press("Escape")
    except Exception as e:
        print(f"  WARNING: could not set the recording layout ({e})")


def _any_visible(page, labels, exact=False):
    """The first label whose button is visible — WITHOUT clicking it."""
    for label in labels:
        try:
            if page.get_by_role("button", name=label, exact=exact).first.is_visible():
                return label
        except Exception:
            continue
    return None


def mute_av(page, platform):
    """Make sure the bot's camera and microphone are off.

    The browser blocks both devices (screen/browser.py), so normally there is
    nothing to turn off and Meet shows "microphone/camera has a problem" —
    that is the expected state and nothing is clicked. Only a device that is
    actually ON (its "turn off" button is showing) gets clicked.

    Two earlier behaviours are gone on purpose, both seen live 2026-09-29:
      * The "already off?" check CLICKED the label it found — "เปิดไมโครโฟน"
        (turn mic ON) — so the check itself unmuted the bot.
      * Blind Ctrl+E / Ctrl+D toggles: with a blocked device they open Meet's
        device-problem dialog, and on a live device they flip whatever state
        it was in.

    Best-effort: a miss is a warning, never a failed recording.
    """
    if platform == "google_meet":
        blocked = {"camera": ["กล้องมีปัญหา", "camera problem", "Camera is blocked",
                              "Allow camera"],
                   "microphone": ["ไมโครโฟนมีปัญหา", "microphone problem",
                                  "Microphone is blocked", "Allow microphone"]}
        on = {"camera": ["Turn off camera", "ปิดกล้อง"],
              "microphone": ["Turn off microphone", "Mute microphone", "ปิดไมโครโฟน"]}
        off = {"camera": ["Turn on camera", "เปิดกล้อง", "Camera is off"],
               "microphone": ["Turn on microphone", "Unmute", "เปิดไมโครโฟน",
                              "Microphone is off"]}
    elif platform == "zoom":
        blocked = {"camera": [], "microphone": []}
        on = {"camera": ["Stop video", "Mute video"], "microphone": ["Mute", "Mute microphone"]}
        off = {"camera": ["Start video"], "microphone": ["Unmute"]}
    else:
        print(f"WARNING: unknown platform {platform!r} - skipping mute.")
        return

    print(f"Checking camera + mic on {platform}...")
    for kind in ("camera", "microphone"):
        label = _any_visible(page, blocked[kind])
        if label:
            print(f"  {kind}: blocked by the browser ('{label}') - nothing to turn off.")
            continue
        label = _any_visible(page, off[kind])
        if label:
            print(f"  {kind}: already off ('{label}').")
            continue
        if click_first_match(page, on[kind], timeout=1500):
            print(f"  {kind}: was ON - turned it off.")
            continue
        print(f"  WARNING: could not tell whether the {kind} is off - check the recording.")


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


def _sleep_hosting(page, seconds):
    """Sleep, but keep admitting knockers and honour the kill switch."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if kill_requested():
            return
        try:
            host_admit_waiting(page)
        except Exception:
            pass
        time.sleep(min(HOST_ADMIT_POLL_SECONDS, max(0.0, deadline - time.time())))


def meeting_audio_silent_for():
    """Seconds the meeting audio has been silent, from record_screen.sh's
    audio watcher (runs/<id>/audio_level: "<epoch> <peak dB> <silent s>").
    None when unknown — no file, or a stale one — and then the audio gate is
    open, i.e. the rules behave as they did before audio was measured."""
    if not RUN_DIR:
        return None
    try:
        with open(os.path.join(RUN_DIR, "audio_level")) as fh:
            at, _peak, silent = fh.read().split()[:3]
        if time.time() - int(at) > 60:
            return None
        return int(silent)
    except (OSError, ValueError):
        return None


def quiet_enough():
    silent = meeting_audio_silent_for()
    return silent is None or silent >= AUTO_LEAVE_SILENCE_SECONDS


def wait_until_meeting_ends(page, poll_seconds=POLL_SECONDS, host=False):
    """Stay in the call until it is over.

    host=True is a meeting the bot created: knockers are admitted while it
    waits, nobody-has-joined-yet is not "the meeting emptied" (it waits
    NEW_MEET_WAIT_SECONDS for the first one), a 1:1 with the bot is a real
    meeting rather than an idle test call, and every exit ends the call for
    everyone instead of just leaving it.
    """
    print("In meeting. Monitoring participant count and end state...")
    _leave = host_end_call if host else leave_meeting

    def leave(page):
        # Out of the call already (the lobby page, "you left the meeting"):
        # there is nothing to click, and hunting for Leave buttons that don't
        # exist took longer than kill_meeting.sh's grace period (2026-09-29).
        if is_admitted(page):
            _leave(page)
        else:
            print("Not in the call any more - nothing to leave.")
    out_of_call = 0
    peak_count = None
    low_streak = 0
    idle_since_ts = None    # first poll at which count was in (1, 2)
    start_ts = time.time()  # for the hard max-duration timeout backstop
    someone_joined = False  # host mode: has anyone but the bot been in?
    count_ever_read = False
    warned_unreadable = False
    # The count at the previous poll. While the bot is alone there is no
    # floating self view to minimise — its own tile IS the stage, and that
    # menu has no Minimize — so the attempt waits for company. Mostly a
    # hosted call's first minutes: trying then cost a failed menu in the
    # recording and a five-minute back-off, which left the first guest
    # looking at the bot's full-size tile.
    last_count = None
    polls = 0

    while True:
        polls += 1
        try:
            if host:
                _sleep_hosting(page, poll_seconds)
            else:
                time.sleep(poll_seconds)

            if kill_requested():
                print("Kill signal received - leaving the meeting cleanly.")
                try:
                    leave(page)
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
                    leave(page)
                except Exception as e:
                    print(f"Clean leave failed ({e}) - exiting anyway.")
                return

            # Dropped out of the call without the page saying "ended": seen
            # live 2026-09-29, the bot back on the pre-join page ("พร้อมจะ
            # เข้าร่วมไหม") while the recorder kept filming an empty lobby for
            # what would have been hours. Two polls in a row without the
            # in-call controls is the end of this recording.
            if is_admitted(page):
                out_of_call = 0
            else:
                out_of_call += 1
                if out_of_call >= 2:
                    print("No longer in the call (its controls are gone) - "
                          "ending the recording.")
                    try:
                        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "left_call.png"))
                    except Exception:
                        pass
                    return

            # Anything Meet has put over the call since the last poll (a
            # notice, the People panel) comes off the recording.
            if "meet.google.com" in (getattr(page, "url", "") or ""):
                dismiss_notices(page)
                # An unreadable count (Meet renamed the chip) must not stop
                # it for good: after a few polls it tries regardless.
                if (last_count is not None and last_count >= 2) or \
                        (last_count is None and polls > 3):
                    minimize_self_tile(page)

            # Screen-share defenses (Layers 2 and 3). Layer 1 is the Chrome
            # flag set at launch; these two are the runtime catch-nets.
            block_screen_share_dialog(page)
            stop_unwanted_presenting(page)

            count = get_participant_count(page)
            if count is not None:
                if count >= 2 and (last_count is not None and last_count < 2):
                    # Someone just arrived and the floating self view with
                    # them: minimise it at the next poll, not after a back-off.
                    self_tile_retry_now()
                last_count = count
            if host:
                if count is not None:
                    count_ever_read = True
                    if count >= 2 and not someone_joined:
                        someone_joined = True
                        print(f"First participant joined (count={count}).")
                if not someone_joined:
                    # An empty call it just created is not a meeting that
                    # ended. Only the wait for the first participant can end
                    # it — and only when the count is actually readable: a
                    # participant chip Meet has renamed must not end a call
                    # people are in; MAX_MEETING_MINUTES still bounds it.
                    waited = time.time() - start_ts
                    if count_ever_read and waited >= NEW_MEET_WAIT_SECONDS:
                        print(f"Nobody joined within {int(waited // 60)}m - "
                              "ending the call.")
                        leave(page)
                        return
                    if (not count_ever_read and not warned_unreadable
                            and waited >= NEW_MEET_WAIT_SECONDS):
                        warned_unreadable = True
                        print("WARNING: cannot read the participant count; "
                              "staying until MAX_MEETING_MINUTES or a kill.")
                    continue
            if count is not None:
                peak_count = count if peak_count is None else max(peak_count, count)
                is_alone = count <= 1
                is_mass_exodus = peak_count and count <= max(1, int(peak_count * DROP_RATIO_THRESHOLD))

                # Idle auto-leave: only the bot (count==1) or only the bot
                # + one other person (count==2) for IDLE_LEAVE_SECONDS.
                # Independent of mass-exit; both can fire on the same call.
                # Hosting, a 1:1 with the bot is a meeting — only the bot alone
                # is idle.
                idle_counts = (1,) if host else (1, 2)
                # Idle needs silence too: two people talking is a meeting.
                if IDLE_LEAVE_SECONDS > 0 and count in idle_counts and quiet_enough():
                    if idle_since_ts is None:
                        idle_since_ts = time.time()
                    elif time.time() - idle_since_ts >= IDLE_LEAVE_SECONDS:
                        mins = int((time.time() - idle_since_ts) // 60)
                        print(
                            f"Idle threshold reached ({mins}m, count={count}, "
                            f"peak={peak_count}) - leaving."
                        )
                        leave(page)
                        return
                else:
                    idle_since_ts = None

                # Alone is alone. "Most people left" waits for silence: the
                # lecturer may still be talking to the few who stayed.
                if is_mass_exodus and not is_alone and not quiet_enough():
                    is_mass_exodus = False
                if is_alone or is_mass_exodus:
                    low_streak += 1
                    print(f"Low participant count ({count}, peak {peak_count}) - streak {low_streak}")
                else:
                    low_streak = 0

                if low_streak >= LOW_COUNT_CONFIRMATIONS:
                    print("Confirmed most/all participants have left - leaving.")
                    leave(page)
                    return

        except KeyboardInterrupt:
            print("Manual stop.")
            return
        except Exception as e:
            print(f"Page unreachable ({e}) - assuming meeting ended.")
            return


class _PageCloser:
    """browser.open_page() owns the browser; `context.close()` is a no-op."""

    def close(self):
        pass


def ensure_bot_account(page, expected):
    """Refuse to join a Google Meet from any account but BOT_GOOGLE_ACCOUNT.

    Unset: nothing is checked (a warning says so). Set: the profile must be
    signed in and the account must be among those it holds; the Meet URL then
    carries authuser=<account>, so Meet picks it even beside another one.
    An unreadable answer (Google changed the endpoint) is a warning, not a
    refusal — the authuser parameter still steers Meet to the right account.
    """
    accounts = browser.read_signed_in_accounts(page) if expected else None
    verdict, message = browser.account_verdict(accounts, expected)
    if verdict in ("ok", "unchecked"):
        print(("Bot account: " if verdict == "ok" else "WARNING: bot account: ")
              + message)
        return True
    print(f"Cannot join: {message}")
    try:
        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "wrong_account.png"))
    except Exception:
        pass
    return False


def _exit_on_term(signum, frame):
    # kill_meeting.sh / record_screen.sh stop this process with SIGTERM.
    # Unhandled, SIGTERM ends Python on the spot: browser.open_page()'s
    # cleanup never runs and geckodriver + Firefox are orphaned, still holding
    # the profile (two were found on 2026-09-29, and the next recording
    # couldn't start). As SystemExit, the `with` blocks unwind and quit them.
    raise SystemExit(128 + signum)


def main():
    import signal
    signal.signal(signal.SIGTERM, _exit_on_term)
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

    # The browser's own profile lock is cleared by browser.open_page(): a run
    # killed before its cleanup leaves one behind, and the next launch then
    # refuses the profile as "in use".
    with browser.open_page(headless=False) as page:
        context = _PageCloser()
        expected = browser.expected_account()
        if is_new_meet(url) or "meet.google.com" in url:
            if not ensure_bot_account(page, expected):
                return
            if not is_new_meet(url):
                url = browser.with_authuser(url, expected)

        host = is_new_meet(url)
        if host:
            created = host_create_google_meet(page)
            if not created:
                page.screenshot(path=os.path.join(SCREENSHOT_DIR, "create_failed.png"))
                context.close()
                return
            url = created
            clicked = join_google_meet(page, url, display_name, navigate=False)
        elif "meet.google.com" in url:
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
        if platform == "google_meet":
            # A clean picture for the recording: close the cards Meet opens
            # on arrival, then Spotlight with tiles-without-video hidden
            # (which removes the bot's own tile), then close anything left.
            dismiss_notices(page)
            set_recording_layout(page)
            dismiss_notices(page)

        # Signal the orchestrator (record_screen.sh) that it's safe to start
        # recording now - we're actually in the call, not a lobby.
        with open(ADMITTED_MARKER, "w") as f:
            f.write(str(time.time()))

        try:
            wait_until_meeting_ends(page, host=host)
        finally:
            if os.path.exists(ADMITTED_MARKER):
                os.remove(ADMITTED_MARKER)
        context.close()


if __name__ == "__main__":
    main()