"""Bounded adapters around MAVLink and the vendor's original RC frame."""

from .wire import MAX_FRAME, ProtocolError, decode_datagram


def crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ (0x07 if crc & 0x80 else 0)) & 255
    return crc


def rc_encode(channels):
    if len(channels) != 8 or any(type(value) is not int or not 0 <= value <= 65535 for value in channels):
        raise ProtocolError("Exactly eight u16 vendor channels required")
    data = bytearray(b"\xaa\xaf\x03\x1b" + b"\x00" * 28)
    for index, value in enumerate(channels):
        data[4 + index * 2:6 + index * 2] = value.to_bytes(2, "big")
    data[31] = sum(data[:31]) & 255
    return bytes(data)


def rc_decode(data):
    raw = bytes(data)
    if len(raw) != 32 or raw[:4] != b"\xaa\xaf\x03\x1b" or sum(raw[:31]) & 255 != raw[31]:
        raise ProtocolError("Not the verified vendor RC frame")
    return tuple(int.from_bytes(raw[4 + index * 2:6 + index * 2], "big") for index in range(8))


def nrf_fragments(frame, transfer_id):
    raw = bytes(frame)
    decode_datagram(raw)
    if not 0 <= transfer_id <= 65535:
        raise ProtocolError("transfer_id must be u16")
    count = (len(raw) + 23) // 24
    fragments = []
    for index in range(count):
        piece = raw[index * 24:(index + 1) * 24]
        header = bytearray([0xD6, 2, transfer_id & 255, transfer_id >> 8, index, count, len(piece), 0])
        padded = piece.ljust(24, b"\x00")
        header[7] = crc8(header[:7] + padded)
        fragments.append(bytes(header) + padded)
    return fragments


class NRFAssembler:
    def __init__(self, timeout_ms):
        if not 0 < timeout_ms < 0x80000000:
            raise ValueError("Finite measured reassembly timeout required")
        self.timeout_ms = timeout_ms
        self.reset()

    def reset(self):
        self.transfer = None
        self.count = 0
        self.started = 0
        self.pieces = {}

    def feed(self, data, now_ms):
        raw = bytes(data)
        if (len(raw) != 32 or raw[:2] != b"\xd6\x02" or not 1 <= raw[5] <= 12
                or raw[4] >= raw[5] or not 1 <= raw[6] <= 24
                or raw[4] * 24 + raw[6] > MAX_FRAME
                or (raw[4] + 1 < raw[5] and raw[6] != 24)
                or any(raw[8 + raw[6]:]) or crc8(raw[:7] + raw[8:]) != raw[7]):
            self.reset()
            raise ProtocolError("Malformed NRF fragment")
        transfer = int.from_bytes(raw[2:4], "little")
        if self.transfer is not None and (now_ms < self.started or now_ms - self.started >= self.timeout_ms):
            self.reset()
        if transfer != self.transfer:
            if raw[4] != 0:
                raise ProtocolError("Orphan NRF fragment")
            self.transfer, self.count, self.started = transfer, raw[5], now_ms
            self.pieces = {}
        if self.count != raw[5]:
            self.reset()
            raise ProtocolError("Conflicting fragment count")
        piece = raw[8:8 + raw[6]]
        old = self.pieces.get(raw[4])
        if old is not None and old != piece:
            self.reset()
            raise ProtocolError("Conflicting duplicate")
        self.pieces[raw[4]] = piece
        if len(self.pieces) != self.count:
            return None
        frame = b"".join(self.pieces[index] for index in range(self.count))
        self.reset()
        if len(frame) > MAX_FRAME:
            raise ProtocolError("Reassembled frame too long")
        decode_datagram(frame)
        return frame


class Gateway:
    """Validate routing and return unchanged bytes; it never fabricates FC results."""

    def __init__(self, system_id=1, pc_system=255, pc_component=190):
        self.system_id = system_id
        self.pc = (pc_system, pc_component)

    def forward(self, raw, from_pc):
        message = decode_datagram(raw)
        source = (message.get_srcSystem(), message.get_srcComponent())
        if from_pc:
            if source != self.pc or message.get_msgId() not in (0, 76, 60000, 60001):
                raise ProtocolError("Unexpected PC source or direction")
            if message.get_msgId() != 0 and (message.target_system, message.target_component) != (self.system_id, 1):
                raise ProtocolError("Wrong destination")
        elif source != (self.system_id, 1):
            raise ProtocolError("Only the paired FC may produce FC feedback")
        return bytes(raw)
