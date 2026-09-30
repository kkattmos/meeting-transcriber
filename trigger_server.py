#!/usr/bin/env python3
"""
The meeting bot's web UI and HTTP trigger.

Intended to be reached only from this machine or over Tailscale, with a
shared-secret token as a second layer. MEETING_BOT_BIND is a comma-separated
list of addresses to listen on; the word `tailscale` stands for this host's
Tailscale IPv4 (looked up at start, skipped with a warning if Tailscale is
down). The PC default in .env.example is `127.0.0.1,tailscale`. pm2 runs it
(ecosystem.config.js, ./webui.sh on|off) — nothing starts it at boot.

GET /
  The web UI (web/index.html): start a new Google Meet, fill in the pipeline's
  options as a form instead of a command line, check them before anything
  starts, and watch the runs. No auth to load the page itself — it holds no
  data; every call it makes carries the token.

POST /trigger          (Authorization: Bearer <token>)
  Body (JSON), one input:
    {"url": "<meeting_or_youtube_url>", "name": "Weekly Standup"}
  or several at once:
    {"urls": ["https://youtu.be/a", "https://youtu.be/b"], "jobs": 2,
     "language": "th", "prompt": "lecture"}
  or a new meeting the bot creates and hosts:
    {"new_meet": true, "name": "Project sync"}

  Optional fields: name, language (spoken), prompt (video | meeting | lecture |
  tutorial), summary_language (th | en, what the notes are written in),
  pdf_font, instructions (extra instructions for the summarizer),
  summary_source (both | voice: frames and transcript, or the transcript
  alone), record_media (video | audio: what a meeting's recording keeps), jobs,
  display_name, clip, combine, no_combine_pdf, resources (a GitHub repo or
  local path, or a list of them), playlist, and force.

  Responds 202 immediately; pipeline.sh runs detached. Its output goes to
  $MEETING_BOT_ROOT/logs/trigger_<timestamp>.log — the response carries the
  path, because a run triggered from a phone is exactly the one you can't
  watch, and a failure that left no trace can't be diagnosed later.

POST /api/check        the same body; runs `pipeline.sh --dry-run` — the real
                       parser and classifier, so the form cannot disagree with
                       the pipeline — and returns what would run, or the error.
GET  /api/options      prompts, languages, fonts and defaults for the form
GET  /api/runs         recent runs with their stage status
GET  /api/runs/<id>    one run: state.json plus the tail of each stage log
POST /api/runs/<id>/resume   ./pipeline.sh --run-id <id>, detached
POST /api/runs/<id>/stop     ./kill_meeting.sh --run-id <id> (leaves cleanly)
GET  /api/log?name=trigger_<...>.log   the tail of one trigger log

GET /health
  No auth. Returns 200 so you can check the service is up from your phone.

Env vars:
  MEETING_BOT_TOKEN   - shared secret (required)
  MEETING_BOT_SCRIPT  - path to pipeline.sh (default: beside this file)
  MEETING_BOT_PORT    - listen port (default 8765)
  MEETING_BOT_BIND    - listen addresses, comma-separated; `tailscale` = this
                        host's Tailscale IPv4 (default 127.0.0.1)
  MEETING_BOT_ROOT    - the pipeline's state root (default ~/.local/share/meeting-bot)
"""
import hmac
import json
import os
import re
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import sys

# Stdlib-only helpers shared with the summarizer, so the form offers exactly
# what summarize.py accepts: the four prompts and their old names, the output
# languages, and the fonts each language may use.
sys.path.insert(0, str(Path(__file__).resolve().parent / "summarize"))
import fontchoice  # noqa: E402
import language  # noqa: E402
from promptnames import canonical_prompt_name  # noqa: E402

REPO = Path(__file__).resolve().parent
TOKEN = os.environ.get("MEETING_BOT_TOKEN")
SCRIPT = os.environ.get("MEETING_BOT_SCRIPT", str(REPO / "pipeline.sh"))
KILL_SCRIPT = str(REPO / "kill_meeting.sh")
PORT = int(os.environ.get("MEETING_BOT_PORT", "8765"))
BIND = os.environ.get("MEETING_BOT_BIND", "127.0.0.1")
BOT_ROOT = Path(os.environ.get("MEETING_BOT_ROOT", os.path.expanduser("~/.local/share/meeting-bot")))
LOG_DIR = BOT_ROOT / "logs"
RUNS_DIR = BOT_ROOT / "runs"
PROMPTS_DIR = REPO / "summarize" / "prompts"
INDEX_HTML = REPO / "web" / "index.html"

