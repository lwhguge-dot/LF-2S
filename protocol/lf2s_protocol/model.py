"""Executable contract model, with caller-supplied measurements and no motors.

No simulated ACK is evidence of real firmware or flight capability.
"""

import math
from collections import OrderedDict, deque
from dataclasses import dataclass

from .version import DIALECT_HASH
from .wire import Codec, business_key, decode_datagram

CONTROL = ("UNKNOWN", "HELLO", "PERMIT", "ACQUIRE", "RENEW", "RELEASE", "QUERY_RESULT", "CONFIRM_RESULT", "QUERY_PROFILE")
ACTION = ("UNKNOWN", "BEGIN", "TAKEOFF", "POSITION", "VELOCITY", "HOLD", "LAND", "CANCEL", "END", "FAILSAFE", "DISARM")
PHASE = ("UNKNOWN", "ACCEPTED", "IN_PROGRESS", "COMPLETED", "REJECTED", "CANCELLED", "SUPERSEDED", "FAILED")
REASON = ("OK", "BAD_FIELD", "WRONG_TARGET", "BAD_SESSION", "BUSY", "SCHEMA", "STALE", "DUPLICATE_CONFLICT", "FRESHNESS", "NO_PERMIT", "NO_AUTHORITY", "CONFIG", "SENSOR", "MANUAL", "LIMIT", "TASK", "NOT_COMPLETE", "TIMEOUT", "UNKNOWN_RESULT", "LOW_BATTERY", "RESET", "UNSUPPORTED")
MODE = ("IDLE", "READY", "AUTO", "HOLD", "LANDING", "FAILSAFE_HOLD", "FAILSAFE_LAND", "FAULT", "MANUAL")
CONTROL_FIELDS = {
    "HELLO": {"client_nonce", "dialect_hash"},
    "PERMIT": {"challenge", "permit_id", "profile_id", "calibration_id", "reset_counter", "valid_ms", "scope", "flags"},
    "ACQUIRE": {"challenge", "permit_id", "valid_ms"},
    "RENEW": {"challenge", "lease_id", "valid_ms"}, "RELEASE": {"lease_id"},
    "QUERY_RESULT": {"query_id"}, "CONFIRM_RESULT": {"query_id"}, "QUERY_PROFILE": set(),
}
ACTION_FIELDS = {
    "BEGIN": {"duration_ms"}, "TAKEOFF": {"z_mm", "duration_ms", "settle_ms"},
    "POSITION": {"x_mm", "y_mm", "z_mm", "duration_ms", "settle_ms"},
    "VELOCITY": {"vx_mm_s", "vy_mm_s", "z_mm", "duration_ms"},
    "HOLD": {"duration_ms", "settle_ms"}, "LAND": {"duration_ms"},
    "CANCEL": set(), "END": {"outcome"}, "FAILSAFE": set(), "DISARM": set(),
}


@dataclass
class Sensors:
    x_mm: int = 0
    y_mm: int = 0
    z_mm: int = 0
    attitude_valid: bool = False
    position_valid: bool = False
    height_valid: bool = False
    attitude_ms: int | None = None
    position_ms: int | None = None
    height_ms: int | None = None
    landed: bool = False
    landed_valid: bool = False
    landed_ms: int | None = None
    armed: bool = False
    battery_mv: int | None = None
    battery_ms: int | None = None
    preflight: bool = False
    reset_counter: int = 1
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    q_frd: tuple | None = None
    velocity_frd: tuple | None = None


class Rejected(Exception):
    pass


