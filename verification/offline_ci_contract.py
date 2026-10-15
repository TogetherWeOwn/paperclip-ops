#!/usr/bin/env python3
"""Pin this repository's retained suites producers after the toolkit cutover.

The inventory is captured from the accepted pre-cutover workflow, not inferred
from the workflow being checked. This deliberately understands only our
six-space step layout; an unrecognised runnable step fails closed.
"""
from collections import Counter
import copy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "vendor/offline-ci-contract.json"
DEFAULT_WORKFLOW = ROOT / ".github/workflows/ci.yml"

PROPOSE_HINT = "python3 verification/offline_ci_contract.py --propose"
REVIEW_LINE = "Contract diff needs independent review before merging."


def jobs(text):
    section = text.split("\njobs:\n", 1)[1]
    headers = re.findall(r"(?m)^  (\S.*)$", section)
    if any(not header.startswith("#") and not re.fullmatch(r"[\w-]+:", header)
           for header in headers):
        raise ValueError("unrecognised job key layout")
    pairs = re.findall(r"(?ms)^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", section)
    if len({name for name, _ in pairs}) != len(pairs):
        raise ValueError("duplicate job key")
    return dict(pairs)


def job_definition(body):
    header = body.split("    steps:\n", 1)[0]
    return hashlib.sha256(definition(header).encode()).hexdigest()


def workflow_defaults_definition(text):
    header = text.split("\njobs:\n", 1)[0]
    inherited = "\n".join(re.findall(
        r"(?ms)^(?:env|defaults):[^\n]*\n.*?(?=^\S|\Z)", header))
    return hashlib.sha256(definition(inherited).encode()).hexdigest()


def restored_errors(root, contract):
    errors = []
    for dest, expected in contract["restored_files_sha256"].items():
        path = root / dest
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f"stale or missing restored input: {dest}")
    return errors


@lru_cache(maxsize=16)
def steps(body):
    return tuple(re.findall(r"(?ms)^      - .*?(?=^      - |\Z)", body))


@lru_cache(maxsize=1024)
def definition(block):
    # Ignore YAML comments/trailing separators, but retain inline script comments
    # and every executable field, including if/env/working-directory/shell.
    return "\n".join(line for line in block.splitlines()
                     if not re.match(r"^ {0,8}#", line)).rstrip("\n")


def record(block):
    name = re.search(r"(?m)^      - name: (.+)$", block)
    if not name:
        raise ValueError("runnable suites step has no explicit name")
    return {"name": name[1], "definition_sha256": hashlib.sha256(
        definition(block).encode()).hexdigest()}


def is_runnable(block):
    return re.search(r"(?m)^(?:      -|       ) run:", block) is not None


@lru_cache(maxsize=1024)
def run_body(block):
    match = re.search(r"(?ms)^        run: (.*?)(?=^        \S|\Z)", block)
    if not match:
        match = re.search(r"(?ms)^      - run: (.*?)(?=^        \S|\Z)", block)
    if not match:
        raise ValueError("unrecognised run layout")
    return "\n".join(line for line in match[1].splitlines()
                     if not line.lstrip().startswith("#"))


@lru_cache(maxsize=1024)
def run_signature(block):
    body = run_body(block)
    lines = body.splitlines()
    if lines and lines[0].strip() in ("|", "|-", "|+", ">", ">-", ">+"):
        body = textwrap.dedent("\n".join(lines[1:]))
    return hashlib.sha256(body.strip().encode()).hexdigest()


@lru_cache(maxsize=16)
def default_directory(body, indent):
    defaults = re.search(r"(?ms)^" + indent + r"defaults:\n(.*?)(?=^" + indent + r"\S|\Z)", body)
    directory = re.search(r"(?m)^" + indent + r"    working-directory: (.+)$",
                          defaults[1] if defaults else "")
    return directory[1] if directory else None


def working_directory(block, job, workflow_default=None):
    step = re.search(r"(?m)^        working-directory: (.+)$", block)
    value = step[1] if step else default_directory(job, "    ") or workflow_default or "."
    return str(Path(value.strip().strip("\"'")))


