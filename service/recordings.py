"""Bounded synthetic-session recording. Playback never calls a protocol sender."""

import copy
import json
import queue
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from lf2s_protocol.wire import ProtocolError

MAX_BYTES = 4 * 1024 * 1024
MAX_FILES = 6
MAX_QUEUE = 256
ID = re.compile(r"[a-f0-9]{24}\Z")


class Recordings:
    def __init__(self, directory, *, max_bytes=MAX_BYTES, max_files=MAX_FILES):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.id = secrets.token_hex(12)
        self.started = datetime.now(timezone.utc).isoformat()
        self.bytes = self.frames = self.dropped = 0
        self.truncated = False
        self.error = None
        self.closed = False
        self.last_sample = -500
        self.queue = queue.Queue(maxsize=MAX_QUEUE)
        self._prune()
        self.worker = threading.Thread(target=self._write, daemon=True)
        self.worker.start()

    def _prune(self):
        files = sorted(self.directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        for path in files[: max(0, len(files) - self.max_files + 1)]:
            if ID.fullmatch(path.stem):
                path.unlink()
                path.with_suffix(".summary.json").unlink(missing_ok=True)

    def stats(self):
        return {
            "id": self.id,
            "started": self.started,
            "frames": self.frames,
            "bytes": self.bytes,
            "dropped": self.dropped,
            "truncated": self.truncated,
            "error": self.error,
            "complete": self.closed,
            "simulation": True,
            "max_bytes": self.max_bytes,
            "max_files": self.max_files,
            "image_policy": "metadata_only",
        }

    def add(self, kind, value):
        if self.closed or self.truncated or self.error:
            self.dropped += 1
            return
        item = {"kind": kind, "value": copy.deepcopy(value)}
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def sample(self, snapshot):
        now = snapshot["service_time_ms"]
        if now - self.last_sample < 500:
            return
        self.last_sample = now
        self.add("snapshot", {k: v for k, v in snapshot.items() if k != "events"})

    def _write(self):
        try:
            with (self.directory / f"{self.id}.jsonl").open("xb") as stream:
                header = (
                    json.dumps(
                        {
                            "format": "lf2s-recording-1",
                            "id": self.id,
                            "started": self.started,
                            "simulation": True,
                        }
                    ).encode()
                    + b"\n"
                )
                stream.write(header)
                self.bytes += len(header)
                while True:
                    item = self.queue.get()
                    if item is None:
                        break
                    raw = (
                        json.dumps(item, ensure_ascii=False, allow_nan=False).encode(
                            "utf-8"
                        )
                        + b"\n"
                    )
                    if self.truncated or self.bytes + len(raw) > self.max_bytes:
                        self.truncated = True
                        self.dropped += 1
                        continue
                    stream.write(raw)
                    stream.flush()
                    self.bytes += len(raw)
                    if item["kind"] == "snapshot":
                        self.frames += 1
        except (OSError, ValueError) as exc:
            self.error = f"录制停止：{type(exc).__name__}"
        finally:
            self.closed = True
            try:
                target = self.directory / f"{self.id}.summary.json"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(self.stats(), ensure_ascii=False), "utf-8"
                )
                temporary.replace(target)
            except OSError:
                self.error = "录制索引无法写入"

    def close(self):
        if self.worker.is_alive():
            # A full queue must not make shutdown's sentinel fail.
            deadline = time.monotonic() + 2
            while self.worker.is_alive():
                try:
                    self.queue.put(None, timeout=0.05)
                    break
                except queue.Full:
                    if time.monotonic() >= deadline:
                        self.error = "录制关闭超时"
                        return
            self.worker.join(timeout=2)

    def _path(self, recording_id):
        if not ID.fullmatch(recording_id):
            raise ProtocolError("录制编号无效")
        path = self.directory / f"{recording_id}.jsonl"
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > self.max_bytes
        ):
            raise ProtocolError("录制不存在、已轮换或超过读取上限")
        return path

    def list(self):
        result = []
        for path in sorted(
            self.directory.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            if not ID.fullmatch(path.stem):
                continue
            if path.stem == self.id:
                result.append(self.stats())
                continue
            summary = path.with_suffix(".summary.json")
            try:
                if summary.stat().st_size > 8192:
                    continue
                result.append(json.loads(summary.read_text("utf-8")))
            except (OSError, ValueError):
                # A crash may leave a usable partial recording without an index.
                result.append(
                    {
                        "id": path.stem,
                        "bytes": path.stat().st_size,
                        "complete": False,
                        "error": "录制未正常关闭，可只读检查已有帧",
                    }
                )
        return result[: self.max_files]

    def frame(self, recording_id, index):
        path = self._path(recording_id)
        if index < 0:
            raise ProtocolError("回放帧编号无效")
        frames = 0
        selected = None
        events = []
        with path.open("r", encoding="utf-8") as stream:
            try:
                header = json.loads(next(stream))
                if (
                    header.get("format") != "lf2s-recording-1"
                    or header.get("simulation") is not True
                ):
                    raise ProtocolError("不支持该录制格式")
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        break  # interrupted last line; never invent a missing sample
                    if item.get("kind") == "event" and frames <= index:
                        events.append(item["value"])
                        events = events[-60:]
                    elif item.get("kind") == "snapshot":
                        if frames == index:
                            selected = item["value"]
                        frames += 1
            except (StopIteration, ValueError, KeyError) as exc:
                raise ProtocolError("录制内容不完整") from exc
        if selected is None:
            raise ProtocolError("该回放帧尚未录制或已丢弃")
        return {
            "recording_id": recording_id,
            "index": index,
            "frames": frames,
            "read_only": True,
            "historical": True,
            "simulation": True,
            "state": selected,
            "events": events,
            "image_policy": "未录制图像字节，不显示实时图像",
        }
