"""LF-2S MAVLink reference code. This package is not flight firmware."""

from .version import DIALECT_HASH, VERSION
from .wire import Codec, ProtocolError, StreamParser, decode_datagram

__all__ = ["DIALECT_HASH", "VERSION", "Codec", "ProtocolError", "StreamParser", "decode_datagram"]
