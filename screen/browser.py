#!/usr/bin/env python3
"""
The recorder's browser: which one, which profile, and how it is launched.

MEETING_BROWSER picks it:

  firefox-esr (default)  Debian's own firefox-esr package, driven by Selenium
                         through geckodriver (Marionette). Playwright can only
                         drive its own patched Firefox build, never the stock
                         ESR binary, so this path does not use Playwright.
  chrome                 google-chrome-stable through Playwright with
                         channel="chrome" — the original path, kept as the
                         fallback in case Google starts refusing the
                         automated Firefox.

capture.py is written against Playwright's `page` API. For Firefox,
`FirefoxPage` implements the subset of that API capture.py uses (goto,
evaluate, keyboard.press, get_by_role, locator, inner_text, screenshot, ...),
so the Meet/Zoom logic is one code path for both browsers. If capture.py
starts using another Playwright call, add it here or the Firefox path fails
at runtime with an AttributeError.

Each browser keeps its own persistent profile (FIREFOX_PROFILE_DIR /
CHROME_PROFILE_DIR); first_time_login.sh signs into whichever MEETING_BROWSER
names.

This module also carries the bot-account check (BOT_GOOGLE_ACCOUNT): the
recorder refuses to join a Google Meet from a profile that is signed in as
anyone else. `python3 screen/browser.py check-account` is the same check
from the command line; first_time_login.sh runs it after the login window
closes.
"""
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager

_BOT_ROOT = os.environ.get("MEETING_BOT_ROOT",
                           os.path.expanduser("~/.local/share/meeting-bot"))

GEOMETRY = os.environ.get("RECORD_GEOMETRY", "1920x1080").lower()
_W, _H = (int(v) for v in GEOMETRY.split("x", 1))

# The Chrome command line, in one place so screen/browser_smoke.py launches
# an identical browser without a live meeting — a flag that breaks recording
# should break the smoke test too. capture.py re-exports it.
#
# --kiosk: true fullscreen with no tab/address bar, so x11grab captures only
# the meeting. --window-size pins the drawable area to the Xvfb head;
# --window-position=0,0 stops Chrome placing the window at (10,10), which left
# a 10px black band on every recording (found by verify_e2e.sh --browser-smoke).
#
# Media devices: BLOCKED, both of them (operator's decision, 2026-09-29). The
# bot only listens and watches; it never needs a microphone or a camera, and
# on a PC the real ones must never reach Meet. A blocked device can't be
# turned on by a mis-aimed mute click either — which happened live, when the
# "already muted?" check clicked "turn mic ON". Chrome denies every prompt
# (--deny-permission-prompts, and no permissions granted); Firefox denies by
# default without asking. Listening (audio OUTPUT to the per-run sink) is
# unaffected.
#
# --disable-features=ScreenCapture is layer 1 of the screen-share defense;
# layers 2 and 3 are in capture.wait_until_meeting_ends.
CHROME_ARGS = [
    "--kiosk",
    f"--window-size={_W},{_H}",
    "--window-position=0,0",
    "--deny-permission-prompts",
    "--disable-features=ScreenCapture",
]

# Firefox: the same intent expressed as prefs. Set on every launch (through
# geckodriver), so a profile edited by hand can't drift from them.
FIREFOX_PREFS = {
    # Meet's UI in Thai, so Thai participant names render — the Chrome path's
    # locale="th-TH". capture.py carries English + Thai labels either way.
    "intl.accept_languages": "th-TH, th, en-US, en",
    # Media: microphone and camera both denied without a prompt (a prompt in
    # a kiosk window would be recorded and never answered). NOT
    # media.navigator.permission.disabled — that one skips the check and
    # GRANTS access. record_screen.sh still points PULSE_SOURCE at a silent
    # sink as a second line of defence.
    "media.navigator.permission.disabled": False,
    "permissions.default.microphone": 2,
    "permissions.default.camera": 2,
    "media.navigator.video.enabled": False,
    # Layer 1 of the screen-share defense.
    "media.getdisplaymedia.enabled": False,
    "permissions.default.screen": 2,
    # Meet's audio must play without a user gesture or the recording is silent.
    "media.autoplay.default": 0,
    "media.autoplay.blocking_policy": 0,
    # Hide navigator.webdriver where this Firefox still honours the pref.
    "dom.webdriver.enabled": False,
    # No interruptions in an unattended kiosk window.
    "browser.shell.checkDefaultBrowser": False,
    "browser.sessionstore.resume_from_crash": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.aboutwelcome.enabled": False,
    "full-screen-api.warning.timeout": 0,
    "full-screen-api.transition-duration.enter": "0 0",
    "full-screen-api.transition-duration.leave": "0 0",
    "app.update.auto": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "signon.rememberSignons": False,
    # Ignore per-site zoom saved in the profile. The sign-in window is an
    # ordinary browser, and a Ctrl+- there was remembered for meet.google.com
    # at 50% (found 2026-09-29: devicePixelRatio 0.5, a 3840x2160 CSS
    # viewport on the 1920x1080 head) — every recording would have shown
    # Meet at half size.
    "browser.zoom.siteSpecific": False,
    # Raw text for JSON responses — the account check reads ListAccounts'
    # body, and the JSON viewer would hand it a UI instead.
    "devtools.jsonview.enabled": False,
}
FIREFOX_ARGS = ["--kiosk", "--width", str(_W), "--height", str(_H)]


