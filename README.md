# taskumikseri -- REAPER monitor mix web UI

A small local bridge that lets you ride per-receive levels on a REAPER bus
track (e.g. a "Jaakko Monitor Out" track fed by one receive per performer)
from a phone browser during a gig -- something neither REAPER's built-in Web
Remote nor plain OSC/MIDI Learn can do, since individual sends/receives
aren't addressable faders in either of those.

## How it works

`bridge/` is a Python 3 server (standard library only, no install step)
that:

- Talks OSC to REAPER, which already exposes per-receive volume for every
  track over its stock OSC control surface -- no ReaScript or other
  REAPER-side scripting needed.
- Advertises itself on the local network as `mix.local` (mDNS/Bonjour),
  so phones can find it without anyone looking up an IP address.
- Serves a plain mobile web page listing the tracks that have receives
  (e.g. monitor buses): tap one, then get one volume slider per receive
  on that track. Each phone picks its own track (so every
  performer can ride their own monitor bus at once), and never changes
  REAPER's track selection. Phones showing the same track stay in sync
  with each other, and with REAPER itself, live.

## Requirements

- REAPER (tested on the Linux build; should work identically on
  Windows/macOS), with a project where some tracks send to a bus track via
  receives.
- Python 3, already installed on most systems -- no `pip install` needed.
- A phone/tablet and the computer running REAPER on the same Wi-Fi network.

## Set up REAPER's OSC device

Do this once per machine, in **Preferences > Control/OSC/web**.

1. **Add... > OSC (Open Sound Control)**.
2. Set the device's **listen port** (the port REAPER receives commands on --
   this is `--reaper-port` below, default `8000`).
3. Set the **feedback IP/port** to the machine and port the bridge listens
   on (`--listen-port` below, default `9000`; use `127.0.0.1` if the bridge
   runs on the same machine as REAPER).
4. Make sure the device is enabled (not just added).

REAPER's OSC surfaces only address 8 tracks and 4 receives per track at a
time by default (`DEVICE_TRACK_COUNT`/`DEVICE_RECEIVE_COUNT` in
`Default.ReaperOSC`) -- the bridge raises both limits itself on startup via
OSC (`/device/track/count`, `/device/receive/count`), so there's nothing to
configure for that.

## Set up REAPER's web interface (optional, for track colors)

OSC feedback never includes track color, so the web UI needs a second,
separate control surface to color the track list to match REAPER's own
track colors. Skip this section if you don't care about that -- everything
else works without it.

1. **Preferences > Control/OSC/web > Add... > Web browser interface**.
2. Set its **port** (this is `--webremote-port` below, default `8080`).
3. Make sure the device is enabled.

The bridge polls this over plain HTTP (`http://127.0.0.1:8080/_/TRACK` by
default) every few seconds -- no browser needs to actually be pointed at it.

## Run the bridge

```
python3 bridge/main.py --reaper-host 127.0.0.1 --reaper-port 8000 --listen-port 9000 --http-port 8090
```

All flags have the defaults shown above; run `python3 bridge/main.py --help`
for the full list. Leave this running for the duration of the
gig/session -- it's just a script, no build step or dependencies.

## Use it from a phone

1. Join the same Wi-Fi network as the machine running the bridge.
2. Open `http://mix.local:8090/` (the name from `--mdns-name`, the port
   from `--http-port`). If that doesn't load, use the IP address the
   bridge prints on startup instead, e.g. `http://192.168.1.120:8090/`.
3. Tap a track (e.g. "Jaakko Monitor Out") to see its receives. Only
   tracks that have receives are listed. The
   phone remembers it across reloads (it's in the page URL, so a bookmark
   of it opens straight to that track).
4. Drag a receive's slider to ride that performer's level in the monitor
   mix. The number next to each slider is REAPER's own dB readout.

Multiple phones/tabs can be open at once, each on its own track, and stay
in sync with each other and with REAPER itself. If the Wi-Fi drops or the
phone sleeps, the page reconnects on its own once it can reach the bridge
again.

## Troubleshooting

- **Web UI shows no tracks**: confirm the bridge's `--listen-port` matches
  the feedback port configured in REAPER's OSC device, and that the OSC
  device is enabled (not just added) in Preferences.
- **Track list or receives are incomplete right after starting the
  bridge**: REAPER only sends feedback for what changed, so an
  already-running REAPER won't re-announce tracks it already told a
  previous OSC listener about. The bridge asks REAPER to resend everything
  on startup, which REAPER paces out over roughly 10 seconds -- tracks
  appear in the page as they arrive.
- **Sliders don't move REAPER's levels**: confirm the bridge's
  `--reaper-host`/`--reaper-port` match REAPER's OSC device's listen
  address, and that phone and computer are on the same network with no
  firewall blocking the chosen ports. Run the bridge with `--debug` to log
  the actual receive OSC traffic it sends and gets back.
- **`mix.local` doesn't load on a phone**: iPhones/iPads resolve `.local`
  names out of the box, but some Android versions don't -- use the IP
  address the bridge prints on startup instead. Also check that the
  computer's firewall allows UDP port 5353 (mDNS) in, and that nothing
  else on the network already calls itself `mix.local` (choose another
  name with `--mdns-name`). Some guest/public Wi-Fi networks block
  traffic between devices, including mDNS, entirely.
- **`mix.local` doesn't resolve when the bridge runs on a Mac**: the
  bridge's built-in mDNS responder doesn't work reliably on macOS. Turn it
  off:

  ```
  python3 bridge/main.py --mdns-name ""
  ```

  and have macOS's own Bonjour daemon publish the name instead, by running
  this in a second Terminal window for as long as the bridge runs:

  ```
  dns-sd -P "Monitor Mix" _http._tcp local 8090 mix.local "$(ipconfig getifaddr en0)"
  ```

  `ipconfig getifaddr en0` prints the Mac's Wi-Fi address (use the
  Ethernet interface's name instead if it's wired, e.g. `en1`), and `8090`
  is the bridge's `--http-port`. Stop `dns-sd` with Ctrl+C to unpublish
  the name. Turning off the built-in responder matters: when it answers
  for `mix.local` too, Bonjour sees a name conflict and renames its record
  (e.g. to `mix-2.local`).
- **Track list buttons have no color**: confirm REAPER's "Web browser
  interface" device (see above) is added and enabled, and that its port
  matches `--webremote-port`. This is separate from the OSC device and only
  affects track colors -- everything else works without it. Tracks using
  REAPER's default (non-custom) color are expected to show no color here too.
- **Nothing responds at all, even after the above checks pass**: if REAPER's
  OSC device was ever flooded (e.g. a bug that hammered it with requests),
  it can end up in a stuck state that only REAPER itself can clear -- toggle
  the OSC device off and back on in Preferences (or restart REAPER), then
  restart the bridge.

## Notes on scope

Only receive **volume** is exposed, by design -- not pan, mute, or other
send parameters. Plain track volume/pan/mute/solo/record-arm for the bus
track itself is already covered by REAPER's own built-in Web Remote
(Preferences > Control/OSC/web > Add... > Web browser interface) and needs
no part of this repo.

## License

GNU General Public License v3.0 -- see [LICENSE](LICENSE).
