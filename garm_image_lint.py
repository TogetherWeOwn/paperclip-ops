#!/usr/bin/env python3
"""Structural GARM image lint. Standard library only; no execution or network."""
import json
from pathlib import Path
import re
import shlex
import sys
from typing import NamedTuple

PROFILE = Path(__file__).resolve().parent / "github-runner/garm/isolated-image-profile.json"
# --json emits exactly one object of this frozen shape; see docs/garm-image-lint.md.
JSON_SCHEMA = "garm-image-lint.v1"
ERROR_ACTIONS = {
    "INPUT_READ_ERROR": "restore_input",
    "INPUT_DECODE_ERROR": "encode_utf8",
    "PROFILE_READ_ERROR": "restore_profile",
    "PROFILE_INVALID": "fix_profile",
    "NO_INPUT": "provide_input",
}
HELP = ("Usage: garm_image_lint.sh [--json] [--] <Dockerfile> [...]\n"
        "Offline structural lint; no image or host operations.\n"
        "--json prints one garm-image-lint.v1 object on stdout (see docs/garm-image-lint.md).")
USAGE = "Usage: garm_image_lint.sh [--json] [--] <Dockerfile> [...]"
SECRET_NAME = re.compile(r"password|passwd|secret|token|api[-_]?key|_key", re.I)
PACKAGE_PIN = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?=[a-zA-Z0-9][a-zA-Z0-9.+:~_-]*")
KNOWN_INSTRUCTIONS = {"FROM", "RUN", "COPY", "ADD", "USER", "ENV", "ARG", "ENTRYPOINT",
                      "CMD", "SHELL", "WORKDIR", "LABEL", "EXPOSE", "VOLUME", "STOPSIGNAL",
                      "HEALTHCHECK", "MAINTAINER"}


class Instruction(NamedTuple):
    line: int
    op: str
    args: str
    comments: str


class FormatError(ValueError):
    pass


def instructions(text):
    """Join Docker continuations before inspecting any instruction's operands."""
    result = []
    comments = []
    pending = ""
    start = 0
    for number, physical in enumerate(text.splitlines(), 1):
        stripped = physical.strip()
        if stripped.startswith("#"):
            if re.match(r"#\s*escape\s*=", stripped, re.I) and not stripped.endswith("\\"):
                raise FormatError("unsupported Docker escape directive")
            if not pending:
                comments.append(stripped)
            continue
        if not stripped:
            continue
        if not pending:
            start = number
        # Docker continuations use a trailing backslash, not shell execution.
        continued = stripped.endswith("\\")
        pending += (stripped[:-1] + " ") if continued else stripped
        if continued:
            continue
        match = re.fullmatch(r"([A-Za-z]+)\s+(.+)", pending)
        if not match or match[1].upper() not in KNOWN_INSTRUCTIONS:
            raise FormatError("unknown or malformed Docker instruction")
        result.append(Instruction(start, match[1].upper(), match[2], "\n".join(comments)))
        comments = []
        pending = ""
    if pending:
        raise FormatError("unterminated Docker continuation")
    if not result:
        raise FormatError("empty Dockerfile")
    return result


