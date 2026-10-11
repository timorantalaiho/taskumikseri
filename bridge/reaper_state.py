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

REAPER reports receives for *every* banked track, regardless of which
tracks are selected in REAPER -- so all of them are cached here, and which
track's receives a phone shows is purely that phone's own choice. The
bridge never touches REAPER's track selection: OSC `/track/<n>/select` is
additive (it never deselects anything), and several performers each riding
their own monitor bus from their own phone must not fight over a single
shared selection.

A plain control message never makes REAPER resend state it doesn't think
changed; only the "refresh all surfaces" action does, and it redumps the
*entire* OSC surface (every field of every banked track -- ~20k messages
for 64 tracks x 16 receives), paced by REAPER over several *seconds*. So
it's only fired on startup, and retried (with delays longer than a dump
takes) only if REAPER hasn't sent us anything at all. Retrying while a
dump is still draining just stacks another full dump behind it.

Track color is the one piece of state this module doesn't get over OSC at
all -- REAPER's OSC feedback never includes it, in any version. It's polled
separately over REAPER's "Web browser interface" control surface instead
(see webremote.py).
"""

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
# A full refresh dump takes REAPER ~9s to send for the default bank sizes
# below, so a retry only fires if nothing at all has arrived by then.
STARTUP_RETRY_DELAYS = (3.0, 12.0)
# Re-asserting the bank sizes is two tiny packets that REAPER doesn't answer
# when they're unchanged, but restores them if REAPER was restarted (which
# resets them to its 8-track/4-receive defaults) while the bridge kept running.
BANK_SIZE_REASSERT_INTERVAL = 5.0
WEBREMOTE_POLL_INTERVAL = 3.0  # track colors barely change; a slow poll is plenty


class Subscriber:
    """One SSE client's mailbox of pending updates.

    Every update carries full state for its key (the track list, or one
    track's receives), so only the latest one per key is ever worth
    delivering: a slow phone gets coalesced updates instead of an unbounded
    backlog of stale ones.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._pending = {}  # key -> payload, in first-queued order

    def put(self, key, payload):
        with self._cond:
            self._pending.pop(key, None)
            self._pending[key] = payload
            self._cond.notify()

    def get_all(self, timeout):
        """Returns all pending payloads, or [] if none arrived within timeout."""
        with self._cond:
            if not self._pending:
                self._cond.wait(timeout)
            payloads = list(self._pending.values())
            self._pending.clear()
            return payloads


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
        self._receives = {}  # track index -> {receive index -> {name, volume, volume_str}}
        self._colors = {}  # index (1-based) -> "#rrggbb", from the web-remote poll below
        self._subscribers = []  # list[Subscriber]
        self._last_rx = None  # time.monotonic() of the last packet from REAPER
        # Serializes computing an update with delivering it, so concurrent
        # publishers (OSC thread, HTTP threads, color poll) can never deliver
        # an older snapshot after a newer one.
        self._publish_lock = threading.Lock()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((listen_host, listen_port))

        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

        self._send_bank_sizes()
        self._send("/action", REFRESH_ALL_SURFACES_ACTION)

        threading.Thread(target=self._startup_retries, daemon=True).start()
        threading.Thread(target=self._reassert_bank_sizes, daemon=True).start()
        if self.webremote_port:
            threading.Thread(target=self._poll_colors, daemon=True).start()

    def _send_bank_sizes(self):
        # Raise REAPER's default OSC bank limits (8 tracks, 4 receives) so
        # every track/receive is addressable without paging through banks.
        self._send("/device/track/count", int(self.max_tracks))
        self._send("/device/receive/count", int(self.max_receives))

    def _startup_retries(self):
        for delay in STARTUP_RETRY_DELAYS:
            time.sleep(delay)
            with self._lock:
                if self._last_rx is not None:
                    return
            print("No OSC feedback from REAPER yet -- asking it to resend everything again", flush=True)
            self._send_bank_sizes()
            self._send("/action", REFRESH_ALL_SURFACES_ACTION)

    def _reassert_bank_sizes(self):
        while self._running:
            time.sleep(BANK_SIZE_REASSERT_INTERVAL)
            self._send_bank_sizes()

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
                # Windows reports an ICMP "port unreachable" from an earlier
                # send (e.g. while REAPER isn't running yet) as an error on
                # the *next* recvfrom -- that mustn't kill this thread.
                if not self._running:
                    break
                time.sleep(0.1)
                continue
            with self._lock:
                self._last_rx = time.monotonic()
            try:
                messages = osc.decode_packet(data)
            except Exception:
                continue
            for address, args in messages:
                if self.debug and "recv" in address:
                    print(f"RECV {address} {args}", flush=True)
                self._handle_message(address, args)

    def _handle_message(self, address, args):
        parts = address.strip("/").split("/")
        if not args or len(parts) < 3 or parts[0] != "track" or not parts[1].isdigit():
            return
        track = int(parts[1])
        changed = None

        with self._lock:
            if len(parts) == 3 and parts[2] == "name":
                name = str(args[0])
                old = self._tracks.get(track)
                if old != name:
                    self._tracks[track] = name
                    if not (_PLACEHOLDER_TRACK_NAME.match(name)
                            and (old is None or _PLACEHOLDER_TRACK_NAME.match(old))):
                        changed = "tracks"

            elif len(parts) >= 5 and parts[2] == "recv" and parts[3].isdigit():
                idx = int(parts[3])
                fields = parts[4:]
                if fields == ["name"]:
                    key, value = "name", str(args[0])
                elif fields == ["volume"]:
                    key, value = "volume", float(args[0])
                elif fields == ["volume", "str"]:
                    key, value = "volume_str", str(args[0])
                else:
                    return
                entry = self._receives.setdefault(track, {}).setdefault(
                    idx, {"name": "", "volume": 0.0, "volume_str": ""})
                if entry[key] != value:
                    was_placeholder = not entry["name"] or _PLACEHOLDER_RECV_NAME.match(entry["name"])
                    entry[key] = value
                    if not (was_placeholder and _PLACEHOLDER_RECV_NAME.match(entry["name"])):
                        changed = ("receives", track)

        if changed:
            self._publish(changed)

    # -- OSC send ----------------------------------------------------------

    def _send(self, address, *args):
        if self.debug and "recv" in address:
            print(f"SEND {address} {args}", flush=True)
        try:
            self._sock.sendto(osc.encode_message(address, *args), self.reaper_addr)
        except OSError as e:
            # e.g. no route to a remote REAPER host right now -- the
            # caller's state stays as-is and REAPER's feedback will correct it.
            print(f"Couldn't send {address} to REAPER: {e}", flush=True)

    def set_receive_volume(self, track, index, value):
        value = max(0.0, min(1.0, float(value)))
        with self._lock:
            entry = self._receives.setdefault(track, {}).setdefault(
                index, {"name": "", "volume": value, "volume_str": ""})
            entry["volume"] = value
        self._publish(("receives", track))
        self._send(f"/track/{track}/recv/{index}/volume", value)

    # -- Read access ---------------------------------------------------

    def get_tracks(self):
        with self._lock:
            items = sorted(self._tracks.items())
            colors = self._colors
        return [{"index": i, "name": n, "color": colors.get(i)}
                for i, n in items if not _PLACEHOLDER_TRACK_NAME.match(n)]

    def get_receives(self, track):
        with self._lock:
            items = sorted(self._receives.get(track, {}).items())
            return [{"index": i, **v} for i, v in items if not _PLACEHOLDER_RECV_NAME.match(v["name"])]

    def _tracks_with_receives(self):
        with self._lock:
            return sorted(self._receives)

    # -- Pub/sub for SSE -------------------------------------------------

    def _payload(self, key):
        if key == "tracks":
            return {"type": "tracks", "tracks": self.get_tracks()}
        _, track = key
        return {"type": "receives", "track": track, "receives": self.get_receives(track)}

    def subscribe(self):
        """Returns a Subscriber already holding a full snapshot of current state."""
        sub = Subscriber()
        with self._publish_lock:
            sub.put("tracks", self._payload("tracks"))
            for track in self._tracks_with_receives():
                key = ("receives", track)
                sub.put(key, self._payload(key))
            with self._lock:
                self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def _publish(self, key):
        with self._publish_lock:
            payload = self._payload(key)
            with self._lock:
                subscribers = list(self._subscribers)
            for sub in subscribers:
                sub.put(key, payload)
