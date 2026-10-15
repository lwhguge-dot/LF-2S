"""Small PC request tracker; retries preserve business identity and bytes."""

import secrets

from .model import ACTION, CONTROL, PHASE
from .version import DIALECT_HASH
from .wire import Codec, ProtocolError, decode_datagram


class Client:
    def __init__(self, vehicle=1, system=255, component=190, *, max_retries=2, ack_timeout_ms=200):
        self.vehicle = vehicle
        self.codec = Codec(system, component)
        self.system, self.component = system, component
        self.max_retries = max_retries
        self.ack_timeout_ms = ack_timeout_ms
        self.request_id = 0
        self.boot = self.session = self.lease = self.challenge = self.reset_counter = self.task = 0
        self.nonce = 0
        self.sample = 0
        self.pending = {}
        self.results = {}

    def _request(self, name, fields, now):
        if len(self.pending) >= 4:
            raise ProtocolError("At most four pending requests; do not queue old movement")
        if self.request_id == 0xFFFFFFFF:
            raise ProtocolError("Request counter exhausted; establish a new session")
        self.request_id += 1
        values = {"boot_id": self.boot, "session_id": self.session, "request_id": self.request_id,
                  "target_system": self.vehicle, "target_component": 1}
        values.update(fields)
        raw = self.codec.encode(name, **values)
        self.pending[self.request_id] = {"raw": raw, "created": now, "last": now, "tries": 0,
                                         "valid_ms": fields.get("valid_ms", self.ack_timeout_ms * (1 + self.max_retries))}
        return raw

    def hello(self, now=0, nonce=None):
        self.pending.clear()
        self.results.clear()
        self.boot = self.session = self.lease = self.sample = self.request_id = 0
        self.nonce = nonce or secrets.randbits(64) or 1
        return self._request("LF2S_CONTROL_REQUEST", {"op": 1, "client_nonce": self.nonce, "dialect_hash": DIALECT_HASH}, now)

    def control(self, op, now, **values):
        fields = {"op": CONTROL.index(op)}
        if op in ("PERMIT", "ACQUIRE", "RENEW"):
            fields["challenge"] = self.challenge
        if op in ("RENEW", "RELEASE"):
            fields["lease_id"] = self.lease
        fields.update(values)
        return self._request("LF2S_CONTROL_REQUEST", fields, now)

    def action(self, op, now, **values):
        fields = {"op": ACTION.index(op), "lease_id": self.lease, "task_id": self.task,
                  "reset_counter": self.reset_counter}
        if op not in ("CANCEL", "FAILSAFE"):
            fields["challenge"] = self.challenge
        fields.update(values)
        return self._request("LF2S_ACTION_REQUEST", fields, now)

    def retry(self, request_id, now):
        item = self.pending.get(request_id)
        if item is None:
            raise ProtocolError("Request already answered or unknown")
        if (now < item["last"] or now - item["created"] >= item["valid_ms"]
                or item["tries"] >= self.max_retries):
            self.pending.pop(request_id)
            raise ProtocolError("Retry budget exhausted; query result, do not automatically repeat the action with a new ID")
        if now - item["last"] < self.ack_timeout_ms:
            raise ProtocolError("ACK timer has not expired")
        item["tries"] += 1
        item["last"] = now
        return item["raw"]

    def observe(self, raw):
        message = decode_datagram(raw)
        if (message.get_srcSystem(), message.get_srcComponent()) != (self.vehicle, 1):
            raise ProtocolError("Unexpected FC source")
        if message.get_msgId() not in (60002, 60003):
            return message
        if message.get_msgId() == 60002:
            if (message.target_system, message.target_component) != (self.system, self.component):
                raise ProtocolError("Result addressed to another client")
            hello = message.request_msg == 60000 and message.echo_op == 1 and message.client_nonce == self.nonce
            if hello and message.phase == PHASE.index("ACCEPTED"):
                self.boot, self.session = message.boot_id, message.session_id
            if not hello and (message.boot_id, message.session_id) != (self.boot, self.session):
                raise ProtocolError("Result from another boot/session")
            key = (message.request_msg, message.request_id)
            old = self.results.get(key)
            # A late ACCEPTED/IN_PROGRESS cannot overwrite an already terminal result.
            if old is None or old.phase in (1, 2):
                self.results[key] = message
            elif message.phase not in (1, 2) and (message.phase, message.reason, message.event_id) != (old.phase, old.reason, old.event_id):
                raise ProtocolError("Conflicting terminal result; retain the original and investigate")
            if key[1] in self.pending:
                del self.pending[key[1]]
            while len(self.results) > 32:
                self.results.pop(next(iter(self.results)))
        else:
            if (message.boot_id, message.session_id, message.client_nonce) != (self.boot, self.session, self.nonce):
                raise ProtocolError("State from another boot/session; establish HELLO explicitly")
            if message.dialect_hash != DIALECT_HASH:
                raise ProtocolError("Dialect definition mismatch")
            if message.sample_id > self.sample:
                self.sample = message.sample_id
                self.lease, self.challenge = message.lease_id, message.challenge
                self.reset_counter, self.task = message.reset_counter, message.active_task
        return message
