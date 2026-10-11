"""Minimal mDNS responder (RFC 6762), standard library only.

Answers A-record queries for a single `<name>.local` hostname with this
machine's IPv4 address, so phones on the same Wi-Fi can open e.g.
http://mix.local:8090/ instead of typing an IP. Only the parts that needs:
no service discovery (DNS-SD), no IPv6, no conflict probing.

It shares UDP port 5353 with any system mDNS daemon (Avahi,
mDNSResponder) via SO_REUSEADDR/SO_REUSEPORT -- multicast queries are
delivered to every socket bound to the port, and each responder answers
only for the names it owns.

The address in each answer is the one this machine would use to reach the
querier (asked of the OS routing table), so a machine with several
interfaces (Wi-Fi + Ethernet, a VPN, Docker bridges) answers with the
address on the querier's own network.
"""

import socket
import struct
import threading
import time

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
TTL = 120

_TYPE_A = 1
_TYPE_ANY = 255
_CLASS_IN = 1
_CACHE_FLUSH = 0x8000  # "this is the complete answer set" bit on the class field
_UNICAST_RESPONSE = 0x8000  # "QU" bit on a question's class field


def _encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _read_name(data, offset):
    """Returns (lowercased dotted name, offset just past the name)."""
    labels = []
    end = None
    jumps = 0
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:  # compression pointer
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            jumps += 1
            if jumps > 16:
                raise ValueError("compression loop")
        elif length == 0:
            offset += 1
            break
        else:
            labels.append(data[offset + 1 : offset + 1 + length].decode("utf-8", "replace"))
            offset += 1 + length
    return ".".join(labels).lower(), end if end is not None else offset


def _parse_questions(data):
    """Yields (name, qtype, unicast_response) for each question in a query."""
    msg_id, flags, qdcount = struct.unpack_from(">HHH", data, 0)
    if flags & 0x8000:  # a response, not a query
        return
    offset = 12
    for _ in range(qdcount):
        name, offset = _read_name(data, offset)
        qtype, qclass = struct.unpack_from(">HH", data, offset)
        offset += 4
        yield name, qtype, bool(qclass & _UNICAST_RESPONSE)


def _local_ip_towards(addr):
    """This machine's IPv4 address on the route to addr (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((addr, MDNS_PORT))
        return s.getsockname()[0]
    finally:
        s.close()


def default_ip():
    """Best-guess LAN address of this machine, for display purposes."""
    try:
        return _local_ip_towards(MDNS_GROUP)
    except OSError:
        return None


class MdnsResponder:
    def __init__(self, hostname):
        self.fqdn = hostname.lower().rstrip(".")
        if not self.fqdn.endswith(".local"):
            self.fqdn += ".local"
        self._encoded_name = _encode_name(self.fqdn)
        self._running = True

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", MDNS_PORT))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        self._join_group(sock)
        self._sock = sock

        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._announce, daemon=True).start()

    @staticmethod
    def _join_group(sock):
        group = socket.inet_aton(MDNS_GROUP)
        joined = False
        # Join on every interface where the OS supports picking one by index
        # (Linux's ip_mreqn); otherwise only the default interface.
        if hasattr(socket, "if_nameindex"):
            for ifindex, _name in socket.if_nameindex():
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                    group + socket.inet_aton("0.0.0.0") + struct.pack("@i", ifindex))
                    joined = True
                except OSError:
                    pass  # no IPv4 on it, already joined, or ip_mreqn unsupported
        if not joined:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            group + socket.inet_aton("0.0.0.0"))

    def close(self):
        self._running = False
        try:
            self._send_answer(default_ip(), ttl=0)  # "goodbye": drop us from caches now
        except OSError:
            pass
        self._sock.close()

    def _announce(self):
        # RFC 6762 section 8.3: announce at least twice, a second apart, so
        # anything that looked us up before we started stops caching a miss.
        for _ in range(2):
            ip = default_ip()
            if ip:
                try:
                    self._send_answer(ip)
                except OSError:
                    pass
            time.sleep(1.0)

    def _answer_record(self, ip, ttl):
        return (self._encoded_name
                + struct.pack(">HHIH", _TYPE_A, _CLASS_IN | _CACHE_FLUSH, ttl, 4)
                + socket.inet_aton(ip))

    def _send_answer(self, ip, ttl=TTL, dest=(MDNS_GROUP, MDNS_PORT)):
        header = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)  # authoritative response, 1 answer
        self._sock.sendto(header + self._answer_record(ip, ttl), dest)

    def _recv_loop(self):
        while self._running:
            try:
                data, (src_ip, src_port) = self._sock.recvfrom(9000)
            except OSError:
                if not self._running:
                    break
                time.sleep(0.1)
                continue
            try:
                self._handle_query(data, src_ip, src_port)
            except (struct.error, IndexError, ValueError, OSError):
                continue  # malformed packet, or no route back to the querier

    def _handle_query(self, data, src_ip, src_port):
        wanted = [unicast for name, qtype, unicast in _parse_questions(data)
                  if name == self.fqdn and qtype in (_TYPE_A, _TYPE_ANY)]
        if not wanted:
            return
        ip = _local_ip_towards(src_ip)

        if src_port != MDNS_PORT:
            # "Legacy" one-shot resolver (RFC 6762 section 6.7, e.g. `dig -p
            # 5353 @224.0.0.251`): reply directly, echoing its ID and question.
            msg_id = struct.unpack_from(">H", data, 0)[0]
            question = self._encoded_name + struct.pack(">HH", _TYPE_A, _CLASS_IN)
            answer = (self._encoded_name + struct.pack(">HHIH", _TYPE_A, _CLASS_IN, 10, 4)
                      + socket.inet_aton(ip))
            header = struct.pack(">HHHHHH", msg_id, 0x8400, 1, 1, 0, 0)
            self._sock.sendto(header + question + answer, (src_ip, src_port))
        elif all(wanted):
            self._send_answer(ip, dest=(src_ip, src_port))
        else:
            self._send_answer(ip)
