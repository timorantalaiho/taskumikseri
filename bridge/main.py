#!/usr/bin/env python3
"""Entry point: bridges REAPER OSC receive-volume control to a phone web UI.

Standard library only -- see README.md for the REAPER-side OSC setup this
depends on.
"""

import argparse
from http.server import ThreadingHTTPServer

from reaper_state import ReaperState
from server import Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reaper-host", default="127.0.0.1",
                         help="IP REAPER is running on (default: 127.0.0.1)")
    parser.add_argument("--reaper-port", type=int, default=8000,
                         help="REAPER's OSC device listen port (default: 8000)")
    parser.add_argument("--listen-port", type=int, default=9000,
                         help="Port this bridge listens on for REAPER's OSC feedback -- "
                              "must match the port configured in REAPER's OSC device (default: 9000)")
    parser.add_argument("--http-host", default="0.0.0.0",
                         help="Interface to serve the web UI on (default: 0.0.0.0)")
    parser.add_argument("--http-port", type=int, default=8090,
                         help="Port to serve the web UI on (default: 8090)")
    parser.add_argument("--debug", action="store_true",
                         help="Log select/receive OSC traffic to stdout")
    args = parser.parse_args()

    state = ReaperState(args.reaper_host, args.reaper_port, listen_port=args.listen_port, debug=args.debug)

    httpd = ThreadingHTTPServer((args.http_host, args.http_port), Handler)
    httpd.reaper_state = state

    print(f"Listening for REAPER OSC feedback on UDP :{args.listen_port}, "
          f"sending control to {args.reaper_host}:{args.reaper_port}")
    print(f"Web UI: http://<this-machine-ip>:{args.http_port}/")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        state.close()


if __name__ == "__main__":
    main()
