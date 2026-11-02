#!/usr/bin/env python3
"""Google Drive for the summary PDF: the bot account's OAuth login, its
folders, and the upload (settled with the operator 2026-11-02).

Why OAuth and why this account: the operator asked for the PDF to go "to a
Google Drive folder that the host Gmail account has access to" — the bot
account, BOT_GOOGLE_ACCOUNT, the same one the recorder signs into Meet with.
A service account was rejected because it owns what it uploads and sees no
folder unless each is shared with it. So: an OAuth "Desktop app" client
(GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET in .env), one consent as the bot
account (`gdrive.py login`, the loopback redirect Google requires of desktop
clients), and a refresh token kept in $MEETING_BOT_ROOT/state/gdrive_token.json
(mode 600). The scope is the full `drive`: the web UI's folder browser lists
folders shared with the bot, which `drive.file` cannot see.

Uploads replace a same-named file in the target folder (a new revision, same
link) rather than adding a second one — the operator's choice.

CLI (every command prints one JSON object on stdout; errors exit 1 with the
reason on stderr):

    gdrive.py status                      configured / logged in / which account
    gdrive.py login [--no-browser] [--port N]
    gdrive.py logout
    gdrive.py folders [--parent root|shared|drives|<folder id>]
    gdrive.py folder  --id <folder id or URL>        its name, and whether we can write
    gdrive.py upload  --file F --folder <id or URL> [--name NAME]

Test seams, like ASSEMBLYAI_BASE_URL: GDRIVE_AUTH_URL, GDRIVE_TOKEN_URL,
GDRIVE_API_URL, GDRIVE_UPLOAD_URL, and GDRIVE_TOKEN_FILE.

`requests` is imported lazily, so `status`, `parse` and the web UI's import of
this module never need it.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"

AUTH_URL = os.environ.get("GDRIVE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URL = os.environ.get("GDRIVE_TOKEN_URL", "https://oauth2.googleapis.com/token")
API_URL = os.environ.get("GDRIVE_API_URL", "https://www.googleapis.com/drive/v3").rstrip("/")
UPLOAD_URL = os.environ.get("GDRIVE_UPLOAD_URL",
                            "https://www.googleapis.com/upload/drive/v3").rstrip("/")
TIMEOUT = 60

# A folder id, or a folder link pasted from the browser:
#   https://drive.google.com/drive/folders/<id>
#   https://drive.google.com/drive/u/1/folders/<id>?usp=sharing
#   https://drive.google.com/open?id=<id>
_FOLDER_URL_RE = re.compile(r"/folders/([A-Za-z0-9_-]{10,})|[?&]id=([A-Za-z0-9_-]{10,})")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
# The pseudo-parents the folder browser starts from.
PSEUDO_PARENTS = ("root", "shared", "drives")


class DriveError(Exception):
    """Anything that stops a Drive call, worded for the operator."""


class NotLoggedIn(DriveError):
    pass


def _root():
    return Path(os.environ.get("MEETING_BOT_ROOT",
                               os.path.expanduser("~/.local/share/meeting-bot")))


def token_file():
    override = os.environ.get("GDRIVE_TOKEN_FILE")
    return Path(override) if override else _root() / "state" / "gdrive_token.json"


def client():
    """(client id, client secret) from .env, or (None, None)."""
    cid = (os.environ.get("GDRIVE_CLIENT_ID") or "").strip()
    secret = (os.environ.get("GDRIVE_CLIENT_SECRET") or "").strip()
    return (cid or None), (secret or None)


def expected_account():
    return (os.environ.get("BOT_GOOGLE_ACCOUNT") or "").strip() or None


def normalize_email(addr):
    """Gmail addresses compare case- and dot-insensitively (as browser.py)."""
    addr = (addr or "").strip().lower()
    local, _, domain = addr.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}" if domain else local


def parse_folder_id(value):
    """A folder id from an id or a pasted Drive link; None if neither."""
    value = (value or "").strip()
    if not value:
        return None
    if value in PSEUDO_PARENTS or _ID_RE.match(value):
        return value
    m = _FOLDER_URL_RE.search(value)
    if m:
        return m.group(1) or m.group(2)
    return None


def pdf_name(name, fallback):
    """The name a PDF is given in Drive: the operator's, else the fallback,
    always ending in .pdf, never containing a path separator."""
    name = (name or "").strip() or (fallback or "").strip() or "summary"
    name = name.replace("/", "-").replace("\\", "-").strip()
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def _q_quote(text):
    """A string literal in a Drive `q` query."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


