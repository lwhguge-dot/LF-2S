"""Bounded UI event history and rotating JSONL on a separate writer thread."""

import copy
import json
import logging
import logging.handlers
import queue
import secrets
import time
from collections import deque
from datetime import datetime, timezone


class Journal:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.history = deque(maxlen=240)
        self.sequence = 0
        self.dropped = 0
        self.run_id = secrets.token_hex(12)
        self.started = time.monotonic()
        self.context = lambda: {}
        self.recorder = None
        self.queue = queue.Queue(maxsize=1024)
        self.file = logging.handlers.RotatingFileHandler(
            directory / "events.jsonl",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        self.listener = logging.handlers.QueueListener(self.queue, self.file)
        self.listener.start()

    def add(self, kind, text, **details):
        self.sequence += 1
        item = {
            "id": self.sequence,
            "time": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "text": text,
            "simulation": True,
            "source": "synthetic",
            "run_id": self.run_id,
            "received_at_ms": int((time.monotonic() - self.started) * 1000),
            **self.context(),
            **details,
        }
        item = copy.deepcopy(item)
        self.history.append(item)
        if self.recorder and item["simulation"]:
            self.recorder.add("event", item)
        record = logging.LogRecord(
            "lf2s",
            logging.INFO,
            "",
            0,
            json.dumps(item, ensure_ascii=False, allow_nan=False),
            (),
            None,
        )
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
        return item

    def close(self):
        # QueueListener.stop uses a nonblocking sentinel; reserve a slot if saturated.
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
        self.listener.stop()
        self.file.close()
