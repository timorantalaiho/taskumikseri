"""HTTP + SSE server that serves the web UI and bridges it to ReaperState."""

import json
import os
from http.server import BaseHTTPRequestHandler

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Phones on Wi-Fi drop idle connections, and a dead SSE client is only
# noticed when a write to it fails -- so write something periodically even
# when nothing changed. The page also uses these to detect a dead stream.
SSE_KEEPALIVE_INTERVAL = 10.0


class Handler(BaseHTTPRequestHandler):
    server_version = "MonitorMixBridge/1.0"
    # Keep-alive: slider drags POST many small updates, and reusing one
    # connection avoids a fresh TCP handshake per update over Wi-Fi.
    protocol_version = "HTTP/1.1"

    @property
    def state(self):
        return self.server.reaper_state

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif self.path == "/api/tracks":
            self._send_json({"tracks": self.state.get_tracks()})
        elif self.path == "/api/events":
            self._serve_events()
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            body = self._read_json_body()
            if self.path == "/api/receive/volume":
                args = int(body["track"]), int(body["index"]), float(body["value"])
            else:
                self.send_error(404)
                return
        except (ValueError, KeyError, TypeError):
            self.send_error(400, "invalid request body")
            return

        self.state.set_receive_volume(*args)
        self._send_json({"ok": True})

    def log_message(self, format, *args):
        # Slider drags POST many times a second -- only log what's unusual.
        if self.command == "POST" and len(args) > 1 and str(args[1]) == "200":
            return
        super().log_message(format, *args)

    # -- helpers ---------------------------------------------------------

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

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
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self):
        # The stream has no length and ends only when the client goes away.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        sub = self.state.subscribe()
        try:
            while True:
                payloads = sub.get_all(timeout=SSE_KEEPALIVE_INTERVAL) or [{"type": "ping"}]
                self.wfile.write(b"".join(
                    f"data: {json.dumps(p)}\n\n".encode("utf-8") for p in payloads))
                self.wfile.flush()
        except OSError:
            pass  # client went away (broken pipe, reset, timeout, ...)
        finally:
            self.state.unsubscribe(sub)
