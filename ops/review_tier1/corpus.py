"""Eval-unit construction for the docs-vs-diff check (case_8c53addb).

Pure functions over diff text and doc text. A unit is one *changed* doc section
at one head; its gold is `block`/`hold` when a labelled reviewer finding sits in
that section at that head, else `none`. No network, no filesystem.
"""

import re

from deterministic import shared_identifiers

_FILE_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
DOC_RE = re.compile(r"\.mdx?$")


def parse_diff(diff_text):
    """Return {path: {"status", "added_lines" (new-side numbers), "patch"}}."""
    files = {}
    current = None
    for line in (diff_text or "").splitlines():
        head = _FILE_RE.match(line)
        if head:
            current = {"status": "modified", "added_lines": set(), "patch": [line]}
            files[head.group(2)] = current
            new_line = None
            continue
        if current is None:
            continue
        current["patch"].append(line)
        if line.startswith("new file mode"):
            current["status"] = "added"
        elif line.startswith("deleted file mode"):
            current["status"] = "deleted"
        hunk = _HUNK_RE.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if new_line is None or line.startswith("\\"):
            continue  # before the first hunk, or a "\ No newline" marker
        if line.startswith("+") and not line.startswith("+++"):
            current["added_lines"].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    for item in files.values():
        item["patch"] = "\n".join(item["patch"])
    return files


def sections(doc_text):
    """Split a markdown doc into sections by heading (1-based inclusive lines)."""
    lines = (doc_text or "").splitlines()
    out = []
    start, heading = 1, "(preamble)"
    in_fence = False
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        head = None if in_fence else _HEADING_RE.match(line)
        if head:
            if number > start or heading != "(preamble)":
                out.append((heading, start, number - 1))
            start, heading = number, head.group(2)
    out.append((heading, start, len(lines)))
    return [{"heading": h, "start": s, "end": e,
             "text": "\n".join(lines[s - 1:e])}
            for h, s, e in out if e >= s and "\n".join(lines[s - 1:e]).strip()]


def changed_sections(doc_text, added_lines):
    """Sections that contain at least one added line (the 'changed' sections)."""
    return [s for s in sections(doc_text)
            if any(s["start"] <= n <= s["end"] for n in added_lines)]


def code_context(files, cap_chars, skip_docs=True):
    """Concatenate non-doc patches within a char budget; return (text, truncated).

    The budget is shared fairly: each file gets cap/len(files) first, leftovers
    go to larger files, so one huge vendored file cannot starve the rest.
    """
    patches = [(p, f["patch"]) for p, f in sorted(files.items())
               if not (skip_docs and DOC_RE.search(p))]
    if not patches:
        return "", False
    total = sum(len(t) for _, t in patches)
    if total <= cap_chars:
        return "\n".join(t for _, t in patches), False
    share = cap_chars // len(patches)
    small = sum(len(t) for _, t in patches if len(t) <= share)
    big = [t for _, t in patches if len(t) > share]
    big_share = (cap_chars - small) // len(big) if big else 0
    out = []
    for _, text in patches:
        limit = len(text) if len(text) <= share else max(big_share, share)
        out.append(text[:limit])
    return "\n".join(out), True


def gold_for_sections(sects, findings):
    """Map findings {line, gold, id} onto sections; return [(section, gold, [ids])].

    gold is the strongest finding gold in the section (block > hold), else none.
    """
    result = []
    for sect in sects:
        hits = [f for f in findings if sect["start"] <= f["line"] <= sect["end"]]
        if any(f["gold"] == "block" for f in hits):
            gold = "block"
        elif hits:
            gold = "hold"
        else:
            gold = "none"
        result.append((sect, gold, [f["id"] for f in hits]))
    return result


def relevant_hunks(files, section_text, cap_chars):
    """Non-doc hunks that share an identifier with the doc section.

    Returns (text, truncated). Each kept hunk carries its file header so the
    model sees the path. Empty text means the diff does not touch anything the
    section names (the unit is not sent to a model).
    """
    kept, size, truncated = [], 0, False
    for path, info in sorted(files.items()):
        if DOC_RE.search(path):
            continue
        parts = re.split(r"(?m)^(?=@@ )", info["patch"])
        header, hunks = parts[0], parts[1:]
        for hunk in hunks:
            if not shared_identifiers(section_text, hunk):
                continue
            block = header.rstrip("\n") + "\n" + hunk
            if size + len(block) > cap_chars:
                truncated = True
                continue
            kept.append(block)
            size += len(block)
    return "\n".join(kept), truncated
