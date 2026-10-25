#!/usr/bin/env python3
"""Offline, literal evidence-retention and adapter-reported capacity audit.

No provider calls, token estimates, prompt rewrites or operational mutations.
See docs/context-evidence.md for the input contract and limits of the verdict.
"""

import argparse
import hashlib
import json
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected nonempty text")
    return value


def _count(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("expected a bounded token count")
    if value > 1_000_000_000:
        raise ValueError("token count exceeds input bound")
    return value


def _unknown(reason):
    return {"status": "unknown", "reason": reason}


def _capacity(bundle):
    try:
        run = _object(bundle.get("run"))
        model = _text(_object(run.get("usageJson")).get("model"))
        models = _object(_object(run.get("resultJson")).get("modelUsage"))
        if len(models) != 1 or model not in models:
            return _unknown("missing, mixed or mismatched served-model metadata")
        metadata = _object(models[model])
        if metadata.get("canonicalModel", model) != model:
            return _unknown("canonical model differs from served-model key")
        window = _count(metadata.get("contextWindow"), 1)
    except ValueError:
        return _unknown("missing or invalid adapter-reported capacity")

    result = {
        "status": "unknown",
        "model": model,
        "contextWindow": window,
        "provenance": "run.resultJson.modelUsage (adapter-reported, not endpoint-verified)",
    }
    try:
        request = _object(bundle.get("request"))
        if request.get("model") != model:
            result["reason"] = "request model differs from served model"
            return result
        source = _text(request.get("source"))
        required = _count(request.get("inputTokens")) + _count(
            request.get("outputReserveTokens"), 1
        )
    except ValueError:
        result["reason"] = "per-request usage and output reserve not measured"
        return result
    result.update(
        status="pass" if required <= window else "fail",
        requiredTokens=required,
        headroomTokens=window - required,
        requestSource=source,
    )
    return result


def _fidelity(bundle):
    evidence = bundle.get("rawEvidence")
    anchors = bundle.get("anchors")
    packet = bundle.get("statePacket")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("nonempty rawEvidence is required")
    if not isinstance(anchors, list) or not anchors:
        raise ValueError("nonempty selected anchors are required")
    if not isinstance(packet, str):
        raise ValueError("statePacket must be text")

    sources = {}
    for item in evidence:
        row = _object(item)
        identity = _text(row.get("id"))
        if identity in sources:
            raise ValueError("duplicate raw evidence identity")
        sources[identity] = _text(row.get("body"))

    identities, selections, covered = set(), set(), set()
    missing = []
    for item in anchors:
        row = _object(item)
        identity = _text(row.get("id"))
        source = _text(row.get("sourceId"))
        text = _text(row.get("text"))
        if identity in identities or (source, text) in selections:
            raise ValueError("duplicate selected anchor")
        if source not in sources or text not in sources[source]:
            raise ValueError("selected anchor is not grounded in raw evidence")
        identities.add(identity)
        selections.add((source, text))
        covered.add(source)
        if text not in packet:
            missing.append(identity)
    if covered != set(sources):
        raise ValueError("every raw source requires a selected anchor")

    retained = len(anchors) - len(missing)
    return {
        "status": "fail" if missing else "pass",
        "retained": retained,
        "total": len(anchors),
        "retentionRate": retained / len(anchors),
        "missingAnchorIds": missing,
        "rawEvidenceHashes": {
            identity: hashlib.sha256(body.encode("utf-8")).hexdigest()
            for identity, body in sources.items()
        },
        "scope": "selected literal anchors only; not semantic fidelity or completeness",
    }


def audit(bundle):
    """Return a diagnostic without mutating the replay bundle or echoing bodies."""
    try:
        _object(bundle)
        if bundle.get("schema") != "context-evidence.v1":
            raise ValueError("unsupported replay schema")
        fidelity = _fidelity(bundle)
    except ValueError as error:
        return {"schema": "context-evidence-report.v1", **_unknown(str(error))}
    capacity = _capacity(bundle)
    statuses = [fidelity["status"], capacity["status"]]
    status = "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"
    return {
        "schema": "context-evidence-report.v1",
        "status": status,
        "fidelity": fidelity,
        "capacity": capacity,
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite JSON number")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="local JSON replay bundle (maximum 8 MiB)")
    args = parser.parse_args()
    try:
        with args.bundle.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("replay bundle exceeds 8 MiB")
        bundle = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        report = audit(bundle)
    except (OSError, ValueError, RecursionError):
        report = _unknown("unreadable, malformed or oversized replay bundle")
        report["schema"] = "context-evidence-report.v1"
    print(json.dumps(report, sort_keys=True))
    return {"pass": 0, "fail": 1, "unknown": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
