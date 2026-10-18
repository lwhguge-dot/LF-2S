"""Request identity, lifecycle and receipt-clock observations; no control qualification."""

import asyncio
import copy
import re
import secrets
from dataclasses import dataclass
from itertools import pairwise

from .contracts import (
    Frame,
    ObservationError,
    currency_summary,
    integer,
    intersection,
    parse_response,
)


class SignalHistory:
    def __init__(self, policy):
        self.policy = policy
        self.samples = []

    def clear(self):
        self.samples.clear()

    def observe(self, signals, received_ms):
        if len(signals) != 1 or signals[0]["light"] not in ("green", "off"):
            self.clear()
            return {
                "evidence": "non_green"
                if len(signals) == 1 and signals[0]["light"] in ("red", "yellow")
                else "unknown_or_multiple_groups",
                "samples": 0,
                "span_ms": 0,
                "green_off_transitions": 0,
            }
        current = signals[0]
        if self.samples:
            prior_time, prior_box, _ = self.samples[-1]
            overlap = intersection(prior_box, current["box"])
            def area(box):
                return (box[2] - box[0]) * (box[3] - box[1])

            union = area(prior_box) + area(current["box"]) - overlap
            if (
                received_ms - prior_time > self.policy.max_sample_gap_ms
                or overlap * 1000000 < union * self.policy.signal_match_iou_millionths
            ):
                self.clear()
        self.samples.append((received_ms, current["box"], current["light"]))
        # Keep one sample before the configured window, and at most 128 samples.
        while (
            len(self.samples) > 2
            and self.samples[1][0] < received_ms - self.policy.green_run_ms
        ):
            self.samples.pop(0)
        self.samples = self.samples[-128:]
        lights = [row[2] for row in self.samples]
        transitions = sum(a != b for a, b in pairwise(lights))
        span = received_ms - self.samples[0][0]
        if transitions >= 2:
            evidence = "green_off_alternation"
        elif (
            len(lights) >= 2
            and set(lights) == {"green"}
            and span >= self.policy.green_run_ms
        ):
            evidence = "sampled_green_run"
        else:
            evidence = "insufficient_temporal_evidence"
        return {
            "evidence": evidence,
            "samples": len(lights),
            "span_ms": span,
            "green_off_transitions": transitions,
        }


@dataclass(frozen=True)
class Ticket:
    request_id: str
    frame: Frame
    started_ms: int
    image_info: dict


