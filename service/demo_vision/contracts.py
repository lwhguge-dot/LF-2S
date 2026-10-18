"""Bounded local contracts and strict observation parsing, independent of MAVLink."""

import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass

from PIL import Image


class ObservationError(ValueError):
    pass


def integer(value, low, high, reason):
    if type(value) is not int or not low <= value <= high:
        raise ObservationError(reason)
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ObservationError("invalid_identifier")
    if int(value) >= 2**64:
        raise ObservationError("invalid_identifier")


def strict_json(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > 65536:
        raise ObservationError("response_size")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ObservationError("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ObservationError("nonfinite_json")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, ObservationError):
            raise
        raise ObservationError("invalid_json") from None


@dataclass(frozen=True)
class Policy:
    # All temporal and denomination choices must be explicit. No flight defaults.
    max_receipt_age_ms: int
    request_timeout_ms: int
    max_sample_gap_ms: int
    green_run_ms: int
    signal_match_iou_millionths: int
    denominations_fen: tuple[int, ...]

    def __post_init__(self):
        for name in (
            "max_receipt_age_ms",
            "request_timeout_ms",
            "max_sample_gap_ms",
            "green_run_ms",
        ):
            integer(getattr(self, name), 1, 60000, "invalid_policy_" + name)
        integer(
            self.signal_match_iou_millionths, 1, 1000000, "invalid_signal_match_iou"
        )
        if (
            type(self.denominations_fen) is not tuple
            or not 1 <= len(self.denominations_fen) <= 16
            or any(
                type(v) is not int or not 1 <= v <= 1000000
                for v in self.denominations_fen
            )
            or len(set(self.denominations_fen)) != len(self.denominations_fen)
        ):
            raise ObservationError("explicit_denominations_required")

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ObservationError("policy_fields")
        if type(value["denominations_fen"]) is not list:
            raise ObservationError("explicit_denominations_required")
        return cls(**{**value, "denominations_fen": tuple(value["denominations_fen"])})

    @property
    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Frame:
    image: bytes
    frame_id: str
    stream_id: str
    received_ms: int
    clock_id: str
    source: str
    session_id: str | None = None
    reset_seq: int | None = None
    source_capture_us: str | None = None
    source_clock_id: str | None = None
    frame_id_source: str = "local_replay"

    def __post_init__(self):
        if type(self.image) is not bytes or not 0 < len(self.image) <= 1048576:
            raise ObservationError("image_size")
        identifier(self.frame_id)
        identifier(self.stream_id)
        integer(self.received_ms, 0, 2**63 - 1, "invalid_receipt_time")
        if not isinstance(self.clock_id, str) or not re.fullmatch(
            r"[A-Za-z0-9:_-]{1,80}", self.clock_id
        ):
            raise ObservationError("invalid_clock_id")
        if self.source not in (
            "synthetic",
            "offline_image",
            "offline_video",
            "esp32_preview",
        ):
            raise ObservationError("invalid_source")
        if self.frame_id_source not in ("local_replay", "source_reported"):
            raise ObservationError("invalid_id_source")
        if self.session_id is not None:
            identifier(self.session_id)
        if self.reset_seq is not None:
            integer(self.reset_seq, 0, 2**32 - 1, "invalid_reset_seq")
        if (self.source_capture_us is None) != (self.source_clock_id is None):
            raise ObservationError("capture_clock_pair")
        if self.source_capture_us is not None and (
            not isinstance(self.source_capture_us, str)
            or not re.fullmatch(r"0|[1-9][0-9]{0,19}", self.source_capture_us)
            or int(self.source_capture_us) >= 2**64
            or not isinstance(self.source_clock_id, str)
            or not re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", self.source_clock_id)
        ):
            raise ObservationError("invalid_capture_metadata")

    @property
    def context(self):
        return (
            self.source,
            self.frame_id_source,
            self.stream_id,
            self.session_id,
            self.reset_seq,
            self.clock_id,
            self.source_clock_id,
        )

    def provenance(self):
        return {k: v for k, v in asdict(self).items() if k != "image"} | {
            "image_sha256": hashlib.sha256(self.image).hexdigest(),
        }

    def image_info(self):
        try:
            with Image.open(io.BytesIO(self.image)) as image:
                if (
                    image.format not in ("JPEG", "PNG")
                    or getattr(image, "n_frames", 1) != 1
                    or min(image.size) < 1
                    or image.width * image.height > 2000000
                ):
                    raise ObservationError("image_format_or_dimensions")
                info = {
                    "width": image.width,
                    "height": image.height,
                    "mime": "image/jpeg" if image.format == "JPEG" else "image/png",
                }
                image.verify()
                return info
        except (OSError, ValueError, Image.DecompressionBombError):
            raise ObservationError("image_format_or_dimensions") from None


LIGHTS = ("red", "yellow", "green", "off", "ambiguous", "unknown")


def schema(policy):
    box = {
        "type": "array",
        "minItems": 4,
        "maxItems": 4,
        "items": {"type": "integer", "minimum": 0, "maximum": 1000},
    }

    def obj(properties):
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        }

    return obj(
        {
            "signals": {
                "type": "array",
                "maxItems": 8,
                "items": obj(
                    {
                        "box": box,
                        "light": {"type": "string", "enum": list(LIGHTS)},
                    }
                ),
            },
            "zebra_visible": {"type": ["boolean", "null"]},
            "checkout_visible": {"type": ["boolean", "null"]},
            "currency": obj(
                {
                    "visible_notes_complete": {"type": ["boolean", "null"]},
                    "notes": {
                        "type": "array",
                        "maxItems": 32,
                        "items": obj(
                            {
                                "box": box,
                                "denomination_fen": {
                                    "type": ["integer", "null"],
                                    "enum": [*policy.denominations_fen, None],
                                },
                            }
                        ),
                    },
                }
            ),
        }
    )


def fields(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ObservationError("unexpected_observation_fields")


def box(value):
    if type(value) is not list or len(value) != 4:
        raise ObservationError("invalid_box")
    for number in value:
        integer(number, 0, 1000, "invalid_box")
    if value[0] >= value[2] or value[1] >= value[3]:
        raise ObservationError("invalid_box")


def parse_response(raw, policy):
    envelope = strict_json(raw)
    if type(envelope) is not dict or envelope.get("model") != "qwen38":
        raise ObservationError("response_model")
    choices = envelope.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise ObservationError("response_choices")
    choice = choices[0]
    message = choice.get("message")
    if choice.get("finish_reason") != "stop" or type(message) is not dict:
        raise ObservationError("response_incomplete")
    if (
        message.get("reasoning")
        or message.get("reasoning_content")
        or message.get("tool_calls")
        or message.get("function_call")
        or message.get("refusal")
        or message.get("role") != "assistant"
    ):
        raise ObservationError("response_not_observation")
    value = strict_json(message.get("content"))
    fields(value, ("signals", "zebra_visible", "checkout_visible", "currency"))
    for key in ("zebra_visible", "checkout_visible"):
        if value[key] is not None and type(value[key]) is not bool:
            raise ObservationError("invalid_visibility")
    if type(value["signals"]) is not list or len(value["signals"]) > 8:
        raise ObservationError("signal_limit")
    for signal in value["signals"]:
        fields(signal, ("box", "light"))
        box(signal["box"])
        if type(signal["light"]) is not str or signal["light"] not in LIGHTS:
            raise ObservationError("invalid_light")
    currency = value["currency"]
    fields(currency, ("visible_notes_complete", "notes"))
    if (
        currency["visible_notes_complete"] is not None
        and type(currency["visible_notes_complete"]) is not bool
    ):
        raise ObservationError("invalid_completeness")
    if type(currency["notes"]) is not list or len(currency["notes"]) > 32:
        raise ObservationError("banknote_limit")
    for note in currency["notes"]:
        fields(note, ("box", "denomination_fen"))
        box(note["box"])
        denomination = note["denomination_fen"]
        if denomination is not None and (
            type(denomination) is not int
            or denomination not in policy.denominations_fen
        ):
            raise ObservationError("unsupported_denomination")
    return value


def intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )


def currency_summary(currency):
    notes = currency["notes"]
    overlap = any(
        intersection(a["box"], b["box"]) > 0
        for i, a in enumerate(notes)
        for b in notes[i + 1 :]
    )
    complete = currency["visible_notes_complete"] is True
    known = [
        note["denomination_fen"]
        for note in notes
        if note["denomination_fen"] is not None
    ]
    resolved = complete and len(known) == len(notes) and not overlap
    return {
        "scope": "model_report_of_visible_frame_only",
        "provisional": True,
        "reason": "overlapping_or_duplicate_boxes"
        if overlap
        else "complete_visible_report"
        if resolved
        else "incomplete_visible_report",
        "visible_count": len(notes) if complete and not overlap else None,
        "recognized_count": len(known) if not overlap else None,
        "recognized_subtotal_fen": sum(known) if not overlap else None,
        "visible_total_fen": sum(known) if resolved else None,
        "denomination_breakdown": [
            {"denomination_fen": v, "count": known.count(v)} for v in sorted(set(known))
        ]
        if not overlap
        else [],
        "scene_total_fen": None,
        "physical_accuracy_verified": False,
    }
