#!/usr/bin/env python3
"""Run ONE reviewer model on ONE benchmark case through the codex CLI and CLIProxy.

This is the same transport the review-bot reviewer agent will use
(`codex_local` + the `cliproxy` provider overrides in
docs/review-bot/reviewer-agent-provisioning.md), so a model that cannot complete
a case here will not complete it in the bot either.

  CLIPROXY_KEY_ENV=ANTHROPIC_AUTH_TOKEN python3 run_review.py \\
      --repo /path/to/full/clone --case dataset/cases.json --case-id r-0286 \\
      --model gpt-6.1-sol --effort medium --out results/

The key is read from an environment variable NAME given by --key-env and passed
to the child only as OPENAI_API_KEY. It never appears in argv, a log or a result.
Nothing here writes to the repo under test: the controller builds inline input
from a detached worktree. The CLI runs in a separate Landlock-confined private
directory with an empty CODEX_HOME; both directories are removed afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops" / "review_tier1"))
import evidence_gate

from hygiene import EFFORTS

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_BASE = "http://cliproxy:8317/v1"
MAX_DIFF_CHARS = 150_000
MAX_CONTEXT_CHARS = 90_000
MAX_FILE_CHARS = 40_000

# label -> served CLIProxy id, and the Keeper lane family its quota is drawn from.
CANDIDATES = {
    "GPT-6.1 Sol": ("gpt-6.1-sol", "codex"),
    "Astra": ("gpt-6-astra", "codex"),
    "Codex (auto-review)": ("codex-auto-review", "codex"),
    "Sonnet 5.5": ("claude-sonnet-5-5", "claude"),
    "Opus 5.5": ("claude-opus-5-5", "claude"),
    "Muse 1.3": ("muse-spark-1.3", "meta"),
    "GLM-5.3": ("glm-5.3", "zai"),
}


def sh(*args: str, cwd: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def build_prompt(template: str, title: str, body: str, files: list[str], diff: str, context: str = "") -> str:
    """Single-pass substitution: values are never re-scanned for placeholders, and
    braces in the template or the diff are left alone (so not str.format)."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n[diff truncated at {MAX_DIFF_CHARS} characters; read the files directly]\n"
    values = {"title": title, "body": body or "(none)", "files": "\n".join(files), "diff": diff,
              "context": context or "(none)"}
    return re.sub(r"\{(title|body|files|diff|context)\}", lambda m: values[m.group(1)], template)


def read_git_file(cwd: str, path: str, head_sha: str = "HEAD") -> str | None:
    """Read a text blob at the revision, never tree listings or submodules.

    Decode bytes without universal-newline translation: only LF delimits Git
    source lines, including when a blob contains CR or Unicode separators.
    """
    proc = subprocess.run(["git", "cat-file", "blob", f"{head_sha}:{path}"],
                          cwd=cwd, capture_output=True)
    return proc.stdout.decode("utf-8", errors="replace") \
        if proc.returncode == 0 and b"\0" not in proc.stdout else None


def read_git_blob_at(cwd: str, path: str, head_sha: str) -> str | None:
    """Blob read pinned to an immutable head, for review context and evidence.

    run_case must resolve files at the reviewed commit even if the worktree
    HEAD moves mid-review, so the head is a required argument here rather
    than an optional default. A non-bytes transport result fails closed to
    None (unreadable -> the finding is withheld), never to a crash or a
    guess. Behavior on real Git output is identical to read_git_file.
    """
    proc = subprocess.run(["git", "cat-file", "blob", f"{head_sha}:{path}"],
                          cwd=cwd, capture_output=True)
    out = proc.stdout
    if proc.returncode != 0 or not isinstance(out, bytes) or b"\0" in out:
        return None
    return out.decode("utf-8", errors="replace")


def number_lines(text: str) -> str:
    return "\n".join(f"{i:>5}  {line}" for i, line in enumerate(evidence_gate.source_lines(text), 1))


