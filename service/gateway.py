"""Explicitly selected, fixed-port ESP32 UART1 telemetry transport."""

import asyncio
import socket

from lf2s_protocol.wire import ProtocolError, decode_datagram

from service.esp32 import target


class Gateway(asyncio.DatagramProtocol):
    def __init__(self, host, receive):
        self.peer = (target(host, 80)[0], 12700)
        self.receive = receive
        self.transport = None
        self.local = None
        self.active = False
        self.error = None

    async def start(self):
        route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            route.connect(self.peer)
            local_ip = route.getsockname()[0]
        except OSError as exc:
            raise ProtocolError("无法确定到 ESP32 的本机网络接口") from exc
        finally:
            route.close()
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: self, local_addr=(local_ip, 12701), family=socket.AF_INET
            )
        except OSError as exc:
            raise ProtocolError("无法打开 UDP 12701；请关闭其他串口链路测试程序") from exc
        self.transport = transport
        self.local = transport.get_extra_info("sockname")
        self.active = True

    def datagram_received(self, data, address):
        if self.active:
            self.receive(data, address)

    def error_received(self, exc):
        self.error = str(exc)

    def send(self, raw):
        message = decode_datagram(raw)
        if not self.active or self.transport is None:
            raise ProtocolError("真实 UART 遥测通道已关闭")
        heartbeat = message.get_msgId() == 0 and message.type == 6 and message.autopilot == 8 and message.base_mode == 0 and message.custom_mode == 0
        if not heartbeat and (message.get_msgId() != 60000 or message.op not in (1, 8)):
            raise ProtocolError("真实 UART 通道只允许只读握手、配置查询及地面站心跳")
        self.transport.sendto(raw, self.peer)

    def close(self):
        self.active = False
        if self.transport is not None:
            self.transport.close()
            self.transport = None
