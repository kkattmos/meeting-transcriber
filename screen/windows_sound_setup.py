#!/usr/bin/env python3
"""Windows, once: make the bot's browser play into VB-CABLE.

Windows routes sound per application, by the program's path, and only lists
a program in Settings > System > Sound > Volume mixer while it is playing.
This opens the bot's own browser (MEETING_BROWSER — by default the Firefox ESR
setup.ps1 installed in its own folder, so your everyday Firefox is never
affected) on a page that plays a quiet tone for 90 seconds. While it plays:

    Settings > System > Sound > Volume mixer > firefox.exe (the bot's)
      > Output device: CABLE Input (VB-Audio Virtual Cable)

Windows remembers it for that program from then on. Then check with

    ffmpeg -f dshow -i audio="CABLE Output (VB-Audio Virtual Cable)" -t 5 check.wav

while this runs again: check.wav should hold the tone.

    .venv/bin/python3 screen/windows_sound_setup.py [--seconds 90]
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser  # noqa: E402

TONE_PAGE = ("data:text/html,<title>Meeting bot sound check</title>"
             "<body style='font:24px sans-serif;padding:40px'>"
             "<h1>Meeting bot: sound check</h1><p>A quiet tone is playing. In Windows: "
             "Settings &gt; System &gt; Sound &gt; Volume mixer &gt; this firefox.exe &gt; "
             "Output device: <b>CABLE Input</b>.</p>"
             "<script>const c=new AudioContext();const o=c.createOscillator();"
             "const g=c.createGain();g.gain.value=0.05;o.frequency.value=440;"
             "o.connect(g).connect(c.destination);o.start();</script>")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seconds", type=int, default=90)
    args = ap.parse_args(argv)
    if os.name != "nt":
        print("This is for the Windows branch; on Linux the sink is per run.", file=sys.stderr)
    with browser.open_page(headless=False) as page:
        page.goto(TONE_PAGE)
        print(__doc__.split("\n\n")[1])
        print(f"\nPlaying for {args.seconds}s — set the output device now.")
        time.sleep(args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