# --- tokens -------------------------------------------------------------------

def load_token():
    try:
        return json.loads(token_file().read_text())
    except (OSError, ValueError):
        return None


def save_token(data):
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _requests():
    try:
        import requests  # noqa: WPS433 — lazy on purpose (see the docstring)
    except ImportError as exc:  # pragma: no cover - pinned in requirements.txt
        raise DriveError("the 'requests' package is missing; run ./setup.sh") from exc
    return requests


def _with_retries(func, label):
    """The project's backoff policy (summarize/retry.py), as kaltura.py uses it."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "summarize"))
    try:
        from retry import with_retries
    finally:
        sys.path.pop(0)
    return with_retries(func, label=label)


def _check(resp, what):
    """Raise for a failed response: HTTPError for the statuses retry.py
    retries, DriveError (never retried) with Google's own words otherwise."""
    if resp.status_code < 400:
        return resp
    err = None
    try:
        err = resp.json().get("error")
        detail = err.get("message") if isinstance(err, dict) else (
            resp.json().get("error_description") or err)
    except (ValueError, AttributeError):
        detail = resp.text[:300]
    if resp.status_code in (408, 429, 500, 502, 503, 504):
        resp.raise_for_status()
    if resp.status_code == 401 or err == "invalid_grant":
        raise NotLoggedIn(f"{what}: Google refused the login ({detail}). "
                          "Sign in again: python3 lib/gdrive.py login")
    raise DriveError(f"{what}: HTTP {resp.status_code}: {detail}")


def access_token():
    """A fresh access token from the stored refresh token."""
    tok = load_token()
    if not tok or not tok.get("refresh_token"):
        raise NotLoggedIn("Google Drive is not signed in. On the PC run: "
                          "python3 lib/gdrive.py login   (as BOT_GOOGLE_ACCOUNT)")
    cid, secret = client()
    cid = cid or tok.get("client_id")
    if not cid or not secret:
        raise DriveError("GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET are not set in .env")
    requests = _requests()

    def call():
        return _check(requests.post(TOKEN_URL, data={
            "client_id": cid, "client_secret": secret,
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
        }, timeout=TIMEOUT), "refreshing the Drive login")
    return _with_retries(call, "drive token").json()["access_token"]


