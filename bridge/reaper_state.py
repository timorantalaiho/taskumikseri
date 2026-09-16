"""REAPER-facing half of the bridge: OSC I/O and shared, lock-guarded state.

Talks to REAPER's OSC control surface (Preferences > Control/OSC/web). Track
names/selection use REAPER's absolute track addressing; receive name/volume
use REAPER's "currently selected track" addressing, which is why selecting a
track and reading its receives are two separate steps.
"""

import queue
import socket
import threading

import osc


class ReaperState:
    def __init__(self, reaper_host, reaper_port, listen_host="0.0.0.0", listen_port=9000):
        self.reaper_addr = (reaper_host, reaper_port)

        self._lock = threading.Lock()
        self._tracks = {}  # index (1-based) -> name
        self._selected_track = None
        self._receives = {}  # index (0-based) -> {name, volume, volume_str}
        self._subscribers = []  # list[queue.Queue]

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((listen_host, listen_port))

        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._running = False
        self._sock.close()

    # -- OSC receive -----------------------------------------------------

    def _recv_loop(self):
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(65535)
            except OSError:
                break
            try:
                messages = osc.decode_packet(data)
            except Exception:
                continue
            for address, args in messages:
                self._handle_message(address, args)

    def _handle_message(self, address, args):
        parts = address.strip("/").split("/")
        changed = None

        with self._lock:
            if len(parts) == 3 and parts[0] == "track" and parts[1].isdigit() and parts[2] == "name":
                if args:
                    self._tracks[int(parts[1])] = str(args[0])
                    changed = "tracks"

            elif len(parts) == 3 and parts[0] == "track" and parts[1].isdigit() and parts[2] == "select":
                if args and float(args[0]) >= 0.5:
                    idx = int(parts[1])
                    if self._selected_track != idx:
                        self._selected_track = idx
                        self._receives = {}
                    changed = "selected"

            elif len(parts) >= 4 and parts[0] == "track" and parts[1] == "recv" and parts[2].isdigit():
                idx = int(parts[2])
                field = parts[3]
                entry = self._receives.setdefault(idx, {"name": "", "volume": 0.0, "volume_str": ""})
                if field == "name" and args:
                    entry["name"] = str(args[0])
                    changed = "receives"
                elif field == "volume" and len(parts) == 4 and args:
                    entry["volume"] = float(args[0])
                    changed = "receives"
                elif field == "volume" and len(parts) == 5 and parts[4] == "str" and args:
                    entry["volume_str"] = str(args[0])
                    changed = "receives"

        if changed:
            self._publish(changed)

    # -- OSC send ----------------------------------------------------------

    def _send(self, address, *args):
        self._sock.sendto(osc.encode_message(address, *args), self.reaper_addr)

    def select_track(self, index):
        with self._lock:
            self._selected_track = index
            self._receives = {}
        self._publish("selected")
        self._publish("receives")
        self._send(f"/track/{index}/select", 1.0)

    def set_receive_volume(self, index, value):
        value = max(0.0, min(1.0, float(value)))
        with self._lock:
            entry = self._receives.setdefault(index, {"name": "", "volume": value, "volume_str": ""})
            entry["volume"] = value
        self._publish("receives")
        self._send(f"/track/recv/{index}/volume", value)

    # -- Read access ---------------------------------------------------

    def get_tracks(self):
        with self._lock:
            return [{"index": i, "name": n} for i, n in sorted(self._tracks.items())]

    def get_selected(self):
        with self._lock:
            return self._selected_track

    def get_receives(self):
        with self._lock:
            return [{"index": i, **v} for i, v in sorted(self._receives.items())]

    # -- Pub/sub for SSE -------------------------------------------------

    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _publish(self, kind):
        payload = {"type": kind}
        if kind == "tracks":
            payload["tracks"] = self.get_tracks()
        elif kind == "selected":
            payload["selected"] = self.get_selected()
        elif kind == "receives":
            payload["receives"] = self.get_receives()

        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(payload)
