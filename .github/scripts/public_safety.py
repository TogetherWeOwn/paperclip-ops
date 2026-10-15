#!/usr/bin/env python3
"""Fail closed on common private artifacts; gitleaks handles credential shapes.
This is a backstop, not a proof that arbitrary personal data is absent.
Never print matched values: report only paths and categories.
"""
import ipaddress
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IP = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
INTERNAL_URL = re.compile(r"https?://[^\s/'\"]+\.(?:internal|lan|local)(?=[:/'\"\s]|$)", re.I)
INTERNAL_CARD = re.compile(r"(?<![a-z0-9])(?:TOG|PAP)[-_]?\d+(?!\d)", re.I)
EXAMPLES = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
ARTIFACTS = {".pem", ".key", ".p12", ".bundle", ".sqlite", ".db", ".jsonl"}


def inspect(path):
    relative = path.relative_to(ROOT)
    if path.is_symlink():
        return ["symlink requires publication review"]
    reasons = []
    if path.suffix in ARTIFACTS or path.name.endswith(".env"):
        reasons.append("private artifact or live environment file")
    if re.search(r"(?:backup|restore).*\.json$", path.name, re.I):
        reasons.append("backup/restore dump")
    if INTERNAL_CARD.search(str(relative)):
        reasons.append("internal task identifier in path")
    raw = path.read_bytes()
    if b"\0" in raw:
        reasons.append("unreviewed binary payload")
        return reasons
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return reasons + ["non-UTF8 payload requires review"]
    if INTERNAL_CARD.search(text):
        reasons.append("internal task identifier")
    if INTERNAL_URL.search(text):
        reasons.append("internal endpoint")
    for match in IP.finditer(text):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if address.is_loopback or address.is_unspecified or any(address in net for net in EXAMPLES):
            continue
        if address.is_private or address.is_link_local:
            reasons.append("non-example private network address")
            break
    return sorted(set(reasons))


def main():
    files = 0
    findings = 0
    for path in sorted(ROOT.rglob("*")):
        if ".git" in path.relative_to(ROOT).parts or not (path.is_file() or path.is_symlink()):
            continue
        files += 1
        reasons = inspect(path)
        if reasons:
            findings += len(reasons)
            print(str(path.relative_to(ROOT)) + ": " + "; ".join(reasons))
    print(f"public-safety: {files} files; {findings} findings")
    return int(findings != 0)


if __name__ == "__main__":
    sys.exit(main())
