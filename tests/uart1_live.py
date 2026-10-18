"""Opt-in real PC/ESP32/UART1/STM32 read-only capture with concurrent camera."""

import argparse
import concurrent.futures
import io
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx
from PIL import Image
from test_backend import ROOT, Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operator-events", action="store_true",
                        help="Record phases from output/operator.json; stop=true ends capture")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    service = Service((ROOT / ".work" / ("qa-uart1-" + args.output.name)).resolve())
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    capture = None
    records, photos, errors, events = [], [], [], []
    operator = None
    started = time.monotonic()
    before = httpx.get(f"http://{args.host}/health", timeout=4, trust_env=False).json()

    def photo():
        response = service.http.get("/api/v1/bench/preview")
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content))
        image.load()
        return response.content, image.size

    try:
        service.post("bench/connect", json={"host": args.host, "port": 80})
        operation = service.post("gateway/connect")
        initial = service.until(lambda state: state["link"]["phase"] == "online", timeout=5)
        profile = service.post("gateway/profile")
        started = time.monotonic()
        print(json.dumps({"capture_started": datetime.now().astimezone().isoformat(), "operation": operation,
                          "profile": profile, "seconds": args.seconds, "identity": initial["identity"]}), flush=True)
        with (args.output / "snapshots.jsonl").open("w", encoding="utf-8") as stream:
            next_photo = 0
            while time.monotonic() - started < args.seconds:
                elapsed = time.monotonic() - started
                if args.operator_events and (args.output / "operator.json").is_file():
                    try:
                        updated = json.loads((args.output / "operator.json").read_text("utf-8-sig"))
                    except (ValueError, OSError):
                        updated = operator
                    if updated != operator:
                        operator = updated
                        events.append({"elapsed_s": elapsed, "timestamp": datetime.now().astimezone().isoformat(), **operator})
                        print(json.dumps({"operator_event": events[-1]}, ensure_ascii=False), flush=True)
                    if operator and operator.get("stop"):
                        break
                state = service.get("state")
                item = {"elapsed_s": elapsed, "timestamp": datetime.now().astimezone().isoformat(),
                        "operator_phase": operator.get("phase") if operator else None,
                        **{key: state[key] for key in
                        ("mode", "link", "identity", "sensors", "controller", "counters", "gateway", "hardware_control_allowed")}}
                records.append(item)
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                stream.flush()
                if capture is not None and capture.done():
                    try:
                        data, size = capture.result()
                        photos.append({"elapsed_s": elapsed, "bytes": len(data), "size": size})
                        if len(photos) == 1:
                            (args.output / "first-camera.jpg").write_bytes(data)
                    except Exception as exc:
                        errors.append({"elapsed_s": elapsed, "error": str(exc)})
                    capture = None
                if capture is None and elapsed >= next_photo:
                    capture = pool.submit(photo)
                    next_photo = elapsed + 1
                time.sleep(.1)
        after = httpx.get(f"http://{args.host}/health", timeout=4, trust_env=False).json()
        good = [row for row in records if row["sensors"]["attitude"]["valid"]]
        angles = list(zip(*(row["sensors"]["attitude"]["value"] for row in good))) if good else []
        state_ages = sorted(row["link"]["state_age_ms"] for row in records if row["link"]["state_age_ms"] is not None)
        result = {"timestamp": datetime.now().astimezone().isoformat(), "scope": __doc__,
                  "application_version": initial["application_version"], "initial_identity": initial["identity"],
                  "seconds": time.monotonic() - started, "snapshots": len(records),
                  "valid_attitude_snapshots": len(good), "unknown_attitude_snapshots": len(records) - len(good),
                  "attitude_ranges_rad": [[min(values), max(values)] for values in angles],
                  "link_phases": {phase: sum(row["link"]["phase"] == phase for row in records)
                                  for phase in sorted({r["link"]["phase"] for r in records})},
                  "state_age_p95_ms": state_ages[max(0, int(.95 * len(state_ages)) - 1)] if state_ages else None,
                  "valid_attitude_age_median_ms": statistics.median(row["sensors"]["attitude"]["age_ms"] for row in good) if good else None,
                  "camera_frames": photos, "camera_errors": errors,
                  "operator_events": events,
                  "esp_counter_delta": {key: after[key] - before[key] for key in
                                        ("uart_frames", "uart_errors", "uart_rejected", "uart_submitted")},
                  "no_actions_sent": True, "flight_qualified": False}
        (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "camera_frames"}, ensure_ascii=False), flush=True)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        service.close()


if __name__ == "__main__":
    main()
