#!/usr/bin/env python3
"""
Launch the recorder's browser without a meeting, and hold it open.

Everything about stage 1 except the meeting itself can be checked this way:
that real Google Chrome starts under Xvfb, that Playwright drives it with
`channel="chrome"`, that the persistent profile opens, that the kiosk window
fills the display so ffmpeg's x11grab has no black edges, and that audio played
by the page lands in this run's PulseAudio sink.

It deliberately imports CHROME_ARGS and PROFILE_DIR from capture.py rather than
repeating them: a flag that breaks recording should break this too.

    python3 screen/browser_smoke.py [--seconds 5] [--url URL]

With no --url it loads a self-contained page that fills the viewport with a
bright panel and plays a tone through an oscillator, so a recording made while
this runs has both a picture and a waveform to check.

verify_e2e.sh --browser-smoke wraps this with the display, the sink and ffmpeg.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture import CHROME_ARGS, PROFILE_DIR  # noqa: E402

# A page with something worth capturing: a large light panel (so the slide
# crop has a target), a caption, and a tone. Data URL, so no network and no
# temp file.
SMOKE_PAGE = (
    "data:text/html,"
    "<body style='margin:0;background:%23101014'>"
    "<div style='position:absolute;top:6%25;left:6%25;width:88%25;height:80%25;"
    "background:%23fafaf5;font:48px sans-serif;display:flex;"
    "align-items:center;justify-content:center'>meeting-bot smoke test</div>"
    "<script>"
    "const c=new (window.AudioContext||window.webkitAudioContext)();"
    "const o=c.createOscillator();o.frequency.value=440;"
    "const g=c.createGain();g.gain.value=0.2;"
    "o.connect(g);g.connect(c.destination);o.start();"
    "</script></body>"
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--url", default=SMOKE_PAGE)
    ap.add_argument("--screenshot", help="also save a PNG of the page here")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    profile = Path(PROFILE_DIR)
    profile.mkdir(parents=True, exist_ok=True)
    # Same stale-lock cleanup as capture.py: a killed run leaves a
    # SingletonLock that makes the next Chrome refuse to start.
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = profile / lock
        if path.exists() or path.is_symlink():
            path.unlink()

    print(f"==> Launching Chrome (profile: {profile})")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            channel="chrome",
            args=CHROME_ARGS,
            permissions=["camera", "microphone"],
            no_viewport=True,
            locale="th-TH",
        )
        page = context.new_page()
        page.goto(args.url)
        page.wait_for_timeout(1000)

        size = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        print(f"==> Chrome is up. Window is {size[0]}x{size[1]}")
        if args.screenshot:
            page.screenshot(path=args.screenshot)
            print(f"==> Screenshot: {args.screenshot}")

        time.sleep(args.seconds)
        context.close()
    print("==> Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
