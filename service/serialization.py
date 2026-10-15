"""Keep IEEE non-finite telemetry out of JSON; preserve wire u64 as strings."""

import math


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def message_json(message):
    values = {
        name: str(getattr(message, name))
        if kind in ("uint64_t", "int64_t")
        else clean(getattr(message, name))
        for name, kind in zip(message.fieldnames, message.fieldtypes, strict=True)
    }
    return {
        "name": message.get_type(),
        "message_id": message.get_msgId(),
        "source_system": message.get_srcSystem(),
        "source_component": message.get_srcComponent(),
        "fields": values,
    }
