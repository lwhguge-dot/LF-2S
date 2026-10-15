"""One fixed, loopback-only demonstration using the existing protocol contract.

The plant supplies fresh synthetic samples; this service never treats an ACK or
an elapsed delay as an achieved altitude, a completed hold, or a landing.
"""

import asyncio
import secrets

from lf2s_protocol.wire import ProtocolError

TERMINAL = {"completed", "rejected", "cancelled", "superseded", "failed"}


class TaskService:
    def __init__(self, adapter):
        self.adapter = adapter
        self.current = None
        self.runner = self.renewer = None
        self.task_counter = 0
        self.context = None
        self.lease = 0
        self.unknown_request_id = None
        self.task_bound = False

    @property
    def running(self):
        return self.runner is not None and not self.runner.done()

    def view(self):
        return {
            "running": self.running,
            "current": self.current,
            "unknown_request_id": self.unknown_request_id,
            "preset": "takeoff_hold_land",
            "target_height_mm": 500,
            "simulation_only": True,
            "hardware_control_allowed": False,
        }

    def stage(self, stage, **fields):
        if self.current:
            self.current.update(stage=stage, **fields)
            self.adapter.journal.add(
                "task", f"模拟任务：{stage}", task=self.current.copy()
            )

    def check(self, *, lease=False):
        a = self.adapter
        state = a.snapshot()
        wire = a.last_state
        if a.replaying or not a.sim.task_bench:
            raise ProtocolError("仅显式准备的本机模拟任务环境可执行；回放不能执行")
        if state["link"]["phase"] != "online" or state["link"]["state_age_ms"] >= 300:
            raise ProtocolError("任务状态不新鲜；停止流程，旧动作不恢复")
        if wire.manual_latched:
            raise ProtocolError("人工接管已锁存；重新连接不能解除")
        if not all(
            state["sensors"][n]["valid"] for n in ("attitude", "position", "height")
        ):
            raise ProtocolError("任务需要新的有效姿态、位置和高度样本")
        if not a.profile or (
            a.profile.boot_id,
            a.profile.profile_id,
            a.profile.calibration_id,
            a.profile.reset_counter,
        ) != (wire.boot_id, wire.profile_id, wire.calibration_id, wire.reset_counter):
            raise ProtocolError("请先查询当前模拟配置；配置、标定或定位代数已变化")
        context = (
            a.client.boot,
            a.client.session,
            wire.profile_id,
            wire.calibration_id,
            wire.reset_counter,
        )
        if self.context is not None and context != self.context:
            raise ProtocolError("任务会话或定位代数已变化；旧任务作废")
        if not wire.challenge:
            raise ProtocolError("没有当前会话 challenge")
        if lease and (
            wire.lease_id != self.lease
            or wire.lease_remaining_ms <= 0
            or wire.owner_system != a.client.system
            or wire.owner_component != a.client.component
        ):
            raise ProtocolError("控制权租期或所有者不匹配")
        if self.task_bound and wire.active_task != self.task_counter:
            raise ProtocolError("当前任务编号与本次许可绑定任务不匹配")
        return wire

    def start(self, confirmed):
        if confirmed is not True:
            raise ProtocolError("必须由现场人员明确勾选本次模拟许可")
        if self.running or (self.current and self.current["status"] == "unknown"):
            raise ProtocolError("已有模拟任务或未知结果；不能重复提交动作")
        self.context = None
        self.task_bound = False
        self.lease = 0
        wire = self.check()
        if wire.lease_id or wire.active_task:
            raise ProtocolError("模拟器尚有控制权或任务；请等待撤权或显式重置模拟器")
        if not wire.valid_flags & 8 or not wire.valid_flags & 32:
            raise ProtocolError("模拟着地和预检资格不足")
        self.task_counter += 1
        if self.task_counter > 0xFFFFFFFF:
            raise ProtocolError("模拟任务编号耗尽，请重启服务")
        self.context = (
            self.adapter.client.boot,
            self.adapter.client.session,
            wire.profile_id,
            wire.calibration_id,
            wire.reset_counter,
        )
        self.unknown_request_id = None
        self.lease = 0
        self.task_bound = False
        self.current = {
            "task_id": self.task_counter,
            "status": "pending",
            "stage": "现场已确认",
            "reason": None,
            "simulation": True,
            "source": "synthetic",
            "session_id": str(self.adapter.client.session),
            "confirmed_at_ms": self.adapter.now(),
            "steps": [],
        }
        self.runner = asyncio.create_task(self.run())
        return self.view()

    async def new_state(self, sample):
        for _ in range(60):
            if self.adapter.last_state and self.adapter.last_state.sample_id > sample:
                return
            await asyncio.sleep(0.02)
        raise ProtocolError("没有新的当前会话状态样本")

    async def step(self, name, *, action=False, completion=False, **fields):
        self.check(lease=self.lease != 0)
        self.stage(name)
        sample = self.adapter.client.sample
        op = await self.adapter.request_operation(
            name, action=action, on_created=self.track, **fields
        )
        if op["status"] == "unknown":
            self.unknown_request_id = op["request_id"]
            raise ProtocolError("结果未知；只查询原请求编号，不新建编号重复动作")
        if op["status"] in ("rejected", "failed", "cancelled", "superseded"):
            raise ProtocolError(f"{name}：{op['protocol_phase']} / {op['reason']}")
        if completion:
            # Completion is exclusively a correlated final LF2S_RESULT.
            deadline = self.adapter.now() + fields.get("duration_ms", 1500) + 600
            while op["status"] not in TERMINAL:
                self.check(lease=True)
                if self.adapter.now() >= deadline:
                    op.update(status="unknown", reason="尚未收到最终结果")
                    self.unknown_request_id = op["request_id"]
                    raise ProtocolError("最终结果未知；仅可查询原请求")
                await asyncio.sleep(0.025)
            if op["status"] != "completed":
                raise ProtocolError(f"{name}：{op['protocol_phase']} / {op['reason']}")
        await self.new_state(sample)
        return op

    def track(self, op):
        self.current["steps"].append(op["operation_id"])
        self.current["request_id"] = op["request_id"]

    async def renew(self):
        try:
            while True:
                await asyncio.sleep(0.45)
                self.check(lease=True)
                op = await self.adapter.request_operation("RENEW", valid_ms=500)
                if op["status"] != "accepted":
                    raise ProtocolError("续租结果未知或被拒绝，停止任务")
        except (ProtocolError, TimeoutError) as exc:
            self.interrupt(str(exc))

    async def run(self):
        try:
            self.stage("PERMIT", status="in_progress")
            wire = self.check()
            permit_id = secrets.randbelow(0xFFFFFFFF) + 1
            await self.step(
                "PERMIT",
                permit_id=permit_id,
                profile_id=wire.profile_id,
                calibration_id=wire.calibration_id,
                reset_counter=wire.reset_counter,
                scope=1,
                flags=1,
                valid_ms=500,
            )
            await self.step("ACQUIRE", permit_id=permit_id, valid_ms=500)
            self.lease = self.adapter.client.lease
            self.check(lease=True)
            self.renewer = asyncio.create_task(self.renew())
            await self.step(
                "BEGIN",
                action=True,
                task_id=self.task_counter,
                duration_ms=20000,
                valid_ms=500,
            )
            if self.adapter.last_state.active_task != self.task_counter:
                raise ProtocolError("BEGIN 未被当前状态确认为本次任务")
            self.task_bound = True
            await self.step(
                "TAKEOFF",
                action=True,
                z_mm=500,
                duration_ms=8000,
                settle_ms=300,
                valid_ms=500,
                completion=True,
            )
            await self.step(
                "HOLD",
                action=True,
                duration_ms=2500,
                settle_ms=500,
                valid_ms=500,
                completion=True,
            )
            await self.step(
                "LAND", action=True, duration_ms=8000, valid_ms=500, completion=True
            )
            await self.step(
                "END", action=True, outcome=0, valid_ms=500, completion=True
            )
            self.task_bound = False
            await self.step("RELEASE")
            self.stage(
                "已完成",
                status="completed",
                reason="起飞、保持和着地/上锁均由新合成样本确认；非实机验收",
            )
        except ProtocolError as exc:
            request_id = self.current.get("request_id")
            known = self.adapter.known_requests.get(request_id)
            last = self.adapter.operations.get(known[1]) if known else None
            if last and last["status"] not in TERMINAL:
                # A locally stopped sequence does not prove its last action's outcome.
                self.unknown_request_id = request_id
            self.stage(
                "已停止",
                status="unknown" if self.unknown_request_id else "failed",
                reason=str(exc),
            )
        except asyncio.CancelledError:
            pass
        finally:
            if self.renewer and self.renewer is not asyncio.current_task():
                self.renewer.cancel()
                await asyncio.gather(self.renewer, return_exceptions=True)
            self.adapter.stop_task_retries()

    def interrupt(self, reason):
        if not self.running:
            return
        self.stage("已中断", status="unknown", reason=reason + "；旧任务不自动恢复")
        if self.current.get("request_id"):
            self.unknown_request_id = self.current["request_id"]
        if self.runner is not asyncio.current_task():
            self.runner.cancel()
        if self.renewer and self.renewer is not asyncio.current_task():
            self.renewer.cancel()
        self.adapter.stop_task_retries()

    async def cancel(self):
        if not self.running:
            raise ProtocolError("没有正在运行的模拟任务")
        a = self.adapter
        self.interrupt("人员取消")
        await asyncio.gather(self.runner, return_exceptions=True)
        if a.connected and a.last_state and a.last_state.lease_id:
            op = await a.request_operation(
                "CANCEL", action=True, challenge=0, valid_ms=0
            )
            if op["status"] == "completed":
                self.unknown_request_id = None
                self.stage(
                    "已取消", status="cancelled", reason="已收到匹配的 CANCEL 最终结果"
                )
        return self.view()

    async def query(self):
        a = self.adapter
        if self.running or not self.unknown_request_id or self.context is None:
            raise ProtocolError("没有待查询的原任务请求")
        if (a.client.boot, a.client.session) != self.context[:2] or a.snapshot()[
            "link"
        ]["phase"] != "online":
            raise ProtocolError("原会话已丢失；不能用新会话冒充查询原结果")
        target = self.unknown_request_id
        await a.request_operation("QUERY_RESULT", query_id=target)
        original = a.known_requests.get(target)
        op = a.operations.get(original[1]) if original else None
        if op and op["status"] in TERMINAL:
            self.current["queried_result"] = {
                "request_id": target,
                "phase": op.get("protocol_phase"),
                "reason": op["reason"],
            }
            # Recovering one result never resumes a sequence or declares the whole task complete.
            self.stage(
                "查询结束，流程未恢复",
                status="interrupted",
                reason="原请求结果已取回；旧任务不继续执行",
            )
            self.unknown_request_id = None
        return self.view()

    async def close(self):
        self.interrupt("本地服务关闭")
        jobs = [t for t in (self.runner, self.renewer) if t]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