def browser_kind(value=None):
    """Normalise MEETING_BROWSER to 'firefox' or 'chrome'."""
    raw = (value if value is not None
           else os.environ.get("MEETING_BROWSER", "firefox-esr")).strip().lower()
    if raw in ("", "firefox", "firefox-esr", "ff", "esr"):
        return "firefox"
    if raw in ("chrome", "google-chrome", "google-chrome-stable"):
        return "chrome"
    raise SystemExit(f"MEETING_BROWSER={raw!r} is not a supported browser "
                     "(use firefox-esr or chrome)")


def profile_dir(kind=None):
    kind = kind or browser_kind()
    if kind == "chrome":
        return os.environ.get("CHROME_PROFILE_DIR",
                              os.path.join(_BOT_ROOT, "chrome-profile"))
    return os.environ.get("FIREFOX_PROFILE_DIR",
                          os.path.join(_BOT_ROOT, "firefox-profile"))


def browser_binary(kind=None):
    kind = kind or browser_kind()
    if kind == "chrome":
        return os.environ.get("CHROME_BIN") or shutil.which("google-chrome-stable")
    return (os.environ.get("FIREFOX_BIN") or shutil.which("firefox-esr")
            or shutil.which("firefox"))


class ProfileInUse(RuntimeError):
    """The profile's lock names a browser that is still running."""


def _lock_owner_pid(path):
    """The pid a browser lock names, or None. Firefox: `lock -> host:+PID`;
    Chrome: `SingletonLock -> host-PID`."""
    try:
        target = os.readlink(path)
    except OSError:
        return None
    m = re.search(r"[+-](\d+)$", target)
    return int(m.group(1)) if m else None


def _pid_alive(pid):
    """True for a running process. A zombie (exited, not yet reaped by an
    orphaned parent) counts as dead: kill -0 succeeds on it, and that made a
    dead recording's Firefox block the next one (2026-09-29)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


def clear_stale_locks(kind, profile):
    """Remove a lock left by a killed browser — and ONLY a dead one.

    Chrome's Singleton* and Firefox's lock/.parentlock encode the owner; a run
    killed before its cleanup leaves one behind and the next launch refuses
    ("profile appears to be in use"). A lock whose pid is alive belongs to a
    running browser — first_time_login.sh's window, or another recording —
    and deleting it would let two browsers write one profile. That happened
    on 2026-09-29 (the sign-in window was still open); now it is refused.
    """
    main = "SingletonLock" if kind == "chrome" else "lock"
    owner = _lock_owner_pid(os.path.join(profile, main))
    if owner and owner != os.getpid() and _pid_alive(owner):
        raise ProfileInUse(
            f"the {kind} profile {profile} is in use by pid {owner} — close the "
            "browser window using it (a first_time_login.sh window?) or wait for "
            "the recording that holds it to finish")
    names = (("SingletonLock", "SingletonSocket", "SingletonCookie")
             if kind == "chrome" else ("lock", ".parentlock"))
    for name in names:
        path = os.path.join(profile, name)
        if os.path.lexists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# --- Timeouts ----------------------------------------------------------------

class BrowserTimeout(Exception):
    """Raised by FirefoxPage where Playwright would raise its TimeoutError."""


def timeout_errors():
    """Tuple usable in `except`: Playwright's TimeoutError (if importable) and ours."""
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        return (PWTimeout, BrowserTimeout)
    except Exception:
        return (BrowserTimeout,)


