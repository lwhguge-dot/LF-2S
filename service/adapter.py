"""Single UDP owner and application validity model over the reference Client."""

import asyncio
import os
import secrets
import time

from lf2s_protocol.client import Client
from lf2s_protocol.model import PHASE, REASON
from lf2s_protocol.version import DIALECT_HASH, VERSION
from lf2s_protocol.wire import ProtocolError, decode_datagram

from service.serialization import clean, message_json
from service.version import APPLICATION_VERSION
from simulation.network import SCENARIOS, Simulation

# Application simulation budgets, not signed production flight parameters.
LINK_MAX_AGE_MS = 1000
MEASUREMENT_MAX_AGE_MS = 300


class Adapter:
    def __init__(self, root, journal):
        self.started = time.monotonic()
        self.journal = journal
        self.sim = Simulation(root, self.now)
        self.connected = False
        self.client = Client()
        self.last_state = None
        self.state_at = None
        self.last_rx = None
        self.telemetry = {}
        self.profile = None
        self.profile_at = None
        self.failure = None
        self.operations = {}
        self.waiters = {}
        self.query_request = None
        self.received = self.rejected = self.sent = 0
        self.last_state_log = -1000
        self.last_reject = None
        self.link_label = None
        self.task = None

    def now(self):
        return int((time.monotonic() - self.started) * 1000)

    async def start(self):
        await self.sim.start(self.receive)
        self.task = asyncio.create_task(self.schedule())
        self.journal.add("service", "本地服务就绪；只读模拟模式，不连接飞机")

    def clear_session(self, reason):
        for future in self.waiters.values():
            if not future.done():
                future.set_exception(ProtocolError(reason))
        self.waiters.clear()
        self.client = Client(max_retries=2, ack_timeout_ms=200)
        self.last_state = self.state_at = self.last_rx = None
        self.telemetry.clear()
        self.profile = self.profile_at = self.query_request = None
        self.failure = None
        self.last_reject = None

    def begin_operation(self, name):
        op_id = secrets.token_hex(6)
        self.operations[op_id] = {
            "operation_id": op_id,
            "name": name,
            "status": "pending",
            "request_id": None,
            "request_msg": 60000,
            "reason": None,
        }
        while len(self.operations) > 32:
            self.operations.pop(next(iter(self.operations)))
        return self.operations[op_id]

    def transmit(self, raw, operation, retry=False):
        msg = decode_datagram(raw)
        # No action / permission / lease / standard COMMAND_LONG can pass this boundary.
        if msg.get_msgId() != 60000 or msg.op not in (1, 8):
            raise ProtocolError("只读版本拒绝所有动作和控制权请求")
        operation["request_id"] = msg.request_id
        operation["boot_id"] = str(msg.boot_id)
        operation["session_id"] = str(msg.session_id)
        self.sent += 1
        self.journal.add(
            "tx",
            "同编号只读重试" if retry else f"发送 {operation['name']}",
            operation_id=operation["operation_id"],
            request_id=msg.request_id,
            wire=message_json(msg),
            raw_hex=raw.hex(),
        )
        self.sim.send(raw)

    async def connect(self):
        if self.connected:
            raise ProtocolError("请先断开，再创建新会话")
        self.clear_session("重新连接，旧会话已失效")
        self.connected = True
        op = self.begin_operation("HELLO")
        future = asyncio.get_running_loop().create_future()
        self.waiters[1] = future
        self.transmit(
            self.client.hello(self.now(), nonce=secrets.randbits(64) | (1 << 63)), op
        )
        try:
            await asyncio.wait_for(future, 1.5)
        except (TimeoutError, ProtocolError) as exc:
            op.update(
                status="unknown", reason=str(exc) or "HELLO 超时；没有建立有效会话"
            )
            self.failure = op["reason"]
        finally:
            self.waiters.pop(1, None)
        return op.copy()

    def disconnect(self):
        self.connected = False
        for op in self.operations.values():
            if op["status"] == "pending":
                op.update(status="unknown", reason="连接已断开")
        self.clear_session("连接已断开，旧请求不重放")
        self.journal.add("connection", "已断开；旧会话和图像失效，不重放旧动作")
        return {"disconnected": True}

    async def query_profile(self):
        state = self.snapshot()
        if state["link"]["phase"] != "online":
            raise ProtocolError("链路未在线或状态已失效，请先恢复并重新连接")
        if self.query_request is not None:
            raise ProtocolError("已有配置查询，请等待结果")
        op = self.begin_operation("QUERY_PROFILE")
        raw = self.client.control("QUERY_PROFILE", self.now())
        request_id = self.client.request_id
        self.query_request = request_id
        future = asyncio.get_running_loop().create_future()
        self.waiters[request_id] = future
        # PROFILE has no request_id: serialize profile queries; old cache cannot satisfy this query.
        self.profile = self.profile_at = None
        self.transmit(raw, op)
        try:
            await asyncio.wait_for(future, 1.5)
        except (TimeoutError, ProtocolError) as exc:
            op.update(status="unknown", reason=str(exc) or "配置查询结果不确定")
        finally:
            self.query_request = None
            self.waiters.pop(request_id, None)
        return op.copy()

    def reject(self, reason):
        self.rejected += 1
        self.failure = reason
        if self.last_reject != reason:
            self.journal.add("warning", reason)
            self.last_reject = reason

    def receive(self, raw, source):
        if not self.connected:
            return
        try:
            if source != self.sim.esp_address:
                raise ProtocolError("UDP 来源不匹配，数据已拒绝")
            msg = decode_datagram(raw)
            mid = msg.get_msgId()
            if (msg.get_srcSystem(), msg.get_srcComponent()) != (1, 1):
                raise ProtocolError("飞控来源不匹配，数据已拒绝")
            if mid == 60003:
                if msg.dialect_hash != DIALECT_HASH:
                    raise ProtocolError("协议指纹不匹配，请检查两端协议版本")
                if self.client.boot and msg.boot_id != self.client.boot:
                    self.client.pending.clear()
                    self.telemetry.clear()
                    self.profile = self.profile_at = None
                    raise ProtocolError("飞控已重启；旧会话失效，请断开后重新连接")
                if msg.sample_id <= self.client.sample:
                    return
            if mid == 60002:
                item = self.client.pending.get(msg.request_id)
                if item is None:
                    return  # no unsolicited ACK may establish a session or satisfy an operation
                req = decode_datagram(item["raw"])
                if (msg.request_msg, msg.echo_op) != (req.get_msgId(), req.op):
                    raise ProtocolError("结果与原请求不匹配")
                if req.op == 1 and msg.client_nonce != self.client.nonce:
                    raise ProtocolError("HELLO 随机编号不匹配")
            if mid == 60004:
                if (
                    not self.client.session
                    or msg.boot_id != self.client.boot
                    or self.query_request is None
                    or not self.last_state
                    or (msg.profile_id, msg.calibration_id, msg.reset_counter)
                    != (
                        self.last_state.profile_id,
                        self.last_state.calibration_id,
                        self.last_state.reset_counter,
                    )
                ):
                    raise ProtocolError("配置不属于当前会话、标定或查询")
            if mid not in (60002, 60003, 60004, 30, 331):
                return
            if mid in (30, 331) and (
                not self.client.session or self.failure is not None
            ):
                return
            self.client.observe(raw)
            now = self.now()
            self.last_rx = now
            self.received += 1
            if mid == 60003:
                self.last_state, self.state_at = msg, now
                # Failures requiring reconnection remain latched. Transient mismatches recover only
                # when a valid current-context state actually arrives, never from arbitrary traffic.
                if self.failure and "重启" not in self.failure:
                    self.failure = None
                    self.last_reject = None
                if now - self.last_state_log >= 1000:
                    self.journal.add(
                        "state", "收到当前会话状态", wire=message_json(msg)
                    )
                    self.last_state_log = now
            elif mid == 60004:
                self.profile, self.profile_at = msg, now
                self.journal.add(
                    "rx",
                    "收到模拟配置；未签认",
                    wire=message_json(msg),
                    raw_hex=raw.hex(),
                )
                self.complete_query()
            elif mid in (30, 331):
                self.telemetry[mid] = (msg, now)
            elif mid == 60002:
                op = next(
                    (
                        o
                        for o in reversed(list(self.operations.values()))
                        if o["status"] == "pending"
                        and o["request_id"] == msg.request_id
                    ),
                    None,
                )
                if op:
                    phase = PHASE[msg.phase] if msg.phase < len(PHASE) else "UNKNOWN"
                    reason = (
                        REASON[msg.reason] if msg.reason < len(REASON) else "UNKNOWN"
                    )
                    op.update(
                        status="accepted" if msg.phase == 1 else "rejected",
                        protocol_phase=phase,
                        reason=reason,
                        boot_id=str(msg.boot_id),
                        session_id=str(msg.session_id),
                    )
                    self.journal.add(
                        "rx",
                        f"{op['name']}：{phase} / {reason}（只读查询）",
                        operation_id=op["operation_id"],
                        request_id=msg.request_id,
                        wire=message_json(msg),
                        raw_hex=raw.hex(),
                    )
                    if msg.echo_op == 1:
                        self.resolve(msg.request_id)
                    elif msg.phase != 1:
                        self.resolve(msg.request_id)
                    else:
                        self.complete_query()
        except ProtocolError as exc:
            reason = str(exc)
            translated = {
                "State from another boot/session; establish HELLO explicitly": "会话不匹配，请断开后重新连接",
                "Result from another boot/session": "结果不属于当前会话",
            }
            self.reject(translated.get(reason, reason))

    def resolve(self, request_id):
        future = self.waiters.get(request_id)
        if future and not future.done():
            future.set_result(True)

    def complete_query(self):
        if self.query_request is not None and self.profile is not None:
            result = self.client.results.get((60000, self.query_request))
            if result and result.phase == 1:
                self.resolve(self.query_request)

    async def schedule(self):
        while True:
            if self.connected:
                self.sim.tick()
                for request_id, pending in list(self.client.pending.items()):
                    if self.now() - pending["last"] < self.client.ack_timeout_ms:
                        continue
                    op = next(
                        (
                            o
                            for o in self.operations.values()
                            if o["request_id"] == request_id
                            and o["status"] == "pending"
                        ),
                        None,
                    )
                    if op is None:
                        self.client.pending.pop(request_id, None)
                        continue
                    try:
                        self.transmit(
                            self.client.retry(request_id, self.now()), op, retry=True
                        )
                    except ProtocolError:
                        op.update(status="unknown", reason="只读请求重试耗尽；结果未知")
                        self.journal.add(
                            "warning",
                            op["reason"],
                            operation_id=op["operation_id"],
                            request_id=request_id,
                        )
                        future = self.waiters.get(request_id)
                        if future and not future.done():
                            future.set_exception(ProtocolError(op["reason"]))
            phase = self.snapshot()["link"]["phase"]
            if phase != self.link_label:
                self.link_label = phase
                self.journal.add("connection", f"连接阶段：{phase}")
            await asyncio.sleep(0.1)

    def set_scenario(self, name):
        self.sim.set_scenario(name)
        self.journal.add("scenario", f"模拟场景：{SCENARIOS[name]}")
        return {"scenario": name}

    def sensor(self, name, bit, age_field, value, telemetry_at=None):
        now = self.now()
        state = self.last_state
        age = None
        reason = None
        if not self.connected or state is None:
            reason = "尚无当前会话测量"
        elif self.failure:
            reason = self.failure
        elif self.state_at is None or now - self.state_at >= LINK_MAX_AGE_MS:
            reason = "链路状态过期"
        else:
            reported = getattr(state, age_field) if age_field else None
            age = (
                None
                if reported == 0xFFFFFFFF
                else (reported + now - self.state_at if reported is not None else None)
            )
            if age is not None and age >= MEASUREMENT_MAX_AGE_MS:
                reason = "传感器样本过期"
            elif not state.valid_flags & (1 << bit):
                reason = "传感器未提供有效测量"
            elif (
                telemetry_at is not None
                and now - telemetry_at >= MEASUREMENT_MAX_AGE_MS
            ):
                reason = "标准遥测样本过期"
            elif value is None:
                reason = "尚未收到有效标准遥测"
        valid = reason is None and value is not None
        if name == "battery" and telemetry_at is not None:
            age = now - telemetry_at
            if age >= MEASUREMENT_MAX_AGE_MS:
                valid, reason = False, "电压样本过期"
        return {
            "valid": valid,
            "value": clean(value) if valid else None,
            "age_ms": age,
            "reason": reason,
            "source": "synthetic",
            "unit": {
                "height": "m",
                "position": "m / LOCAL_FLU",
                "attitude": "rad",
                "battery": "V",
            }[name],
        }

    def snapshot(self):
        now = self.now()
        state = self.last_state
        age = now - self.state_at if self.state_at is not None else None
        phase = (
            "disconnected"
            if not self.connected
            else "blocked"
            if self.failure
            else "connecting"
            if state is None
            else "stale"
            if age >= LINK_MAX_AGE_MS
            else "online"
        )
        odom, odom_at = self.telemetry.get(331, (None, None))
        attitude, attitude_at = self.telemetry.get(30, (None, None))
        position = None
        height = None
        if odom and odom.frame_id == 20 and odom.child_frame_id == 12:
            values = clean([odom.x, -odom.y, -odom.z])
            if all(v is not None for v in values):
                position, height = values, values[2]
        angles = (
            clean([attitude.roll, attitude.pitch, attitude.yaw]) if attitude else None
        )
        if angles and any(v is None for v in angles):
            angles = None
        voltage = (
            state.battery_mv / 1000 if state and state.battery_mv != 65535 else None
        )
        return {
            "api_version": "1",
            "application_version": APPLICATION_VERSION,
            "simulation": True,
            "service_pid": os.getpid(),
            "read_only": True,
            "service_ready": True,
            "scenario": self.sim.scenario,
            "scenarios": SCENARIOS,
            "service_time_ms": now,
            "link": {
                "phase": phase,
                "state_age_ms": age,
                "max_age_ms": LINK_MAX_AGE_MS,
                "reason": self.failure
                or ("超过 1000 ms 未收到当前会话状态" if phase == "stale" else None),
                "transport": "127.0.0.1 UDP → 模拟 ESP32 → FCModel",
                "ports": [
                    a[1]
                    for a in (
                        self.sim.pc_address,
                        self.sim.esp_address,
                        self.sim.fc_address,
                    )
                ],
            },
            "identity": {
                "boot_id": str(self.client.boot) if self.client.boot else None,
                "session_id": str(self.client.session) if self.client.session else None,
                "client_nonce": str(self.client.nonce) if self.client.nonce else None,
                "expected_dialect_hash": str(DIALECT_HASH),
                "observed_dialect_hash": str(state.dialect_hash) if state else None,
                "protocol_version": VERSION,
                "sample_id": state.sample_id if state else None,
            },
            "sensors": {
                "height": self.sensor("height", 2, "height_age_ms", height, odom_at),
                "position": self.sensor(
                    "position", 1, "position_age_ms", position, odom_at
                ),
                "attitude": self.sensor(
                    "attitude", 0, "attitude_age_ms", angles, attitude_at
                ),
                "battery": self.sensor("battery", 4, None, voltage, self.state_at),
            },
            "qualification": {
                "configuration": "unsigned" if self.profile else "unknown",
                "configuration_reason": "模拟配置未签认"
                if self.profile
                else "尚未读取配置",
                "control_allowed": False,
                "control_reason": "S1 只读；现场许可和动作入口尚未实现",
                "hardware_freshness_qualified": False,
                "manual_latched": bool(state.manual_latched) if state else None,
                "lease_id": state.lease_id if state else None,
                "task_id": state.active_task if state else None,
            },
            "profile": message_json(self.profile) if self.profile else None,
            "operations": list(self.operations.values()),
            "counters": {
                "sent": self.sent,
                "received": self.received,
                "rejected": self.rejected,
                "log_dropped": self.journal.dropped,
            },
            "events": list(self.journal.history),
            "sensor_max_age_ms": MEASUREMENT_MAX_AGE_MS,
        }

    async def close(self):
        self.disconnect()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.sim.close()
