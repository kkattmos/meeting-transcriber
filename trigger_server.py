#!/usr/bin/env python3
"""
Minimal HTTP endpoint to remotely trigger a meeting recording.
Intended to be reached only over Tailscale (no public exposure), plus a
shared-secret token as a second layer.

POST /trigger
  Headers: Authorization: Bearer <token>
  Body (JSON): {"url": "<meeting_url>", "name": "Weekly Standup"}

Env vars:
  MEETING_BOT_TOKEN   - shared secret (required)
  MEETING_BOT_SCRIPT  - path to record_and_transcribe.sh (default below)
  MEETING_BOT_PORT    - listen port (default 8765)
"""
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("MEETING_BOT_TOKEN")
SCRIPT = os.environ.get(
    "MEETING_BOT_SCRIPT", "/opt/meeting-bot/pipeline.sh"
)
PORT = int(os.environ.get("MEETING_BOT_PORT", "8765"))

if not TOKEN:
    raise SystemExit("MEETING_BOT_TOKEN env var must be set")


class Handler(BaseHTTPRequestHandler):
    def _unauthorized(self):
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b"unauthorized")

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

        url = body.get("url")
        name = body.get("name", "meeting")
        if not url:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"missing 'url'")
            return

        subprocess.Popen(
            [SCRIPT, url, name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "started", "name": name}).encode())

    def log_message(self, fmt, *args):
        print(f"[trigger-server] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on :{PORT} (bind to Tailscale interface via firewall/config)")
    server.serve_forever()
