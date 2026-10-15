#!/usr/bin/env python3
"""case_d0c2375d: does an issues PATCH replace or merge `assigneeAdapterOverrides`?

Run on a scratch card the caller owns, never a live card. Three probes:

  A. full override write (model + env with model keys, secret_ref-shaped
     entries, and a marker) -> read back. Control: proves the field is
     writable by this token and shows the stored shape. If this fails to
     land, probes B/C are unreadable and the script says so.
  B. model-only PATCH — the exact shape `apply.ts:98` writes on main.
     Read back: did `adapterConfig.env` survive?
  C. partial-env PATCH (one key). Read back: per-key env merge, or
     wholesale env replacement?

Prints one JSON object. Exits 0 only if probe A landed.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ["PAPERCLIP_API_URL"].rstrip("/")
if BASE.endswith("/api"):
    BASE = BASE[: -len("/api")]
KEY = os.environ["PAPERCLIP_API_KEY"]
COMPANY = os.environ["PAPERCLIP_COMPANY_ID"]
AGENT = os.environ["PAPERCLIP_AGENT_ID"]
RUN = os.environ.get("PAPERCLIP_RUN_ID", "")

MODEL_KEYS = [
    "PAPERCLIP_ASSIGNED_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
]


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + KEY,
            "Content-Type": "application/json",
            **({"X-Paperclip-Run-Id": RUN} if RUN else {}),
        },
    )
    try:
        with urllib.request.urlopen(req) as res:
            raw = res.read().decode()
            return res.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:500]


def override_of(issue):
    if not isinstance(issue, dict):
        return None
    return (issue.get("assigneeAdapterOverrides") or {}).get("adapterConfig")


def main():
    out = {"steps": []}

    def step(name, status, note=None):
        rec = {"step": name, "status": status}
        if note:
            rec["note"] = note
        out["steps"].append(rec)
        print(json.dumps(rec), file=sys.stderr)

    st, issue = call(
        "POST",
        f"/api/companies/{COMPANY}/issues",
        {
            "title": "case_d0c2375d scratch probe: assigneeAdapterOverrides PATCH semantics",
            "description": (
                "Scratch measurement card for case_d0c2375d. Probes whether an issues PATCH "
                "replaces or merges assigneeAdapterOverrides.adapterConfig.env. "
                "Auto-closes when the probe completes. Do not dispatch work here."
            ),
            "assigneeId": AGENT,
        },
    )
    issue_id = (issue or {}).get("id")
    out["scratch_issue"] = {"status": st, "id": issue_id}
    if st != 201 and st != 200 or not issue_id:
        step("create", st, str(issue)[:300])
        print(json.dumps(out, indent=2))
        sys.exit(2)
    step("create", st, issue.get("identifier"))

    full_env = {}
    for k in MODEL_KEYS:
        full_env[k] = {"type": "plain", "value": "gpt-5.6-sol"}
    full_env["ANTHROPIC_AUTH_TOKEN"] = {"type": "secret_ref", "secretKey": "ANTHROPIC_AUTH_TOKEN"}
    full_env["CLIPROXY_USAGE_LANE_KEY"] = {"type": "secret_ref", "secretKey": "CLIPROXY_USAGE_LANE_KEY"}
    full_env["case_dbd201c8_MARKER"] = {"type": "plain", "value": "probe-a"}

    st, _ = call(
        "PATCH",
        f"/api/issues/{issue_id}",
        {"assigneeAdapterOverrides": {"adapterConfig": {"model": "gpt-5.6-sol", "env": full_env}}},
    )
    step("A.write", st)
    st, issue = call("GET", f"/api/issues/{issue_id}")
    a = override_of(issue) or {}
    out["A_readback"] = a
    env_a = a.get("env") or {}
    landed = a.get("model") == "gpt-5.6-sol" and "case_dbd201c8_MARKER" in json.dumps(env_a)
    step("A.readback", st, f"landed={landed} env_keys={sorted(env_a)[:12]}")
    if not landed:
        out["verdict"] = "UNREADABLE: full write did not land; B/C prove nothing"
        call("PATCH", f"/api/issues/{issue_id}", {"status": "done"})
        print(json.dumps(out, indent=2))
        sys.exit(3)

    # B: the exact model-only shape apply.ts:98 writes on main.
    st, _ = call("PATCH", f"/api/issues/{issue_id}", {"assigneeAdapterOverrides": {"adapterConfig": {"model": "claude-opus-5"}}})
    step("B.write(model-only)", st)
    # A refused write (409 run-ownership, 4xx) leaves the prior env in place, which
    # reads as "MERGE/PRESERVE" — the exact opposite of the truth, and the verdict
    # the fix turns on. Gate the verdict on the write having actually landed.
    if st not in (200, 201):
        out["verdict"] = f"UNREADABLE: probe B write returned {st}; env readback proves nothing"
        call("PATCH", f"/api/issues/{issue_id}", {"assigneeAdapterOverrides": None})
        call("PATCH", f"/api/issues/{issue_id}", {"status": "done"})
        print(json.dumps(out, indent=2))
        sys.exit(4)
    st, issue = call("GET", f"/api/issues/{issue_id}")
    b = override_of(issue) or {}
    env_b = b.get("env")
    out["B_readback"] = b
    if env_b is None:
        verdict_b = "REPLACE at adapterConfig level: env key dropped by model-only PATCH"
    elif not env_b:
        verdict_b = "env present but EMPTY after model-only PATCH"
    else:
        kept = sorted(set(env_b))
        verdict_b = f"MERGE/PRESERVE: env survives model-only PATCH ({len(kept)} keys)"
    out["verdict_B"] = verdict_b
    step("B.readback", st, verdict_b)

    # C: partial env — one plain key only.
    st, _ = call(
        "PATCH",
        f"/api/issues/{issue_id}",
        {"assigneeAdapterOverrides": {"adapterConfig": {"model": "claude-opus-5", "env": {"case_dbd201c8_MARKER": {"type": "plain", "value": "probe-c"}}}}},
    )
    step("C.write(one env key)", st)
    if st not in (200, 201):
        out["verdict_C"] = f"UNREADABLE: probe C write returned {st}"
        call("PATCH", f"/api/issues/{issue_id}", {"assigneeAdapterOverrides": None})
        call("PATCH", f"/api/issues/{issue_id}", {"status": "done"})
        print(json.dumps(out, indent=2))
        sys.exit(5)
    st, issue = call("GET", f"/api/issues/{issue_id}")
    c = override_of(issue) or {}
    env_c = c.get("env") or {}
    out["C_readback"] = c
    marker = (env_c.get("case_dbd201c8") or env_c.get("case_dbd201c8_MARKER") or {}).get("value")
    others = sorted(k for k in env_c if k != "case_dbd201c8_MARKER")
    out["verdict_C"] = (
        f"per-key MERGE: other keys survive ({len(others)})"
        if others
        else "env REPLACED wholesale: only the written key remains"
    )
    step("C.readback", st, out["verdict_C"] + f" marker={marker}")

    # cleanup: null the override, close the scratch card
    st, _ = call("PATCH", f"/api/issues/{issue_id}", {"assigneeAdapterOverrides": None})
    step("cleanup.null-override", st)
    st, _ = call("PATCH", f"/api/issues/{issue_id}", {"status": "done"})
    step("cleanup.close", st)
    out["scratch_issue"]["identifier"] = issue.get("identifier")

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
