#!/usr/bin/env python3
"""Fail closed on common private artifacts; gitleaks handles credential shapes.
This is a backstop, not a proof that arbitrary personal data is absent.
Never print matched values: report only paths and categories.
"""
import hashlib
import io
import ipaddress
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
IP = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
INTERNAL_URL = re.compile(r"https?://[^\s/'\"]+\.(?:internal|lan|local)(?=[:/'\"\s]|$)", re.I)
INTERNAL_CARD = re.compile(r"(?<![a-z0-9])(?:TOG|PAP)[-_]?\d+(?!\d)", re.I)
EXAMPLES = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
ARTIFACTS = {".pem", ".key", ".p12", ".bundle", ".sqlite", ".db", ".jsonl"}
# Reviewed archiver@7 stream fixtures, not arbitrary binary/archive exemptions.
# Every payload must equal the separately scanned public JSON fixture byte-for-byte.
PUBLIC_ZIP_FIXTURES = {
    "protection-rule/test/fixtures/staging-migrate-apply-claim.upload-artifact.zip": (
        "63b113e94c8952b5e709e75f3caddf6fe59f47c7aeeb0db08dee8c7f5a1ddb98",
        "staging-migrate-apply-claim.json", "staging-migrate-apply-claim.json"),
    "protection-rule/test/fixtures/staging-migrate-plan.upload-artifact.zip": (
        "7759abe812f7669d36bfe38a644616a946b363287ea24a46b4b412b06a00efe8",
        "staging-migrate-manifest.json", "staging-migrate-plan.json"),
}


def inspect(path):
    relative = path.relative_to(ROOT)
    if path.is_symlink():
        return ["symlink requires publication review"]
    reasons = []
    raw = path.read_bytes()
    # This exact file contains only the public Actions runner release URL,
    # version, archive name and digest. Any changed byte requires new review;
    # neither other .env paths nor arbitrary added variables are exempt.
    public_artifact_pin = (
        relative.as_posix() == "github-runner/actions-runner.env"
        and hashlib.sha256(raw).hexdigest()
        == "c2c84eb7eb28ce90ce6372c1e8102f4c9d0bfac9244b9b9ccc20d4edae282de2"
    )
    if path.suffix in ARTIFACTS or (path.name.endswith(".env") and not public_artifact_pin):
        reasons.append("private artifact or live environment file")
    if re.search(r"(?:backup|restore).*\.json$", path.name, re.I):
        reasons.append("backup/restore dump")
    if INTERNAL_CARD.search(str(relative)):
        reasons.append("internal task identifier in path")
    pin = PUBLIC_ZIP_FIXTURES.get(relative.as_posix())
    if pin and hashlib.sha256(raw).hexdigest() == pin[0]:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if archive.namelist() == [pin[1]]:
                    payload = archive.read(pin[1])
                    if payload == (path.parent / pin[2]).read_bytes():
                        raw = payload  # Continue ordinary text/privacy checks on the content.
        except (OSError, ValueError, zipfile.BadZipFile):
            pass  # Missing/mismatched public payload keeps the binary refusal below.
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
        # The well-known cloud metadata address is a protocol constant, not
        # tenant inventory. Security fixtures must retain its real deny target.
        if (address.is_loopback or address.is_unspecified
                or str(address) == "169.254.169.254"
                or any(address in net for net in EXAMPLES)):
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
