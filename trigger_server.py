#!/usr/bin/env python3
"""
Minimal HTTP endpoint to remotely trigger a meeting recording.
Intended to be reached only over Tailscale (no public exposure), plus a
shared-secret token as a second layer.

POST /trigger
  Headers: Authorization: Bearer <token>
  Body (JSON), one input:
    {"url": "<meeting_or_youtube_url>", "name": "Weekly Standup"}
  or several at once:
    {"urls": ["https://youtu.be/a", "https://youtu.be/b"], "jobs": 2,
     "language": "th", "prompt": "lecture-gemini"}

  Optional fields: name, language, prompt, jobs, display_name, combine,
  resources (a GitHub repo or local path with the session's slides), and force.

  Responds 202 immediately; pipeline.sh runs detached. Its output goes to
  /opt/meeting-bot/logs/trigger_<timestamp>.log — the response carries the path,
  because a run triggered from a phone is exactly the one you can't watch, and
  a failure that left no trace can't be diagnosed later.

GET /health
  No auth. Returns 200 so you can check the service is up from your phone.

Env vars:
  MEETING_BOT_TOKEN   - shared secret (required)
  MEETING_BOT_SCRIPT  - path to pipeline.sh (default below)
  MEETING_BOT_PORT    - listen port (default 8765)
  MEETING_BOT_ROOT    - output root (default /opt/meeting-bot)
"""
import json
import os
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = os.environ.get("MEETING_BOT_TOKEN")
SCRIPT = os.environ.get(
    "MEETING_BOT_SCRIPT", "/opt/meeting-bot/pipeline.sh"
)
PORT = int(os.environ.get("MEETING_BOT_PORT", "8765"))
BOT_ROOT = Path(os.environ.get("MEETING_BOT_ROOT", "/opt/meeting-bot"))
LOG_DIR = BOT_ROOT / "logs"

if not TOKEN:
    raise SystemExit("MEETING_BOT_TOKEN env var must be set")


class Handler(BaseHTTPRequestHandler):
    def _unauthorized(self):
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b"unauthorized")

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Unauthenticated on purpose: it reveals nothing beyond "the service is
        # running", and needing a token to check that from a phone is friction
        # with no benefit.
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/trigger":
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {TOKEN}":
            self._unauthorized()
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"invalid json")
            return

        # Accept either a single "url" or a list of "urls".
        urls = body.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            single = body.get("url")
            urls = [single] if single else []
        urls = [u for u in urls if isinstance(u, str) and u.strip()]

        if not urls:
            self._json(400, {"error": "missing 'url' or 'urls'"})
            return

        cmd = [SCRIPT, *urls]
        # --name only applies to a single input; pipeline.sh derives per-input
        # names otherwise and warns if you pass one anyway.
        name = body.get("name")
        if name and len(urls) == 1:
            cmd += ["--name", str(name)]
        for field, flag in (("language", "--language"),
                            ("prompt", "--prompt"),
                            ("display_name", "--display-name"),
                            ("jobs", "--jobs"),
                            ("combine", "--combine")):
            value = body.get(field)
            if value:
                cmd += [flag, str(value)]
        # `resources` may be a single spec or a list of them; pipeline.sh takes
        # the flag repeatedly.
        resources = body.get("resources")
        if isinstance(resources, str):
            resources = [resources]
        for spec in resources or []:
            if isinstance(spec, str) and spec.strip():
                cmd += ["--resources", spec.strip()]
        if body.get("force"):
            cmd.append("--force")

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
        log_path = candidate

        # Detached, with output captured to a file rather than discarded: this
        # is the path you use when you can't watch the terminal, so a failure
        # that left no trace would be undiagnosable.
        try:
            subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            # The child holds its own descriptor; ours is no longer needed.
            log_file.close()

        self._json(202, {
            "status": "started",
            "inputs": urls,
            "log": str(log_path),
        })

    def log_message(self, fmt, *args):
        print(f"[trigger-server] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on :{PORT} (bind to Tailscale interface via firewall/config)")
    server.serve_forever()