STAGES = ("record", "fetch_video", "clip", "transcribe", "frames", "summarize")
# A run id is a directory name under runs/; nothing else may reach a path.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# trigger_*: started from here. pipeline_*: a meeting started from the command
# line, which pipeline.sh detached into the background with its own log.
LOG_NAME_RE = re.compile(r"^(trigger|pipeline)_[0-9_]+\.log$")

if not TOKEN:
    raise SystemExit("MEETING_BOT_TOKEN env var must be set")


def build_args(body):
    """The pipeline.sh arguments a request body stands for.

    Shared by /trigger and /api/check, so what is checked is exactly what
    runs. Returns (args, error).
    """
    urls = body.get("urls")
    if isinstance(urls, str):
        urls = urls.splitlines()
    if not urls:
        single = body.get("url")
        urls = [single] if single else []
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    new_meet = bool(body.get("new_meet"))
    if not urls and not new_meet:
        return None, "missing 'url' or 'urls' (or 'new_meet')"

    args = list(urls)
    if new_meet:
        args.append("--new-meet")
    # --name only applies to a single input; pipeline.sh derives per-input
    # names otherwise and warns if you pass one anyway.
    name = body.get("name")
    if name and len(urls) + int(new_meet) == 1:
        args += ["--name", str(name)]
    for field, flag in (("language", "--language"),
                        ("prompt", "--prompt"),
                        ("summary_language", "--summary-language"),
                        ("pdf_font", "--pdf-font"),
                        ("summary_source", "--summary-source"),
                        ("record_media", "--record-media"),
                        ("display_name", "--display-name"),
                        ("jobs", "--jobs"),
                        ("clip", "--clip"),
                        ("combine", "--combine")):
        value = body.get(field)
        if value not in (None, ""):
            args += [flag, str(value).strip()]
    # Free text, possibly several lines: passed as one argument, never
    # through a shell. pipeline.sh stores it with the run.
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        args += ["--instructions", instructions.strip()]
    if body.get("no_combine_pdf"):
        args.append("--no-combine-pdf")
    # `resources` may be a single spec, a list, or newline-separated text;
    # pipeline.sh takes the flag repeatedly.
    resources = body.get("resources")
    if isinstance(resources, str):
        resources = resources.splitlines()
    for spec in resources or []:
        if isinstance(spec, str) and spec.strip():
            args += ["--resources", spec.strip()]
    if body.get("force"):
        args.append("--force")
    if body.get("playlist"):
        args.append("--playlist")
    return args, None


