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
GET  /api/runs/<id>/sizes    bytes of its frames / chunk cache / download
POST /api/runs/<id>/clear    {"with_members": bool} — drop its bookkeeping
POST /api/runs/<id>/cache    {"what": ["frames","chunks","media"]}
POST /api/runs/<id>/tags     {"tags": ["…"]}
POST /api/runs/bulk          {"action": "clear"|"cache", "run_ids": [...], ...}
GET  /api/cache              the global tmp size; POST /api/cache/tmp empties it
GET  /api/drive/status       is Drive set up for the bot account (lib/gdrive.py)
GET  /api/drive/folders?parent=root|shared|drives|<id>   the folder browser
GET  /api/drive/folder?id=<id or link>                   one folder's name

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
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import fontchoice  # noqa: E402
import runadmin  # noqa: E402
import language  # noqa: E402
from promptnames import canonical_prompt_name  # noqa: E402

REPO = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("MEETING_BOT_ENV_FILE", str(REPO / ".env")))


def read_dotenv(path=None):
    """.env as it is on disk now: {key: value}, by source_env.sh's rules
    (blank lines and # comments skipped, one layer of quotes stripped, a
    leading ~/ is $HOME). Missing or unreadable -> {}."""
    out = {}
    try:
        lines = Path(path or ENV_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = "".join(key.split())
        if key.startswith("export") and key != "export":
            key = key[len("export"):]
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if val.startswith("~/"):
            val = os.path.expanduser("~") + val[1:]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            out[key] = val
    return out


# The keys .env named when this server started. serve.sh loaded them into
# os.environ, so they are a snapshot of .env at that moment — not overrides.
STARTUP_ENV_KEYS = frozenset(read_dotenv())


def current_config():
    """os.environ with .env re-read: the file's values win, and a key the file
    named at startup but has since dropped is gone. Without this every run
    started from the page inherited .env as it was when the UI started, and
    source_env.sh never overrides a set variable (2026-10-14: a run went to
    the Gemini chain six minutes after .env had switched it to claude)."""
    fresh = read_dotenv()
    env = {k: v for k, v in os.environ.items()
           if k not in STARTUP_ENV_KEYS or k in fresh}
    env.update(fresh)
    return env


def child_env():
    """The environment for a pipeline.sh child: without any .env key, so its
    own source_env.sh loads .env as it is at launch."""
    # pm2's Node IPC variables make deno (yt-dlp's JS solver) fail in children.
    drop = (STARTUP_ENV_KEYS | set(read_dotenv())
            | {"NODE_CHANNEL_FD", "NODE_CHANNEL_SERIALIZATION_MODE", "NODE_APP_INSTANCE"})
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["MEETING_BOT_FOREGROUND"] = "1"
    return env


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

STAGES = ("record", "fetch_video", "clip", "transcribe", "frames", "summarize",
          "upload")
GDRIVE = REPO / "lib" / "gdrive.py"
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
                        ("drive_folder", "--drive-folder"),
                        ("drive_folder_name", "--drive-folder-name"),
                        ("drive_name", "--drive-name")):
        value = body.get(field)
        if value not in (None, ""):
            args += [flag, str(value).strip()]
    # The combine toggle (2026-11-02): true = one document in the default
    # places (SUMMARIES_DIR / PDF_DIR, named for the combine run). A string
    # ending in .md is still taken as the path, for older phone shortcuts.
    combine = body.get("combine")
    if combine is True or (isinstance(combine, str)
                           and combine.strip().lower() in ("1", "true", "yes", "on")):
        args.append("--combine")
    elif isinstance(combine, str) and combine.strip():
        args += ["--combine", combine.strip()]
    tags = body.get("tags")
    if isinstance(tags, str):
        tags = tags.split(",")
    for tag in tags or []:
        if isinstance(tag, str) and tag.strip():
            args += ["--tag", tag.strip()]
    # Free text, possibly several lines: passed as one argument, never
    # through a shell. pipeline.sh stores it with the run.
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        args += ["--instructions", instructions.strip()]
    if body.get("no_drive"):
        args.append("--no-drive")
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


