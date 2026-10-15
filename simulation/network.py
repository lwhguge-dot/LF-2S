"""Three real UDP endpoints on loopback: PC -> Gateway -> FCModel.

All share the application process. No serial, Wi-Fi peer or motors are opened.
The explicit synthetic producer supplies telemetry, never Codec default values.
"""

import asyncio
import json
import math
from pathlib import Path

from lf2s_protocol.model import FCModel, Sensors
from lf2s_protocol.transports import Gateway
from lf2s_protocol.version import DIALECT_HASH
from lf2s_protocol.wire import Codec, ProtocolError, decode_datagram

SCENARIOS = {
    "healthy": "合成测量正常",
    "unknown": "所有测量未知",
    "sensor_freeze": "传感器停止更新",
    "link_pause": "暂停模拟链路",
    "bad_fingerprint": "协议指纹不匹配",
    "wrong_source": "飞控来源不匹配",
    "reboot": "模拟飞控重启",
}


class Endpoint(asyncio.DatagramProtocol):
    def __init__(self, receive):
        self.receive = receive
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.receive(data, addr)


class Simulation:
    def __init__(self, data_root, clock):
        self.clock = clock
        self.profile = json.loads(
            (Path(data_root) / "simulation/profile.json").read_text("utf-8")
        )
        self.boot = 0xF1234567  # session_id deliberately exceeds JavaScript's safe integer range
        self.model = self.make_model()
        self.scenario = "healthy"
        self.gateway = Gateway()
        self.pc_peer = None
        self.channels = []

    def make_model(self):
        return FCModel(self.profile, boot_id=self.boot, boot_storage_verified=False)

    async def start(self, receive):
        loop = asyncio.get_running_loop()
        for handler in (receive, self.gateway_receive, self.fc_receive):
            channel, _ = await loop.create_datagram_endpoint(
                lambda h=handler: Endpoint(h), local_addr=("127.0.0.1", 0)
            )
            self.channels.append(channel)
        self.pc, self.esp, self.fc = self.channels
        self.pc_address, self.esp_address, self.fc_address = [
            c.get_extra_info("sockname") for c in self.channels
        ]

    def gateway_receive(self, raw, source):
        if self.scenario == "link_pause":
            return
        try:
            if source == self.fc_address:
                if self.pc_peer:
                    self.esp.sendto(self.gateway.forward(raw, False), self.pc_peer)
            elif source == self.pc_address:
                self.pc_peer = source
                self.esp.sendto(self.gateway.forward(raw, True), self.fc_address)
        except ProtocolError:
            # The wrong-source scenario reaches PC validation by explicit fault injection below.
            pass

    def fc_receive(self, raw, source):
        if source != self.esp_address or self.scenario == "link_pause":
            return
        try:
            msg = decode_datagram(raw)
            if msg.get_msgId() != 60000 or msg.op not in (1, 8):
                return  # defense in depth: the simulation network is read-only too
            self.emit(self.model.handle(raw, 1, self.clock()))
        except (ProtocolError, ValueError):
            pass

    def emit(self, frames):
        for raw in frames:
            if self.scenario == "bad_fingerprint":
                msg = decode_datagram(raw)
                if msg.get_msgId() == 60003:
                    fields = {n: getattr(msg, n) for n in msg.fieldnames}
                    fields["dialect_hash"] = DIALECT_HASH + 1
                    raw = self.model.codec.encode(msg.get_type(), **fields)
            if self.scenario == "wrong_source":
                msg = decode_datagram(raw)
                raw = Codec(2, 1).encode(
                    msg.get_type(), **{n: getattr(msg, n) for n in msg.fieldnames}
                )
                self.esp.sendto(
                    raw, self.pc_address
                )  # intentional injection, not Gateway acceptance
            else:
                self.fc.sendto(raw, self.esp_address)

    def send(self, raw):
        self.pc.sendto(raw, self.esp_address)

    def tick(self):
        now = self.clock()
        if self.scenario == "link_pause":
            return
        if self.scenario not in ("sensor_freeze", "unknown"):
            # Stationary synthetic bench sample: IDLE, motors locked. No flying implied.
            s = Sensors(
                x_mm=250,
                y_mm=-180,
                z_mm=420,
                attitude_valid=True,
                position_valid=True,
                height_valid=True,
                attitude_ms=now,
                position_ms=now,
                height_ms=now,
                battery_mv=3820,
                battery_ms=now,
                landed=False,
                landed_valid=False,
                preflight=False,
                reset_counter=1,
                roll=0.02 * math.sin(now / 1000),
                pitch=0.01,
                yaw=0.0,
            )
            self.emit(self.model.update_sensors(s, now))
            self.emit(
                [
                    self.model.codec.encode(
                        "ATTITUDE",
                        time_boot_ms=now,
                        roll=s.roll,
                        pitch=s.pitch,
                        yaw=s.yaw,
                        rollspeed=0.02 * math.cos(now / 1000),
                        pitchspeed=0.0,
                        yawspeed=0.0,
                    )
                ]
            )
            self.emit(
                [
                    self.model.codec.encode(
                        "ODOMETRY",
                        time_usec=now * 1000,
                        frame_id=20,
                        child_frame_id=12,
                        x=s.x_mm / 1000,
                        y=-s.y_mm / 1000,
                        z=-s.z_mm / 1000,
                        q=[
                            math.cos(s.roll / 2) * math.cos(s.pitch / 2),
                            math.sin(s.roll / 2) * math.cos(s.pitch / 2),
                            math.cos(s.roll / 2) * math.sin(s.pitch / 2),
                            -math.sin(s.roll / 2) * math.sin(s.pitch / 2),
                        ],
                        vx=0.0,
                        vy=0.0,
                        vz=0.0,
                        rollspeed=0.02 * math.cos(now / 1000),
                        pitchspeed=0.0,
                        yawspeed=0.0,
                        pose_covariance=[float("nan")] + [0.0] * 20,
                        velocity_covariance=[float("nan")] + [0.0] * 20,
                        reset_counter=1,
                        estimator_type=0,
                        quality=100,
                    )
                ]
            )
        else:
            if self.scenario == "unknown":
                self.model.sensors = Sensors(reset_counter=1)
            self.emit(self.model.tick(now))

    def set_scenario(self, name):
        if name not in SCENARIOS:
            raise ValueError("Unknown simulation scenario")
        self.scenario = name
        if name == "reboot":
            self.boot += 1
            self.model = self.make_model()
        if name == "unknown":
            self.model.sensors = Sensors(reset_counter=1)

    def close(self):
        for channel in self.channels:
            channel.close()
        self.channels.clear()
