#!/usr/bin/env python3
"""Freeze and verify saved-output scoring bundles, without invoking inference.

  python3 replay.py bundle --config config.json --output portable-bundle
  python3 replay.py verify --bundle portable-bundle

Config paths are relative to the config file. Bundles include this verifier and
its scorer; they never execute the archived harness. See REPLAY.md for the v1
contract, provenance limits, and the distinction between replay and generation.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
import score


class ContractError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def require(condition, code, detail):
    if not condition:
        raise ContractError(code, detail)


def canonical(value):
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContractError("invalid_json", "string is not UTF-8 encodable") from exc


def digest(data):
    return hashlib.sha256(data).hexdigest()


def decode(data, name):
    def pairs(items):
        obj = {}
        for key, value in items:
            require(key not in obj, "invalid_json", f"duplicate key in {name}: {key}")
            obj[key] = value
        return obj

    def constant(value):
        raise ContractError("invalid_json", f"nonfinite number in {name}: {value}")

    def finite(value):
        number = float(value)
        require(math.isfinite(number), "invalid_json", f"nonfinite number in {name}: {value}")
        return number

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite)
        canonical(value)
        return value
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invalid_json", f"{name}: {exc}") from exc


def safe_path(root, name):
    require(isinstance(name, str) and name and "\\" not in name and chr(0) not in name,
            "unsafe_path", f"invalid relative path: {name!r}")
    path = Path(name)
    require(not path.is_absolute() and ".." not in path.parts and path.as_posix() == name,
            "unsafe_path", f"invalid relative path: {name}")
    target = root / path
    require(not any(p.is_symlink() for p in [target, *target.parents] if p != root.parent),
            "unsafe_path", f"symlink path: {name}")
    require(target.resolve().is_relative_to(root.resolve()), "unsafe_path", f"path escapes bundle: {name}")
    return target


def read(root, name):
    path = safe_path(root, name)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ContractError("missing_artifact", f"{name}: {exc.strerror}") from exc


def text(value):
    return isinstance(value, str) and bool(value.strip())


def integer(value):
    return type(value) is int


def timestamp(value):
    require(text(value), "invalid_config", "timestamp must include an authoritative timezone")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("invalid_config", f"invalid timestamp: {value}") from exc
    require(date.tzinfo is not None, "invalid_config", f"timestamp lacks timezone: {value}")
    return date


def config_contract(config):
    require(isinstance(config, dict) and config.get("schema") == "review-bakeoff-config/v1",
            "invalid_config", "expected review-bakeoff-config/v1")
    source = config.get("source")
    require(isinstance(source, dict) and text(source.get("revision")) and text(source.get("provenance")),
            "invalid_config", "source revision and provenance are required declarations")
    timestamp(config.get("cutoff"))
    artifacts = config.get("artifacts")
    require(isinstance(artifacts, dict) and set(artifacts) in (
        {"cases", "prompt", "harness", "output_schema"},
        {"cases", "prompt", "harness", "output_schema", "judgments"}),
        "invalid_config", "cases, prompt, harness, output_schema and optional judgments are required")
    for role in ("result_dirs", "diagnostic_result_dirs"):
        require(isinstance(config.get(role, []), list) and all(text(p) for p in config.get(role, [])),
                "invalid_config", f"{role} must be a list of relative directories")
    dirs = config.get("result_dirs", []) + config.get("diagnostic_result_dirs", [])
    require(len(set(dirs)) == len(dirs), "invalid_config", "result directories must be distinct")
    metadata = config.get("case_metadata", {})
    require(isinstance(metadata, dict) and all(isinstance(v, dict) for v in metadata.values()),
            "invalid_config", "case_metadata must map case IDs to provenance objects")
    for value in metadata.values():
        require(value.get("exposure", "unknown") in ("unknown", "exposed", "unexposed"),
                "invalid_config", "exposure must be unknown, exposed or unexposed")
        if value.get("observed_at") is not None:
            timestamp(value["observed_at"])


def cases_contract(data):
    require(isinstance(data, dict) and isinstance(data.get("cases"), list), "invalid_case", "cases list required")
    cases = {}
    for case in data["cases"]:
        require(isinstance(case, dict) and text(case.get("id")) and isinstance(case.get("defects"), list),
                "invalid_case", "case id and complete defects list required")
        require(case["id"] not in cases, "duplicate_case", case["id"])
        ids = set()
        for defect in case["defects"]:
            require(isinstance(defect, dict) and text(defect.get("id"))
                    and defect["id"] not in ("invalid", "valid-other") and text(defect.get("file"))
                    and text(defect.get("severity")) and defect["severity"] in score.SEVERITY_RANK
                    and integer(defect.get("line_start")) and integer(defect.get("line_end"))
                    and 0 < defect["line_start"] <= defect["line_end"] and defect["id"] not in ids,
                    "invalid_case", f"incomplete or duplicate defect in {case['id']}")
            ids.add(defect["id"])
        cases[case["id"]] = case
    return cases


def result_contract(result, cases, name):
    require(isinstance(result, dict) and text(result.get("case_id")) and text(result.get("model")),
            "invalid_result", f"case_id and model required: {name}")
    require(result["case_id"] in cases, "unknown_case", f"{name}: {result['case_id']}")
    latency = result.get("latency_s")
    require(type(latency) in (int, float) and 0 <= latency <= sys.float_info.max,
            "invalid_result", f"finite nonnegative latency_s required: {name}")
    usage = result.get("usage", {})
    require(isinstance(usage, dict) and all(integer(v) and 0 <= v <= 2 ** 63 - 1 for v in usage.values()),
            "invalid_result", f"nonnegative 63-bit token counts required: {name}")
    require("review" in result, "invalid_result", f"explicit review or null required: {name}")
    review = result["review"]
    if review is not None:
        require(isinstance(review, dict) and integer(review.get("score")) and 0 <= review["score"] <= 5
                and isinstance(review.get("findings"), list), "invalid_result", f"invalid review: {name}")
        for finding in review["findings"]:
            require(isinstance(finding, dict) and text(finding.get("file")) and integer(finding.get("line"))
                    and finding["line"] > 0 and text(finding.get("severity"))
                    and finding["severity"] in score.SEVERITY_RANK,
                    "invalid_result", f"invalid finding: {name}")
    effort, rep = result.get("effort"), result.get("rep")
    require((effort is None or text(effort)) and (rep is None or (integer(rep) and rep > 0)),
            "invalid_result", f"invalid effort or rep: {name}")
    return (result["case_id"], result["model"], effort, rep)


def holdout(cases, config):
    """Unite declared relatives, duplicate labels and identical repo/head snapshots."""
    metadata = config.get("case_metadata", {})
    require(set(metadata) <= set(cases), "invalid_config", "metadata names an unknown case")
    parent = {cid: cid for cid in cases}

    def find(cid):
        while parent[cid] != cid:
            cid = parent[cid]
        return cid

    tokens = {}
    for cid, case in cases.items():
        task = {k: v for k, v in case.items() if k != "id"}
        task["defects"] = sorted(
            [{k: v for k, v in d.items() if k != "id"} for d in case["defects"]], key=canonical)
        keys = [("task", digest(canonical(task)))]
        if case["defects"]:
            locations = sorted({(d["file"], d["line_start"], d["line_end"]) for d in case["defects"]})
            keys.append(("label_locations", canonical(case.get("repo")), digest(canonical(locations))))
        meta = metadata.get(cid, {})
        if text(meta.get("related_group")):
            keys.append(("group", meta["related_group"]))
        if text(case.get("repo")) and text(case.get("head_sha")):
            keys.append(("snapshot", case["repo"], case["head_sha"]))
        for key in keys:
            if key in tokens:
                parent[find(cid)] = find(tokens[key])
            tokens[key] = cid
    groups = {}
    for cid in cases:
        groups.setdefault(find(cid), []).append(cid)
    out = {}
    cutoff = timestamp(config["cutoff"])
    for members in groups.values():
        dates, unknown, exposed = set(), False, False
        for cid in members:
            meta = metadata.get(cid, {})
            exposed |= meta.get("exposure") == "exposed"
            unknown |= meta.get("exposure", "unknown") == "unknown" or not text(meta.get("group_source")) \
                or not text(meta.get("related_group"))
            if meta.get("observed_at") is None or not text(meta.get("date_source")):
                unknown = True
            else:
                dates.add("train" if timestamp(meta["observed_at"]) < cutoff else "holdout")
        if exposed or len(dates) > 1:
            status, partition, reason = "contaminated", "quarantine", "prior_exposure_or_crossing_group"
        elif unknown or not dates:
            status, partition, reason = "unknown", "unknown", "missing_date_exposure_or_group_provenance"
        else:
            status, partition, reason = "clean", next(iter(dates)), "declared_unexposed_with_provenance"
        for cid in members:
            out[cid] = {"status": status, "partition": partition, "reason": reason, "group": sorted(members)}
    return out


INPUT_NAMES = {"cases": "cases.json", "prompt": "prompt.md", "harness": "harness.py",
               "output_schema": "output_schema.json", "judgments": "judgments.json"}
SCORING_CONTRACT = {"version": 1, "line_slack": score.SLACK, "failed_runs": "retain_denominators",
                    "unknown_cases": "refuse", "partial_labels": "refuse", "aggregation": "model",
                    "cutoff_equality": "holdout", "generation_reproduction": "not_claimed"}


def assemble(files, specs):
    config = decode(files["inputs/config.json"], "config")
    config_contract(config)
    cases = cases_contract(decode(files["inputs/cases.json"], "cases"))
    partitions = holdout(cases, config)
    judgments = decode(files["inputs/judgments.json"], "judgments")
    require(isinstance(judgments, dict), "invalid_judgment", "judgments must be an object")
    rows, records, identities, keys = [], [], set(), set()
    for spec in sorted(specs, key=lambda s: (s["role"], s["key"])):
        key, role, path = spec["key"], spec["role"], spec["path"]
        result = decode(files[path], path)
        identity = result_contract(result, cases, path)
        require((role, identity) not in identities and (role, key) not in keys,
                "duplicate_result", f"duplicate identity or filename stem: {path}")
        identities.add((role, identity))
        keys.add((role, key))
        if role == "primary":
            rows.append((key, result))
        records.append(dict(spec, case_id=result["case_id"], result_sha256=digest(files[path]),
                            failed=result["review"] is None, generation_reproducibility="unknown",
                            missing_generation_evidence=["rendered_prompt", "provider_config", "historical_environment"]))
    require(rows, "empty_results", "at least one primary result is required")
    by_finding = {f"{key}#{i}": result for key, result in rows
                  for i in range(len((result["review"] or {}).get("findings", [])))}
    for key, verdict in judgments.items():
        require(key in by_finding, "invalid_judgment", f"unknown finding: {key}")
        result = by_finding[key]
        allowed = {"valid-other", "invalid"} | {d["id"] for d in cases[result["case_id"]]["defects"]}
        require(isinstance(verdict, str) and verdict in allowed, "invalid_judgment", f"invalid verdict: {key}")
    scores = (score.render(score.summarize(cases, rows, judgments)) + "\n").encode("utf-8")
    bindings = {path: digest(data) for path, data in sorted(files.items())
                if path.startswith(("inputs/", "tool/"))}
    for record in records:
        key, path, role = record["key"], record["path"], record["role"]
        sidecar = "contracts/" + ("diagnostic/" if role == "diagnostic" else "") + key + ".manifest.json"
        result = decode(files[path], path)
        metrics = score.score_result(result, cases[result["case_id"]], judgments, key) if role == "primary" else None
        if metrics is not None:
            metrics["found"] = sorted(metrics["found"])
        record["sidecar"] = sidecar
        files[sidecar] = canonical({"schema": "review-bakeoff-result/v1", **record, "bindings": bindings,
                                    "scoring_contract": SCORING_CONTRACT, "metrics": metrics,
                                    "holdout": partitions[result["case_id"]]})
    report = {"schema": "review-bakeoff-report/v1", "reproduction_scope": "saved_output_scoring_only",
              "coverage": {"eligible": len(rows), "replayed": len(rows),
                           "failed": sum(r["review"] is None for _, r in rows),
                           "diagnostic": len(records) - len(rows)},
              "holdout": partitions, "inference_calls": 0,
              "results": [{"key": r["key"], "role": r["role"], "failed": r["failed"]} for r in records]}
    files["scores.md"], files["report.json"] = scores, canonical(report)
    manifest = {"schema": "review-bakeoff-bundle/v1", "scoring_contract": SCORING_CONTRACT,
                "source": config["source"], "results": records,
                "files": {path: digest(data) for path, data in sorted(files.items())}}
    files["manifest.json"] = canonical(manifest)
    return report


def inventory(root):
    require(root.is_dir() and not root.is_symlink(), "missing_artifact", "bundle directory is absent or unsafe")
    files = {}
    for path in root.rglob("*"):
        require(not path.is_symlink(), "unsafe_path", f"symlink in bundle: {path.name}")
        if path.is_file():
            name = path.relative_to(root).as_posix()
            files[name] = read(root, name)
    return files


def bundle(config_path, output):
    config_path, output = Path(config_path), Path(output)
    root = config_path.parent
    raw_config = read(root, config_path.name)
    config = decode(raw_config, "config")
    config_contract(config)
    files = {"inputs/config.json": raw_config}
    for role, dest in INPUT_NAMES.items():
        files["inputs/" + dest] = read(root, config["artifacts"][role]) if role in config["artifacts"] else canonical({})
    files["tool/score.py"] = Path(score.__file__).read_bytes()
    files["tool/replay.py"] = Path(__file__).read_bytes()
    specs = []
    for role, field, dest in (("primary", "result_dirs", "results"),
                              ("diagnostic", "diagnostic_result_dirs", "diagnostic")):
        for dirname in config.get(field, []):
            directory = safe_path(root, dirname)
            require(directory.is_dir(), "missing_artifact", f"result directory: {dirname}")
            for path in sorted(directory.glob("*.json")):
                if path.name.startswith(("_", ".")):
                    continue
                source = path.relative_to(root).as_posix()
                target = dest + "/" + path.name
                require(target not in files, "duplicate_result", f"duplicate result filename: {source}")
                files[target] = read(root, source)
                specs.append({"key": path.stem, "role": role, "path": target, "source_path": source})
    report = assemble(files, specs)
    safe_path(output.parent, output.name)
    if output.exists() or output.is_symlink():
        require(not output.is_symlink() and output.is_dir() and inventory(output) == files,
                "output_conflict", "refusing to replace different existing evidence")
        return report
    require(output.parent.is_dir(), "output_conflict", "output parent must already exist")
    with tempfile.TemporaryDirectory(prefix=".replay-", dir=output.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        for name, data in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        try:
            os.rename(staging, output)
        except OSError as exc:
            require(output.is_dir() and not output.is_symlink() and inventory(output) == files,
                    "output_conflict", f"concurrent output conflict: {exc.strerror}")
    return report


def verify(root):
    root = Path(root)
    manifest = decode(read(root, "manifest.json"), "manifest")
    require(isinstance(manifest, dict) and manifest.get("schema") == "review-bakeoff-bundle/v1"
            and isinstance(manifest.get("files"), dict) and isinstance(manifest.get("results"), list),
            "invalid_manifest", "expected review-bakeoff-bundle/v1")
    files = inventory(root)
    require(set(files) == set(manifest["files"]) | {"manifest.json"},
            "inventory_mismatch", "missing or extra bundle files")
    for name, expected in manifest["files"].items():
        require(digest(files[name]) == expected, "hash_mismatch", name)
    for name, path in (("tool/score.py", Path(score.__file__)), ("tool/replay.py", Path(__file__))):
        require(files.get(name) == path.read_bytes(), "implementation_mismatch", f"run the bundled verifier: {name}")
    config = decode(files.get("inputs/config.json", b"null"), "config")
    config_contract(config)
    specs = []
    for record in manifest["results"]:
        require(isinstance(record, dict) and isinstance(record.get("key"), str) and bool(record["key"])
                and all(text(record.get(k)) for k in ("path", "source_path"))
                and record.get("role") in ("primary", "diagnostic"), "invalid_manifest", "invalid result descriptor")
        role, key, path = record["role"], record["key"], record["path"]
        expected = ("results/" if role == "primary" else "diagnostic/") + key + ".json"
        require(path == expected and Path(key).name == key and key not in (".", "..") and path in files,
                "invalid_manifest", "invalid result path or key")
        safe_path(root, record["source_path"])
        source = Path(record["source_path"])
        directories = config.get("result_dirs" if role == "primary" else "diagnostic_result_dirs", [])
        require(source.parent.as_posix() in directories and source.name == key + ".json",
                "invalid_manifest", "source result is outside the declared role directories")
        specs.append({k: record[k] for k in ("key", "path", "source_path", "role")})
    required = {"inputs/" + name for name in INPUT_NAMES.values()} | {
        "inputs/config.json", "tool/score.py", "tool/replay.py"} | {s["path"] for s in specs}
    inputs = {name: data for name, data in files.items() if name in required}
    require(set(inputs) == required, "invalid_manifest", "missing required inputs")
    report = assemble(inputs, specs)
    require(inputs == files, "manifest_mismatch", "manifest, sidecars or reported values differ from recomputation")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("bundle")
    create.add_argument("--config", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = bundle(args.config, args.output) if args.command == "bundle" else verify(args.bundle)
    except ContractError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"error": "io_error", "detail": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(canonical(report).decode(), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
