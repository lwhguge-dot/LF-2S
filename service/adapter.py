"""Single UDP owner and application validity model over the reference Client."""

import asyncio
import os
import secrets
import time

from lf2s_protocol.client import Client
from lf2s_protocol.model import PHASE, REASON
from lf2s_protocol.version import DIALECT_HASH, VERSION
from lf2s_protocol.wire import ProtocolError, decode_datagram

from service.controller import controller_snapshot
from service.gateway import Gateway
from service.serialization import clean, message_json
from service.tasks import TERMINAL, TaskService
from service.version import APPLICATION_VERSION
from simulation.network import SCENARIOS, Simulation

# Application simulation budgets, not signed production flight parameters.
LINK_MAX_AGE_MS = 1000
MEASUREMENT_MAX_AGE_MS = 300
REBOOT_RECONNECT_REASON = "飞控已重启或会话已改变；旧会话失效，请断开后重新连接"


class Adapter:
    def __init__(self, root, journal):
        self.started = time.monotonic()
        self.journal = journal
        self.sim = Simulation(root, self.now)
        self.gateway = None
        self.gateway_heartbeat_at = None
        self.gateway_recovery_at = None
        self.gateway_recovery_tries = 0
        self.connected = False
        self.client = Client()
        self.last_state = None
        self.state_at = None
        self.last_rx = None
        self.telemetry = {}
        self.profile = None
        self.profile_at = None
        self.failure = None
        self.reconnect_required = False
        self.reconnect_reason = None
        self.operations = {}
        self.waiters = {}
        self.query_request = None
        self.received = self.rejected = self.sent = 0
        self.last_state_log = -1000
        self.last_reject = None
        self.link_label = None
        self.task = None
        self.known_requests = {}
        self.replaying = False
        self.tasks = TaskService(self)
        self.journal.context = lambda: {
            "simulation": self.gateway is None,
            "source": "uart1" if self.gateway else "synthetic",
            "boot_id": str(self.client.boot) if self.client.boot else None,
            "session_id": str(self.client.session) if self.client.session else None,
            "client_nonce": str(self.client.nonce) if self.client.nonce else None,
        }

    def now(self):
        return int((time.monotonic() - self.started) * 1000)

    async def start(self):
        await self.sim.start(self.receive_simulation)
        self.task = asyncio.create_task(self.schedule())
        self.journal.add("service", "本地服务就绪；只读模拟模式，不连接飞机")

    def receive_simulation(self, raw, source):
        if self.gateway is None:
            self.receive(raw, source)

    def clear_session(self, reason):
        self.tasks.interrupt(reason)
        for future in self.waiters.values():
            if not future.done():
                future.set_exception(ProtocolError(reason))
        self.waiters.clear()
        self.client = Client(max_retries=2, ack_timeout_ms=200)
        self.last_state = self.state_at = self.last_rx = None
        self.telemetry.clear()
        self.profile = self.profile_at = self.query_request = None
        self.failure = None
        self.reconnect_required = False
        self.reconnect_reason = None
        self.last_reject = None
        self.known_requests.clear()
        self.gateway_recovery_at = None
        self.gateway_recovery_tries = 0

    def require_reconnect(self, reason):
        self.tasks.interrupt(reason)
        self.reconnect_required = True
        self.reconnect_reason = self.failure = reason
        self.client.pending.clear()
        self.telemetry.clear()
        self.profile = self.profile_at = None
        self.last_state = self.state_at = self.last_rx = None

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
        while len(self.operations) > 96:
            self.operations.pop(next(iter(self.operations)))
        return self.operations[op_id]

    def transmit(self, raw, operation, retry=False):
        msg = decode_datagram(raw)
        # No configurable destination, generic sender or COMMAND_LONG bypass.
        if self.replaying:
            raise ProtocolError("回放通道只读，禁止发送协议请求")
        if self.reconnect_required:
            raise ProtocolError(self.reconnect_reason or REBOOT_RECONNECT_REASON)
        readonly = msg.get_msgId() == 60000 and msg.op in (1, 8)
        simulated = self.gateway is None and self.sim.task_bench and (
            (msg.get_msgId() == 60000 and msg.op in (2, 3, 4, 5, 6, 7))
            or (msg.get_msgId() == 60001 and msg.op in (1, 2, 5, 6, 7, 8))
        )
        if not readonly and not simulated:
            raise ProtocolError("仅允许只读请求及固定的本机模拟任务操作")
        operation["request_id"] = msg.request_id
        operation["request_msg"] = msg.get_msgId()
        operation["boot_id"] = str(msg.boot_id)
        operation["session_id"] = str(msg.session_id)
        self.known_requests[msg.request_id] = (raw, operation["operation_id"])
        while len(self.known_requests) > 128:
            self.known_requests.pop(next(iter(self.known_requests)))
        self.sent += 1
        self.journal.add(
            "tx",
            "原字节同编号重试" if retry else f"发送 {operation['name']}",
            operation_id=operation["operation_id"],
            request_id=msg.request_id,
            wire=message_json(msg),
            raw_hex=raw.hex(),
        )
        if self.gateway is not None:
            self.gateway.send(raw)
        else:
            self.sim.send(raw)

    async def connect_gateway(self, host):
        if self.replaying or self.connected or self.gateway is not None:
            raise ProtocolError("请先断开当前连接并退出回放，再连接真实 UART1")
        gateway = Gateway(host, self.receive)
        await gateway.start()
        self.gateway = gateway
        self.gateway_heartbeat_at = None
        self.journal.add("connection", "连接真实 UART1 只读遥测；飞行动作关闭")
        try:
            return await self.connect()
        except BaseException:
            self.disconnect()
            raise

    async def connect(self):
        if self.replaying:
            raise ProtocolError("请先退出只读回放，再显式建立新会话")
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
        self.tasks.interrupt("连接已断开")
        self.connected = False
        if self.gateway is not None:
            self.gateway.close()
            self.gateway = None
        for op in self.operations.values():
            if op["status"] in ("pending", "accepted", "in_progress"):
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

    async def request_operation(self, name, *, action=False, on_created=None, **fields):
        if not self.connected or self.replaying:
            raise ProtocolError("未连接或正在只读回放")
        op = self.begin_operation(name)
        raw = (self.client.action if action else self.client.control)(
            name, self.now(), **fields
        )
        request_id = self.client.request_id
        future = asyncio.get_running_loop().create_future()
        self.waiters[request_id] = future
        self.transmit(raw, op)
        if on_created:
            on_created(op)
        try:
            await asyncio.wait_for(future, 1.5)
        except (TimeoutError, ProtocolError) as exc:
            if op["status"] not in TERMINAL:
                op.update(status="unknown", reason=str(exc) or "协议结果未知")
        finally:
            self.waiters.pop(request_id, None)
        return op

    def reject(self, reason):
        self.rejected += 1
        self.failure = (self.reconnect_reason or REBOOT_RECONNECT_REASON) if self.reconnect_required else reason
        if self.last_reject != reason:
            self.journal.add("warning", reason)
            self.last_reject = reason

    def receive(self, raw, source):
        if not self.connected:
            return
        if self.reconnect_required:
            self.reject(self.reconnect_reason or REBOOT_RECONNECT_REASON)
            return
        try:
            peer = self.gateway.peer if self.gateway is not None else self.sim.esp_address
            if source != peer:
                raise ProtocolError("UDP 来源不匹配，数据已拒绝")
            msg = decode_datagram(raw)
            mid = msg.get_msgId()
            if (msg.get_srcSystem(), msg.get_srcComponent()) != (1, 1):
                raise ProtocolError("飞控来源不匹配，数据已拒绝")
            if mid == 60003:
                if msg.dialect_hash != DIALECT_HASH:
                    raise ProtocolError("协议指纹不匹配，请检查两端协议版本")
                if self.client.boot and (
                    msg.boot_id != self.client.boot
                    or (self.gateway is not None and self.client.session and
                        (msg.session_id, msg.client_nonce) != (self.client.session, self.client.nonce))
                ):
                    self.require_reconnect(REBOOT_RECONNECT_REASON)
                    raise ProtocolError(REBOOT_RECONNECT_REASON)
                if msg.sample_id <= self.client.sample:
                    return
            if mid == 60002:
                known = self.known_requests.get(msg.request_id)
                if known is None:
                    return  # no unsolicited ACK may establish a session or satisfy an operation
                req = decode_datagram(known[0])
                if (self.gateway is not None and self.client.session and req.op == 1 and
                    (msg.boot_id != self.client.boot or msg.session_id != self.client.session)):
                    self.require_reconnect(REBOOT_RECONNECT_REASON)
                    raise ProtocolError(REBOOT_RECONNECT_REASON)
                if (msg.request_msg, msg.echo_op) != (req.get_msgId(), req.op):
                    raise ProtocolError("结果与原请求不匹配")
                if req.op == 1 and msg.client_nonce != self.client.nonce:
                    if req.get_msgId() == 60000:
                        raise ProtocolError("HELLO 随机编号不匹配")
                if req.get_msgId() == 60001:
                    # CANCEL/FAILSAFE and failed END report their own final
                    # result after revocation, so their current lease is zero.
                    # Rejections can also follow an expired lease. These cases
                    # must not admit another nonzero lease or a false completion.
                    revoked_final = (
                        msg.lease_id == 0
                        and msg.event_id != 0
                        and (
                            (req.op in (7, 9) and msg.phase == 3 and msg.reason == 0)
                            or (
                                req.op == 8
                                and req.outcome == 1
                                and msg.phase == 7
                                and msg.reason == 15
                            )
                        )
                    )
                    rejected_without_lease = msg.lease_id == 0 and msg.phase == 4
                    if msg.task_id != req.task_id or (
                        msg.lease_id != req.lease_id
                        and not revoked_final
                        and not rejected_without_lease
                    ):
                        raise ProtocolError("动作结果的任务或租期与原请求不匹配")
                    if msg.phase in (3, 5, 6, 7) and msg.event_id == 0:
                        raise ProtocolError("动作最终结果缺少事件编号")
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
            if mid in (30, 331) and self.gateway is not None:
                if self.last_state is None or self.now() - self.state_at >= LINK_MAX_AGE_MS:
                    return
                old = self.telemetry.get(mid)
                field = "time_boot_ms" if mid == 30 else "time_usec"
                if old is not None:
                    delta = getattr(msg, field) - getattr(old[0], field)
                    if mid == 30:
                        delta &= 0xFFFFFFFF
                    if delta <= 0 or (mid == 30 and delta >= 0x80000000):
                        return  # Duplicate/backward sample never refreshes age.
            self.client.observe(raw)
            now = self.now()
            self.last_rx = now
            self.received += 1
            if mid == 60003:
                self.last_state, self.state_at = msg, now
                self.gateway_recovery_at = None
                self.gateway_recovery_tries = 0
                # Failures requiring reconnection remain latched. Transient mismatches recover only
                # when a valid current-context state actually arrives, never from arbitrary traffic.
                if self.failure and not self.reconnect_required:
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
                    "收到真实只读配置；未签认" if self.gateway else "收到模拟配置；未签认",
                    wire=message_json(msg),
                    raw_hex=raw.hex(),
                )
                self.complete_query()
            elif mid in (30, 331):
                self.telemetry[mid] = (msg, now)
            elif mid == 60002:
                known = self.known_requests.get(msg.request_id)
                op = self.operations.get(known[1]) if known else None
                if op:
                    if op["status"] in TERMINAL:
                        if msg.phase in (1, 2):
                            self.journal.add(
                                "warning",
                                "忽略迟到的接受/进行中结果；保留最终结果",
                                request_id=msg.request_id,
                            )
                        return
                    phase = PHASE[msg.phase] if msg.phase < len(PHASE) else "UNKNOWN"
                    reason = (
                        REASON[msg.reason] if msg.reason < len(REASON) else "UNKNOWN"
                    )
                    op.update(
                        status=phase.lower(),
                        protocol_phase=phase,
                        reason=reason,
                        boot_id=str(msg.boot_id),
                        session_id=str(msg.session_id),
                        event_id=msg.event_id,
                        result_at_ms=now,
                    )
                    self.journal.add(
                        "rx",
                        f"{op['name']}：{phase} / {reason}",
                        operation_id=op["operation_id"],
                        request_id=msg.request_id,
                        wire=message_json(msg),
                        raw_hex=raw.hex(),
                    )
                    if msg.request_msg == 60000 and msg.echo_op == 1:
                        self.resolve(msg.request_id)
                    elif msg.request_msg != 60000 or msg.echo_op != 8 or msg.phase != 1:
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
            # Onboard synthetic timers continue when the PC is disconnected or its link is paused.
            self.sim.tick()
            if self.gateway is not None and self.connected and not self.reconnect_required and not self.failure:
                now = self.now()
                if self.gateway_heartbeat_at is None or now - self.gateway_heartbeat_at >= 500:
                    # Peer idle budget in the paired 0.1.7 build is 3000ms.
                    # Heartbeat maintains only transport presence; it never
                    # refreshes FC state/measurement age or grants authority.
                    self.gateway.send(self.client.codec.encode(
                        "HEARTBEAT", type=6, autopilot=8, base_mode=0,
                        custom_mode=0, system_status=4, mavlink_version=3,
                    ))
                    self.gateway_heartbeat_at = now
                if self.state_at is not None and now - self.state_at >= 750:
                    if self.gateway_recovery_at is None or now - self.gateway_recovery_at >= 500:
                        if self.gateway_recovery_tries >= 3:
                            self.require_reconnect("真实 UART1 只读链路恢复失败；请断开后显式重连")
                        else:
                            known = self.known_requests.get(1)
                            if known:
                                # Reuse the exact original HELLO after a peer
                                # reset. Never create a nonce/session or replay
                                # a control/action request. A different returned
                                # boot/session latches explicit reconnection.
                                self.gateway.send(known[0])
                                self.gateway_recovery_at = now
                                self.gateway_recovery_tries += 1
                                self.journal.add("connection", "正在恢复同一只读会话；重发原 HELLO 字节")
            if self.connected:
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
                        op.update(status="unknown", reason="原请求重试耗尽；结果未知")
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
            if self.tasks.running and (
                phase != "online"
                or (self.last_state and self.last_state.manual_latched)
            ):
                self.tasks.interrupt("链路失效或人工接管锁存")
            if phase != self.link_label:
                self.link_label = phase
                self.journal.add("connection", f"连接阶段：{phase}")
            if self.journal.recorder and self.gateway is None:
                self.journal.recorder.sample(self.snapshot())
            await asyncio.sleep(0.1)

    def set_scenario(self, name):
        if self.gateway is not None:
            raise ProtocolError("真实 UART1 连接期间不能改变模拟场景")
        if self.replaying:
            raise ProtocolError("回放通道不能修改模拟器")
        if name == "task_bench":
            self.disconnect()
            self.tasks.context = None
            self.tasks.task_bound = False
            self.tasks.lease = 0
            self.tasks.unknown_request_id = None
            self.tasks.current = None
        self.sim.set_scenario(name)
        self.journal.add("scenario", f"模拟场景：{SCENARIOS[name]}")
        return {"scenario": name}

    def stop_task_retries(self):
        for request_id, (_, op_id) in list(self.known_requests.items()):
            op = self.operations.get(op_id)
            if (
                op
                and op["status"] == "pending"
                and op["name"] not in ("HELLO", "QUERY_PROFILE", "QUERY_RESULT")
            ):
                op.update(
                    status="unknown",
                    reason="流程已停止，停止旧请求重试；仅可查询原编号",
                )
                self.client.pending.pop(request_id, None)

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
            "source": "uart1" if self.gateway is not None else "synthetic",
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
            if self.failure or self.reconnect_required
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
            "simulation": self.gateway is None,
            "service_pid": os.getpid(),
            "read_only": self.gateway is not None or self.replaying or not self.sim.task_bench,
            "hardware_control_allowed": False,
            "task_fixture": self.gateway is None and self.sim.task_bench,
            "service_ready": True,
            "scenario": self.sim.scenario,
            "scenarios": SCENARIOS,
            "service_time_ms": now,
            "link": {
                "phase": phase,
                "state_age_ms": age,
                "reconnect_required": self.reconnect_required,
                "max_age_ms": LINK_MAX_AGE_MS,
                "reason": self.failure
                or ("超过 1000 ms 未收到当前会话状态" if phase == "stale" else None),
                "transport": "UDP → ESP32 → STM32 UART1（只读）" if self.gateway else "127.0.0.1 UDP → 模拟 ESP32 → FCModel",
                "ports": [self.gateway.local[1], self.gateway.peer[1]] if self.gateway else [
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
            "controller": controller_snapshot(
                state,
                available=phase == "online" and not self.replaying,
                age_ms=age,
                reason=self.failure or ("链路状态过期" if phase == "stale" else None),
            ),
            "qualification": {
                "configuration": "unsigned" if self.profile else "unknown",
                "configuration_reason": ("真实只读配置未签认" if self.gateway else "模拟配置未签认")
                if self.profile
                else "尚未读取配置",
                "control_allowed": False,
                "control_reason": "真实 UART1 仅观察遥测；飞行动作关闭" if self.gateway else "真实硬件与生产配置未签认；仅允许显式本机模拟任务",
                "hardware_freshness_qualified": False,
                "manual_latched": bool(state.manual_latched) if state else None,
                "lease_id": state.lease_id if state else None,
                "task_id": state.active_task if state else None,
            },
            "profile": message_json(self.profile) if self.profile else None,
            "task": self.tasks.view(),
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
        await self.tasks.close()
        self.disconnect()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.sim.close()