def shell_commands(args):
    """Literal shell commands with their preceding connector, never evaluated.

    Opaque RUN forms are refused instead of silently dropping them from gates.
    This is a structural policy checker, not a general shell security analyser.
    """
    if args.startswith("[") or any(part in args for part in ("$", "`", "<<")):
        raise FormatError("RUN must use the supported literal shell form")
    lexer = shlex.shlex(args, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""  # only full-line Docker comments are comments
    words = list(lexer)
    result = []
    command = []
    before = ""
    for word in words:
        if word in {"&&", "||", ";", "|"}:
            if not command:
                raise FormatError("empty shell command")
            result.append((before, command))
            command = []
            before = word
        elif re.fullmatch(r"[;&|<>()]+", word):
            raise FormatError("unsupported shell operator")
        else:
            command.append(word)
    if not command:
        raise FormatError("empty shell command")
    result.append((before, command))
    return result


def executable(command):
    words = list(command)
    while words and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words.pop(0)
    if words and Path(words[0]).name == "env":
        words.pop(0)
        while words and (words[0].startswith("-") or "=" in words[0]):
            words.pop(0)
    return Path(words[0]).name if words else ""


def cleans_apt(command):
    return (executable(command) == "rm"
            and any(word in {"-rf", "-fr"} for word in command[1:])
            and "/var/lib/apt/lists/*" in command[1:])


def findings(text, distribution):
    problems = []

    def fail(code, line, detail):
        # Details are fixed strings: never echo operands which may be secrets.
        problems.append((code, line, detail))

    try:
        body = instructions(text)
    except (FormatError, ValueError):
        return [("GARM-FORMAT-09", 0, "unsupported or malformed Dockerfile syntax")]

    # MUTATION-ANCHOR-START: base gate
    bases = [inst for inst in body if inst.op == "FROM"]
    if len(bases) != 1:
        fail("GARM-BASE-01", 0, "want exactly one FROM")
    else:
        base = bases[0]
        words = shlex.split(base.args)
        reference = words[0] if words else ""
        repository, release = distribution.rsplit("-", 1)
        pin = re.fullmatch(r"([^:@\s]+):([0-9]+\.[0-9]+)(?:@sha256:([0-9a-fA-F]{64}))?", reference)
        if (len(words) != 1 or not pin or pin[1] != repository or pin[2] != release):
            fail("GARM-BASE-01", base.line, "FROM must match the isolated profile's repository and full release")
        elif not pin[3] and not re.search(r"#\s*pinned-at-build:\s*\S", base.comments, re.I):
            fail("GARM-BASE-01", base.line, "base needs a digest or an operator pinned-at-build justification")
    # MUTATION-ANCHOR-END

    # MUTATION-ANCHOR-START: user gate
    users = [inst for inst in body if inst.op == "USER"]
    if not users:
        fail("GARM-USER-02", 0, "no USER: image would run as root")
    else:
        user = users[-1]
        value = user.args.split(":", 1)[0]
        if (not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_-]*|[0-9]+)(?::(?:[A-Za-z_][A-Za-z0-9_-]*|[0-9]+))?", user.args)
                or value.lower() == "root" or (value.isdigit() and int(value) == 0)):
            fail("GARM-USER-02", user.line, "final USER must name a literal non-root account, before its optional group")
    # MUTATION-ANCHOR-END

    for inst in body:
        terms = re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", inst.args.lower())
        if any(term == "sudo" or term.startswith("sudo-") for term in terms):
            fail("GARM-PRIV-03", inst.line, "sudo refused on the isolated image")
        if any(term in {"docker", "docker.io", "docker_host", "moby-engine"}
               or term.startswith("docker-") for term in terms) or "/var/run/docker" in inst.args.lower():
            fail("GARM-PRIV-03", inst.line, "Docker client, daemon, socket or relay surface refused")
        if inst.op == "ADD":
            fail("GARM-COPY-05", inst.line, "ADD refused: use COPY")
        if inst.op in {"ENTRYPOINT", "CMD", "SHELL"}:
            try:
                value = json.loads(inst.args)
                if not isinstance(value, list) or not value or not all(isinstance(word, str) for word in value):
                    raise ValueError("not an exec array")
            except (ValueError, TypeError):
                fail("GARM-EXEC-06", inst.line, "use a valid nonempty JSON array of strings")
        if inst.op in {"ENV", "ARG"}:
            words = shlex.split(inst.args)
            if inst.op == "ARG":
                if len(words) != 1:
                    fail("GARM-FORMAT-09", inst.line, "ARG must declare one name with an optional default")
                names = [word.split("=", 1)[0] for word in words]
            elif words and "=" not in words[0]:  # legacy ENV name value
                names = words[:1]
                if len(words) < 2:
                    fail("GARM-FORMAT-09", inst.line, "ENV must declare a value")
            else:
                names = [word.split("=", 1)[0] for word in words]
                if not words or any("=" not in word for word in words):
                    fail("GARM-FORMAT-09", inst.line, "ENV assignments must each declare a value")
            if any(SECRET_NAME.search(name) for name in names):
                fail("GARM-SECRET-07", inst.line, "credential-shaped ENV/ARG name refused")
        if re.search(r"--(?:password|token|api-?key)(?:[=\s]|$)", inst.args, re.I):
            fail("GARM-SECRET-07", inst.line, "credential flag refused")
        if re.search(r":latest(?:[\s\"'@,\]]|$)", inst.args, re.I) or inst.op == "MAINTAINER":
            fail("GARM-FORMAT-09", inst.line, "floating latest tag or deprecated MAINTAINER refused")
        if inst.op != "RUN":
            continue
        try:
            commands = shell_commands(inst.args)
        except (FormatError, ValueError):
            fail("GARM-FORMAT-09", inst.line, "unsupported or malformed RUN syntax")
            continue
        downloaded = False
        for index, (before, command) in enumerate(commands):
            name = executable(command)
            if before != "|":
                downloaded = False
            if name in {"curl", "wget"}:
                downloaded = True
            elif downloaded and before == "|" and name in {"sh", "bash", "dash", "zsh"}:
                fail("GARM-FETCH-08", inst.line, "piped remote installer refused: COPY verified artifacts")
            if name not in {"apt-get", "apt"}:
                continue
            if any(word in {"upgrade", "dist-upgrade", "full-upgrade"} for word in command[1:]):
                fail("GARM-APT-04", inst.line, "apt upgrades float the base: rebuild instead")
            if "install" not in command[1:]:
                continue
            position = command.index("install")
            options = command[1:position] + command[position + 1:]
            if "--no-install-recommends" not in options or "--install-recommends" in options:
                fail("GARM-APT-04", inst.line, "each install needs --no-install-recommends")
            packages = [word for word in command[position + 1:] if not word.startswith("-")]
            if not packages or any(not PACKAGE_PIN.fullmatch(word) for word in packages):
                fail("GARM-APT-04", inst.line, "each apt package needs a literal exact =version pin")
            if not any(cleans_apt(later) for _, later in commands[index + 1:]):
                fail("GARM-APT-04", inst.line, "each install layer must clean /var/lib/apt/lists/* after installing")
    return problems


