"""Standard HTTP JPEG capture, with a small set of project metadata headers."""

from dataclasses import dataclass

from .wire import ProtocolError


@dataclass(frozen=True)
class CaptureRequest:
    request_id: int
    stream_id: int
    sent_ms: int


class ImageGate:
    def __init__(self, calibration_id, max_age_ms, max_jpeg_bytes, *, hardware_freshness_qualified=False):
        if calibration_id <= 0 or max_age_ms <= 0 or not 4 <= max_jpeg_bytes <= 16 * 1024 * 1024:
            raise ValueError("Explicit calibrated limits required")
        self.calibration_id = calibration_id
        self.max_age_ms = max_age_ms
        self.max_bytes = max_jpeg_bytes
        self.qualified = hardware_freshness_qualified
        self.stream = self.frame = self.request_counter = 0
        self.outstanding = None

    def begin(self, now_ms):
        if self.outstanding is not None or self.request_counter == 0xFFFFFFFF:
            raise ProtocolError("Only one capture request in flight; no counter wrapping")
        self.request_counter += 1
        self.outstanding = CaptureRequest(self.request_counter, self.stream, now_ms)
        return self.outstanding

    def cancel(self):
        self.outstanding = None

    def accept(self, headers, jpeg, received_ms, *, for_control=True):
        request = self.outstanding
        self.outstanding = None
        if request is None:
            raise ProtocolError("Unsolicited image")
        h = {key.lower(): str(value) for key, value in headers.items()}
        try:
            number = lambda name: int(h["x-lf2s-" + name])
            stream, frame, response_id = number("stream-id"), number("frame-id"), number("request-id")
            width, height = number("width"), number("height")
            declared = int(h["content-length"])
            if (h["content-type"].split(";")[0].strip() != "image/jpeg" or declared != len(jpeg)
                    or not 4 <= declared <= self.max_bytes or not 1 <= width <= 4096 or not 1 <= height <= 4096
                    or not 0 < stream < 1 << 64 or not 0 < frame <= 0xFFFFFFFF
                    or number("capture-us") < 0 or number("calibration-id") != self.calibration_id
                    or number("capture-after-request") != 1 or response_id != request.request_id):
                raise ProtocolError("Image metadata mismatch")
        except (KeyError, ValueError) as exc:
            raise ProtocolError("Missing or invalid image metadata") from exc
        if (request.stream_id and request.stream_id != stream) or (self.stream == stream and frame <= self.frame):
            raise ProtocolError("Camera restart or replay; reset capture session explicitly")
        if not 0 <= received_ms - request.sent_ms < self.max_age_ms:
            raise ProtocolError("Image exceeded the PC-clock freshness budget")
        if for_control and not self.qualified:
            raise ProtocolError("Real camera freshness is not qualified; display only")
        import cv2
        import numpy as np

        decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None or decoded.shape[:2] != (height, width):
            raise ProtocolError("JPEG decode/dimensions failed")
        self.stream, self.frame = stream, frame
        return decoded


def fetch_capture(http_client, url, request, max_bytes):
    """Read headers before a bounded body; caller sets finite HTTP timeouts."""
    with http_client.stream("GET", url, params={"request_id": request.request_id, "stream_id": request.stream_id},
                            headers={"Cache-Control": "no-store", "Accept-Encoding": "identity"}) as response:
        response.raise_for_status()
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ProtocolError("Compressed HTTP transfer is outside the bounded JPEG profile")
        try:
            length = int(response.headers["Content-Length"])
        except (KeyError, ValueError) as exc:
            raise ProtocolError("A bounded Content-Length is required") from exc
        if not 4 <= length <= max_bytes:
            raise ProtocolError("JPEG length exceeds the signed configuration")
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=min(8192, max_bytes)):
            if len(body) + len(chunk) > length:
                raise ProtocolError("HTTP body exceeds Content-Length")
            body.extend(chunk)
        if len(body) != length:
            raise ProtocolError("Truncated HTTP body")
        return dict(response.headers), bytes(body)
