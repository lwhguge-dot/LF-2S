"""Vendor USB telemetry bench, independent of MAVLink sessions and flight control."""

import asyncio
import copy
import json
import logging
import logging.handlers
import queue
import secrets
import sys
import threading
import time
from collections import Counter, deque

from lf2s_protocol.wire import ProtocolError

from service.zin_hid import StreamParser, WindowsHid, decode

DISPLAY_AGE_MS = (
    1000  # Host receive age for display; not a qualified sensor sampling age.
)
RAW_LIMIT = 8 * 1024 * 1024


class CaptureFile(logging.handlers.RotatingFileHandler):
    failed_records = 0
    last_error = None

    def handleError(self, record):
        self.failed_records += 1
        self.last_error = str(sys.exc_info()[1])


class RawCapture:
    def __init__(self, directory, epoch):
        directory.mkdir(parents=True, exist_ok=True)
        self.epoch, self.queue_dropped = epoch, 0
        self.connection_id = secrets.token_hex(12)
        self.queue = queue.Queue(maxsize=1024)
        self.file = CaptureFile(
            directory / "fc-usb.jsonl",
            maxBytes=RAW_LIMIT,
            backupCount=3,
            encoding="utf-8",
        )
        self.listener = logging.handlers.QueueListener(self.queue, self.file)
        self.listener.start()

    def add(self, **data):
        record = logging.LogRecord(
            "fc-usb",
            logging.INFO,
            "",
            0,
            json.dumps(
                {
                    "connection_id": self.connection_id,
                    "connection_epoch": self.epoch,
                    **data,
                },
                allow_nan=False,
            ),
            (),
            None,
        )
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.queue_dropped += 1

    @property
    def dropped(self):
        return self.queue_dropped + self.file.failed_records

    def close(self):
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.queue_dropped += 1
            except queue.Empty:
                pass
        self.listener.stop()
        self.file.close()