def parse_args(argv):
    """Split flags from operands. Returns (json_mode, operands, error) with
    error "help" for human help or None; raises ValueError for an unknown
    flag so the caller can refuse before any report construction.
    --help/-h wins over --json; -- ends flag parsing; a later bare --, a
    lone - and values not starting with - are operands."""
    prefix = argv[:argv.index("--")] if "--" in argv else argv
    if any(arg in ("-h", "--help") for arg in prefix):
        return False, [], "help"
    json_mode = False
    operands = []
    parsing = True
    for arg in argv:
        if parsing and arg == "--":
            parsing = False
        elif parsing and arg == "--json":
            json_mode = True
        elif parsing and arg.startswith("-") and arg != "-":
            raise ValueError("unrecognized option")
        else:
            operands.append(arg)
    return json_mode, operands, None


def load_profile():
    """Return (distribution, None) or (None, stable error code). A missing
    or unreadable profile differs from decodable-but-invalid content."""
    try:
        raw = PROFILE.read_bytes()
    except OSError:
        return None, "PROFILE_READ_ERROR"
    try:
        # Undecodable bytes are invalid content, not a read failure.
        distribution = json.loads(raw.decode("utf-8"))["base"]["distribution"]
        if not isinstance(distribution, str) or not re.fullmatch(r"[a-z0-9]+-[0-9]+\.[0-9]+", distribution):
            raise ValueError("invalid profile base")
    except (ValueError, KeyError, TypeError):
        return None, "PROFILE_INVALID"
    return distribution, None