class FCModel:
    def __init__(self, profile, *, boot_id=1, boot_storage_verified=False, system_id=1,
                 enable_nrf=False, enable_airborne_reacquire=False):
        if not 0 < boot_id <= 0xFFFFFFFF:
            raise ValueError("Persistent non-repeating u32 boot generation required")
        self.profile = dict(profile)
        self.boot = boot_id
        self.boot_verified = boot_storage_verified
        self.system_id = system_id
        self.codec = Codec(system_id, 1)
        self.enable_nrf = enable_nrf
        self.enable_reacquire = enable_airborne_reacquire
        self.sensors = Sensors(reset_counter=profile.get("reset_counter") or 0)
        self.now = 0
        self.mode = "IDLE"
        self.reason = "OK"
        self.manual_latched = False
        self.session = self.session_counter = self.lease = self.lease_counter = 0
        self.nonce = 0
        self.source = None
        self.route = 0
        self.owner = False
        self.takeoff_used = False
        self.lease_deadline = self.task_deadline = 0
        self.highwater = self.high_task = self.task = 0
        self.sample_id = self.challenge_counter = self.event_id = 0
        self.challenges = OrderedDict()
        self.cache = OrderedDict()
        self.events = deque(maxlen=8)
        self.used_permits = set()
        self.permit = None
        self.active = None
        self.task_request = None
        self.last_action = None
        self.hold_deadline = None
        self.hello_key = None
        self.hello_result = None
        self._out = []

    def _require(self, condition, reason):
        if not condition:
            raise Rejected(reason)

    def _age(self, time):
        return self.now - time if time is not None and 0 <= time <= self.now else 0xFFFFFFFF

    def _valid(self, name):
        limit = self.profile.get("state_max_age_ms") or 0
        return bool(getattr(self.sensors, name + "_valid")) and self._age(getattr(self.sensors, name + "_ms")) < limit

    def _sensors_ok(self):
        return all(self._valid(name) for name in ("attitude", "position", "height"))

    def _battery_valid(self):
        value = self.sensors.battery_mv
        return value is not None and 0 < value < 65535 and self._age(self.sensors.battery_ms) < (self.profile.get("state_max_age_ms") or 0)

    def _low_battery(self):
        threshold = self.profile.get("low_voltage_mv") or 0
        return bool(threshold and self._battery_valid() and self.sensors.battery_mv <= threshold)

    def _config_ok(self):
        p = self.profile
        positive = ("profile_id", "calibration_id", "max_xy_mm_s", "max_z_mm_s", "xy_tolerance_mm", "z_tolerance_mm",
                    "settle_min_ms", "state_period_ms", "fresh_window_ms", "lease_ms", "goal_max_ms", "action_max_ms",
                    "state_max_age_ms", "task_max_ms", "land_speed_mm_s", "ack_timeout_ms", "event_repeat_ms")
        return (self.boot_verified and p.get("config_valid") == 1 and type(p.get("validated_mask")) is int
                and 0 <= p["validated_mask"] <= 15 and p["validated_mask"] & 1
                and all(type(p.get(key)) is int and 0 < p[key] < 0x80000000 for key in positive)
                and all(type(p.get(key)) is int and -0x80000000 <= p[key] <= 0x7FFFFFFF for key in ("xmin_mm", "xmax_mm", "ymin_mm", "ymax_mm", "zmin_mm", "zmax_mm"))
                and p["xmin_mm"] < p["xmax_mm"] and p["ymin_mm"] < p["ymax_mm"]
                and 0 <= p["zmin_mm"] < p["zmax_mm"]
                and p["fresh_window_ms"] <= 8 * p["state_period_ms"]
                and 0 < p["land_speed_mm_s"] <= p["max_z_mm_s"]
                and p.get("failsafe_policy") in (1, 2)
                and type(p.get("hold_before_land_ms")) is int and 0 <= p["hold_before_land_ms"] < 0x80000000)

    def _clear_challenges(self):
        self.challenges.clear()

    def _challenge(self):
        period = self.profile.get("state_period_ms") or 100
        if not self.challenges or self.now - next(reversed(self.challenges.values())) >= period:
            self.challenge_counter += 1
            if self.challenge_counter > 0xFFFFFFFF:
                raise RuntimeError("Challenge counter exhausted; require a new verified boot")
            self.challenges[self.challenge_counter] = self.now
            while len(self.challenges) > 8:
                self.challenges.popitem(last=False)
        return next(reversed(self.challenges))

    def _fresh(self, req):
        self._require(0 < req.valid_ms <= (self.profile.get("fresh_window_ms") or 0), "FRESHNESS")
        issued = self.challenges.get(req.challenge)
        self._require(issued is not None and 0 <= self.now - issued < req.valid_ms, "FRESHNESS")
        return issued

    def _result(self, req, phase="ACCEPTED", reason="OK", *, final=False):
        if final:
            self.event_id += 1
            if self.event_id > 0xFFFFFFFF:
                raise RuntimeError("Event counter exhausted")
        result = {"boot_id": self.boot, "session_id": self.session, "client_nonce": req.client_nonce if req.get_msgId() == 60000 and req.op == 1 else 0,
                  "request_id": req.request_id, "request_msg": req.get_msgId(), "task_id": getattr(req, "task_id", 0),
                  "event_id": self.event_id if final else 0, "lease_id": self.lease,
                  "reason": REASON.index(reason), "phase": PHASE.index(phase), "progress": 100 if phase == "COMPLETED" else 255,
                  "echo_op": req.op, "target_system": req.get_srcSystem(), "target_component": req.get_srcComponent()}
        self._out.append(("LF2S_RESULT", result))
        if final:
            self.events.append(result.copy())
            if req.request_id in self.cache:
                key, _ = self.cache[req.request_id]
                self.cache[req.request_id] = key, result.copy()
        return result

    def _remember(self, req, result):
        self.cache[req.request_id] = business_key(req), result.copy()
        self.highwater = max(self.highwater, req.request_id)
        while len(self.cache) > 16:
            self.cache.popitem(last=False)

    def state(self):
        self.sample_id += 1
        if self.sample_id > 0xFFFFFFFF:
            raise RuntimeError("State counter exhausted")
        s, p = self.sensors, self.profile
        flags = sum((1 << index) for index, name in enumerate(("attitude", "position", "height", "landed")) if self._valid(name))
        flags |= 16 if self._battery_valid() else 0
        flags |= 32 if s.preflight and self._sensors_ok() else 0
        return {"boot_id": self.boot, "session_id": self.session, "client_nonce": self.nonce,
                "dialect_hash": DIALECT_HASH, "sample_id": self.sample_id, "challenge": self._challenge(),
                "challenge_window_ms": p.get("fresh_window_ms") or 0, "lease_id": self.lease,
                "lease_remaining_ms": max(0, self.lease_deadline - self.now) if self.owner else 0,
                "profile_id": p.get("profile_id") or 0, "calibration_id": p.get("calibration_id") or 0,
                "reset_counter": s.reset_counter, "active_task": self.task,
                "active_request": self.active["req"].request_id if self.active else 0,
                "event_highwater": self.event_id, "attitude_age_ms": self._age(s.attitude_ms),
                "position_age_ms": self._age(s.position_ms), "height_age_ms": self._age(s.height_ms),
                "capabilities": 7 | (8 if self.enable_nrf else 0) | (16 if self.enable_reacquire else 0),
                "battery_mv": s.battery_mv if self._battery_valid() else 65535, "valid_flags": flags,
                "owner_system": self.source[0] if self.owner else 0, "owner_component": self.source[1] if self.owner else 0,
                "route": self.route if self.owner else 3 if self.manual_latched else 0,
                "mode": MODE.index(self.mode), "failsafe_reason": REASON.index(self.reason), "manual_latched": int(self.manual_latched)}

    def _drain(self, state=True):
        if state:
            self._out.append(("LF2S_CONTROL_STATE", self.state()))
        result = [self.codec.encode(name, **fields) for name, fields in self._out]
        self._out.clear()
        return result

    def _can_land(self):
        mask = self.profile.get("validated_mask") or 0
        return (self._config_ok() and bool(mask & 1) and self._valid("attitude") and (self._valid("position") or bool(mask & 2))
                and (self._valid("height") or bool(mask & 4)) and (not self._low_battery() or bool(mask & 8)))

    def _finish_active(self, phase, reason="OK"):
        if self.active and not self.active.get("completed"):
            req = self.active["req"]
            self._result(req, phase, reason, final=True)
            self.last_action = (ACTION[req.op], phase)
            if phase != "COMPLETED" or ACTION[req.op] in ("LAND", "DISARM"):
                self.active = None
            else:
                self.active["completed"] = True

    def _failsafe(self, reason, phase="FAILED"):
        self._finish_active(phase, reason)
        if self.task_request:
            self._result(self.task_request, "CANCELLED" if phase == "CANCELLED" else "FAILED", reason, final=True)
        self.task_request = None
        self.active = None
        self.task = 0
        self.owner = False
        self.lease = 0
        self.permit = None
        self.reason = reason
        self._clear_challenges()
        if self.manual_latched:
            self.mode = "MANUAL"
            self.hold_deadline = None
        elif self._valid("landed") and self.sensors.landed and not self.sensors.armed:
            self.mode = "IDLE"
            self.hold_deadline = None
        elif self._config_ok() and self._sensors_ok() and not self._low_battery() and self.profile.get("failsafe_policy") == 1:
            self.mode = "FAILSAFE_HOLD"
            self.hold_deadline = self.now + self.profile["hold_before_land_ms"]
        else:
            self.mode = "FAILSAFE_LAND" if self._can_land() else "FAULT"
            self.hold_deadline = None

    def _advance(self, now):
        if now < self.now:
            raise ValueError("Model clock cannot go backwards")
        self.now = now
        if self.owner:
            reason = ("CONFIG" if not self._config_ok() else "SENSOR" if not self._sensors_ok()
                      else "LOW_BATTERY" if self._low_battery() else "TIMEOUT" if now >= self.lease_deadline
                      or (self.task and now >= self.task_deadline) or (self.active and now >= self.active["deadline"]) else None)
            if reason:
                self._failsafe(reason)
            elif self.sensors.armed:
                p, s = self.profile, self.sensors
                if not (p["xmin_mm"] <= s.x_mm <= p["xmax_mm"] and p["ymin_mm"] <= s.y_mm <= p["ymax_mm"] and 0 <= s.z_mm <= p["zmax_mm"]):
                    self._failsafe("LIMIT")
        if self.mode == "FAILSAFE_HOLD" and (not self._sensors_ok() or self._low_battery() or self.now >= self.hold_deadline):
            self.mode = "FAILSAFE_LAND" if self._can_land() else "FAULT"
            self.hold_deadline = None
        if self.mode == "FAILSAFE_LAND" and self._valid("landed") and self.sensors.landed and not self.sensors.armed:
            self.mode = "IDLE"

    def tick(self, now):
        self._advance(now)
        return self._drain()

    def update_sensors(self, sensors, now):
        old_reset = self.sensors.reset_counter
        self.sensors = sensors
        self._advance(now)
        if self.owner and sensors.reset_counter != old_reset:
            self._failsafe("RESET")
        active = self.active
        if active and not active.get("completed"):
            op = ACTION[active["req"].op]
            s = self.sensors
            if op in ("LAND", "DISARM"):
                if self._valid("landed") and s.landed and not s.armed:
                    self._finish_active("COMPLETED")
                    self.mode = "READY" if self.owner else "IDLE"
            elif op in ("TAKEOFF", "POSITION", "HOLD") and self._sensors_ok():
                x, y, z = active["goal"]
                inside = (math.hypot(s.x_mm - x, s.y_mm - y) <= self.profile["xy_tolerance_mm"]
                          and abs(s.z_mm - z) <= self.profile["z_tolerance_mm"] and s.armed and not s.landed)
                stamp = min(s.attitude_ms, s.position_ms, s.height_ms)
                if not inside:
                    active["stable"] = None
                elif stamp > active.get("last_sample", -1):
                    active["last_sample"] = stamp
                    if active["stable"] is None:
                        active["stable"] = stamp
                    if stamp - active["stable"] >= active["req"].settle_ms:
                        self._finish_active("COMPLETED")
                        self.mode = "HOLD"
        return self._drain()

    def manual_takeover(self, now):
        if now < self.now:
            raise ValueError("Model clock cannot go backwards")
        self.now = now
        self.manual_latched = True
        self._failsafe("MANUAL", "CANCELLED")
        return self._drain()

    def manual_released_locally(self):
        """Only call from the validated physical/manual-release condition, never a packet."""
        self.manual_latched = False
        self.mode = "IDLE" if self._valid("landed") and self.sensors.landed else "MANUAL"

    def handle(self, frame, route, now):
        req = decode_datagram(frame)
        self._advance(now)
        if req.get_msgId() == 0:
            return self._drain()  # heartbeat is visibility, never a lease renewal
        if req.get_msgId() == 76:
            return self._standard_request(req)
        if req.get_msgId() not in (60000, 60001):
            return self._drain()
        try:
            self._require((req.target_system, req.target_component) == (self.system_id, 1), "WRONG_TARGET")
            self._require(route == 1 or (route == 2 and self.enable_nrf), "UNSUPPORTED")
            self._require(req.request_id != 0, "BAD_FIELD")
            self._require(req.get_srcSystem() != 0 and req.get_srcComponent() != 0, "BAD_FIELD")
            names = CONTROL if req.get_msgId() == 60000 else ACTION
            self._require(0 < req.op < len(names), "UNSUPPORTED")
            op = names[req.op]
            base = {"boot_id", "session_id", "request_id", "target_system", "target_component", "op"}
            used = CONTROL_FIELDS[op] if req.get_msgId() == 60000 else ACTION_FIELDS[op] | {"lease_id", "task_id", "reset_counter", "challenge", "valid_ms"}
            self._require(all(not getattr(req, name) for name in req.fieldnames if name not in base | used), "BAD_FIELD")
            if req.get_msgId() == 60000 and op == "HELLO":
                self._hello(req, route)
                return self._drain()
            self._require(req.boot_id == self.boot and req.session_id == self.session and self.session != 0
                          and self.source == (req.get_srcSystem(), req.get_srcComponent()) and self.route == route, "BAD_SESSION")
            old = self.cache.get(req.request_id)
            if old:
                self._require(old[0] == business_key(req), "DUPLICATE_CONFLICT")
                self._out.append(("LF2S_RESULT", old[1].copy()))
                return self._drain()
            safety = (req.get_msgId() == 60000 and op == "RELEASE") or (req.get_msgId() == 60001 and op in ("CANCEL", "FAILSAFE"))
            self._require(req.request_id > self.highwater or safety, "STALE")
            if req.get_msgId() == 60000:
                result = self._control(req, op)
            else:
                result = self._action(req, op)
            self._remember(req, result)
        except Rejected as exc:
            result = self._result(req, "REJECTED", str(exc))
            # A conflicting retry must never replace the original cached identity.
            if (req.boot_id == self.boot and req.session_id == self.session and self.source == (req.get_srcSystem(), req.get_srcComponent())
                    and self.route == route and req.request_id > self.highwater
                    and not (req.get_msgId() == 60000 and req.op == 1)):
                self._remember(req, result)
        return self._drain()

    def _hello(self, req, route):
        self._require(req.boot_id == 0 and req.session_id == 0 and req.client_nonce != 0, "BAD_FIELD")
        self._require(req.dialect_hash == DIALECT_HASH, "SCHEMA")
        key = route, req.get_srcSystem(), req.get_srcComponent(), business_key(req)
        if self.nonce == req.client_nonce and self.hello_key:
            self._require(key == self.hello_key, "DUPLICATE_CONFLICT")
            self._out.append(("LF2S_RESULT", self.hello_result.copy()))
            return
        airborne = self.sensors.armed or (self._valid("landed") and not self.sensors.landed) or self.mode in ("AUTO", "HOLD", "LANDING", "FAILSAFE_HOLD", "FAILSAFE_LAND", "FAULT")
        self._require(not self.owner and (not airborne or (self.enable_reacquire and self.mode == "FAILSAFE_HOLD" and self._sensors_ok())), "BUSY")
        self.session_counter += 1
        self._require(self.session_counter <= 0xFFFFFFFF, "CONFIG")
        self.session = (self.boot << 32) | self.session_counter
        self.nonce = req.client_nonce
        self.route = route
        self.source = req.get_srcSystem(), req.get_srcComponent()
        self.highwater = self.high_task = self.task = 0
        self.cache.clear()
        self.used_permits.clear()
        self.permit = None
        self.last_action = None
        self._clear_challenges()
        self.hello_key = key
        self.hello_result = self._result(req)
        self._remember(req, self.hello_result)

    def _control(self, req, op):
        if op in ("PERMIT", "ACQUIRE", "RENEW"):
            issued = self._fresh(req)
        if op == "PERMIT":
            self._require(self._config_ok(), "CONFIG")
            self._require(not self.manual_latched, "MANUAL")
            self._require(not self.owner, "BUSY")
            self._require(req.flags == 1 and req.permit_id and req.permit_id not in self.used_permits and req.scope in (1, 2), "NO_PERMIT")
            self._require((req.profile_id, req.calibration_id, req.reset_counter) == (self.profile["profile_id"], self.profile["calibration_id"], self.sensors.reset_counter), "CONFIG")
            ground = self._valid("landed") and self.sensors.landed and not self.sensors.armed
            self._require((req.scope == 1 and ground) or (req.scope == 2 and self.enable_reacquire and self.mode == "FAILSAFE_HOLD" and self._sensors_ok() and not self._low_battery()), "NO_PERMIT")
            # One session holds at most 16 explicitly confirmed permits; reconnect on exhaustion.
            self._require(len(self.used_permits) < 16, "LIMIT")
            self.permit = req.permit_id, req.scope, issued + req.valid_ms
            self.used_permits.add(req.permit_id)
        elif op == "ACQUIRE":
            self._require(self._config_ok(), "CONFIG")
            self._require(not self.manual_latched, "MANUAL")
            self._require(not self.owner and self.permit and self.permit[0] == req.permit_id and self.now < self.permit[2], "NO_PERMIT")
            self._require(self._sensors_ok() and not self._low_battery(), "SENSOR")
            scope = self.permit[1]
            ground = self._valid("landed") and self.sensors.landed and not self.sensors.armed
            self._require((scope == 1 and ground and self.sensors.preflight) or (scope == 2 and self.mode == "FAILSAFE_HOLD" and self.enable_reacquire), "NO_PERMIT")
            self.lease_counter += 1
            self._require(self.lease_counter <= 0xFFFFFFFF, "CONFIG")
            self.lease = self.lease_counter
            self.owner = True
            self.takeoff_used = scope == 2
            self.lease_deadline = self.now + self.profile["lease_ms"]
            self.permit = None
            self.mode = "READY" if scope == 1 else "HOLD"
            self.reason = "OK"
            self.hold_deadline = None
            self._clear_challenges()
        elif op == "RENEW":
            self._require(self.owner and req.lease_id == self.lease, "NO_AUTHORITY")
            self.lease_deadline = self.now + self.profile["lease_ms"]
        elif op == "RELEASE":
            self._require(self.owner and req.lease_id == self.lease, "NO_AUTHORITY")
            self._failsafe("OK", "CANCELLED")
        elif op == "QUERY_RESULT":
            record = self.cache.get(req.query_id)
            found = record[1] if record else next((event for event in reversed(self.events) if event["session_id"] == self.session and event["request_id"] == req.query_id), None)
            self._require(found is not None, "UNKNOWN_RESULT")
            self._out.append(("LF2S_RESULT", found.copy()))
        elif op == "CONFIRM_RESULT":
            self._require(0 < req.query_id <= self.event_id, "BAD_FIELD")
            self.events = deque((event for event in self.events if event["event_id"] > req.query_id), maxlen=8)
        elif op == "QUERY_PROFILE":
            fields = {key: value or 0 for key, value in self.profile.items()}
            fields.update(boot_id=self.boot, reset_counter=self.sensors.reset_counter)
            self._out.append(("LF2S_PROFILE", fields))
        return self._result(req)

    def _point_inside(self, x, y, z):
        p = self.profile
        return p["xmin_mm"] <= x <= p["xmax_mm"] and p["ymin_mm"] <= y <= p["ymax_mm"] and p["zmin_mm"] <= z <= p["zmax_mm"]

    def _action(self, req, op):
        self._require(self.owner and req.lease_id == self.lease, "NO_AUTHORITY")
        if op in ("CANCEL", "FAILSAFE"):
            self._require(req.challenge == 0 and req.valid_ms == 0, "BAD_FIELD")
            self._require(req.task_id == self.task and (self.task != 0 or op == "FAILSAFE"), "TASK")
            self._failsafe("OK", "CANCELLED")
            return self._result(req, "COMPLETED", final=True)
        issued = self._fresh(req)
        self._require(req.reset_counter == self.sensors.reset_counter, "RESET")
        self._require(self._sensors_ok(), "SENSOR")
        if op == "BEGIN":
            self._require(not self.task and req.task_id > self.high_task and req.task_id != 0, "TASK")
            self._require(0 < req.duration_ms <= self.profile["task_max_ms"], "LIMIT")
            self.task = self.high_task = req.task_id
            self.task_request = req
            self.task_deadline = self.now + req.duration_ms
            self.last_action = None
            self._clear_challenges()
            return self._result(req)
        self._require(req.task_id == self.task and self.task != 0, "TASK")
        if op == "END":
            self._require(req.outcome in (0, 1), "BAD_FIELD")
            self._require(req.outcome == 1 or self.last_action in (("HOLD", "COMPLETED"), ("LAND", "COMPLETED")), "NOT_COMPLETE")
            self.task = 0
            if req.outcome:
                self._failsafe("TASK")
            else:
                if self.task_request:
                    self._result(self.task_request, "COMPLETED", final=True)
                self.task_request = None
            return self._result(req, "FAILED" if req.outcome else "COMPLETED", "TASK" if req.outcome else "OK", final=True)
        self._require(self.active is None or self.active["req"].op not in (2, 6) or self.active.get("completed"), "BUSY")
        s, p = self.sensors, self.profile
        goal = (s.x_mm, s.y_mm, s.z_mm)
        if op == "DISARM":
            self._require(self._valid("landed") and s.landed, "SENSOR")
        else:
            self._require(0 < req.duration_ms <= p["goal_max_ms" if op in ("POSITION", "VELOCITY") else "action_max_ms"], "LIMIT")
            if op in ("TAKEOFF", "POSITION", "HOLD"):
                self._require(p["settle_min_ms"] <= req.settle_ms < req.duration_ms, "LIMIT")
            if op == "TAKEOFF":
                self._require(self.mode == "READY" and self._valid("landed") and s.landed and not s.armed and s.preflight, "TASK")
                self._require(not self.takeoff_used, "NO_PERMIT")
                goal = s.x_mm, s.y_mm, req.z_mm
                self._require(self._point_inside(*goal), "LIMIT")
            else:
                self._require(self.mode in ("AUTO", "HOLD") and s.armed and not s.landed, "TASK")
                if op == "POSITION":
                    goal = req.x_mm, req.y_mm, req.z_mm
                    self._require(self._point_inside(*goal), "LIMIT")
                elif op == "VELOCITY":
                    self._require(math.hypot(req.vx_mm_s, req.vy_mm_s) <= p["max_xy_mm_s"], "LIMIT")
                    goal = s.x_mm + req.vx_mm_s * req.duration_ms / 1000, s.y_mm + req.vy_mm_s * req.duration_ms / 1000, req.z_mm
                    self._require(self._point_inside(s.x_mm, s.y_mm, req.z_mm) and self._point_inside(*goal), "LIMIT")
                elif op == "HOLD":
                    self._require(self._point_inside(*goal), "LIMIT")
        if self.active and not self.active.get("completed"):
            self._finish_active("SUPERSEDED")
        self.last_action = None
        # Streaming targets expire against the FC challenge clock as well as execution TTL.
        deadline = min(self.now + req.duration_ms, self.task_deadline)
        if op in ("POSITION", "VELOCITY"):
            deadline = min(deadline, issued + req.valid_ms)
        if op == "DISARM":
            deadline = min(self.now + p["action_max_ms"], self.task_deadline)
        self.active = {"req": req, "goal": goal, "deadline": deadline, "stable": None, "completed": False}
        if op == "TAKEOFF":
            self.takeoff_used = True
        self.mode = "LANDING" if op == "LAND" else "AUTO"
        return self._result(req, "IN_PROGRESS")

    def repeat_events(self):
        """Caller schedules this at event_repeat_ms; repeats cannot execute an action."""
        self._out.extend(("LF2S_RESULT", event.copy()) for event in self.events)
        return self._drain()

    def _standard_request(self, req):
        from . import dialect as mav

        accepted = False
        if (req.target_system, req.target_component) == (self.system_id, 1) and req.command == mav.MAV_CMD_REQUEST_MESSAGE:
            # This reference only serves the safe read-only heartbeat request.
            accepted = req.param1 == 0 and all(getattr(req, f"param{i}") == 0 for i in range(2, 7)) and req.param7 in (0, 1, 2)
        if accepted:
            self._out.append(("HEARTBEAT", {"type": mav.MAV_TYPE_QUADROTOR, "autopilot": mav.MAV_AUTOPILOT_GENERIC,
                                             "base_mode": mav.MAV_MODE_FLAG_SAFETY_ARMED if self.sensors.armed else 0,
                                             "system_status": mav.MAV_STATE_STANDBY, "mavlink_version": 3}))
        self._out.append(("COMMAND_ACK", {"command": req.command, "result": mav.MAV_RESULT_ACCEPTED if accepted else mav.MAV_RESULT_UNSUPPORTED,
                                           "progress": 255, "target_system": req.get_srcSystem(), "target_component": req.get_srcComponent()}))
        return self._drain()
