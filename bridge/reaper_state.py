"""REAPER-facing half of the bridge: OSC I/O and shared, lock-guarded state.

Talks to REAPER's OSC control surface (Preferences > Control/OSC/web).
Track/receive addresses are all of the form `/track/<n>/...` and
`/track/<n>/recv/<i>/...`, where `<n>` is a slot in REAPER's OSC "track
bank" (which defaults to only 8 tracks and 4 receives visible at once,
per REAPER's Default.ReaperOSC). Rather than page through banks, we just
tell REAPER's OSC device to raise those limits on connect via
`/device/track/count` and `/device/receive/count` (documented in
Default.ReaperOSC as overridable at runtime), so `<n>` lines up with the
real track/receive number for any project this bridge is likely to see.

Every control message here is a single fire-and-forget UDP packet with no
ack, and REAPER only sends feedback for *changes* -- so if a select/refresh
packet is ever lost, our local state can silently drift from REAPER's
actual state (a slider then "moves" a track REAPER never really selected).

"Refresh all surfaces" (the only way to force REAPER to resend state it
doesn't think changed) redumps its *entire* OSC surface -- every field for
every banked track, not just what we use -- so it's expensive. A continuous
heartbeat that fired it on a timer was tried and made things *worse*: it
kept re-triggering that large dump before the previous one had drained,
flooding REAPER's OSC output and causing real packet loss, which looked
stale, which triggered another refresh -- a self-sustaining storm. Instead,
recovery is bounded and targeted: a short retry sequence after startup (for
the track list) and after each select_track() call (for that track's
receives), each stopping as soon as the data it's waiting for shows up.

Track color is the one piece of state this module doesn't get over OSC at
all -- REAPER's OSC feedback never includes it, in any version. It's polled
separately over REAPER's "Web browser interface" control surface instead
(see webremote.py).
"""

import queue
import re
import socket
import threading
import time

import osc
import webremote

# REAPER pads OSC feedback for track/receive slots beyond what actually
# exists in the project with generic placeholder names in this exact shape
# (mirroring how it displays an unnamed real track/receive in its own UI,
# which is the one case this can't distinguish from a real item).
_PLACEHOLDER_TRACK_NAME = re.compile(r"^Track \d+$")
_PLACEHOLDER_RECV_NAME = re.compile(r"^Recv \d+$")

REFRESH_ALL_SURFACES_ACTION = 41743
RETRY_DELAYS = (0.4, 1.0, 2.0)  # bounded backoff for post-action recovery
WEBREMOTE_POLL_INTERVAL = 3.0  # track colors barely change; a slow poll is plenty