def launch(cmd):
    """Start cmd detached, its output in a fresh trigger log. Returns the log path."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Two triggers in the same second must not share a log file — the
    # second would silently overwrite the first, losing exactly the record
    # this exists to keep. Open exclusively and suffix until it's unique.
    suffix = 0
    while True:
        candidate = LOG_DIR / (f"trigger_{stamp}.log" if suffix == 0
                               else f"trigger_{stamp}_{suffix}.log")
        try:
            log_file = open(candidate, "xb")
            break
        except FileExistsError:
            suffix += 1
    # Detached, with output captured to a file rather than discarded: this
    # is the path you use when you can't watch the terminal, so a failure
    # that left no trace would be undiagnosable.
    try:
        # Already detached and logged here, so pipeline.sh must not detach a
        # meeting a second time into a log this page doesn't know about.
        env = dict(os.environ, MEETING_BOT_FOREGROUND="1")
        subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                         start_new_session=True, cwd=str(REPO), env=env)
    finally:
        # The child holds its own descriptor; ours is no longer needed.
        log_file.close()
    return candidate


def tail(path, lines=60):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


def read_state(run_dir):
    try:
        return json.loads((run_dir / "state.json").read_text())
    except (OSError, ValueError):
        return None


def run_summary(run_dir, data):
    stages = data.get("stages") or {}
    owner = ""
    try:
        owner = (run_dir / "run.lock" / "pid").read_text().strip()
    except OSError:
        pass
    active = False
    if owner.isdigit():
        try:
            os.kill(int(owner), 0)
            active = True
        except OSError:
            pass
    # The recorder's live audio check (record_screen.sh audio_watch):
    # "<epoch> <peak dB> <seconds silent>".
    audio = None
    try:
        at, peak, silent = (run_dir / "audio_level").read_text().split()[:3]
        audio = {"at": int(at), "peak_db": float(peak), "silent_for": int(silent)}
    except (OSError, ValueError):
        pass
    return {
        "run_id": run_dir.name,
        "audio": audio,
        "name": data.get("name"),
        "input": data.get("input"),
        "input_type": data.get("input_type"),
        "clip": data.get("clip"),
        "meet_url": data.get("meet_url"),
        "updated_at": data.get("updated_at"),
        "active": active,
        "stages": {s: (stages.get(s) or {}).get("status", "pending") for s in STAGES},
        "artifacts": (stages.get("summarize") or {}).get("artifacts") or {},
        "paused": bool((stages.get("summarize") or {}).get("rate_limited")),
        # What the page needs to leave out stages that never run: frames on a
        # voice-only run, summarize on a member of a combined set.
        "summary_source": data.get("summary_source"),
        "combined_into": data.get("combined_into"),
    }


def options():
    prompts = sorted(p.stem for p in PROMPTS_DIR.glob("*.md") if not p.stem.startswith("_"))
    default_prompt = canonical_prompt_name(os.environ.get("SUMMARY_PROMPT", ""))
    try:
        default_summary_language = language.output_language()
    except language.UnknownLanguage:
        # A typo in SUMMARY_LANGUAGE must not take the page down; the
        # pipeline reports it properly on Check.
        default_summary_language = language.DEFAULT
    codes = sorted(fontchoice.CHOICES)
    return {
        "prompts": prompts,
        "default_prompt": default_prompt if default_prompt in prompts else "",
        "summary_languages": codes,
        "summary_language_names": {c: language.language_name(c) for c in codes},
        "default_summary_language": default_summary_language,
        "fonts": {c: list(fontchoice.CHOICES[c]) for c in codes},
        "default_fonts": {c: fontchoice.default_font(c) for c in codes},
        "languages": ["th", "en", "auto"],
        "default_language": os.environ.get("ASSEMBLYAI_LANGUAGE", "th"),
        "default_jobs": os.environ.get("PIPELINE_JOBS", "2"),
        "default_resources": os.environ.get("RESOURCES", ""),
        "summaries_dir": os.environ.get("SUMMARIES_DIR", ""),
        "recordings_dir": os.environ.get("RECORDINGS_DIR", ""),
        # .env's SUMMARY_SOURCE / RECORD_MEDIA preselect the form; an invalid
        # value falls back here and is reported by Check (pipeline.sh).
        "default_summary_source": _choice(os.environ.get("SUMMARY_SOURCE"),
                                          ("both", "voice")),
        "default_record_media": _choice(os.environ.get("RECORD_MEDIA"),
                                        ("video", "audio")),
    }


def _choice(value, allowed):
    value = (value or "").strip()
    return value if value in allowed else allowed[0]


class Handler(BaseHTTPRequestHandler):
    def _unauthorized(self):
        self._json(401, {"error": "unauthorized — check the token"})

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        # Constant-time: the UI may be reachable from the tailnet.
        given = self.headers.get("Authorization", "").encode("utf-8", "replace")
        if hmac.compare_digest(given, f"Bearer {TOKEN}".encode("utf-8")):
            return True
        self._unauthorized()
        return False

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return None
        if not isinstance(body, dict):
            self._json(400, {"error": "expected a JSON object"})
            return None
        return body

    def _run_dir(self, run_id):
        if not RUN_ID_RE.match(run_id or "") or not (RUNS_DIR / run_id).is_dir():
            self._json(404, {"error": f"no such run: {run_id}"})
            return None
        return RUNS_DIR / run_id

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        # Unauthenticated on purpose: it reveals nothing beyond "the service is
        # running", and needing a token to check that from a phone is friction
        # with no benefit.
        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path in ("/", "/index.html"):
            try:
                page = INDEX_HTML.read_bytes()
            except OSError:
                self._json(500, {"error": f"missing {INDEX_HTML}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        if not path.startswith("/api/"):
            self.send_response(404)
            self.end_headers()
            return
        if not self._authorized():
            return

        if path == "/api/options":
            self._json(200, options())
            return
        if path == "/api/runs":
            limit = int((parse_qs(url.query).get("limit") or ["40"])[0])
            runs = []
            if RUNS_DIR.is_dir():
                dirs = sorted((d for d in RUNS_DIR.iterdir() if d.is_dir()),
                              key=lambda d: d.stat().st_mtime, reverse=True)
                for d in dirs[:limit]:
                    data = read_state(d)
                    if data:
                        runs.append(run_summary(d, data))
            self._json(200, {"runs": runs})
            return
        m = re.match(r"^/api/runs/([^/]+)$", path)
        if m:
            run_dir = self._run_dir(m.group(1))
            if run_dir is None:
                return
            data = read_state(run_dir) or {}
            logs = {}
            for log in sorted((run_dir / "logs").glob("*.log")):
                logs[log.stem] = tail(log, 40)
            self._json(200, {"summary": run_summary(run_dir, data),
                             "state": data, "logs": logs})
            return
        if path == "/api/log":
            name = (parse_qs(url.query).get("name") or [""])[0]
            if not LOG_NAME_RE.match(name):
                self._json(400, {"error": "bad log name"})
                return
            log = LOG_DIR / name
            if not log.is_file():
                self._json(404, {"error": "no such log"})
                return
            self._json(200, {"name": name, "text": tail(log, 200)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/trigger", "/api/check") and not path.startswith("/api/runs/"):
            self.send_response(404)
            self.end_headers()
            return
        if not self._authorized():
            return

        m = re.match(r"^/api/runs/([^/]+)/(resume|stop)$", path)
        if m:
            run_dir = self._run_dir(m.group(1))
            if run_dir is None:
                return
            if m.group(2) == "resume":
                log = launch([SCRIPT, "--run-id", run_dir.name])
                self._json(202, {"status": "started", "log": log.name})
            else:
                proc = subprocess.run([KILL_SCRIPT, "--run-id", run_dir.name],
                                      capture_output=True, text=True, timeout=120,
                                      cwd=str(REPO))
                self._json(200 if proc.returncode == 0 else 500,
                           {"status": "stopping" if proc.returncode == 0 else "error",
                            "output": (proc.stdout + proc.stderr)[-4000:]})
            return

        body = self._body()
        if body is None:
            return
        args, error = build_args(body)
        if error:
            self._json(400, {"error": error})
            return

        if path == "/api/check":
            proc = subprocess.run([SCRIPT, *args, "--dry-run"], capture_output=True,
                                  text=True, timeout=120, cwd=str(REPO))
            plan = []
            for line in proc.stdout.splitlines():
                parts = line.split("\t")
                if parts[0] == "ok" and len(parts) == 5:
                    plan.append({"status": "ok", "kind": parts[1],
                                 "clip": None if parts[2] == "-" else parts[2],
                                 "resume": None if parts[3] == "new" else parts[3],
                                 "input": parts[4]})
                elif parts[0] == "combine" and len(parts) == 2:
                    plan.append({"status": "ok", "kind": "combine", "input": parts[1]})
                # The dry run reports every input it can't use, not just the
                # first, so the page can mark each line. `bad` names the input
                # as classified (window split off, meet.new canonical), in
                # input order; `badarg` the argument as typed (a #t= window
                # that did not parse, refused before classification).
                elif parts[0] in ("bad", "badarg") and len(parts) == 3:
                    plan.append({"status": "bad", "reason": parts[1], "input": parts[2],
                                 "arg": parts[0] == "badarg"})
                # Not an input at all. On the command line that may be the
                # legacy form's name; every line of this form is meant as an
                # input, so here it is a mistake, even though the dry run
                # itself passed (a mistyped path would become the run's name).
                elif parts[0] == "extra" and len(parts) == 2:
                    plan.append({"status": "bad", "extra": True, "arg": True,
                                 "input": parts[1],
                                 "reason": "not recognised as an input: check the link, "
                                           "or that the file exists on this machine"})
            ok = proc.returncode == 0 and all(p["status"] == "ok" for p in plan)
            self._json(200, {"ok": ok, "plan": plan,
                             "messages": proc.stderr.strip()[-4000:],
                             "command": ["./pipeline.sh", *args]})
            return

        log = launch([SCRIPT, *args])
        self._json(202, {
            "status": "started",
            "inputs": [a for a in args if not a.startswith("--")][:50],
            "log": str(log),
            "log_name": log.name,
        })

    def log_message(self, fmt, *args):
        print(f"[trigger-server] {self.address_string()} - {fmt % args}")


def bind_addresses(spec):
    """MEETING_BOT_BIND -> concrete addresses. `tailscale` is resolved here."""
    out = []
    for item in (spec or "127.0.0.1").split(","):
        item = item.strip()
        if not item:
            continue
        if item.lower() == "localhost":
            item = "127.0.0.1"
        if item.lower() == "tailscale":
            try:
                ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                    text=True, timeout=10).stdout.split()
            except Exception:
                ip = []
            if not ip:
                print("[trigger-server] WARNING: Tailscale has no IPv4 address "
                      "(not running?) — not listening on it.")
                continue
            item = ip[0]
        if item not in out:
            out.append(item)
    return out


if __name__ == "__main__":
    import threading
    servers = []
    for addr in bind_addresses(BIND):
        try:
            servers.append(ThreadingHTTPServer((addr, PORT), Handler))
        except OSError as e:
            print(f"[trigger-server] WARNING: cannot listen on {addr}:{PORT} ({e})")
            continue
        host = "localhost" if addr in ("127.0.0.1", "0.0.0.0") else addr
        print(f"Listening on {addr}:{PORT} — open http://{host}:{PORT}/", flush=True)
    if not servers:
        raise SystemExit("no address to listen on (MEETING_BOT_BIND)")
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()
