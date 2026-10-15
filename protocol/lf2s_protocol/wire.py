"""Use the unmodified official generated codec; restrict the accepted profile."""

import struct

from . import dialect as mav

MAX_FRAME = 267


class ProtocolError(ValueError):
    pass


def decode_datagram(data):
    raw = bytes(data)
    if mav.MAVLINK_IGNORE_CRC:
        raise ProtocolError("MAV_IGNORE_CRC is forbidden for this profile")
    if not 13 <= len(raw) <= MAX_FRAME or raw[0] != 0xFD:
        raise ProtocolError("Expected one unsigned MAVLink 2 frame")
    if raw[2:4] != b"\x00\x00" or len(raw) != raw[1] + 12:
        raise ProtocolError("Unsupported flags, truncation, or multiple frames")
    mid = int.from_bytes(raw[7:10], "little")
    cls = mav.mavlink_map.get(mid)
    if cls is None or raw[1] > cls.unpacker.size:
        raise ProtocolError("Unknown message or overlong known payload")
    try:
        message = mav.MAVLink(None).decode(bytearray(raw))
    except (mav.MAVError, ValueError, IndexError) as exc:
        raise ProtocolError(str(exc)) from exc
    if message.get_type() in ("BAD_DATA", "UNKNOWN"):
        raise ProtocolError("Invalid frame")
    return message


class Codec:
    def __init__(self, system_id, component_id):
        if not 1 <= system_id <= 255 or not 1 <= component_id <= 255:
            raise ValueError("Nonzero MAVLink source IDs required")
        self.sender = mav.MAVLink(None, srcSystem=system_id, srcComponent=component_id)

    def encode(self, name, **values):
        cls = next((item for item in mav.mavlink_map.values() if item.msgname == name), None)
        if cls is None or set(values) - set(cls.fieldnames):
            raise ProtocolError("Unknown message or fields")
        arguments = []
        for index, field in enumerate(cls.fieldnames):
            count = cls.array_lengths[cls.orders[index]]
            default = [0] * count if count else 0
            arguments.append(values.get(field, default))
        message = cls(*arguments)
        try:
            raw = bytes(message.pack(self.sender, force_mavlink1=False))
        except (ValueError, TypeError, OverflowError, struct.error) as exc:
            raise ProtocolError(str(exc)) from exc
        self.sender.seq = (self.sender.seq + 1) & 255
        decode_datagram(raw)
        return raw


class StreamParser:
    """Feed UART chunks; overflow notification must reset the caller's parser."""

    def __init__(self):
        self.reset()
        self.bad_frames = 0

    def reset(self):
        self.parser = mav.MAVLink(None)
        self.parser.robust_parsing = True

    def feed(self, data):
        accepted = []
        for byte in bytes(data):
            try:
                message = self.parser.parse_char(bytes([byte]))
                if message is None:
                    continue
                if message.get_type() == "BAD_DATA":
                    self.bad_frames += 1
                    continue
                raw = bytes(message.get_msgbuf())
                decode_datagram(raw)
                accepted.append(raw)
            except (ProtocolError, mav.MAVError, ValueError):
                self.bad_frames += 1
        return accepted


def business_key(message):
    """Ignore transport seq/checksum; exact decoded project fields identify retries."""
    return message.get_msgId(), tuple(getattr(message, name) for name in message.fieldnames)


def flu_to_frd(x, y, z):
    return x, -y, -z
