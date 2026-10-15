#!/usr/bin/env python3
"""Scorer-only full-trace audit; never turn missing enforcement into a clean review.

V4 cards have no enforced closed-book boundary. Even an observed clean trace is
UNVERIFIED and not scoreable; a contained executor and captured launch context
are still required. PENDING is a queue state, not a model-quality result.
"""
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

import stage

ARENA = stage.ARENA
BASE = os.environ.get("PAPERCLIP_API_URL", "").rstrip("/").removesuffix("/api")
PAGE_BYTES = 1048576
MAX_TRACE_BYTES = 32 * PAGE_BYTES
ALLOWED_TOOLS = {"Read", "Grep", "Glob"}


class TraceError(RuntimeError):
    pass


def get(path):
    key = os.environ["PAPERCLIP_API_KEY"]
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def read_trace(record):
    """Follow byte offsets and reconstruct fragmented NDJSON streams, or refuse."""
    expected = record.get("logBytes")
    if type(expected) is not int or not 0 < expected <= MAX_TRACE_BYTES:
        raise TraceError("missing/invalid terminal log byte count")
    offset, chunks = 0, []
    while True:
        try:
            page = get(f"/api/heartbeat-runs/{record['id']}/log?offset={offset}&limitBytes={PAGE_BYTES}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise TraceError("terminal trace unavailable (HTTP 404)") from e
            raise  # Auth refusals stop; never substitute a credential.
        text = page.get("content")
        if not isinstance(text, str) or not text or page.get("truncated"):
            raise TraceError("missing or explicitly truncated trace page")
        chunks.append(text)
        measured = sum(len(chunk.encode()) for chunk in chunks)
        if measured > expected or measured > MAX_TRACE_BYTES:
            raise TraceError("trace exceeds its terminal byte count")
        next_offset = page.get("nextOffset")
        if next_offset is None:
            break
        if type(next_offset) is not int or next_offset <= offset or next_offset != measured:
            raise TraceError("invalid/non-advancing trace continuation")
        offset = next_offset
    raw = "".join(chunks)
    if len(raw.encode()) != expected:
        raise TraceError("partial trace: byte count differs from terminal record")
    streams = {}
    try:
        for line in raw.splitlines():
            if not line.strip():
                continue
            wrapper = json.loads(line)
            stream, chunk = wrapper.get("stream", "stdout"), wrapper.get("chunk")
            if not isinstance(chunk, str) or stream not in ("stdout", "stderr"):
                raise TraceError("unrecognized trace wrapper")
            streams.setdefault(stream, []).append(chunk)
        events = []
        for line in "".join(streams.get("stdout", [])).splitlines():
            if line.strip():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise TraceError("invalid transcript event")
                events.append(event)
    except (ValueError, AttributeError, TypeError) as e:
        raise TraceError("unparseable trace coverage") from e
    if not events:
        raise TraceError("empty model transcript")
    return raw, events, {"pages": len(chunks), "bytes": expected}


def contained_path(root, path):
    """Resolve symlinks/.. before component containment; missing paths do not pass.

    https://docs.python.org/3.13/library/pathlib.html#pathlib.Path.resolve
    """
    if root.is_symlink() or not isinstance(path, str) or not pathlib.Path(path).is_absolute():
        return False
    try:
        return pathlib.Path(path).resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


def patterns_for(cid):
    if cid not in stage.MAP:
        raise TraceError("unknown cell; held-out screen unavailable")
    try:
        data = json.loads(pathlib.Path(stage.GREPTILE).read_text())
        pr = next(pr for pr in data["prs"] if pr["number"] == stage.MAP[cid])
        return stage.TREE_GLOBAL + stage.own_patterns(stage.MAP[cid]), stage.finding_phrases(pr)
    except (OSError, ValueError, KeyError, StopIteration) as e:
        raise TraceError("held-out screen unavailable") from e


def check_result(body):
    text = body.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    try:
        result = json.loads(text)
        if (not isinstance(result, dict) or set(result) != {"verdict", "findings"}
                or result["verdict"] not in ("APPROVE", "CHANGES")
                or not isinstance(result["findings"], list)):
            return None
        for finding in result["findings"]:
            if (not isinstance(finding, dict)
                    or set(finding) != {"file", "line", "severity", "title", "rationale"}
                    or type(finding["line"]) is not int or finding["line"] < 1
                    or finding["severity"] not in ("P0", "P1", "P2")
                    or not all(isinstance(finding[k], str) and finding[k].strip()
                               for k in ("file", "title", "rationale"))):
                return None
            path = pathlib.PurePosixPath(finding["file"])
            if path.is_absolute() or ".." in path.parts:
                return None
        expected = "CHANGES" if any(f["severity"] in ("P0", "P1") for f in result["findings"]) else "APPROVE"
        return result if result["verdict"] == expected else None
    except (ValueError, KeyError, TypeError):
        return None


def audit(issue_ref):
    issue = get(f"/api/issues/{issue_ref}")
    listed = get(f"/api/issues/{issue['id']}/runs")
    listed = listed if isinstance(listed, list) else listed.get("runs", [])
    match = re.search(r"arena/(C-\d+)/", issue.get("description") or "")
    cid = match.group(1) if match else None
    pin = ((issue.get("assigneeAdapterOverrides") or {}).get("adapterConfig") or {}).get("model")
    out = {"issue": issue["identifier"], "status": issue["status"], "cid": cid,
           "pin": pin, "runs": [], "void": [], "unverified": [], "flags": [],
           "scoreable": False, "result": None}
    if issue.get("parentId") or issue.get("blocks") or issue.get("blockedBy"):
        out["void"].append("parent/blocker context exposure")
    if not pin or not cid:
        out["unverified"].append("missing pin or cell identity")
    try:
        regex, phrases = patterns_for(cid)
    except TraceError as e:
        regex, phrases = stage.TREE_GLOBAL, []
        out["unverified"].append(str(e))
    def screen(label, text):
        if (stage.screen_text(text, regex)
                or stage.screen_text(text, phrases, fixed=True)):
            out["void"].append(label + ": held-out string/identifier")
    screen("card inputs", (issue.get("title") or "") + "\n" + (issue.get("description") or ""))
    own, queued = [], False
    for row in listed:
        if row.get("status") == "queued" and not row.get("startedAt"):
            queued = True
            continue
        record = get(f"/api/heartbeat-runs/{row['runId']}")
        if (record.get("contextSnapshot") or {}).get("issueId") == issue["id"]:
            own.append(record)
    own.sort(key=lambda record: record.get("startedAt") or "")
    for record in own:
        rid = record["id"]
        usage = record.get("usageJson") or {}
        info = {"run": rid, "status": record["status"], "model": usage.get("model"),
                "fresh": usage.get("freshSession"), "reused": usage.get("sessionReused"),
                "in": usage.get("inputTokens"), "cached_in": usage.get("cachedInputTokens"),
                "out": usage.get("outputTokens"), "start": record.get("startedAt"),
                "end": record.get("finishedAt"), "tools": []}
        out["runs"].append(info)
        screen(rid + " context", json.dumps(record.get("contextSnapshot") or {}))
        screen(rid + " persisted output", json.dumps(record.get("resultJson") or {}))
        if usage.get("sessionReused") is True or usage.get("freshSession") is False:
            out["void"].append(rid + ": non-fresh reviewer session")
        elif usage.get("sessionReused") is not False or usage.get("freshSession") is not True:
            out["unverified"].append(rid + ": freshness not positively proven")
        if record["status"] != "succeeded" or not record.get("finishedAt"):
            queued = True
            continue
        try:
            raw, events, info["trace"] = read_trace(record)
        except TraceError as e:
            out["unverified"].append(rid + ": " + str(e))
            continue
        screen(rid + " complete transcript", raw)
        screen(rid + " decoded transcript", json.dumps(events, ensure_ascii=False))
        inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
        terminals = [e for e in events if e.get("type") == "result"]
        if (len(inits) != 1 or len(terminals) != 1
                or any(e.get("subtype") != "success" or e.get("is_error") is True for e in terminals)):
            out["unverified"].append(rid + ": missing/ambiguous/failed init or terminal evidence")
        for init in inits:
            info["init_model"] = init.get("model")
            if not init.get("model") or not usage.get("model"):
                out["unverified"].append(rid + ": model identity missing")
            elif pin and (init["model"] != pin or usage["model"] != pin):
                out["void"].append(rid + ": model does not match pin")
            if (init.get("memory_paths") or {}).get("auto"):
                out["unverified"].append(rid + ": launch-time auto-memory content not captured")
        root = pathlib.Path(ARENA) / (cid or "unknown")
        for event in events:
            if event.get("type") != "assistant":
                continue
            for part in event.get("message", {}).get("content", []):
                if not isinstance(part, dict) or part.get("type") != "tool_use":
                    continue
                name, inp = part.get("name"), part.get("input") or {}
                info["tools"].append(name)
                if name not in ALLOWED_TOOLS:
                    out["void"].append(rid + ": forbidden tool " + str(name))
                    continue
                path = inp.get("file_path") if name == "Read" else inp.get("path")
                if not contained_path(root, path):
                    out["void"].append(rid + ": outside/missing canonical file-tool path")
                if name == "Glob":
                    pattern = inp.get("pattern", "")
                    if not isinstance(pattern, str) or pathlib.PurePosixPath(pattern).is_absolute() or ".." in pathlib.PurePosixPath(pattern).parts:
                        out["void"].append(rid + ": escaping Glob pattern")
    comments = get(f"/api/issues/{issue['id']}/comments?order=asc")
    comments = comments if isinstance(comments, list) else comments.get("comments", [])
    run_ids = {record["id"] for record in own}
    for comment in comments:
        if comment.get("authorType") == "agent" and comment.get("createdByRunId") in run_ids:
            body = comment.get("body") or ""
            screen("persisted reviewer comment", body)
            result = check_result(body)
            if result is not None:
                out["result"] = result
    if own and not out["result"] and not queued:
        out["unverified"].append("no valid run-bound persisted result")
    if not own and not queued:
        out["unverified"].append("no identifiable reviewer run")
    out["trace_verdict"] = "VOID" if out["void"] else ("UNVERIFIED" if out["unverified"] else ("PENDING" if queued else "CLEAN"))
    # An observed tool sequence cannot prove the unrecorded capability/memory boundary.
    out["verdict"] = "VOID" if out["void"] else ("PENDING" if queued else "UNVERIFIED")
    out["enforcement"] = "unverified: v4 has no enforced executor/full launch-prompt capture"
    return out


def main():
    result = audit(sys.argv[1])
    print(json.dumps(result, indent=2))
    # Success means eligibility, not merely that the audit process completed.
    sys.exit({"VOID": 1, "UNVERIFIED": 3, "PENDING": 4}.get(result["verdict"], 3))


if __name__ == "__main__":
    main()
