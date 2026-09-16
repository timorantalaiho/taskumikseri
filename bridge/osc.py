"""Minimal OSC 1.0 codec (encode + decode), standard library only.

Only the pieces this bridge needs: float/int/string arguments, and decoding
single messages as well as #bundle packets (recursively, since a bundle can
contain nested bundles).
"""

import struct


def _osc_string(s):
    b = s.encode("utf-8") + b"\x00"
    b += b"\x00" * ((-len(b)) % 4)
    return b


def _read_osc_string(data, offset):
    end = data.index(b"\x00", offset)
    s = data[offset:end].decode("utf-8", errors="replace")
    total_len = end - offset + 1
    total_len += (-total_len) % 4
    return s, offset + total_len


def encode_message(address, *args):
    type_tags = ","
    arg_bytes = b""
    for a in args:
        if isinstance(a, bool):
            type_tags += "i"
            arg_bytes += struct.pack(">i", int(a))
        elif isinstance(a, float):
            type_tags += "f"
            arg_bytes += struct.pack(">f", a)
        elif isinstance(a, int):
            type_tags += "i"
            arg_bytes += struct.pack(">i", a)
        elif isinstance(a, str):
            type_tags += "s"
            arg_bytes += _osc_string(a)
        else:
            raise TypeError(f"unsupported OSC argument type: {type(a)!r}")
    return _osc_string(address) + _osc_string(type_tags) + arg_bytes


def decode_message(data):
    address, offset = _read_osc_string(data, 0)
    args = []
    if offset >= len(data):
        return address, args
    type_tags, offset = _read_osc_string(data, offset)
    for t in type_tags[1:]:
        if t == "f":
            (val,) = struct.unpack_from(">f", data, offset)
            args.append(val)
            offset += 4
        elif t == "i":
            (val,) = struct.unpack_from(">i", data, offset)
            args.append(val)
            offset += 4
        elif t == "s":
            val, offset = _read_osc_string(data, offset)
            args.append(val)
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        elif t == "N":
            args.append(None)
        elif t == "I":
            args.append(float("inf"))
        else:
            # Unknown/unsupported type tag -- its size is unknown, so we
            # can't safely keep decoding the rest of this message.
            break
    return address, args


def decode_packet(data):
    """Decode a single UDP payload, returning a list of (address, args)."""
    if data[:8] == b"#bundle\x00":
        messages = []
        offset = 16  # "#bundle\0" (8 bytes) + timetag (8 bytes)
        while offset < len(data):
            (size,) = struct.unpack_from(">i", data, offset)
            offset += 4
            element = data[offset : offset + size]
            messages.extend(decode_packet(element))
            offset += size
        return messages
    return [decode_message(data)]
