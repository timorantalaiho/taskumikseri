"""HTTP + SSE server that serves the web UI and bridges it to ReaperState."""

import json
import os
from http.server import BaseHTTPRequestHandler

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class Handler(BaseHTTPRequestHandler):
    server_version = "MonitorMixBridge/1.0"

    @property
    def state(self):
        return self.server.reaper_state

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif self.path == "/api/tracks":
            self._send_json({"tracks": self.state.get_tracks(), "selected": self.state.get_selected()})
        elif self.path == "/api/events":
            self._serve_events()
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            body = self._read_json_body()
        except (ValueError, KeyError):
            self.send_error(400, "invalid request body")
            return

        if self.path == "/api/select":
            self.state.select_track(int(body["index"]))
            self._send_json({"ok": True})
        elif self.path == "/api/receive/volume":
            self.state.set_receive_volume(int(body["index"]), float(body["value"]))
            self._send_json({"ok": True})
        else:
            self.send_error(404)

    # -- helpers ---------------------------------------------------------

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, name, content_type):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = self.state.subscribe()
        try:
            self._write_event({"type": "tracks", "tracks": self.state.get_tracks()})
            self._write_event({"type": "selected", "selected": self.state.get_selected()})
            self._write_event({"type": "receives", "receives": self.state.get_receives()})
            while True:
                self._write_event(q.get())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.state.unsubscribe(q)

    def _write_event(self, payload):
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        self.wfile.flush()