class Pipeline:
    def __init__(self, policy):
        self.policy = policy
        self.history = SignalHistory(policy)
        self.pending = None
        self.context = None
        self.retired = set()
        self.last_frame = self.last_receipt = self.last_capture = None
        self.clock_id = self.last_now = None
        self.clock_fault = False
        self.closed = False
        self.current = self.empty("not_started")

    def empty(self, reason, frame=None):
        return {
            "schema": "LF2S.DemoObservation/0.1-dev",
            "phase": "unavailable",
            "reason": reason,
            "frame": frame.provenance() if frame else None,
            "request_id": None,
            "policy_sha256": self.policy.fingerprint,
            "model": "qwen38",
            "thinking_enabled": False,
            "signals": [],
            "current_light": None,
            "temporal": None,
            "zebra_visible": None,
            "checkout_visible": None,
            "currency": None,
            "receipt_age_ms": None,
            "capture_age_ms": None,
            "signal_controls_crossing_verified": False,
            "flashing_green": None,
            "physical_frame_age_qualified": False,
            "physical_accuracy_verified": False,
            "hardware_control_allowed": False,
            "permission_granted": False,
            "crossing_allowed": False,
            "configuration_signed": False,
        }

    def invalidate(self, reason, frame=None):
        self.pending = None
        self.history.clear()
        self.current = self.empty(reason, frame)

    def time(self, now_ms, clock_id):
        integer(now_ms, 0, 2**63 - 1, "invalid_now")
        if self.clock_fault:
            raise ObservationError("clock_fault_latched")
        if self.clock_id is not None and self.clock_id != clock_id:
            raise ObservationError("clock_mismatch")
        if self.last_now is not None and now_ms < self.last_now:
            self.clock_fault = True
            raise ObservationError("clock_reversed")
        self.clock_id, self.last_now = clock_id, now_ms

    def begin(self, frame, now_ms, clock_id):
        try:
            if not isinstance(frame, Frame):
                raise ObservationError("invalid_frame")
            if self.closed:
                raise ObservationError("closed")
            self.time(now_ms, clock_id)
            if self.pending:
                raise ObservationError("busy_frame_dropped")
            if clock_id != frame.clock_id:
                raise ObservationError("clock_mismatch")
            if not 0 <= now_ms - frame.received_ms <= self.policy.max_receipt_age_ms:
                raise ObservationError("frame_expired_or_future")
            if frame.context in self.retired:
                raise ObservationError("retired_context")
            if frame.context != self.context:
                if len(self.retired) >= 32:
                    raise ObservationError("context_limit")
                if self.context is not None:
                    self.retired.add(self.context)
                self.context = frame.context
                self.last_frame = self.last_receipt = self.last_capture = None
                self.history.clear()
            if self.last_frame is not None and (
                int(frame.frame_id) <= self.last_frame
                or frame.received_ms <= self.last_receipt
            ):
                raise ObservationError("frame_replay_or_reorder")
            capture = (
                int(frame.source_capture_us)
                if frame.source_capture_us is not None
                else None
            )
            if self.last_frame is not None and (
                (capture is None) != (self.last_capture is None)
                or capture is not None
                and capture <= self.last_capture
            ):
                raise ObservationError("capture_replay_or_metadata_changed")
            # Consume identity even if the image or model later fails.
            self.last_frame, self.last_receipt = int(frame.frame_id), frame.received_ms
            self.last_capture = capture
            ticket = Ticket(secrets.token_hex(12), frame, now_ms, frame.image_info())
            self.pending = ticket
            self.current = self.empty("pending", frame) | {
                "phase": "pending",
                "request_id": ticket.request_id,
            }
            return ticket
        except ObservationError as error:
            self.invalidate(str(error), frame if isinstance(frame, Frame) else None)
            raise

    def complete(self, ticket, raw, now_ms, clock_id):
        if self.pending is not ticket or self.closed:
            # A retired request cannot erase a newer observation.
            return self.empty("discarded_request", ticket.frame)
        try:
            self.time(now_ms, clock_id)
            if now_ms - ticket.started_ms > self.policy.request_timeout_ms:
                raise ObservationError("request_timeout")
            age = now_ms - ticket.frame.received_ms
            if not 0 <= age <= self.policy.max_receipt_age_ms:
                raise ObservationError("frame_expired_or_future")
            observation = parse_response(raw, self.policy)
            signals = observation["signals"]
            self.current = self.empty("model_observation", ticket.frame) | {
                "phase": "ready",
                "request_id": ticket.request_id,
                "receipt_age_ms": age,
                "model_elapsed_ms": now_ms - ticket.started_ms,
                "signals": signals,
                "current_light": signals[0]["light"] if len(signals) == 1 else None,
                "temporal": self.history.observe(signals, ticket.frame.received_ms),
                "zebra_visible": observation["zebra_visible"],
                "checkout_visible": observation["checkout_visible"],
                "currency": currency_summary(observation["currency"]),
                "reported_currency": observation["currency"],
                "image_info": ticket.image_info,
            }
            self.pending = None
        except ObservationError as error:
            self.invalidate(str(error), ticket.frame)
        return copy.deepcopy(self.current)

    def snapshot(self, now_ms, clock_id):
        try:
            self.time(now_ms, clock_id)
            if (
                self.pending
                and now_ms - self.pending.started_ms > self.policy.request_timeout_ms
            ):
                raise ObservationError("request_timeout")
            if self.current["phase"] == "ready":
                age = now_ms - self.current["frame"]["received_ms"]
                if age > self.policy.max_receipt_age_ms:
                    raise ObservationError("frame_expired")
                self.current["receipt_age_ms"] = age
        except ObservationError as error:
            self.invalidate(str(error))
        return copy.deepcopy(self.current)

    def reset_clock(self):
        # Explicit lifecycle operation; old stream contexts remain retired.
        if self.context is not None:
            self.retired.add(self.context)
        self.context = None
        self.last_frame = self.last_receipt = self.last_capture = self.clock_id = (
            self.last_now
        ) = None
        self.clock_fault = False
        self.invalidate("explicit_reset")

    def close(self):
        self.closed = True
        self.invalidate("closed")

    async def analyze(self, frame, provider, clock):
        ticket = self.begin(frame, clock(), frame.clock_id)
        try:
            raw = await provider.request(frame, ticket.image_info)
        except asyncio.CancelledError:
            if self.pending is ticket:
                self.invalidate("cancelled", frame)
            raise
        except (ObservationError, TimeoutError) as error:
            if self.pending is ticket:
                self.invalidate("provider_failed", frame)
                code = str(error)
                if code in {
                    "model_timeout",
                    "model_connect",
                    "model_transport",
                    "response_encoding",
                    "response_size",
                } or re.fullmatch(r"model_http_status_[1-5][0-9]{2}", code):
                    self.current["provider_error"] = code
                return copy.deepcopy(self.current)
            return self.empty("discarded_request", frame)
        return self.complete(ticket, raw, clock(), frame.clock_id)