def _shell(cmd):
    """A pipeline.sh / kill_meeting.sh command line Windows can execute: there
    a .sh is not executable, so it runs under Git Bash (MEETING_BOT_BASH, or
    bash on PATH). Unchanged elsewhere."""
    if os.name == "nt" and cmd and str(cmd[0]).endswith(".sh"):
        return [os.environ.get("MEETING_BOT_BASH") or "bash", *cmd]
    return cmd


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
        env = child_env()
        subprocess.Popen(_shell(cmd), stdout=log_file, stderr=subprocess.STDOUT,
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
    active = owner.isdigit() and runadmin.is_active(run_dir)
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
        "stopped": (data.get("stopped") or {}).get("at"),
        # What the page needs to leave out stages that never run: frames on a
        # voice-only run, summarize on a member of a combined set.
        "summary_source": data.get("summary_source"),
        "combined_into": data.get("combined_into"),
        "members": data.get("members") or [],
        # The run history's categories: automatic (input_type, status from
        # the stages, prompt) and the operator's own tags.
        "prompt": canonical_prompt_name(data.get("prompt") or ""),
        "tags": data.get("tags") or [],
        "created_at": data.get("created_at"),
        "drive": ({"folder": data.get("drive_folder"),
                   "folder_name": data.get("drive_folder_name"),
                   "name": data.get("drive_name"),
                   "link": (stages.get("upload") or {}).get("drive_link"),
                   "error": ((stages.get("upload") or {}).get("error") or "")[-600:]}
                  if data.get("drive_folder") else None),
    }


def gdrive(*args, timeout=60):
    """lib/gdrive.py in a child, with .env as it is now (current_config):
    (returncode, parsed JSON or None, stderr)."""
    env = {k: v for k, v in current_config().items()
           if k not in ("NODE_CHANNEL_FD", "NODE_CHANNEL_SERIALIZATION_MODE",
                        "NODE_APP_INSTANCE")}
    try:
        proc = subprocess.run([sys.executable, str(GDRIVE), *args], capture_output=True,
                              text=True, timeout=timeout, cwd=str(REPO), env=env)
    except subprocess.TimeoutExpired:
        return 1, None, "Google Drive did not answer in time"
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
    except ValueError:
        out = None
    return proc.returncode, out, proc.stderr.strip()[-2000:]


