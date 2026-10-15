"""Deterministic prefilters for the Tier-1 checks (case_8c53addb).

Rule: check paths and symbols deterministically first; send only the prose to
the model. Every function here is pure, $0, and hermetic (no network).
"""

import re

_DIFF_FILE_RE = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)
_HUNK_SYMBOL_RE = re.compile(r"^@@.*@@\s*(?:def |class |function |const |let |fn )?([\w.]+)",
                             re.MULTILINE)
_ADDED_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)
_TEST_NAME_RE = re.compile(r"def (test_\w+)|\btest\(['\"]([^'\"]+)['\"]|it\(['\"]([^'\"]+)['\"]")


def changed_files(diff_text):
    """Return the sorted list of paths touched by a unified diff."""
    return sorted(set(_DIFF_FILE_RE.findall(diff_text or "")))


def changed_symbols(diff_text):
    """Return sorted symbols named on hunk headers or added lines (best effort).

    Conservative by design: it may miss symbols (model pass still runs), but it
    never invents paths — every returned path comes from a diff --git line.
    """
    text = diff_text or ""
    symbols = set(_HUNK_SYMBOL_RE.findall(text))
    for line in _ADDED_RE.findall(text):
        match = re.match(r"\s*(?:def|class|function|fn)\s+(\w+)", line)
        if match:
            symbols.add(match.group(1))
    return sorted(s for s in symbols if s)


def changed_tests(diff_text):
    """Return sorted test names added or touched by the diff."""
    names = set()
    for match in _TEST_NAME_RE.finditer(diff_text or ""):
        names.add(next(g for g in match.groups() if g))
    return sorted(names)


def doc_sections_stale(doc_text, files, symbols):
    """Flag doc sections that name paths/symbols absent from the diff.

    Returns [(section_head, missing_token)]: sections whose truth the model
    must judge. Sections naming nothing from the diff need no model call.
    """
    findings = []
    current = None
    names = set(files) | set(symbols)
    for line in (doc_text or "").splitlines():
        head = re.match(r"^#{1,4}\s+(.*)$", line)
        if head:
            current = head.group(1).strip()
            continue
        if current is None:
            continue
        for raw in re.findall(r"[\w./-]*\w[\w./-]*", line):
            token = raw.rstrip(".,;:")
            base = token.split("/")[-1]
            if base in names or token in names:
                continue
            if re.fullmatch(r"[\w./-]+\.(py|mjs|js|ts|sh|md|yml|yaml|json)",
                              token):
                findings.append((current, token))
    seen = set()
    deduped = []
    for item in findings:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def ac_coverage(criteria, files, symbols, tests):
    """Map each acceptance criterion to a changed symbol plus a test name.

    Returns {criterion: (symbol_or_empty, test_or_empty)}. The ship rule is
    deterministic: 100% of criteria must map to a non-empty symbol AND a
    non-empty test, else the check holds.
    """
    _ = files  # files scope the diff; symbol+test evidence carries the mapping
    mapping = {}
    for criterion in criteria:
        words = set(re.findall(r"[A-Za-z_][\w.]*", criterion.lower()))
        symbol = next(
            (s for s in symbols if s.lower() in words or
             any(w in s.lower() for w in words if len(w) > 4)),
            "",
        )
        test = next(
            (t for t in tests if any(w in t.lower() for w in words if len(w) > 3)),
            "",
        )
        mapping[criterion] = (symbol, test)
    return mapping


def ac_all_mapped(mapping):
    """Deterministic 100%-mapped rule: every criterion has symbol AND test."""
    return bool(mapping) and all(s and t for s, t in mapping.values())


_CHANGE_HEAD_RE = re.compile(r"(?im)^#{1,4}\s*(what changed|changes|summary of changes)\b.*$")
_NEXT_HEAD_RE = re.compile(r"(?m)^#{1,4}\s")
_PATHISH_RE = re.compile(r"^(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,5}$|^[\w.-]+\.(?:py|mjs|cjs|js|ts|tsx|rs|sh|ya?ml|json|toml|md|sql)$")


def change_section(pr_body):
    """The PR body's "What Changed" section (empty when the template has none)."""
    body = pr_body or ""
    head = _CHANGE_HEAD_RE.search(body)
    if not head:
        return ""
    rest = body[head.end():]
    nxt = _NEXT_HEAD_RE.search(rest)
    return rest[:nxt.start()] if nxt else rest


def claim_gaps(pr_body, files, symbols=()):
    """Repo paths the "What Changed" section names in backticks that the diff lacks.

    Narrow on purpose: only backticked repo-relative paths in the change
    section, never URLs, ratios or prose. A path the body names as changed but
    the diff does not touch is a claim the diff cannot support (a doc that
    lists edits the commit does not contain). Context mentions elsewhere in the
    body are ignored. `symbols` is kept for API compatibility.
    """
    _ = symbols
    have = set(files) | {f.split("/")[-1] for f in files}
    gaps = set()
    for span in re.findall(r"`([^`\n]{3,120})`", change_section(pr_body)):
        token = span.strip().rstrip(".,;:")
        if "://" in token or token.startswith(("~", "/", "$")) or " " in token:
            continue
        if not _PATHISH_RE.match(token):
            continue
        if token in have or token.split("/")[-1] in have:
            continue
        gaps.add(token)
    return sorted(gaps)


_IDENT_RE = re.compile(
    r"`([^`\n]{3,60})`"                      # backticked spans
    r"|(--[a-z][\w-]{2,})"                    # CLI flags
    r"|\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b"       # ENV_VAR_STYLE
    r"|\b([a-z][a-z0-9]*_[a-z0-9_]+)\b"       # snake_case
    r"|\b([a-z]+[A-Z][A-Za-z0-9]*)\b"         # camelCase
    r"|([\w./-]+\.(?:py|mjs|cjs|js|ts|tsx|rs|sh|ya?ml|json|toml|md|sql))\b")


_CODEY_RE = re.compile(r"[_./:=\-\d]|[a-z][A-Z]")


def is_specific(token):
    """True for code-like tokens (has _ . / - : = a digit or camelCase), not plain words."""
    return bool(_CODEY_RE.search(token))


def identifiers(text):
    """Code-like tokens in prose or a patch, lowercased (>= 4 chars)."""
    found = set()
    for groups in _IDENT_RE.findall(text or ""):
        for token in groups:
            token = token.strip().strip("`")
            if len(token) >= 4 and is_specific(token):
                found.add(token.lower())
    return found


def shared_identifiers(section_text, code_patch):
    """Identifiers a doc section shares with the code diff (the prefilter).

    Empty -> the diff says nothing about this section, so the model is not
    called and the unit counts as "did not fire". Substring matches count in
    one direction only (a doc token inside a longer diff token, e.g. a flag
    inside a quoted command line) so short generic words do not pass.
    """
    doc_ids = identifiers(section_text)
    diff_ids = identifiers(code_patch)
    diff_blob = (code_patch or "").lower()
    hits = {t for t in doc_ids if t in diff_ids}
    hits |= {t for t in doc_ids if len(t) >= 8 and t in diff_blob}
    # a backticked span is the doc naming code on purpose: match it as a whole word
    for span in re.findall(r"`([^`\n]{3,60})`", section_text or ""):
        span = span.strip()
        if not is_specific(span):
            continue
        span = span.lower()
        if len(span) >= 4 and re.search(
                r"(?<![\w])" + re.escape(span) + r"(?![\w])", diff_blob):
            hits.add(span)
    return sorted(hits)