class ReaperState:
    def __init__(self, reaper_host, reaper_port, listen_host="0.0.0.0", listen_port=9000,
                 max_tracks=64, max_receives=16, debug=False,
                 webremote_host=None, webremote_port=None):
        self.reaper_addr = (reaper_host, reaper_port)
        self.max_tracks = max_tracks
        self.max_receives = max_receives
        self.debug = debug
        self.webremote_host = webremote_host
        self.webremote_port = webremote_port

        self._lock = threading.Lock()
        self._tracks = {}  # index (1-based) -> name
        self._selected_track = None
        self._receives = {}  # index (0-based) -> {name, volume, volume_str}
        self._colors = {}  # index (1-based) -> "#rrggbb", from the web-remote poll below
        self._subscribers = []  # list[queue.Queue]

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((listen_host, listen_port))

        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

        # Raise REAPER's default OSC bank limits (8 tracks, 4 receives) so
        # every track/receive is addressable without paging through banks.
        self._send("/device/track/count", int(max_tracks))
        self._send("/device/receive/count", int(max_receives))
        self._send("/action", REFRESH_ALL_SURFACES_ACTION)

        threading.Thread(target=self._retry_while_missing,
                          args=(lambda: bool(self._tracks), self._resync_tracks),
                          daemon=True).start()

        if self.webremote_port:
            threading.Thread(target=self._poll_colors, daemon=True).start()

    def _resync_tracks(self):
        self._send("/device/track/count", int(self.max_tracks))
        self._send("/device/receive/count", int(self.max_receives))
        self._send("/action", REFRESH_ALL_SURFACES_ACTION)

    def _retry_while_missing(self, have_what_we_need, resync):
        """Bounded backoff: stop as soon as have_what_we_need() is true."""
        for delay in RETRY_DELAYS:
            time.sleep(delay)
            with self._lock:
                done = have_what_we_need()
            if done:
                return
            resync()

    def close(self):
        self._running = False
        self._sock.close()

    def _poll_colors(self):
        # Track color isn't part of REAPER's OSC feedback at all -- fetched
        # separately over its "Web browser interface" control surface
        # (see webremote.py). Polled rather than event-driven since nothing
        # here tells us when a color changes.
        while self._running:
            try:
                colors = webremote.fetch_track_colors(self.webremote_host, self.webremote_port)
            except OSError:
                colors = None
            if colors is not None:
                with self._lock:
                    changed = colors != self._colors
                    self._colors = colors
                if changed:
                    self._publish("tracks")
            time.sleep(WEBREMOTE_POLL_INTERVAL)

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
                if self.debug and ("select" in address or "recv" in address):
                    print(f"RECV {address} {args}", flush=True)
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

            else:
                # Receives can come addressed either relative to the
                # selected track (/track/recv/<i>/...) or fully-qualified
                # (/track/<n>/recv/<i>/...) -- REAPER's default config uses
                # the latter, but accept both.
                recv_track, recv_fields = None, None
                if len(parts) >= 4 and parts[0] == "track" and parts[1] == "recv" and parts[2].isdigit():
                    recv_track = self._selected_track
                    recv_fields = parts[2:]
                elif (len(parts) >= 5 and parts[0] == "track" and parts[1].isdigit()
                        and parts[2] == "recv" and parts[3].isdigit()):
                    recv_track = int(parts[1])
                    recv_fields = parts[3:]

                if recv_track is not None and recv_track == self._selected_track:
                    idx = int(recv_fields[0])
                    field = recv_fields[1] if len(recv_fields) > 1 else None
                    entry = self._receives.setdefault(idx, {"name": "", "volume": 0.0, "volume_str": ""})
                    if field == "name" and args:
                        entry["name"] = str(args[0])
                        changed = "receives"
                    elif field == "volume" and len(recv_fields) == 2 and args:
                        entry["volume"] = float(args[0])
                        changed = "receives"
                    elif field == "volume" and len(recv_fields) == 3 and recv_fields[2] == "str" and args:
                        entry["volume_str"] = str(args[0])
                        changed = "receives"

        if changed:
            self._publish(changed)

    # -- OSC send ----------------------------------------------------------

    def _send(self, address, *args):
        if self.debug and ("select" in address or "recv" in address):
            print(f"SEND {address} {args}", flush=True)
        self._sock.sendto(osc.encode_message(address, *args), self.reaper_addr)

    def select_track(self, index):
        with self._lock:
            self._selected_track = index
            self._receives = {}
        self._publish("selected")
        self._publish("receives")
        self._send(f"/track/{index}/select", 1.0)

        def resync():
            # Bail out if the user has since selected a different track --
            # no point recovering receives nobody's looking at anymore.
            with self._lock:
                if self._selected_track != index:
                    return
            self._send(f"/track/{index}/select", 1.0)
            self._send("/action", REFRESH_ALL_SURFACES_ACTION)

        threading.Thread(target=self._retry_while_missing,
                          args=(lambda: self._selected_track != index or bool(self._receives), resync),
                          daemon=True).start()

    def set_receive_volume(self, index, value):
        value = max(0.0, min(1.0, float(value)))
        with self._lock:
            track = self._selected_track
            entry = self._receives.setdefault(index, {"name": "", "volume": value, "volume_str": ""})
            entry["volume"] = value
        self._publish("receives")
        if track is not None:
            self._send(f"/track/{track}/recv/{index}/volume", value)
        else:
            self._send(f"/track/recv/{index}/volume", value)

    # -- Read access ---------------------------------------------------

    def get_tracks(self):
        with self._lock:
            items = sorted(self._tracks.items())
            colors = self._colors
        return [{"index": i, "name": n, "color": colors.get(i)}
                for i, n in items if not _PLACEHOLDER_TRACK_NAME.match(n)]

    def get_selected(self):
        with self._lock:
            return self._selected_track

    def get_receives(self):
        with self._lock:
            items = sorted(self._receives.items())
        return [{"index": i, **v} for i, v in items if not _PLACEHOLDER_RECV_NAME.match(v["name"])]

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