class Drive:
    """A thin client over the Drive v3 REST API."""

    def __init__(self, token=None):
        self.requests = _requests()
        self.token = token or access_token()

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.token}"}
        h.update(extra or {})
        return h

    def get(self, path, params, what):
        def call():
            return _check(self.requests.get(f"{API_URL}/{path}", params=params,
                                            headers=self._headers(), timeout=TIMEOUT), what)
        return _with_retries(call, what).json()

    def about(self):
        return self.get("about", {"fields": "user(emailAddress,displayName)"},
                        "reading the Drive account").get("user") or {}

    def _list(self, params, what):
        params = dict(params, supportsAllDrives="true", includeItemsFromAllDrives="true",
                      pageSize=200)
        out, page = [], None
        while True:
            if page:
                params["pageToken"] = page
            data = self.get("files", params, what)
            out.extend(data.get("files") or [])
            page = data.get("nextPageToken")
            if not page or len(out) >= 1000:
                return out

    def folders(self, parent="root"):
        """Sub-folders of a folder, or of a pseudo-parent: My Drive (root),
        Shared with me (shared), the shared drives (drives)."""
        fields = "nextPageToken, files(id, name, shared, driveId, capabilities/canAddChildren)"
        if parent == "drives":
            items, page = [], None
            while True:
                params = {"pageSize": 100, "fields": "nextPageToken, drives(id, name)"}
                if page:
                    params["pageToken"] = page
                data = self.get("drives", params, "listing shared drives")
                items.extend(data.get("drives") or [])
                page = data.get("nextPageToken")
                if not page:
                    break
            return [{"id": d["id"], "name": d["name"], "can_write": True, "drive": True}
                    for d in items]
        if parent == "shared":
            q = f"sharedWithMe and mimeType = '{FOLDER_MIME}' and trashed = false"
        else:
            q = f"{_q_quote(parent)} in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
        files = self._list({"q": q, "fields": fields, "orderBy": "name"}, "listing folders")
        return [{"id": f["id"], "name": f["name"], "shared": bool(f.get("shared")),
                 "can_write": bool((f.get("capabilities") or {}).get("canAddChildren"))}
                for f in files]

    def folder(self, folder_id):
        data = self.get(f"files/{folder_id}", {
            "fields": "id, name, mimeType, trashed, capabilities/canAddChildren",
            "supportsAllDrives": "true"}, "reading the folder")
        if data.get("mimeType") != FOLDER_MIME:
            raise DriveError(f"{folder_id} is not a folder")
        if data.get("trashed"):
            raise DriveError(f"the folder '{data.get('name')}' is in the trash")
        return {"id": data["id"], "name": data.get("name"),
                "can_write": bool((data.get("capabilities") or {}).get("canAddChildren"))}

    def find(self, folder_id, name):
        q = (f"name = {_q_quote(name)} and {_q_quote(folder_id)} in parents"
             f" and trashed = false and mimeType != '{FOLDER_MIME}'")
        return self._list({"q": q, "fields": "nextPageToken, files(id, name)"},
                          "looking for an existing file")

    def upload(self, path, folder_id, name, mime="application/pdf"):
        """Upload `path` into the folder as `name`; replace a same-named file
        there (a new revision of it) instead of adding a second one."""
        path = Path(path)
        data = path.read_bytes()
        existing = self.find(folder_id, name)
        if existing:
            target = existing[0]["id"]
            method, url = "PATCH", f"{UPLOAD_URL}/files/{target}"
            meta = {"name": name}
        else:
            method, url = "POST", f"{UPLOAD_URL}/files"
            meta = {"name": name, "parents": [folder_id], "mimeType": mime}
        params = {"uploadType": "resumable", "supportsAllDrives": "true",
                  "fields": "id, name, webViewLink"}

        def call():
            start = _check(self.requests.request(
                method, url, params=params, json=meta,
                headers=self._headers({"X-Upload-Content-Type": mime,
                                       "X-Upload-Content-Length": str(len(data))}),
                timeout=TIMEOUT), "starting the upload")
            session = start.headers.get("Location")
            if not session:
                raise DriveError("Drive did not return an upload session")
            return _check(self.requests.put(
                session, data=data,
                headers={"Content-Type": mime, "Content-Length": str(len(data))},
                timeout=max(TIMEOUT, len(data) // 100_000)), "uploading the file")
        result = _with_retries(call, "drive upload").json()
        result["replaced"] = bool(existing)
        if not result.get("webViewLink") and result.get("id"):
            result["webViewLink"] = f"https://drive.google.com/file/d/{result['id']}/view"
        return result


# --- login --------------------------------------------------------------------

def _pkce():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def auth_url(cid, redirect, challenge, state, login_hint=None):
    params = {"client_id": cid, "redirect_uri": redirect, "response_type": "code",
              "scope": SCOPE, "access_type": "offline", "prompt": "consent",
              "code_challenge": challenge, "code_challenge_method": "S256",
              "state": state}
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _open_in_bot_browser(url):
    """Open the consent page in the bot's own browser profile, which is
    already signed in as the bot account — a direct launch of the binary,
    never a driven one (Google's sign-in detects automation). Returns False
    when that is not possible; the caller then prints the URL."""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "screen"))
    try:
        import browser  # noqa: WPS433
        kind = browser.browser_kind()
        binary = browser.browser_binary(kind)
        profile = browser.profile_dir(kind)
        for lock in ("lock", ".parentlock", "SingletonLock"):
            pid = browser._lock_owner_pid(Path(profile) / lock)
            if pid and browser._pid_alive(pid):
                print(f"The bot's browser profile is in use (pid {pid}, a recording?); "
                      "open the link below in any browser signed in as the bot.",
                      file=sys.stderr)
                return False
    except Exception:  # noqa: BLE001 — falling back to printing is always fine
        return False
    finally:
        sys.path.pop(0)
    if not binary or not shutil.which(str(binary)) and not Path(str(binary)).exists():
        return False
    if kind == "chrome":
        cmd = [str(binary), f"--user-data-dir={profile}", "--no-first-run", url]
    else:
        user_js = Path(profile) / "user.js"
        if user_js.exists():
            user_js.replace(Path(profile) / "user.js.recorder")
        cmd = [str(binary), "--no-remote", "--new-instance", "--profile", str(profile), url]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def login(open_browser=True, port=0, timeout=600):
    cid, secret = client()
    if not cid or not secret:
        raise DriveError(
            "GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET must be set in .env first.\n"
            "  Google Cloud console → APIs & Services → enable the Google Drive API,\n"
            "  then Credentials → Create credentials → OAuth client ID → Desktop app.\n"
            "  (README: \"Google Drive upload\".)")
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    got = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            got["code"] = qs.get("code", [None])[0]
            got["error"] = qs.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("Signed in. You can close this window." if got["code"]
                   else f"Sign-in failed: {got['error']}")
            self.wfile.write(f"<p style='font:16px sans-serif'>{msg}</p>".encode())
            done.set()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = auth_url(cid, redirect, challenge, state, expected_account())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opened = open_browser and _open_in_bot_browser(url)
    print(f"Sign in as {expected_account() or 'the bot account'} and allow Drive access"
          + (" in the window that just opened." if opened else ":"), file=sys.stderr)
    if not opened:
        print(f"\n  {url}\n\n(The page redirects to {redirect}, so open it on this PC.)",
              file=sys.stderr)
    finished = done.wait(timeout)
    server.shutdown()
    if not finished:
        raise DriveError("no answer from the sign-in page within "
                         f"{timeout // 60} minutes")
    if not got.get("code"):
        raise DriveError(f"sign-in refused: {got.get('error')}")
    requests = _requests()
    resp = _check(requests.post(TOKEN_URL, data={
        "client_id": cid, "client_secret": secret, "code": got["code"],
        "code_verifier": verifier, "redirect_uri": redirect,
        "grant_type": "authorization_code"}, timeout=TIMEOUT), "finishing the sign-in")
    tok = resp.json()
    if not tok.get("refresh_token"):
        raise DriveError("Google returned no refresh token; remove the app's access at "
                         "myaccount.google.com/permissions and sign in again")
    user = Drive(tok["access_token"]).about()
    email = user.get("emailAddress")
    want = expected_account()
    if want and normalize_email(email) != normalize_email(want):
        raise DriveError(f"signed in as {email}, but BOT_GOOGLE_ACCOUNT is {want}; "
                         "nothing was saved. Sign in again as the bot account.")
    save_token({"refresh_token": tok["refresh_token"], "account": email,
                "client_id": cid, "scope": tok.get("scope", SCOPE)})
    return {"ok": True, "account": email}


def status():
    cid, secret = client()
    tok = load_token() or {}
    want = expected_account()
    account = tok.get("account")
    return {
        "configured": bool(cid and secret),
        "logged_in": bool(tok.get("refresh_token")),
        "account": account,
        "expected": want,
        "account_ok": (not want or not account
                       or normalize_email(account) == normalize_email(want)),
    }


# --- CLI ----------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("login")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--port", type=int, default=0)
    sub.add_parser("logout")
    p = sub.add_parser("folders")
    p.add_argument("--parent", default="root")
    p = sub.add_parser("folder")
    p.add_argument("--id", required=True)
    p = sub.add_parser("parse")
    p.add_argument("value")
    p = sub.add_parser("upload")
    p.add_argument("--file", required=True)
    p.add_argument("--folder", required=True)
    p.add_argument("--name")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "status":
            out = status()
        elif args.cmd == "parse":
            fid = parse_folder_id(args.value)
            if not fid:
                raise DriveError(f"not a Drive folder id or link: {args.value}")
            out = {"id": fid}
        elif args.cmd == "login":
            out = login(open_browser=not args.no_browser, port=args.port)
        elif args.cmd == "logout":
            try:
                token_file().unlink()
            except FileNotFoundError:
                pass
            out = {"ok": True}
        elif args.cmd == "folders":
            parent = parse_folder_id(args.parent)
            if not parent:
                raise DriveError(f"not a Drive folder id or link: {args.parent}")
            out = {"parent": parent, "folders": Drive().folders(parent)}
        elif args.cmd == "folder":
            fid = parse_folder_id(args.id)
            if not fid or fid in PSEUDO_PARENTS and fid != "root":
                raise DriveError(f"not a Drive folder id or link: {args.id}")
            out = Drive().folder(fid)
        elif args.cmd == "upload":
            fid = parse_folder_id(args.folder)
            if not fid or fid in ("shared", "drives"):
                raise DriveError(f"not a Drive folder id or link: {args.folder}")
            src = Path(args.file)
            if not src.is_file():
                raise DriveError(f"no such file: {src}")
            name = pdf_name(args.name, src.stem)
            out = Drive().upload(src, fid, name)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except DriveError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — network failures after retries
        print(f"ERROR: Google Drive: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