def build_context(read_file, files: list[str]) -> str:
    """Numbered merged versions of the changed files, capped per file and in total.

    The sandbox that would let a model read the repo itself is not available in an
    agent container (bwrap cannot create a user namespace), so every arm gets the
    same inline context instead. `read_file(path)` returns text or None (deleted,
    binary, unreadable).
    """
    parts, used = [], 0
    for path in files:
        text = read_file(path)
        if text is None:
            continue
        note = ""
        if len(text) > MAX_FILE_CHARS:
            text, note = text[:MAX_FILE_CHARS], f"\n[file truncated at {MAX_FILE_CHARS} characters]"
        block = f"--- {path} ---\n{number_lines(text)}{note}\n"
        if used + len(block) > MAX_CONTEXT_CHARS:
            parts.append(f"--- {path} --- [omitted: context budget of {MAX_CONTEXT_CHARS} characters reached]\n")
            continue
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def codex_args(model: str, effort: str, schema: str, last_message: str) -> list[str]:
    cfg = [
        'model_provider="cliproxy"',
        'model_providers.cliproxy.name="cliproxy"',
        f'model_providers.cliproxy.base_url="{PROXY_BASE}"',
        'model_providers.cliproxy.env_key="OPENAI_API_KEY"',
        'model_providers.cliproxy.wire_api="responses"',
        f'model_reasoning_effort="{effort}"',
    ]
    if effort not in EFFORTS:
        raise ValueError("reasoning effort must be explicitly pinned")
    args = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
            "-s", "read-only", "--json",
            "--output-schema", schema, "-o", last_message, "-m", model]
    for item in cfg:
        args += ["-c", item]
    return args + ["-"]


def parse_events(stdout: str) -> dict:
    """Token usage and any error items from `codex exec --json` JSONL."""
    usage: dict = {}
    errors: list[str] = []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            for key, val in ev["usage"].items():
                if isinstance(val, int):
                    usage[key] = usage.get(key, 0) + val
        elif ev.get("type") in ("turn.failed", "error"):
            errors.append(str(ev.get("error") or ev.get("message"))[:300])
    return {"usage": usage, "errors": errors}