def scan_operands(operands, distribution):
    """Read each operand occurrence in argv order and evaluate the same nine
    rules. Returns (events, evaluated, refused); events are
    ("finding", code, file, line, message) sorted per occurrence by
    (line, code) or ("error", code, file, 0, message), all in argv order, so
    both output modes consume one stream. Evaluated and refused are the raw
    operand strings and partition the occurrences. Read/decode refusals are
    errors, never GARM-FORMAT-09 findings."""
    events = []
    evaluated = []
    refused = []
    for path in operands:
        try:
            text = Path(path).read_bytes()
        except OSError:
            events.append(("error", "INPUT_READ_ERROR", path, 0, "unreadable file"))
            refused.append(path)
            continue
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            events.append(("error", "INPUT_DECODE_ERROR", path, 0, "file is not valid UTF-8"))
            refused.append(path)
            continue
        try:
            problems = findings(text, distribution)
        except (ValueError, TypeError):
            # Same fail-closed refusal findings() already applies internally.
            problems = [("GARM-FORMAT-09", 0, "malformed instruction operands")]
        evaluated.append(path)
        for code, line, detail in sorted(problems, key=lambda problem: (problem[1], problem[0])):
            events.append(("finding", code, path, line, detail))
    return events, evaluated, refused


def emit_json(operands, evaluated, refused, events, exit_code):
    """One garm-image-lint.v1 object on stdout; nothing else is printed.
    Fixed insertion order and ensure_ascii make repeat bytes identical."""
    complete = bool(evaluated) and not refused
    found = [event for event in events if event[0] == "finding"]
    report = {
        "schema": JSON_SCHEMA,
        "exit_code": exit_code,
        "scan_complete": complete,
        "measurement_state": "complete" if complete else "partial" if evaluated else "not_scanned",
        "requested_files": list(operands),
        "evaluated_files": list(evaluated),
        "refused_files": list(refused),
        "counts": {
            "requested": len(operands),
            "evaluated": len(evaluated),
            "refused": len(refused),
            "findings": len(found),
        },
        "findings": [
            {"code": code, "file": path, "line": line, "message": message,
             "retryable": False, "next_action": "fix_input"}
            for _, code, path, line, message in found
        ],
        "errors": [
            {"code": code, "file": path, "line": line, "message": message,
             "retryable": False, "next_action": ERROR_ACTIONS[code]}
            for kind, code, path, line, message in events if kind == "error"
        ],
    }
    print(json.dumps(report, ensure_ascii=True, allow_nan=False))


def main(argv):
    try:
        json_mode, operands, error = parse_args(argv)
    except ValueError:
        # Parser boundary: refuse before any scan or report construction.
        print(USAGE, file=sys.stderr)
        return 2
    if error == "help":
        print(HELP)
        return 0
    if not operands:
        if json_mode:
            emit_json(operands, [], [], [("error", "NO_INPUT", None, 0, "no input files requested")], 2)
        else:
            print(USAGE, file=sys.stderr)
        return 2
    distribution, profile_error = load_profile()
    if profile_error is not None:
        if json_mode:
            # Global failure: every requested occurrence is refused unscanned.
            message = ("unreadable isolated profile" if profile_error == "PROFILE_READ_ERROR"
                       else "invalid isolated profile")
            emit_json(operands, [], operands, [("error", profile_error, str(PROFILE), 0, message)], 2)
        else:
            print("RULE GARM-USAGE profile:0 unreadable or invalid isolated profile", file=sys.stderr)
        return 2
    events, evaluated, refused = scan_operands(operands, distribution)
    # Existing precedence: a rule failure (1) outranks a refusal (2).
    exit_code = (1 if any(event[0] == "finding" for event in events)
                 else 2 if events else 0)
    if json_mode:
        emit_json(operands, evaluated, refused, events, exit_code)
        return exit_code
    for kind, code, path, line, message in events:
        if kind == "finding":
            print(f"RULE {code} {path}:{line} {message}", file=sys.stderr)
        else:
            print(f"RULE GARM-USAGE {path}:0 {message}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