# --- The Playwright-shaped adapter over Selenium ------------------------------

# Accessible-name lookup for role=button, done in the page: collect button-ish
# elements, compute a name the way Playwright roughly does (aria-label,
# aria-labelledby, text, value, title), keep the visible ones, and match
# exactly or case-insensitively as a substring (Playwright's exact=False).
_FIND_BUTTONS_JS = r"""
const [label, exact, onlyVisible] = arguments;
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const nameOf = el => {
  let n = el.getAttribute('aria-label');
  if (!n && el.getAttribute('aria-labelledby')) {
    n = el.getAttribute('aria-labelledby').split(/\s+/)
         .map(id => { const t = document.getElementById(id); return t ? t.innerText : ''; })
         .join(' ');
  }
  if (!n) n = el.innerText || el.value || el.getAttribute('title') || '';
  return norm(n);
};
const visible = el => {
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return false;
  const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
};
const sel = 'button, [role="button"], input[type="button"], input[type="submit"]';
const want = norm(label);
return Array.from(document.querySelectorAll(sel)).filter(el => {
  if (onlyVisible && !visible(el)) return false;
  if (el.getAttribute('aria-hidden') === 'true') return false;
  if (want === '') return true;
  const n = nameOf(el);
  return exact ? n === want : n.toLowerCase().includes(want.toLowerCase());
});
"""

_KEYS = None


def _key(name):
    global _KEYS
    if _KEYS is None:
        from selenium.webdriver.common.keys import Keys
        _KEYS = {
            "enter": Keys.ENTER, "tab": Keys.TAB, "escape": Keys.ESCAPE,
            "control": Keys.CONTROL, "ctrl": Keys.CONTROL, "alt": Keys.ALT,
            "shift": Keys.SHIFT, "meta": Keys.META, "space": Keys.SPACE,
            "backspace": Keys.BACKSPACE,
        }
    return _KEYS.get(name.lower(), name.lower() if len(name) == 1 else name)


class _Keyboard:
    def __init__(self, driver):
        self._d = driver

    def press(self, combo):
        """Playwright spelling: "Enter", "Tab", "Control+e", "Alt+v"."""
        from selenium.webdriver.common.action_chains import ActionChains
        parts = combo.split("+")
        mods, key = parts[:-1], parts[-1]
        chain = ActionChains(self._d)
        for m in mods:
            chain.key_down(_key(m))
        chain.send_keys(_key(key))
        for m in reversed(mods):
            chain.key_up(_key(m))
        chain.perform()


class _Element:
    """A resolved element — what Playwright's `.first` / `.all()` yield."""

    def __init__(self, page, el):
        self._p, self._el = page, el

    def is_visible(self, timeout=None):
        try:
            return bool(self._el.is_displayed())
        except Exception:
            return False

    def click(self):
        try:
            self._el.click()
        except Exception:
            # An overlay (a tooltip, Meet's snackbar) intercepting the click
            # is common; a DOM click still reaches the button.
            self._p.driver.execute_script("arguments[0].click();", self._el)

    def fill(self, text):
        self._el.clear()
        self._el.send_keys(text)

    def inner_text(self):
        return self._el.text or ""


class _Locator:
    """Lazy query, resolved on each call like Playwright's Locator."""

    def __init__(self, page, finder):
        self._p, self._find = page, finder

    def _poll(self, timeout_ms):
        deadline = time.time() + (timeout_ms or 0) / 1000.0
        while True:
            els = self._find()
            if els or time.time() >= deadline:
                return els
            time.sleep(0.2)

    @property
    def first(self):
        return _FirstLocator(self)

    def all(self):
        return [_Element(self._p, e) for e in self._find()]

    def is_visible(self, timeout=None):
        return bool(self._poll(timeout))

    def click(self, timeout=5000):
        els = self._poll(timeout)
        if not els:
            raise BrowserTimeout("element not found")
        _Element(self._p, els[0]).click()

    def fill(self, text, timeout=5000):
        els = self._poll(timeout)
        if not els:
            raise BrowserTimeout("element not found")
        _Element(self._p, els[0]).fill(text)

    def inner_text(self, timeout=5000):
        els = self._poll(timeout)
        if not els:
            raise BrowserTimeout("element not found")
        return _Element(self._p, els[0]).inner_text()


