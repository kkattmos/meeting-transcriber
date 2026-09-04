#!/usr/bin/env python3
"""
Stub servers for the three paid APIs, so the media pipeline can be run
end-to-end without keys, network, or spend.

These are NOT mocks inside the process under test: they speak the real HTTP
protocols, and the real SDKs (anthropic, assemblyai) and the real requests
client talk to them. That is the point — it exercises the request we actually
build, including the Messages API's `output_config.effort` and adaptive
thinking, which a monkeypatched function would never see.

    python3 lib/fake_api_server.py --which anthropic --port 8801 \
        --record /tmp/requests.jsonl

Endpoints implemented:

  anthropic   POST /v1/messages
              Returns one text block. Every request body is appended to the
              --record file so the test can assert on what was sent.

  assemblyai  POST /v2/upload, POST /v2/transcript,
              GET  /v2/transcript/<id>, GET /v2/transcript/<id>/sentences
              Enough of the pre-recorded flow for the SDK's transcribe() to
              complete, with two sentences of Thai-looking text.

  youtube     POST / (any path)
              The youtube-transcript.io response shape, including the
              tracks[].transcript timing that the real client depends on.
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECORD_PATH = None
SUMMARY_TEXT = (
    "This lecture covers **shortest-path algorithms**.\n\n"
    "## 1. Dijkstra's algorithm\n\n"
    "* Runs in `O(E log V)` with a binary heap *(Frame 1 @ 2.0s)*.\n"
    "* Requires non-negative edge weights (Frame 2).\n\n"
    "---\n\n"
    "## 2. Visual & Board Work Index\n\n"
    "| Frame | Timestamp | Content | Topic |\n"
    "| :--- | :--- | :--- | :--- |\n"
    "| Frame 1 | `00:02` | Title slide | Intro |\n"
    "| Frame 2 | `00:20` | Complexity table | Dijkstra |\n"
)


def _record(kind, payload):
    if not RECORD_PATH:
        return
    with open(RECORD_PATH, "a") as fh:
        fh.write(json.dumps({"api": kind, "body": payload}) + "\n")


class BaseHandler(BaseHTTPRequestHandler):
    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {"_raw_bytes": len(raw)}

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet: the test's own output is what matters


class AnthropicHandler(BaseHandler):
    def do_POST(self):
        body = self._read_json()
        # Images are megabytes of base64; keep a summary rather than the bytes,
        # so the recording file stays greppable.
        summarized = dict(body)
        for message in summarized.get("messages", []):
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [
                    {"type": b.get("type"),
                     "text": b.get("text") if b.get("type") == "text" else None,
                     "bytes": len(b.get("source", {}).get("data", ""))
                     if b.get("type") == "image" else None}
                    for b in content
                ]
        _record("anthropic", summarized)
        self._send({
            "id": "msg_stub",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-opus-5"),
            "content": [{"type": "text", "text": SUMMARY_TEXT}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        })


class AssemblyAIHandler(BaseHandler):
    # `confidence` and per-sentence `words` are required by the SDK's Sentence
    # model; without them get_sentences() raises and the client silently falls
    # back to word-level segments — which is the exact regression the sentence
    # granularity fix was about, so the stub has to be faithful here.
    SENTENCES = [
        {"text": "สวัสดีครับ วันนี้เราจะพูดถึงอัลกอริทึม",
         "start": 0, "end": 4000, "confidence": 0.94, "words": []},
        {"text": "Dijkstra runs in O(E log V) with a binary heap.",
         "start": 4000, "end": 9000, "confidence": 0.96, "words": []},
    ]

    def do_POST(self):
        if self.path.endswith("/upload"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            # Drain the upload; the SDK streams the whole media file here.
            remaining = length
            while remaining > 0:
                remaining -= len(self.rfile.read(min(65536, remaining)))
            _record("assemblyai", {"endpoint": "upload", "bytes": length})
            self._send({"upload_url": "https://stub.invalid/media/1"})
            return
        body = self._read_json()
        _record("assemblyai", {"endpoint": "transcript", **body})
        # audio_url is required by the SDK's response model — omitting it makes
        # transcribe() raise a pydantic validation error rather than poll.
        self._send({"id": "tr_stub", "status": "queued",
                    "audio_url": body.get("audio_url",
                                          "https://stub.invalid/media/1"),
                    "language_code": body.get("language_code")})

    def do_GET(self):
        if self.path.endswith("/sentences"):
            self._send({"id": "tr_stub", "confidence": 0.95,
                        "audio_duration": 9, "sentences": self.SENTENCES})
            return
        words = []
        for s in self.SENTENCES:
            for word in s["text"].split():
                words.append({"text": word, "start": s["start"],
                              "end": s["end"], "confidence": 0.9})
        self._send({
            "id": "tr_stub",
            "status": "completed",
            "audio_url": "https://stub.invalid/media/1",
            "text": " ".join(s["text"] for s in self.SENTENCES),
            "words": words,
            "audio_duration": 9,
            "confidence": 0.95,
            "language_code": "th",
        })


class YouTubeHandler(BaseHandler):
    def do_POST(self):
        body = self._read_json()
        _record("youtube", {"auth": self.headers.get("Authorization", ""),
                            **body})
        video_id = (body.get("ids") or ["unknown"])[0]
        self._send([{
            "id": video_id,
            "title": "Stub lecture",
            # The flat text field the client must NOT parse (no timing).
            "text": "whole transcript in one useless string",
            "languages": [{"label": "English - English", "languageCode": "en"}],
            "tracks": [{
                "language": "English - English",
                "transcript": [
                    {"start": "0", "dur": "4",
                     "text": "Welcome to the &lt;i&gt;lecture&lt;/i&gt;"},
                    {"start": "4", "dur": "5",
                     "text": "Dijkstra &amp; Bellman-Ford"},
                ],
            }],
        }])


HANDLERS = {
    "anthropic": AnthropicHandler,
    "assemblyai": AssemblyAIHandler,
    "youtube": YouTubeHandler,
}


def main():
    global RECORD_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--which", required=True, choices=sorted(HANDLERS))
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--record")
    args = ap.parse_args()
    RECORD_PATH = args.record

    server = ThreadingHTTPServer(("127.0.0.1", args.port), HANDLERS[args.which])
    print(f"stub {args.which} listening on {args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
