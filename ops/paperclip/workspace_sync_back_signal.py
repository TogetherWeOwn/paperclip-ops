#!/usr/bin/env python3
"""Classify one sanitized sync-back payload measurement; never gate execution.

Reads at most 4096 bytes of JSON from stdin. Emits only allowlisted fields,
integer bytes and fixed messages. No workspace traversal, transport or API calls.
See docs/workspace-sync-back-signal.md for the observation and alert contracts.
"""

import json
import sys

THRESHOLD_BYTES = 2_000_000_000
MAX_INPUT_BYTES = 4096
MAX_MEASURED_BYTES = 9_007_199_254_740_991  # JSON integers remain exact in JS sinks.
ENDPOINT_KEY = "platform_workspace-sync-back"


def signal(byte_count=None, reason="measurement_unknown"):
    qualified = byte_count is not None
    oversized = qualified and byte_count > THRESHOLD_BYTES
    status = "unknown" if not qualified else "oversized" if oversized else "within_threshold"
    if not qualified:
        message = f"sync-back size unknown ({reason}); nonqualifying observation; alert-only"
    else:
        boundary = "exceeds" if oversized else "does not exceed"
        message = (
            f"sync-back payload {byte_count} bytes {boundary} 2000000000 bytes "
            "(decimal 2 GB); alert-only"
        )
    return {
        "schema": "workspace-sync-back-signal.v1",
        "status": status,
        "reason": reason if not qualified else "above_threshold" if oversized else "at_or_below_threshold",
        "measurement_qualified": qualified,
        "bytes": byte_count,
        "threshold_bytes": THRESHOLD_BYTES,
        "unit": "bytes",
        "alert_only": True,
        "blocks_execution": False,
        "alert_required": not qualified or oversized,
        "message": message,
        "gatus": {
            "endpoint_key": ENDPOINT_KEY,
            "success": qualified and not oversized,
            "error": message if not qualified or oversized else "",
        },
    }


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("nonfinite number")


def observe(raw):
    if len(raw) > MAX_INPUT_BYTES:
        return signal(reason="input_too_large")
    try:
        observation = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return signal(reason="invalid_json")
    if not isinstance(observation, dict):
        return signal(reason="invalid_shape")
    version = observation.get("schema_version")
    if type(version) is not int or version != 1:
        return signal(reason="unsupported_schema")
    if observation.get("measurement") != "sync_back_payload_bytes":
        return signal(reason="unsupported_measurement")
    if observation.get("complete") is not True:
        return signal(reason="incomplete_measurement")
    byte_count = observation.get("bytes")
    if type(byte_count) is not int or not 0 <= byte_count <= MAX_MEASURED_BYTES:
        return signal(reason="invalid_bytes")
    return signal(byte_count)


def main():
    try:
        result = observe(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1))
    except OSError:
        result = signal(reason="input_unreadable")
    print(json.dumps(result, sort_keys=True))
    # An alert or an unknown measurement is evidence, never an execution gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