def inventory(text):
    result = []
    counts = Counter()
    for block in steps(jobs(text)["suites"]):
        if is_runnable(block):
            item = record(block)
            counts[item["name"]] += 1
            item["id"] = f'{item["name"]}:{counts[item["name"]]}'
            result.append(item)
    return result


def matched_blocks(text, expected):
    # Occurrence IDs belong to the accepted baseline. Removing occurrence 1
    # must not renumber a different retained package's occurrence 2.
    available = {}
    for item in expected:
        available.setdefault((item["name"], item["definition_sha256"]), []).append(item["id"])
    matched, extras = {}, []
    for block in steps(jobs(text)["suites"]):
        if not is_runnable(block):
            continue
        item = record(block)
        ids = available.get((item["name"], item["definition_sha256"]), [])
        if ids:
            matched[ids.pop(0)] = block
        else:
            extras.append(item["name"])
    return matched, extras


def validate(text, contract):
    errors = []
    try:
        bodies = jobs(text)
        for name, body in bodies.items():
            parsed = sum(is_runnable(block) for block in steps(body))
            # A reformatted/new final-line step must not disappear from the
            # inventory merely because our restricted parser did not see it.
            regions = re.split(r"(?m)^    steps:\n", body, maxsplit=1)
            raw = len(re.findall(r"(?m)^ {0,10}(?:- )?run:", regions[-1] if len(regions) == 2 else ""))
            if raw != parsed:
                errors.append(f"unrecognised runnable step layout: {name}")
        if job_definition(bodies["suites"]) != contract["suites_job_sha256"]:
            errors.append("retained suites job policy changed")
        if workflow_defaults_definition(text) != contract["workflow_defaults_sha256"]:
            errors.append("retained workflow defaults or environment changed")
        expected = contract["retained"]
        removed = contract["removed"]
        baseline = contract["baseline_ids"]
        if contract.get("schema_version") != 1 or len(set(baseline)) != len(baseline):
            errors.append("invalid accepted inventory schema")
        for item in expected:
            if not isinstance(item.get("definition_sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", item["definition_sha256"]):
                errors.append("retained producer lacks a valid definition digest")
        wanted = {r["id"]: r["definition_sha256"] for r in expected}
        removed_names = {r["id"] for r in removed}
        if len(wanted) != len(expected) or len(removed_names) != len(removed):
            errors.append("duplicate contract step identity")
        if set(wanted) & removed_names or set(baseline) != set(wanted) | removed_names:
            errors.append("retained/removed partition differs from accepted inventory")
        present, extras = matched_blocks(text, expected)
        for name in wanted:
            if name not in present:
                errors.append(f"retained producer missing or changed: {name}")
        for name in extras:
            errors.append(f"unapproved or changed producer: {name} "
                          f"(draft the pin with {PROPOSE_HINT})")
        # Check all job run steps too: moving/renaming a duplicate must not
        # escape by leaving the suites job. Comments are not invocations.
        runnable = [(block, body) for body in bodies.values()
                    for block in steps(body) if is_runnable(block)]
        invocations = "\n".join(run_body(block) for block, _ in runnable)
        workflow_default = default_directory(text.split("\njobs:\n", 1)[0], "")
        signatures = {(working_directory(block, body, workflow_default), run_signature(block))
                      for block, body in runnable}
        for removed_step in removed:
            digest = removed_step.get("run_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                errors.append(f"removed producer lacks run digest: {removed_step['name']}")
            if (removed_step["working_directory"], digest) in signatures:
                errors.append(f"portable producer reintroduced: {removed_step['name']}")
            for field in ("toolkit_commands", "forbidden_inputs"):
                values = removed_step.get(field)
                if (not isinstance(values, list) or
                        (field == "toolkit_commands" and not values) or
                        any(not isinstance(value, str) or not value for value in values)):
                    errors.append(f"removed producer lacks {field}: {removed_step['name']}")
            if not removed_step.get("evidence"):
                errors.append(f"removed producer lacks evidence: {removed_step['name']}")
            for command in removed_step["forbidden_inputs"]:
                pattern = r"(?<![\w.-])(?:\./)?" + re.escape(command) + r"(?![\w.-])"
                if re.search(pattern, invocations):
                    errors.append(f"portable input reintroduced: {command}")
        pin = contract["toolkit"]
        rows = (ROOT / "vendor/toolkit-pins.txt").read_text().splitlines()
        if f"{pin['tag']} {pin['commit']} {pin['sha256']}" not in rows:
            errors.append("immutable toolkit commit/checksum pin changed")
        manifest_text = (ROOT / "vendor/toolkit-files.txt").read_text()
        if hashlib.sha256(manifest_text.encode()).hexdigest() != contract["manifest_sha256"]:
            errors.append("reviewed toolkit destination/source mappings changed")
        manifest = manifest_text.splitlines()
        allowlist = (ROOT / "vendor/toolkit-tracked-allowlist.txt").read_text().splitlines()
        restored = contract["restored_files_sha256"]
        destinations = {line.split("|", 1)[0] for line in manifest if line}
        tracked = {line for line in allowlist if line and not line.startswith("#")}
        if set(restored) != destinations - tracked or any(
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for digest in restored.values()):
            errors.append("restored input hashes differ from reviewed manifest closure")
        for item in removed:
            inputs = item.get("input_paths")
            if (not isinstance(inputs, list) or not inputs
                    or any(not isinstance(path, str) or path not in restored for path in inputs)):
                errors.append(f"removed producer lacks immutable restored input closure: {item['name']}")
        exceptions = sorted(line for line in manifest if line.count("|") == 2)
        compatibility = contract.get("private_compatibility", {})
        if exceptions != sorted(compatibility.get("entries", [])):
            errors.append("private compatibility exceptions differ from reviewed evidence")
        if compatibility:
            if (f"{compatibility['tag']} {compatibility['commit']} "
                    f"{compatibility['sha256']}") not in rows:
                errors.append("private compatibility archive pin changed")
            if any(line.rsplit("|", 1)[-1] != compatibility["tag"] for line in exceptions):
                errors.append("private compatibility entry uses an unreviewed archive")
        default = (ROOT / "scripts/sync-toolkit.sh").read_text()
        if f'TAG="${{TOOLKIT_TAG:-{pin["tag"]}}}"' not in default:
            errors.append("default toolkit tag differs from cutover evidence")
    except (KeyError, TypeError, ValueError, IndexError) as error:
        errors.append(f"unreadable offline CI contract: {error}")
    return errors


def _step_id_parts(step_id):
    match = re.fullmatch(r"(.*):(\d+)", step_id)
    if not match:
        raise ValueError(f"malformed contract step identity: {step_id}")
    return match[1], int(match[2])


def compute_proposal(text, contract, allow_changed=()):
    """Draft the contract edits that pin unpinned suites producers.

    Returns (new_contract, notes, errors). On success new_contract is a
    copy of contract with the edits applied and notes describes each edit;
    when there is nothing to do both notes and errors are empty and
    new_contract is None. On refusal new_contract is None and errors says
    why. This never touches the workflow text or any file.
    """
    try:
        bodies = jobs(text)
        suites = bodies["suites"]
        expected = contract["retained"]
        removed = contract["removed"]
        baseline = contract["baseline_ids"]
    except (KeyError, TypeError, ValueError, IndexError) as error:
        return None, [], [f"unreadable offline CI contract: {error}"]
    try:
        current_job = job_definition(suites)
        current_defaults = workflow_defaults_definition(text)
    except (ValueError, IndexError) as error:
        return None, [], [f"unreadable offline CI contract: {error}"]
    # A step pin never moves these: job_definition hashes only the job
    # header above "    steps:", so new steps leave both digests alone. A
    # drift here is a policy change, not a pin, and needs a hand-reviewed
    # edit rather than a proposal.
    if current_job != contract.get("suites_job_sha256"):
        return None, [], ["retained suites job policy changed "
                          "(--propose pins producers only; "
                          "policy hashes need a hand-reviewed edit)"]
    if current_defaults != contract.get("workflow_defaults_sha256"):
        return None, [], ["retained workflow defaults or environment changed "
                          "(--propose pins producers only; "
                          "policy hashes need a hand-reviewed edit)"]
    try:
        available = {}
        for item in expected:
            available.setdefault((item["name"], item["definition_sha256"]),
                                 []).append(item["id"])
        unmatched = []
        for block in steps(suites):
            if not is_runnable(block):
                continue
            rec = record(block)
            ids = available.get((rec["name"], rec["definition_sha256"]), [])
            if ids:
                ids.pop(0)
            else:
                unmatched.append((block, rec))
        missing = [step_id for ids in available.values() for step_id in ids]
    except (ValueError, IndexError) as error:
        return None, [], [f"unreadable offline CI contract: {error}"]
    # A producer listed under removed is never proposed for re-adding, even
    # under a new step name: refuse on the portable signature or on a
    # forbidden input, the same two signals validate() enforces.
    try:
        workflow_default = default_directory(text.split("\njobs:\n", 1)[0], "")
        refuse = []
        for block, rec in unmatched:
            signature = (working_directory(block, suites, workflow_default),
                         run_signature(block))
            body = run_body(block)
            for gone in removed:
                if (gone.get("working_directory"), gone.get("run_sha256")) == signature:
                    refuse.append(
                        f"removed producer reintroduced: {rec['name']} matches "
                        f"removed {gone['id']} "
                        "(--propose never re-adds a removed producer)")
                for command in gone.get("forbidden_inputs", []):
                    pattern = (r"(?<![\w.-])(?:\./)?" + re.escape(command) +
                               r"(?![\w.-])")
                    if re.search(pattern, body):
                        refuse.append(
                            f"portable input reintroduced: {command} "
                            f"(removed {gone['id']}; "
                            "--propose never re-adds a removed producer)")
        if refuse:
            return None, [], sorted(set(refuse))
    except (ValueError, IndexError) as error:
        return None, [], [f"unreadable offline CI contract: {error}"]
    # Occurrence IDs belong to the accepted baseline: a new occurrence takes
    # max+1 for its name so nothing already pinned renumbers.
    try:
        peak = Counter()
        for step_id in list(baseline) + [r["id"] for r in removed]:
            name, count = _step_id_parts(step_id)
            peak[name] = max(peak[name], count)
    except (ValueError, TypeError) as error:
        return None, [], [f"unreadable offline CI contract: {error}"]
    missing_by_name = {}
    for step_id in missing:
        try:
            name, _ = _step_id_parts(step_id)
        except ValueError as error:
            return None, [], [f"unreadable offline CI contract: {error}"]
        missing_by_name.setdefault(name, []).append(step_id)
    allowed = set(allow_changed)
    for step_id in allowed:
        if step_id not in missing:
            return None, [], [f"no changed producer with id {step_id} "
                               "(--allow-changed names a retained id whose "
                               "definition changed)"]
    additions, updates, notes = [], {}, []
    for _, rec in unmatched:
        pending = missing_by_name.get(rec["name"], [])
        if pending:
            step_id = pending.pop(0)
            if step_id not in allowed:
                return None, [], [
                    f"changed producer {step_id} needs --allow-changed {step_id} "
                    "(a changed definition of a pinned step is never "
                    "proposed silently)"]
            updates[step_id] = rec["definition_sha256"]
        else:
            peak[rec["name"]] += 1
            step_id = f"{rec['name']}:{peak[rec['name']]}"
            if step_id in set(baseline) | {r["id"] for r in removed}:
                return None, [], [
                    f"removed producer reintroduced: {rec['name']} "
                    f"({step_id} is already in the accepted inventory; "
                    "--propose never re-adds a removed producer)"]
            additions.append({"name": rec["name"],
                              "definition_sha256": rec["definition_sha256"],
                              "id": step_id})
    leftover = [step_id for pending in missing_by_name.values()
                for step_id in pending]
    if leftover:
        return None, [], [
            f"retained producer missing or changed: {step_id}"
            for step_id in sorted(leftover)] + [
            "(a deletion needs a hand-reviewed contract edit; "
            "--propose only adds pins)"]
    if not additions and not updates:
        return None, [], []
    proposed = copy.deepcopy(contract)
    for step_id, digest in updates.items():
        for entry in proposed["retained"]:
            if entry["id"] == step_id:
                entry["definition_sha256"] = digest
    for entry in additions:
        proposed["baseline_ids"].append(entry["id"])
        proposed["retained"].append(entry)
    remaining = validate(text, proposed)
    if remaining:
        return None, [], remaining + [
            "(--propose only pins new suites steps; "
            "anything else needs a hand-reviewed edit)"]
    for entry in additions:
        notes.append(f"+ baseline_ids: {json.dumps(entry['id'])}")
        notes.append(f"+ retained: {json.dumps(entry, sort_keys=True)}")
    for step_id in sorted(updates):
        notes.append(f"~ retained {step_id}: refresh definition_sha256 to "
                     f"{updates[step_id]}")
    notes.append("suites_job_sha256: unchanged")
    notes.append("workflow_defaults_sha256: unchanged")
    return proposed, notes, []


USAGE = ("usage: offline_ci_contract.py [--check-restored] [workflow] "
         "| --propose|--write [--workflow PATH] [--contract PATH] "
         "[--allow-changed ID]... [workflow]")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    mode = "check"
    workflow = None
    contract_path = CONTRACT
    allow_changed = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--check-restored":
            if mode != "check" or workflow is not None:
                print("FAIL: --check-restored takes no other arguments")
                return 2
            mode = "check-restored"
        elif arg in ("--propose", "--write"):
            if mode != "check":
                print("FAIL: choose one of --propose or --write")
                return 2
            mode = arg[2:]
        elif arg == "--allow-changed":
            i += 1
            if i >= len(args):
                print("FAIL: --allow-changed needs a step id")
                return 2
            allow_changed.append(args[i])
        elif arg.startswith("--allow-changed="):
            allow_changed.append(arg.split("=", 1)[1])
        elif arg == "--contract":
            i += 1
            if i >= len(args):
                print("FAIL: --contract needs a path")
                return 2
            contract_path = Path(args[i])
        elif arg.startswith("--contract="):
            contract_path = Path(arg.split("=", 1)[1])
        elif arg == "--workflow":
            i += 1
            if i >= len(args):
                print("FAIL: --workflow needs a path")
                return 2
            if workflow is not None:
                print("FAIL: workflow given twice")
                return 2
            workflow = Path(args[i])
        elif arg.startswith("--workflow="):
            if workflow is not None:
                print("FAIL: workflow given twice")
                return 2
            workflow = Path(arg.split("=", 1)[1])
        elif arg in ("-h", "--help"):
            print(USAGE)
            return 0
        elif arg.startswith("-"):
            print(f"FAIL: unknown option: {arg}")
            print(USAGE)
            return 2
        else:
            if workflow is not None:
                print("FAIL: workflow given twice")
                return 2
            workflow = Path(arg)
        i += 1
    if allow_changed and mode not in ("propose", "write"):
        print("FAIL: --allow-changed needs --propose or --write")
        return 2
    if workflow is None:
        workflow = DEFAULT_WORKFLOW
    try:
        contract = json.loads(contract_path.read_text())
        if mode == "check-restored":
            errors = restored_errors(ROOT, contract)
        elif mode == "check":
            errors = validate(workflow.read_text(), contract)
        else:
            text = workflow.read_text()
            proposed, notes, errors = compute_proposal(
                text, contract, allow_changed)
            if not errors and notes:
                if mode == "write":
                    if workflow.resolve() == contract_path.resolve():
                        errors = ["refusing to read the contract as the workflow"]
                    else:
                        contract_path.write_text(
                            json.dumps(proposed, indent=2) + "\n")
                for note in notes:
                    print(note)
                print(REVIEW_LINE)
                return 0
            if not errors:
                return 0
    except (OSError, ValueError) as error:
        errors = [f"unreadable offline CI input: {error}"]
    for error in errors:
        print("FAIL: " + error)
    if not errors:
        label = ("restored input bytes match reviewed immutable archives"
                 if mode == "check-restored" else
                 "retained/removed offline CI producers at immutable toolkit pin")
        print("PASS: " + label)
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