class FcUsbBench:
    def __init__(
        self, journal, directory, *, api_factory=WindowsHid, clock=time.monotonic
    ):
        self.journal, self.directory = journal, directory
        self.api_factory, self.clock = api_factory, clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.epoch = 0
        self.active = False
        self.phase = "disconnected"
        self.reason = None
        self.device = None
        self.latest = {}
        self.history = deque(maxlen=240)
        self.frame_counts = Counter()
        self.counters = Counter()
        self.last_frame_at = None
        self.raw = None
        self.started_at = None
        self.last_history_at = None

    async def connect(self):
        if self.active or (self.thread and self.thread.is_alive()):
            raise ProtocolError("请先断开飞控 USB 台架")
        with self.lock:
            self.epoch += 1
            self.active, self.phase, self.reason = True, "connecting", None
            self.device, self.latest = None, {}
            self.history.clear()
            self.frame_counts.clear()
            self.counters.clear()
            self.last_frame_at = self.last_history_at = None
            self.stop.clear()
            self.ready.clear()
            self.started_at = self.clock()
            try:
                self.raw = RawCapture(self.directory, self.epoch)
            except OSError as error:
                self.phase, self.reason = (
                    "error",
                    "无法创建 USB 原始日志，请检查日志目录",
                )
                raise ProtocolError(self.reason) from error
        self.thread = threading.Thread(
            target=self._read, name="fc-usb-readonly", daemon=True
        )
        self.thread.start()
        if not await asyncio.to_thread(self.ready.wait, 5):
            self.stop.set()
            with self.lock:
                self.phase, self.reason = "error", "USB 设备查询超时；请断开后检查连接"
            raise ProtocolError(self.reason)
        snapshot = self.snapshot()
        if snapshot["phase"] == "error":
            raise ProtocolError(snapshot["reason"])
        self.journal.add(
            "hardware",
            "开始被动读取飞控 USB；未建立 MAVLink 会话",
            simulation=False,
            source="vendor_usb_hid",
            connection_epoch=self.epoch,
        )
        return snapshot

    async def disconnect(self):
        self.stop.set()
        if self.thread and self.thread.is_alive():
            await asyncio.to_thread(self.thread.join, 3)
            if self.thread.is_alive():
                with self.lock:
                    self.phase, self.reason = (
                        "error",
                        "USB 读取尚未退出；请等待或检查设备",
                    )
                raise ProtocolError(self.reason)
        with self.lock:
            was_active = self.active
            self.active, self.phase = False, "disconnected"
            self.reason = None
            self.latest.clear()
            self.history.clear()
            self.last_frame_at = None
        if was_active:
            self.journal.add(
                "hardware",
                "飞控 USB 已断开；不会自动重新连接",
                simulation=False,
                source="vendor_usb_hid",
                connection_epoch=self.epoch,
            )
        return self.snapshot()

    def _read(self):
        api = handle = None
        parser = StreamParser()
        try:
            api = self.api_factory()
            devices = api.enumerate()
            if len(devices) != 1:
                raise ProtocolError("需要且只能连接一块 ZIN_QUAD 飞控；请检查 USB")
            device = devices[0]
            if (device["vid"], device["pid"], device["product"]) != (
                0x0483,
                0xA001,
                "ZIN_QUAD",
            ):
                raise ProtocolError("USB 飞控标识不匹配")
            caps = device["caps"]
            if (caps["UsagePage"], caps["Usage"], caps["InputReportByteLength"]) != (
                0x8C,
                1,
                65,
            ):
                raise ProtocolError("USB 报告能力与已核对格式不同")
            if self.stop.is_set():
                return
            handle = api.open(device["path"], read=True)
            with self.lock:
                self.device = device
            self.raw.add(
                event="connected",
                source="vendor_usb_hid",
                display_only=True,
                device=device,
                wall_time_unix_s=time.time(),
            )
            self.ready.set()
            while not self.stop.is_set():
                try:
                    raw = api.read(handle, 65, 250)
                except OSError:
                    with self.lock:
                        self.counters["read_errors"] += 1
                    raise
                if self.stop.is_set():
                    break
                now = self.clock()
                if raw is None:
                    with self.lock:
                        self.counters["read_timeouts"] += 1
                    continue
                self.raw.add(
                    received_elapsed_ms=(now - self.started_at) * 1000,
                    report_hex=raw.hex(),
                )
                with self.lock:
                    self.counters["reports"] += 1
                    if len(raw) != 65 or raw[0] != 0 or raw[1] > 63:
                        self.counters["report_errors"] += 1
                        continue
                    for frame in parser.feed(raw[2 : 2 + raw[1]]):
                        fields = decode(frame)
                        key = fields["function"]
                        self.frame_counts[key] += 1
                        self.counters["frames"] += 1
                        self.last_frame_at = now
                        self.phase, self.reason = "online", None
                        if key in self.latest or len(self.latest) < 8:
                            self.latest[key] = (now, fields)
                        if "roll_deg" in fields and (
                            self.last_history_at is None
                            or now - self.last_history_at >= 0.1
                        ):
                            self.history.append(
                                {
                                    "elapsed_s": now - self.started_at,
                                    **{
                                        key: fields[key]
                                        for key in ("roll_deg", "pitch_deg", "yaw_deg")
                                    },
                                }
                            )
                            self.last_history_at = now
                    self.counters["checksum_failures"] = parser.checksum_failures
                    self.counters["discarded_stream_bytes"] = parser.discarded_bytes
        except Exception as error:
            with self.lock:
                if not self.stop.is_set():
                    self.phase, self.reason = "error", str(error)
            self.raw.add(event="error", message=str(error))
        finally:
            if handle is not None:
                api.k.CloseHandle(handle)
            with self.lock:
                self.counters["checksum_failures"] = parser.checksum_failures
                self.counters["discarded_stream_bytes"] = parser.discarded_bytes
            self.raw.add(
                event="reader_closed",
                counters=dict(self.counters),
                trailing_stream_hex=parser.buffer.hex(),
            )
            self.raw.close()
            self.ready.set()

    def snapshot(self):
        now = self.clock()
        with self.lock:
            age = (
                None
                if self.last_frame_at is None
                else max(0, int((now - self.last_frame_at) * 1000))
            )
            phase = self.phase
            if (
                self.active
                and phase == "online"
                and age is not None
                and age > DISPLAY_AGE_MS
            ):
                phase = "stale"

            def measurement(function, keys):
                item = self.latest.get(function)
                field_age = (
                    None if item is None else max(0, int((now - item[0]) * 1000))
                )
                available = (
                    self.active
                    and phase == "online"
                    and item is not None
                    and field_age <= DISPLAY_AGE_MS
                    and all(key in item[1] for key in keys)
                )
                return {
                    "available": available,
                    "received_age_ms": field_age,
                    "sample_age_ms": None,
                    "measurement_qualified": False,
                    "value": {key: item[1][key] for key in keys} if available else None,
                }

            attitude = measurement("0x01", ("roll_deg", "pitch_deg", "yaw_deg"))
            status = measurement("0x01", ("status_byte10_raw", "status_byte11_raw"))
            imu = measurement(
                "0x02",
                (
                    "acc_x_raw",
                    "acc_y_raw",
                    "acc_z_raw",
                    "gyro_x_raw",
                    "gyro_y_raw",
                    "gyro_z_raw",
                    "mag_x_raw",
                    "mag_y_raw",
                    "mag_z_raw",
                ),
            )
            rc = measurement("0x03", ("rc_channels_raw",))
            return copy.deepcopy(
                {
                    "active": self.active,
                    "phase": phase,
                    "reason": "USB 接收已过期；数据不再显示为当前值"
                    if phase == "stale"
                    else self.reason,
                    "epoch": self.epoch,
                    "device": self.device,
                    "receive_age_ms": age,
                    "source": "vendor_usb_hid",
                    "display_only": True,
                    "hardware_control_allowed": False,
                    "hardware_freshness_qualified": False,
                    "mavlink_session_established": False,
                    "axis_convention": "vendor_unverified",
                    "attitude": attitude,
                    "imu_raw": imu,
                    "rc_raw": rc,
                    "status_raw": status,
                    "receiver_link_valid": None,
                    "battery_v": None,
                    "height_m": None,
                    "position_m": None,
                    "firmware_version": None,
                    "mcu_model": None,
                    "boot_id": None,
                    "session_id": None,
                    "history": list(self.history)
                    if self.active and phase == "online"
                    else [],
                    "counters": dict(self.counters),
                    "frames_by_function": dict(self.frame_counts),
                    "raw_log": {
                        "enabled": self.active and self.phase != "error",
                        "dropped": self.raw.dropped if self.raw else 0,
                        "error": self.raw.file.last_error if self.raw else None,
                        "max_file_bytes": RAW_LIMIT,
                        "backup_files": 3,
                    },
                }
            )