class _FirstLocator(_Locator):
    def __init__(self, parent):
        super().__init__(parent._p, lambda: parent._find()[:1])


class FirefoxPage:
    """The subset of playwright.sync_api.Page that capture.py uses."""

    def __init__(self, driver):
        self.driver = driver
        self.keyboard = _Keyboard(driver)

    # navigation
    def goto(self, url, wait_until=None, timeout=60000):
        self.driver.set_page_load_timeout(timeout / 1000.0)
        try:
            self.driver.get(url)
        except Exception as e:
            if "Timeout" in type(e).__name__:
                raise BrowserTimeout(str(e)) from e
            raise

    @property
    def url(self):
        return self.driver.current_url

    def wait_for_url(self, predicate, timeout=30000):
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            try:
                if predicate(self.driver.current_url):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise BrowserTimeout(f"URL condition not met within {timeout}ms")

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000.0)

    def title(self):
        return self.driver.title or ""

    def is_closed(self):
        try:
            self.driver.current_url
            return False
        except Exception:
            return True

    # content
    def evaluate(self, js):
        """Playwright passes a function source ("() => ..."); call it."""
        return self.driver.execute_script(f"return ({js})();")

    def inner_text(self, selector):
        from selenium.webdriver.common.by import By
        return self.driver.find_element(By.CSS_SELECTOR, selector).text or ""

    def content(self):
        return self.driver.page_source or ""

    def screenshot(self, path):
        self.driver.save_screenshot(path)

    # queries
    def get_by_role(self, role, name="", exact=False):
        if role != "button":
            raise NotImplementedError(f"get_by_role({role!r}) — only 'button' is adapted")
        return _Locator(self, lambda: self.driver.execute_script(
            _FIND_BUTTONS_JS, name or "", bool(exact), True) or [])

    def locator(self, css):
        from selenium.webdriver.common.by import By

        def find():
            return [e for e in self.driver.find_elements(By.CSS_SELECTOR, css)
                    if _safe_displayed(e)]
        return _Locator(self, find)


def _safe_displayed(el):
    try:
        return el.is_displayed()
    except Exception:
        return False


# --- Launch --------------------------------------------------------------------

@contextmanager
def open_page(headless=False, kind=None):
    """Yield a page in the bot's persistent profile, closing it on exit."""
    kind = kind or browser_kind()
    profile = profile_dir(kind)
    os.makedirs(profile, exist_ok=True)
    clear_stale_locks(kind, profile)
    if kind == "chrome":
        with _open_chrome(profile, headless) as page:
            yield page
    else:
        with _open_firefox(profile, headless) as page:
            yield page


@contextmanager
def _open_chrome(profile, headless):
    from playwright.sync_api import sync_playwright
    args = list(CHROME_ARGS)
    # Chrome refuses to start as root without --no-sandbox; as a normal user
    # (the PC setup) the sandbox stays on.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
    with sync_playwright() as p:
        # channel="chrome": real Google Chrome, not the bundled Chromium,
        # which Google's sign-in refuses. See CLAUDE.md.
        context = p.chromium.launch_persistent_context(
            profile, headless=headless, channel="chrome", args=args,
            permissions=[], no_viewport=True,
            locale="th-TH")
        try:
            yield context.new_page()
        finally:
            try:
                context.close()
            except Exception:
                pass


@contextmanager
def _open_firefox(profile, headless):
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    binary = browser_binary("firefox")
    if not binary:
        raise SystemExit("firefox-esr not found — install it (sudo apt-get install "
                         "firefox-esr) or set FIREFOX_BIN.")
    opts = Options()
    opts.binary_location = binary
    # `-profile <dir>` makes geckodriver use the persistent profile in place;
    # Options.profile would copy it to a temp dir and every sign-in would be
    # lost at the end of the run.
    opts.add_argument("-profile")
    opts.add_argument(profile)
    if headless:
        opts.add_argument("-headless")
    else:
        for a in FIREFOX_ARGS:
            opts.add_argument(a)
    for k, v in FIREFOX_PREFS.items():
        opts.set_preference(k, v)
    gd = os.environ.get("GECKODRIVER_BIN") or shutil.which("geckodriver")
    # No geckodriver on PATH: Selenium Manager fetches a matching one into
    # ~/.cache/selenium. setup.sh installs one so an offline box works too.
    service = Service(executable_path=gd) if gd else Service()
    driver = webdriver.Firefox(options=opts, service=service)
    try:
        yield FirefoxPage(driver)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# --- The bot account ----------------------------------------------------------

