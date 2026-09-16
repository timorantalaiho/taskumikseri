# taskumikseri -- REAPER monitor mix web UI

A small local bridge that lets you ride per-receive levels on a REAPER bus
track (e.g. a "Jaakko Monitor Out" track fed by one receive per performer)
from a phone browser during a gig -- something neither REAPER's built-in Web
Remote nor plain OSC/MIDI Learn can do, since individual sends/receives
aren't addressable faders in either of those.

## How it works

`bridge/` is a Python 3 server (standard library only, no install step)
that:

- Talks OSC to REAPER, which already exposes per-receive volume for the
  currently selected track over its stock OSC control surface -- no
  ReaScript or other REAPER-side scripting needed.
- Serves a plain mobile web page: tap a track to select it, then get one
  volume slider per receive on that track. Multiple phones/tabs stay in
  sync with each other, and with REAPER itself, live.

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
4. Raise the device's **track/channel count** (bank size) to cover every
   track in your project. By default OSC surfaces only address 8 tracks at
   a time and need a bank-switch to reach the rest; a project with more
   than 8 tracks needs this raised so every track name/receive streams
   without banking. The exact field name for this varies by REAPER version
   -- look for wording like "tracks" or "channels" in the same Add dialog.
5. Make sure the device is enabled (not just added).

## Run the bridge

```
python3 bridge/main.py --reaper-host 127.0.0.1 --reaper-port 8000 --listen-port 9000 --http-port 8090
```

All flags have the defaults shown above; run `python3 bridge/main.py --help`
for the full list. Leave this running for the duration of the
gig/session -- it's just a script, no build step or dependencies.

## Use it from a phone

1. Join the same Wi-Fi network as the machine running the bridge.
2. Open `http://<computer-ip>:8090/` (the port from `--http-port`).
3. Tap a track (e.g. "Jaakko Monitor Out") to select it in REAPER and see
   its receives.
4. Drag a receive's slider to ride that performer's level in the monitor
   mix. The number next to each slider is REAPER's own dB readout.

Multiple phones/tabs can be open at once and stay in sync with each other
and with REAPER itself.

## Troubleshooting

- **Web UI shows no tracks**: confirm the bridge's `--listen-port` matches
  the feedback port configured in REAPER's OSC device, and that the OSC
  device is enabled (not just added) in Preferences.
- **Only some tracks/receives show up**: raise the OSC device's
  track/channel count (bank size) -- see "Set up REAPER's OSC device" above.
- **Sliders don't move REAPER's levels**: confirm the bridge's
  `--reaper-host`/`--reaper-port` match REAPER's OSC device's listen
  address, and that phone and computer are on the same network with no
  firewall blocking the chosen ports.

## Notes on scope

Only receive **volume** is exposed, by design -- not pan, mute, or other
send parameters. Plain track volume/pan/mute/solo/record-arm for the bus
track itself is already covered by REAPER's own built-in Web Remote
(Preferences > Control/OSC/web > Add... > Web browser interface) and needs
no part of this repo.
