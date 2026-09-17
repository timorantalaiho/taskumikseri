"""Client for REAPER's "Web browser interface" control surface.

This is a *different* REAPER control surface from the plain OSC device the
rest of this bridge talks to (Preferences > Control/OSC/web > Add... > Web
browser interface) -- REAPER's stock OSC feedback never includes track
color, but this one's HTTP status endpoint does. It's only used here to
read that color; everything else still goes over OSC.

Protocol: GET /_/TRACK returns one line per track, tab-separated:
  TRACK <tab> index <tab> name <tab> flags <tab> volume <tab> pan <tab>
  peak <tab> pos <tab> width <tab> panmode <tab> sends <tab> recvs <tab>
  hwout <tab> color
where color is 0xaarrggbb, nonzero only if a custom color is set.
(https://mespotin.uber.space/Ultraschall/Reaper_API_Web_Documentation.html)
"""

import http.client

_COLOR_FIELD = 13


def fetch_track_colors(host, port, timeout=1.0):
    """Returns {track_index: "#rrggbb"}, omitting tracks with no custom color."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/_/TRACK")
        body = conn.getresponse().read().decode("utf-8", errors="replace")
    finally:
        conn.close()

    colors = {}
    for line in body.splitlines():
        fields = line.split("\t")
        if len(fields) <= _COLOR_FIELD or fields[0] != "TRACK":
            continue
        try:
            index = int(fields[1])
            value = int(fields[_COLOR_FIELD], 0)
        except ValueError:
            continue
        if value:
            colors[index] = "#{:06x}".format(value & 0xFFFFFF)
    return colors
