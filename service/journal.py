"""Bounded UI event history and rotating JSONL on a separate writer thread."""

import json
import logging
import logging.handlers
import queue
from collections import deque
from datetime import datetime, timezone


class Journal:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.history = deque(maxlen=240)
        self.sequence = 0
        self.dropped = 0
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
            **details,
        }
        self.history.append(item)
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
        self.listener.stop()
        self.file.close()
