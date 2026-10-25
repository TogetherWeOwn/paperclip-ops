"""Deterministic evidence gate for review findings.

The model plans and cites; this module checks every citation against the
exact reviewed head and withholds anything that does not resolve. A finding
whose file:line cannot be read at the head, or whose quoted evidence is not
present on the cited line (whitespace-normalized), is not published: it is
counted and listed in the rationale as "unverified, withheld".

Pure, $0, hermetic (no network): file content at the head is injected via
`read_file`, so the bake-off harness passes blob reads pinned to the reviewed
commit and tests pass fakes. Finding shapes accepted: bake-off
(`file`/`line`/`evidence`) and production assessment (`path` alias, `quote` alias).
"""

REASONS = ("verified", "missing_file", "bad_line", "missing_evidence", "quote_mismatch", "malformed")


def normalize(text):
    """Collapse every whitespace run to one space and strip the ends."""
    return " ".join(str(text).split())


def source_lines(text):
    """Physical Git lines: only LF separates lines; a terminal LF adds none."""
    if not text:
        return []
    lines = text.split("\n")
    return lines[:-1] if text.endswith("\n") else lines


def _clean_path(value):
    """Exact repo-relative path, or None; never rewrite a published citation.

    Absolute paths, parent escapes, non-canonical paths and non-strings fail
    closed before `read_file` is called.
    """
    if not isinstance(value, str) or value != value.strip() or "\\" in value or "\0" in value:
        return None
    path = value
    if not path or path.startswith("/") or path in (".", ".."):
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def _clean_line(value):
    """1-based line number, or None when it is not a usable int."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _clean_quote(finding):
    """Quoted evidence, or None when the finding carries no quote.

    Only `evidence`/`quote` count as quotes; `rationale` is reasoning, not
    a citation, and is never matched against the file.
    """
    for key in ("evidence", "quote"):
        quote = finding.get(key)
        if isinstance(quote, str) and quote.strip():
            return quote
    return None


def verify(finding, read_file):
    """Return (ok, reason) for one finding against the reviewed head.

    `read_file(path)` returns the file text at the exact reviewed head, or
    None when the path has no readable text there (missing, deleted, binary).
    """
    if not isinstance(finding, dict):
        return False, "malformed"
    get = finding.get
    path = _clean_path(get("path", get("file")))
    if path is None:
        return False, "missing_file"
    try:
        text = read_file(path)
    except Exception:
        return False, "missing_file"
    if not isinstance(text, str):
        return False, "missing_file"
    line = _clean_line(get("line"))
    lines = source_lines(text)
    if line is None or line > len(lines):
        return False, "bad_line"
    quote = _clean_quote(finding)
    if quote is None:
        return False, "missing_evidence"
    if normalize(quote) not in normalize(lines[line - 1]):
        return False, "quote_mismatch"
    return True, "verified"


def gate_findings(findings, head_sha, read_file):
    """Split findings into verified/withheld; count the refusals.

    Returns the verified findings (original dicts, order kept), the withheld
    ones with reasons, the refusal count and rate (the talk's "citation
    refusal rate"), and rationale lines listing each withheld finding.
    Never mutates the input list or its dicts.
    """
    verified, withheld, rationale = [], [], []
    for finding in findings or []:
        ok, reason = verify(finding, read_file)
        if ok:
            verified.append(finding)
            continue
        if isinstance(finding, dict):
            get = finding.get
            entry = {"file": get("path", get("file")), "line": get("line"), "reason": reason}
        else:
            entry = {"file": None, "line": None, "reason": reason}
        withheld.append(entry)
        rationale.append(f"unverified, withheld: {entry['file']}:{entry['line']} ({reason})")
    total = len(verified) + len(withheld)
    return {
        "head": head_sha,
        "total": total,
        "verified": verified,
        "withheld": withheld,
        "refusal_count": len(withheld),
        "refusal_rate": round(len(withheld) / total, 3) if total else 0.0,
        "rationale": rationale,
    }
