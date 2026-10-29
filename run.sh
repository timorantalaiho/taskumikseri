#!/bin/sh
# Starts the bridge, published on the local network as mix.local.
#
# Any arguments are passed on to bridge/main.py (see `./run.sh --help`).
#
# Linux (and anything else): the bridge's own built-in mDNS responder
# publishes the name.
#
# macOS: the built-in responder doesn't work reliably there, so it's turned
# off and macOS's own Bonjour daemon publishes the name instead, via a
# `dns-sd` process that runs in the background for as long as the bridge
# does. (Both at once would make Bonjour see a name conflict and rename
# its record, e.g. to mix-2.local.)

set -eu

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found -- install Python 3 first." >&2
    exit 1
fi

if [ "$(uname -s)" != "Darwin" ]; then
    exec python3 bridge/main.py "$@"
fi

# -- macOS --------------------------------------------------------------

# Mirror the bridge's own defaults and flags for what dns-sd publishes.
http_port=8090
mdns_name=mix
prev=
for arg in "$@"; do
    case "$prev" in
        --http-port) http_port=$arg ;;
        --mdns-name) mdns_name=$arg ;;
    esac
    case "$arg" in
        --http-port=*) http_port=${arg#*=} ;;
        --mdns-name=*) mdns_name=${arg#*=} ;;
        -h|--help) exec python3 bridge/main.py --help ;;
    esac
    prev=$arg
done

# The Mac's address on the LAN the phones are on: that of the default
# route's interface, unless that's a VPN tunnel, else the first en*
# interface with an IPv4 address (en0 is Wi-Fi on MacBooks).
lan_ip() {
    iface=$(route -n get default 2>/dev/null | awk '/interface:/ { print $2 }')
    case "$iface" in
        ""|utun*|ipsec*|ppp*|tun*|tap*) ;;
        *) ipconfig getifaddr "$iface" 2>/dev/null && return ;;
    esac
    for iface in en0 en1 en2 en3 en4 en5 en6 en7 en8 en9; do
        ipconfig getifaddr "$iface" 2>/dev/null && return
    done
    return 1
}

dns_sd_pid=
cleanup() {
    if [ -n "$dns_sd_pid" ]; then
        kill "$dns_sd_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "$mdns_name" ]; then
    host=${mdns_name%.local}.local
    if ip=$(lan_ip); then
        dns-sd -P "Monitor Mix" _http._tcp local "$http_port" "$host" "$ip" >/dev/null 2>&1 &
        dns_sd_pid=$!
        echo "Publishing $host -> $ip via Bonjour: http://$host:$http_port/"
    else
        echo "No LAN IPv4 address found -- not publishing $host" >&2
    fi
fi

# Last, so it overrides any --mdns-name given above.
python3 bridge/main.py "$@" --mdns-name ""