# Chrome's own "which accounts are signed in" endpoint. It answers any browser
# holding the Google cookies — but only to a POST from Google's own origin (a
# plain GET is a 400), so the check loads a static page on accounts.google.com
# first and fetches from there. Verified 2026-09-29 on Firefox ESR 140.
LIST_ACCOUNTS_ORIGIN_PAGE = "https://accounts.google.com/robots.txt"
LIST_ACCOUNTS_PATH = "/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard"
_LIST_ACCOUNTS_JS = (
    "async () => { const r = await fetch(%r, {method: 'POST', credentials: 'include'});"
    " return r.ok ? await r.text() : ''; }" % LIST_ACCOUNTS_PATH)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def expected_account():
    return (os.environ.get("BOT_GOOGLE_ACCOUNT") or "").strip() or None


def normalize_email(addr):
    """Case-fold, and ignore dots / +tags in a gmail.com local part."""
    addr = (addr or "").strip().lower()
    local, _, domain = addr.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def parse_accounts(body):
    """Emails in a ListAccounts response (JSON first, regex as the fallback).

    The JSON shape is ["gaia.l.a.r", [["gaia.l.a", 1, "Name", "email", ...], ...]];
    an empty inner list is a signed-out profile. Returns None when the body is
    neither that JSON nor contains any address (a login page, an error page).
    """
    emails = []
    try:
        data = json.loads(body)
        entries = data[1]
        if not isinstance(entries, list):
            raise ValueError("unexpected ListAccounts shape")
        for entry in entries:
            for field in entry:
                if isinstance(field, str) and _EMAIL_RE.fullmatch(field):
                    emails.append(field)
                    break
    except Exception:
        # Not the JSON we know: an email in it is still evidence, but an
        # empty answer here is "unknown", never "signed out".
        emails = _EMAIL_RE.findall(body or "")
        if not emails:
            return None
    seen, out = set(), []
    for e in emails:
        if normalize_email(e) not in seen:
            seen.add(normalize_email(e))
            out.append(e)
    return out


def account_verdict(signed_in, expected):
    """('ok'|'wrong'|'signed-out'|'unchecked', message)."""
    if not expected:
        return "unchecked", "BOT_GOOGLE_ACCOUNT is not set; the account is not checked."
    if signed_in is None:
        return "unchecked", "could not read which Google accounts are signed in."
    want = normalize_email(expected)
    if not signed_in:
        return "signed-out", (f"the browser profile is not signed into Google; "
                              f"expected {expected}. Run ./first_time_login.sh.")
    if any(normalize_email(e) == want for e in signed_in):
        return "ok", f"signed in as {expected}."
    return "wrong", (f"the browser profile is signed in as {', '.join(signed_in)}, "
                     f"not {expected}. Run ./first_time_login.sh and sign in as "
                     f"{expected} only.")


def read_signed_in_accounts(page):
    """Emails the profile is signed into Google as; None when unreadable."""
    try:
        page.goto(LIST_ACCOUNTS_ORIGIN_PAGE, wait_until="domcontentloaded")
        body = page.evaluate(_LIST_ACCOUNTS_JS)
    except Exception as e:
        print(f"WARNING: account check could not reach Google ({e})")
        return None
    if not body or not str(body).strip():
        return None
    return parse_accounts(str(body))


def with_authuser(url, account):
    """Point a Meet URL at a specific signed-in account (authuser=<email>)."""
    if not account or "meet.google.com" not in url or "authuser=" in url:
        return url
    return url + ("&" if "?" in url else "?") + f"authuser={account}"


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-account", help="which Google account the profile is in")
    sub.add_parser("info", help="print the browser, binary and profile in use")
    args = ap.parse_args(argv)
    kind = browser_kind()
    if args.cmd == "info":
        print(f"browser: {kind}\nbinary:  {browser_binary(kind) or '(not found)'}\n"
              f"profile: {profile_dir(kind)}")
        return 0
    with open_page(headless=True, kind=kind) as page:
        accounts = read_signed_in_accounts(page)
    verdict, message = account_verdict(accounts, expected_account())
    print(f"{kind} profile {profile_dir(kind)}: {message}")
    return {"ok": 0, "unchecked": 0}.get(verdict, 1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