def parse_review(text: str) -> tuple[dict | None, str | None]:
    """The final message must be one JSON object with the schema's required keys."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.index("\n") + 1:] if "\n" in text else text
    try:
        obj = json.loads(text)
    except ValueError as exc:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None, f"not JSON: {exc}"
        try:
            obj = json.loads(text[start:end + 1])
        except ValueError as exc2:
            return None, f"not JSON: {exc2}"
    if not isinstance(obj, dict):
        return None, "top level is not an object"
    score = obj.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 5:
        return None, "score missing or outside 0..5"
    findings = obj.get("findings")
    if not isinstance(findings, list):
        return None, "findings missing"
    for i, f in enumerate(findings):
        if not isinstance(f, dict) or f.get("severity") not in ("critical", "major", "minor") \
                or not isinstance(f.get("file"), str) or not isinstance(f.get("line"), int):
            return None, f"finding {i} malformed"
    return obj, None


def publish_gated_review(review: dict, head_sha: str, read_file) -> dict:
    """Strip findings that do not resolve at the exact reviewed head.

    Every published finding must cite a file:line readable at `head_sha` and
    supply non-blank evidence present on that line (whitespace-normalized).
    Withheld findings are not published: they are counted and listed in the
    returned record's rationale as "unverified, withheld". The refusal count
    and rate are the review's citation-refusal metric.
    """
    gated = evidence_gate.gate_findings(review.get("findings", []), head_sha, read_file)
    review["findings"] = gated["verified"]
    return {
        "head": gated["head"],
        "total": gated["total"],
        "refusal_count": gated["refusal_count"],
        "refusal_rate": gated["refusal_rate"],
        "withheld": gated["withheld"],
        "rationale": gated["rationale"],
    }


def prepare_worktree(repo: str, case: dict, dest: str) -> None:
    """Detached worktree whose HEAD is the commit under review.

    Real case: HEAD is the squash-merge commit. Planted case: the parent plus the
    PR's own files plus a mutation patch, committed, so `git show HEAD` is uniform.
    """
    if case["kind"] == "real":
        sh("git", "-C", repo, "worktree", "add", "--detach", dest, case["head_sha"])
        return
    sh("git", "-C", repo, "worktree", "add", "--detach", dest, case["head_sha"] + "^")
    sh("git", "checkout", case["head_sha"], "--", ".", cwd=dest)
    patch = os.path.join(HERE, "dataset", case["plant_patch"])
    sh("git", "apply", "--whitespace=nowarn", patch, cwd=dest)
    sh("git", "-c", "user.name=bakeoff", "-c", "user.email=bakeoff@example.invalid",
       "commit", "-q", "-a", "-m", case["title"] + "\n\n" + (case.get("body") or ""), cwd=dest)


def run_case(repo: str, case: dict, model: str, effort: str, key_env: str, timeout: int) -> dict:
    key = os.environ.get(key_env, "")
    if not key:
        raise SystemExit(f"environment variable {key_env} is empty")
    template = Path(HERE, "review_prompt.md").read_text(encoding="utf-8")
    scratch = tempfile.mkdtemp(prefix="bakeoff-")
    wt = os.path.join(scratch, "wt")
    try:
        prepare_worktree(repo, case, wt)
        # Planted cases commit a new head. Pin context and evidence to that
        # immutable snapshot before invoking the reviewer.
        head = sh("git", "rev-parse", "HEAD", cwd=wt).strip()
        diff = sh("git", "show", "--format=", "--patch", head, cwd=wt)
        files = sh("git", "show", "--format=", "--name-only", head, cwd=wt).split()
        context = build_context(lambda path: read_git_blob_at(wt, path, head), files)
        prompt = build_prompt(template, case["title"], case.get("body", ""), files, diff, context)
        runtime = os.path.join(scratch, "runtime")
        os.mkdir(runtime, mode=0o700)
        last = os.path.join(runtime, "last.json")
        schema = os.path.join(runtime, "output_schema.json")
        shutil.copyfile(os.path.join(HERE, "output_schema.json"), schema)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": runtime,
               "CODEX_HOME": os.path.join(runtime, "ch"), "TMPDIR": os.path.join(runtime, "tmp"),
               "OPENAI_API_KEY": key}
        os.makedirs(env["CODEX_HOME"])
        os.mkdir(env["TMPDIR"], mode=0o700)
        started = time.time()
        timed_out = False
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, "isolation.py"), runtime, "--",
                 *codex_args(model, effort, schema, last)],
                input=prompt, cwd=runtime, env=env, capture_output=True, text=True,
                errors="replace", timeout=timeout)
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, code, timed_out = (exc.stdout or b"").decode(errors="replace") \
                if isinstance(exc.stdout, bytes) else (exc.stdout or ""), None, True
            stderr = (exc.stderr or b"").decode(errors="replace") \
                if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        latency = round(time.time() - started, 1)
        events = parse_events(stdout)
        text = Path(last).read_text(encoding="utf-8") if os.path.exists(last) else ""
        review, parse_error = parse_review(text) if text else (None, "no final message")
        if not text and stderr:
            # A fail-closed launcher can exit before emitting any JSON events.
            # Keep its reason, never the credential or terminal control bytes.
            diagnostic = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stderr.replace(key, "[REDACTED]"))
            diagnostic = " ".join("".join(c if c.isprintable() else " " for c in diagnostic).split())
            if diagnostic:
                events["errors"].append(("launcher stderr: " + diagnostic)[:300])
        if review is not None:
            gate = publish_gated_review(review, head, lambda path: read_git_blob_at(wt, path, head))
        else:
            gate = None
        return {
            "case_id": case["id"], "model": model, "effort": effort,
            "input_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "isolation": "landlock-v1",
            "latency_s": latency, "exit_code": code, "timed_out": timed_out,
            "usage": events["usage"], "errors": events["errors"],
            "prompt_chars": len(prompt), "review": review, "parse_error": parse_error,
            "evidence_gate": gate,
            "raw_final": text if review is None else None,
        }
    finally:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt], capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--case", required=True, help="cases.json")
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default="medium", choices=EFFORTS)
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--key-env", default="CLIPROXY_KEY_ENV_VALUE",
                    help="NAME of the environment variable holding the CLIProxy key")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.rep < 1:
        ap.error("--rep must be a positive integer")
    cases = {c["id"]: c for c in json.load(open(args.case, encoding="utf-8"))["cases"]}
    if args.case_id not in cases:
        print(f"unknown case {args.case_id}", file=sys.stderr)
        return 2
    result = run_case(args.repo, cases[args.case_id], args.model, args.effort, args.key_env, args.timeout)
    result["rep"] = args.rep
    os.makedirs(args.out, exist_ok=True)
    safe = args.model.replace("/", "_")
    path = os.path.join(args.out, f"{args.case_id}__{safe}__{args.effort}__r{args.rep}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    n = len(result["review"]["findings"]) if result["review"] else None
    # The summary line tolerates results without gate metadata (the live
    # run_case always records evidence_gate; stubbed results may predate it).
    w = (result.get("evidence_gate") or {}).get("refusal_count")
    print(f"{args.case_id} {args.model} score={result['review']['score'] if result['review'] else None} "
          f"findings={n} withheld={w} latency={result['latency_s']}s tokens={result['usage']} -> {path}")
    return 0 if result["review"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