def options():
    cfg = current_config()
    prompts = sorted(p.stem for p in PROMPTS_DIR.glob("*.md") if not p.stem.startswith("_"))
    default_prompt = canonical_prompt_name(cfg.get("SUMMARY_PROMPT", ""))
    try:
        default_summary_language = language.output_language(cfg)
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
        "default_fonts": {c: fontchoice.default_font(c, cfg) for c in codes},
        "languages": ["th", "en", "auto"],
        "default_language": cfg.get("ASSEMBLYAI_LANGUAGE", "th"),
        "default_jobs": cfg.get("PIPELINE_JOBS", "2"),
        "default_resources": cfg.get("RESOURCES", ""),
        "summaries_dir": cfg.get("SUMMARIES_DIR", ""),
        "recordings_dir": cfg.get("RECORDINGS_DIR", ""),
        # .env's SUMMARY_SOURCE / RECORD_MEDIA preselect the form; an invalid
        # value falls back here and is reported by Check (pipeline.sh).
        "default_summary_source": _choice(cfg.get("SUMMARY_SOURCE"),
                                          ("both", "voice")),
        "default_record_media": _choice(cfg.get("RECORD_MEDIA"),
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
        m = re.match(r"^/api/runs/([^/]+)/sizes$", path)
        if m:
            run_dir = self._run_dir(m.group(1))
            if run_dir is None:
                return
            self._json(200, runadmin.sizes(run_dir, runadmin.frames_root(current_config())))
            return
        if path == "/api/cache":
            self._json(200, {"tmp": runadmin.tmp_size(BOT_ROOT)})
            return
        if path.startswith("/api/drive/"):
            q = parse_qs(url.query)
            if path == "/api/drive/status":
                rc, out, err = gdrive("status", timeout=20)
            elif path == "/api/drive/folders":
                rc, out, err = gdrive("folders", "--parent", (q.get("parent") or ["root"])[0])
            elif path == "/api/drive/folder":
                rc, out, err = gdrive("folder", "--id", (q.get("id") or [""])[0])
            else:
                self._json(404, {"error": "not found"})
                return
            if rc == 0 and out is not None:
                self._json(200, out)
            else:
                self._json(502, {"error": err.replace("ERROR: ", "", 1) or "Google Drive failed"})
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
        if (path not in ("/trigger", "/api/check", "/api/cache/tmp")
                and not path.startswith("/api/runs/")):
            self.send_response(404)
            self.end_headers()
            return
        if not self._authorized():
            return

        if path == "/api/cache/tmp":
            try:
                self._json(200, runadmin.clear_tmp(BOT_ROOT, RUNS_DIR))
            except runadmin.AdminError as exc:
                self._json(409, {"error": str(exc)})
            return
        if path == "/api/runs/bulk":
            body = self._body()
            if body is None:
                return
            self._json(200, self._bulk(body))
            return
        m = re.match(r"^/api/runs/([^/]+)/(clear|cache|tags)$", path)
        if m:
            run_dir = self._run_dir(m.group(1))
            if run_dir is None:
                return
            body = self._body()
            if body is None:
                return
            try:
                self._json(200, self._admin(m.group(2), run_dir, body))
            except runadmin.AdminError as exc:
                self._json(409, {"error": str(exc)})
            return
        m = re.match(r"^/api/runs/([^/]+)/(resume|stop)$", path)
        if m:
            run_dir = self._run_dir(m.group(1))
            if run_dir is None:
                return
            if m.group(2) == "resume":
                # Optionally with a (new) Drive destination: a finished run
                # then only uploads its PDF (pipeline.sh resets `upload`).
                body = self._body()
                if body is None:
                    return
                extra = []
                for field, flag in (("drive_folder", "--drive-folder"),
                                    ("drive_folder_name", "--drive-folder-name"),
                                    ("drive_name", "--drive-name")):
                    value = body.get(field)
                    if isinstance(value, str) and value.strip():
                        extra += [flag, value.strip()]
                if body.get("no_drive"):
                    extra.append("--no-drive")
                log = launch([SCRIPT, "--run-id", run_dir.name, *extra])
                self._json(202, {"status": "started", "log": log.name})
            else:
                proc = subprocess.run(_shell([KILL_SCRIPT, "--run-id", run_dir.name]),
                                      capture_output=True, text=True, timeout=120,
                                      cwd=str(REPO), env=child_env())
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
            proc = subprocess.run(_shell([SCRIPT, *args, "--dry-run"]), capture_output=True,
                                  text=True, timeout=120, cwd=str(REPO),
                                  env=child_env())
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

    def _admin(self, action, run_dir, body):
        frames = runadmin.frames_root(current_config())
        if action == "clear":
            return runadmin.clear(RUNS_DIR, run_dir.name, frames,
                                  with_members=bool(body.get("with_members")))
        if action == "cache":
            what = body.get("what") or []
            if isinstance(what, str):
                what = what.split(",")
            return {"freed": runadmin.remove_cache(run_dir, what, frames)}
        tags = body.get("tags") or []
        if isinstance(tags, str):
            tags = tags.split(",")
        return runadmin.set_tags(run_dir, [t for t in tags if isinstance(t, str)])

    def _bulk(self, body):
        """The same actions over several runs; one result per run, so one
        refusal (a run being processed) does not hide the rest."""
        action = body.get("action")
        if action not in ("clear", "cache", "tags"):
            return {"error": "action must be clear, cache or tags", "results": []}
        results = []
        for run_id in body.get("run_ids") or []:
            if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
                results.append({"run_id": str(run_id), "error": "bad run id"})
                continue
            run_dir = RUNS_DIR / run_id
            if not (run_dir / "state.json").is_file():
                # Already gone — a combine run cleared with its members first.
                results.append({"run_id": run_id, "skipped": "no such run"})
                continue
            try:
                payload = dict(body)
                if action == "tags":
                    # Bulk tagging adds to each run's own tags.
                    data = read_state(run_dir) or {}
                    add = body.get("tags") or []
                    if isinstance(add, str):
                        add = add.split(",")
                    payload["tags"] = (data.get("tags") or []) + list(add)
                results.append({"run_id": run_id,
                                **self._admin(action, run_dir, payload)})
            except runadmin.AdminError as exc:
                results.append({"run_id": run_id, "error": str(exc)})
        return {"results": results}

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
