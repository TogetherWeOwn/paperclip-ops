#!/usr/bin/env python3
"""upstream_lint.py -- pre-push linter and maintainer memory for upstream PRs.

Every upstream PR is opened in the owner's name, so it has to be flawless
before the first byte leaves our control. This tool is the deterministic part
of that bar: it runs BEFORE the first push to any non-TogetherWeOwn repo and
again on the final head before `upstream_gate.sh ready`.

  lint     the pre-push linter. Fails closed.
  batch    the fewer-complete-PRs policy (overlap and open-PR budget).
  memory   per-repo maintainer memory: init | refresh | show | ingest.
  install-hook
           install the git pre-push hook into a clone.

CHECKS (lint). Every item is PASS, WARN (reported, never blocks), FAIL, or
REFUSED (could not be measured: never a pass):

  memory      a maintainer-memory file exists for the repo and parses
  title       Conventional Commits header (scope optional), at most 100 characters, no period
  commits     every commit subject is a Conventional Commits header (scope optional)
  branch      descriptive kebab-case, no internal ticket id
  signoff     DCO `Signed-off-by:` on every commit, or a recorded CLA
  template    every section of the upstream PR template is present and filled,
              every template checkbox is still there, none left unticked at
              `ready`; Verification names a runnable command
  internal    no internal ids, hosts, tailnet names, agent links or secrets in
              the title, body, branch, commit messages, file paths or added lines
              (localhost/RFC1918 count in the title+body prose only); a patch-less
              text file refuses
  tests       every behavior change ships a test in the same package with a real
              assertion; a patch-less test file refuses, never waives the gate
  budget      the diff stays inside the repo's size budget
  claims      no over-promising, puffery, shouting or emoji in the PR text
  rules       every rule recorded in the repo's maintainer memory
  advisory    past maintainer or Greptile feedback that touches this diff

EXIT CODES (same convention as upstream_gate.sh):
  0 pass   1 fail   2 refused (unmeasurable)   4 usage

SEAMS: GH_TOKEN, GH_API_URL (default https://api.github.com), GH_BIN (default
gh), UPSTREAM_MEMORY_DIR (default <this dir>/upstream-memory). Pure-offline
use needs none of them: `lint --layout DIR` reads pr.json, commits.json and
files.json, the same standard layout `upstream_gate.sh --fixtures` reads.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXIT_PASS, EXIT_FAIL, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2, 4
SCHEMA_VERSION = 1
# GitHub author_association values that speak for the maintainers of a repo.
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})
OWN_ORG = "togetherweown"

CC_TYPES = "feat|fix|perf|refactor|test|docs|build|ci|chore|revert|style|security"
CC_HEADER = re.compile(r"^(?:%s)(?:\([a-z0-9_./-]+\))?!?: \S" % CC_TYPES)
BRANCH_OK = re.compile(r"^(?:[a-z][a-z0-9]*/)?[a-z0-9][a-z0-9._-]*[a-z0-9]$")

# --- internal-reference patterns ------------------------------------------------
# PROSE (title, body): ids, hosts, secrets AND localhost/RFC1918. The upstream
# CONTRIBUTING rule is about prose ("localhost, private IP, or tailnet URLs
# pointing at your own instance"): a local dev address in a test or doc is
# legitimate, but the same address in the PR title or body points at the
# author's own instance.
# CODE-ISH (branch, commit messages, file paths, added diff lines): ids, hosts
# and secrets only; localhost and RFC1918 are legitimate there.
_ID_AND_HOST = [
    r"\bTOG-\d+", r"\bPAP-?\d+", r"\bPAPA-\d+", r"deploymentexample\.net",
    r"[\w-]+(?:\.[\w-]+)*\.ts\.net\b", r"agent://", r"/(?:PAP|TOG)/issues/",
    r"/paperclip/instances/", r"/paperclip/\.claude", r"paperclip-ops",
    r"workforce\.deploymentexample",
]
_SECRETS = [
    r"\bghp_[A-Za-z0-9]{30,}", r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bgh[ousr]_[A-Za-z0-9]{30,}", r"\bsk-(?:ant-|proj-|svcacct-)[A-Za-z0-9_-]{20,}", r"\bsk-[A-Za-z0-9]{40,}",
    r"\bAKIA[0-9A-Z]{16}\b", r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
]
_PROSE_ONLY = [
    r"\blocalhost\b", r"\b127\.0\.0\.1\b", r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    r"\b192\.168\.\d{1,3}\.\d{1,3}\b", r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
]
TEXT_PATTERNS = ([re.compile(p, re.I) for p in _ID_AND_HOST]
                 + [re.compile(p) for p in _SECRETS]
                 + [re.compile(p, re.I) for p in _PROSE_ONLY])
CODE_PATTERNS = [re.compile(p, re.I) for p in _ID_AND_HOST] + [re.compile(p) for p in _SECRETS]

# --- over-promising and tone ----------------------------------------------------
BANNED_CLAIMS = [
    (r"\bguarantee[ds]?\b", "guarantee"), (r"\b100\s*%", "100%"),
    (r"\bperfect(?:ly)?\b", "perfect"), (r"\bbullet-?proof\b", "bulletproof"),
    (r"\bflawless(?:ly)?\b", "flawless"), (r"\bseamless(?:ly)?\b", "seamless"),
    (r"\brobust(?:ness)?\b", "robust"), (r"\bblazing(?:ly)?\b", "blazing"),
    (r"\bzero (?:bugs|risk|regressions?)\b", "zero bugs/risk"),
    (r"\bno risk\b", "no risk"),
    (r"\bfully (?:tested|covered|verified|backward[- ]compatible)\b", "fully tested/covered"),
    (r"\ball edge cases\b", "all edge cases"), (r"\bproduction[- ]ready\b", "production-ready"),
    (r"\bwill never\b", "will never"), (r"\balways works?\b", "always works"),
    (r"\bworld[- ]class\b", "world-class"), (r"\bgame[- ]chang(?:er|ing)\b", "game-changing"),
    (r"\b(?:massive|dramatic|huge)ly?\b", "massive/dramatic"),
    (r"\bonce and for all\b", "once and for all"),
    (r"\b(?:completely|totally|entirely) (?:eliminates?|fix(?:es)?|solves?|removes?)\b",
     "completely eliminates/fixes"),
    (r"\bfixes all\b", "fixes all"), (r"\bnever again\b", "never again"),
]
NEGATOR = re.compile(r"\b(?:not|no|never|without|neither|nor|cannot)\b|n't\b", re.I)
NEGATION_PROOF = {"will never", "never again", "always works", "once and for all"}
WARN_TONE = [
    (r"\b(?:just|simply|obviously|clearly|trivial(?:ly)?)\b", "dismissive filler"),
    (r"\bI think\b|\bI believe\b|\bmaybe\b|\bprobably\b", "hedging"),
]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅❌]")
SHOUT = re.compile(r"\b[A-Z]{7,}\b")
SHOUT_OK = {"README", "CONTRIBUTING", "CHANGELOG", "ROADMAP", "LICENSE", "CODEOWNERS",
            "SECURITY", "WARNING", "DEFAULT"}

PLACEHOLDER = re.compile(
    r"^\s*[-*>]+[ \t]*$|\[Which subsystem|\[What problem|\[Why it|\[Which |\[What |\[Why |"
    r"\bTODO\b|\bTBD\b|This pull request \.\.\.|The benefit is \.\.\.", re.I | re.M)

MODEL_FAMILIES = [  # (family, regex on a lowercase model id or provider word)
    ("anthropic", r"claude|anthropic|opus|sonnet|haiku|fable"),
    ("openai", r"\bgpt|openai|codex|\bo[134]-|chatgpt|astra|luna|terra|\bsol\b"),
    ("muse", r"muse-spark|muse[-_]canary|space-bunny"),
    # No bare "muse" here on purpose: the word alone is ambiguous between
    # Meta's Muse and Muse Spark, and guessing Meta once misclassified a
    # muse-spark author as meta. Meta is proven by "meta" or "llama".
    ("meta", r"\bmeta\b|llama"),
    ("google", r"gemini|google|gemma"),
    ("xai", r"grok|\bxai\b"),
    ("zai", r"\bglm|z\.ai|zhipu"),
    ("moonshot", r"kimi|moonshot"),
    ("deepseek", r"deepseek"),
    ("alibaba", r"qwen|alibaba"),
    ("xiaomi", r"mimo|xiaomi"),
    ("mistral", r"mistral|mixtral"),
]
INTERNAL_MODEL_ALIASES = re.compile(r"\bmuse-canary\b|\bspace-bunny\b|\bunion-alpha\b", re.I)

BINARY_EXT = {"png", "jpg", "jpeg", "gif", "ico", "webp", "avif", "bmp", "pdf", "woff", "woff2",
              "ttf", "otf", "eot", "zip", "gz", "tgz", "mp4", "mov", "wasm", "icns"}
LOCK_NAMES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "poetry.lock",
              "go.sum", "uv.lock", "Gemfile.lock", "composer.lock", "bun.lockb", "npm-shrinkwrap.json"}
PACKAGE_DIRS = {"packages", "apps", "adapters", "plugins", "libs", "modules", "extensions", "crates"}

TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|e2e|spec|specs|__mocks__|fixtures)/|"
    r"\.(?:test|spec)\.[A-Za-z0-9]+$|_test\.(?:go|py|rs)$|(?:^|/)test_[^/]+\.py$|"
    r"(?:^|/)[^/]+Tests?\.(?:java|kt|cs|swift)$")
TEST_ASSERT = re.compile(
    r"\b(?:expect|assert\w*|should)\s*[.(]|\brequire\.[A-Z]\w*\s*\(|"
    r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(|"
    r"(?:^|[;:)]|\belse)[ \t]*assert[ \t]+(?![=,}])\S|"
    r"\{[ \t]*assert[ \t]+(?![=,}]|as\b|from\b)\S|\b(?=\w*assert)\w+!\s*\(|\bassert\s*<[^\n]*>\s*\(", re.M)
DOC_PATH = re.compile(r"\.(?:md|mdx|rst|txt|adoc)$|(?:^|/)docs?/|(?:^|/)(?:LICENSE|NOTICE|CHANGELOG)[^/]*$")
GENERATED_PATH = re.compile(
    r"(?:^|/)(?:dist|build|out|node_modules|coverage|__snapshots__|\.next|target)/|\.snap$|"
    r"\.min\.(?:js|css)$|\.map$|\.d\.ts$")
META_PATH = re.compile(
    r"(?:^|/)(?:\.gitignore|\.gitattributes|\.editorconfig|\.prettierrc[^/]*|\.prettierignore|"
    r"\.nvmrc|\.node-version|CODEOWNERS|dependabot\.yml)$|"
    r"^\.github/(?:ISSUE_TEMPLATE/|PULL_REQUEST_TEMPLATE)")
CI_PATH = re.compile(r"^\.github/(?:workflows|actions|scripts)/")


class Refused(Exception):
    """Something could not be measured. Never a pass."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def memory_dir() -> str:
    return os.environ.get("UPSTREAM_MEMORY_DIR") or os.path.join(HERE, "upstream-memory")


def memory_path(repo: str, mdir: str | None = None) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Refused("not an owner/repo name: %r" % repo)
    return os.path.join(mdir or memory_dir(), repo.replace("/", "__") + ".json")


def load_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise Refused("cannot read %s: %s" % (os.path.basename(path), exc))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_sections(body: str) -> "dict[str, str]":
    """Level-2 sections in order. Text before the first heading is dropped."""
    out: dict[str, str] = {}
    name = None
    buf: list[str] = []
    in_fence = False
    for line in (body or "").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if name is not None:
                out[name] = "\n".join(buf).strip()
            name, buf = m.group(1).strip(), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        out[name] = "\n".join(buf).strip()
    return out


def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text or "", flags=re.S)


def prose_lines(text: str) -> "list[str]":
    """Prose with fenced code, inline code and HTML comments removed."""
    t = strip_comments(text)
    t = re.sub(r"```.*?```", "", t, flags=re.S)
    t = re.sub(r"`[^`\n]*`", "", t)
    return [ln for ln in t.splitlines() if ln.strip()]


def norm(text: str) -> str:
    t = re.sub(r"`", "", text or "").lower()
    t = re.sub(r"\([^)]*\)\s*$", "", t.strip())  # trailing generator note
    return re.sub(r"\s+", " ", t).strip()


def glob_match(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-3] + "/") or path == pattern[:-3]
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(os.path.basename(path), pattern)


def any_glob(path: str, patterns) -> bool:
    return any(glob_match(path, p) for p in patterns or [])


def detect_families(text: str) -> "set[str]":
    low = (text or "").lower()
    return {fam for fam, rx in MODEL_FAMILIES if re.search(rx, low)}


def classify(path: str, mem: dict) -> str:
    tests = (mem.get("tests") or {})
    if os.path.basename(path) in LOCK_NAMES:
        return "lock"
    if GENERATED_PATH.search(path) or any_glob(path, (mem.get("budget") or {}).get("exclude_globs")):
        return "generated"
    if path.rsplit(".", 1)[-1].lower() in BINARY_EXT:
        return "asset"
    if TEST_PATH.search(path) or any_glob(path, tests.get("test_globs")):
        return "test"
    if CI_PATH.search(path):
        return "ci"
    if any_glob(path, tests.get("exempt_globs")):
        return "exempt"
    if DOC_PATH.search(path):
        return "doc"
    if META_PATH.search(path):
        return "meta"
    return "source"


def package_of(path: str) -> str:
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] in PACKAGE_DIRS:
        return "/".join(parts[:2])
    return parts[0] if len(parts) > 1 else "."


def patch_lines(patch: str) -> "list[str]":
    """Split Git patch text only at its actual LF record separators."""
    return [ln[:-1] if ln.endswith("\r") else ln for ln in (patch or "").split("\n")]


def added_lines(patch: str) -> "list[str]":
    out = []
    for ln in patch_lines(patch):
        if ln.startswith("+"):
            out.append(ln[1:])
    return out


_HASH_COMMENT_EXTS = {"cfg", "ini", "pl", "pm", "py", "pyi", "rb", "sh", "toml", "yaml", "yml", "zsh"}
_DASH_COMMENT_EXTS = {"lua", "sql"}
_SLASH_COMMENT_EXTS = {
    "c", "cc", "cpp", "cs", "cxx", "dart", "go", "groovy", "h", "hh", "hpp", "java",
    "cjs", "cts", "js", "jsx", "kt", "kts", "m", "mjs", "mm", "mts", "php", "rs", "scala",
    "swift", "ts", "tsx", "vue",
}
_BLOCK_COMMENT_EXTS = _SLASH_COMMENT_EXTS | _DASH_COMMENT_EXTS


_TRIPLE_QUOTE_EXTS = {"py", "pyi", "java", "kt", "kts", "scala", "swift", "groovy", "cs"}
_TRIPLE_SINGLE_EXTS = {"py", "pyi"}


def added_test_code(patch: str, path: str) -> "list[str]":
    """Added test code with comments and string contents masked (see scan_test_code)."""
    return scan_test_code(patch, path)[0]


def scan_test_code(patch: str, path: str) -> "tuple[list[str], int]":
    """(added test code with comments and string contents masked, dropped hunks).

    Multi-line literals (backtick, triple quotes) opened within a hunk are tracked, so
    a fixture that holds assertion-shaped text is data, not an assertion. Each hunk is
    read as if no literal is open before its first line, because the patch cannot show
    one: assertion-shaped text inside a literal that opened before the hunk counts,
    unless the hunk itself leaves a literal open. A hunk that leaves a literal open is
    counted in the second value. A creation hunk (`@@ -0,0`) starts at a known state, so
    the lines before that literal are kept and the rest is hidden; any other hunk that
    leaves a literal open yields no code.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    line_comment = "#" if ext in _HASH_COMMENT_EXTS else "--" if ext in _DASH_COMMENT_EXTS else "//"
    supports_slash_comments = ext in _SLASH_COMMENT_EXTS
    supports_block_comments = ext in _BLOCK_COMMENT_EXTS
    backtick_literal = ext in _SLASH_COMMENT_EXTS
    triples = (['"""'] if ext in _TRIPLE_QUOTE_EXTS else []) + (["'''"] if ext in _TRIPLE_SINGLE_EXTS else [])
    in_block = False
    multi = None  # delimiter of the multi-line literal still open
    out: "list[str]" = []
    hunk: "list[str]" = []
    creation = False
    dropped = 0

    def end_hunk() -> None:
        nonlocal multi, in_block, hunk, dropped
        if multi is None or creation:
            out.extend(hunk)
        if multi is not None:
            dropped += 1
        multi, in_block, hunk = None, False, []

    for raw in patch_lines(patch):
        if raw.startswith("@@"):
            end_hunk()
            creation = raw.startswith("@@ -0,0 ")
            continue
        if not raw.startswith(("+", " ")):
            continue
        is_added = raw.startswith("+")
        line = raw[1:]
        code = []
        quote = None
        escaped = False
        i = 0
        while i < len(line):
            if multi:
                if line[i] == "\\" and not (ext == "go" and multi == "`"):  # Go raw strings have no escapes
                    i += 2
                elif line.startswith(multi, i):
                    i += len(multi)
                    multi = None
                else:
                    i += 1
                continue
            if in_block:
                end = line.find("*/", i)
                if end < 0:
                    i = len(line)
                    continue
                in_block = False
                i = end + 2
                continue
            char = line[i]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                i += 1
                continue
            opener = next((t for t in triples if line.startswith(t, i)), None)
            if opener:
                multi = opener
                code.append('""')  # a masked literal stays an operand: `assert "" == x`
                i += len(opener)
                continue
            if char == "`" and backtick_literal:
                multi = "`"
                code.append('""')
                i += 1
                continue
            if char in "'\"`":
                quote = char
                code.append('""')
                i += 1
                continue
            if supports_block_comments and line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            if supports_slash_comments and line.startswith("//", i):
                break
            if line.startswith(line_comment, i):
                break
            code.append(char)
            i += 1
        if is_added:
            hunk.append("".join(code))
    end_hunk()
    return out, dropped


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------

def default_memory(repo: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "updated": "",
        "default_base": "main",
        "template": {"path": ".github/PULL_REQUEST_TEMPLATE.md", "blob_sha": "",
                     "sections": [], "checkboxes": [],
                     "pending_ok_at_first_push": []},
        "title": {"max_len": 100},
        "signoff": {"dco": False, "cla": False, "cla_evidence": None, "detected_from": []},
        "budget": {"max_changed_lines": 1500, "max_files": 40,
                   "split_max_changed_lines": 400, "split_max_files": 12,
                   "exclude_globs": []},
        "tests": {"required": True, "test_globs": [], "exempt_globs": []},
        "claims": {"allow": []},
        "batch": {"max_open_prs": 5},
        "rules": [],
        "feedback": [],
    }


def load_memory(repo: str, mdir: str | None = None) -> dict:
    path = memory_path(repo, mdir)
    if not os.path.isfile(path):
        raise Refused(
            "no maintainer memory for %s (expected %s). Create it with: "
            "upstream_lint.py memory init --repo %s" % (repo, path, repo))
    mem = load_json(path)
    if not isinstance(mem, dict) or mem.get("schema_version") != SCHEMA_VERSION:
        raise Refused("memory %s has schema_version %r, need %d" % (
            path, (mem or {}).get("schema_version") if isinstance(mem, dict) else None, SCHEMA_VERSION))
    if (mem.get("repo") or "").lower() != repo.lower():
        raise Refused("memory %s is for %r, not %r" % (path, mem.get("repo"), repo))
    base = default_memory(repo)
    for key, val in base.items():
        if key not in mem:
            mem[key] = val
        elif isinstance(val, dict) and isinstance(mem[key], dict):
            for k2, v2 in val.items():
                mem[key].setdefault(k2, v2)
    return mem


def save_memory(repo: str, mem: dict, mdir: str | None = None) -> str:
    path = memory_path(repo, mdir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mem, fh, indent=2, sort_keys=False, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def parse_template(text: str) -> dict:
    """Sections (level-2 headings) and checkbox lines of a PR template."""
    secs = split_sections(strip_comments(text))
    boxes = []
    for ln in text.splitlines():
        m = re.match(r"^\s*-\s*\[[ xX]\]\s*(.+?)\s*$", ln)
        if m:
            boxes.append(norm(m.group(1)))
    return {"sections": list(secs.keys()), "checkboxes": boxes}


def detect_signoff(contributing: str, files: "list[str]") -> dict:
    dco_hits, cla_hits = [], []
    low = (contributing or "").lower()
    if "signed-off-by" in low or "developer certificate of origin" in low or re.search(r"\bdco\b", low):
        dco_hits.append("CONTRIBUTING.md mentions DCO / Signed-off-by")
    if "contributor license agreement" in low or "cla-assistant" in low or re.search(r"\bcla\b", low):
        cla_hits.append("CONTRIBUTING.md mentions a CLA")
    for f in files:
        fl = f.lower()
        if fl in (".github/dco.yml", "dco") or re.search(r"\.github/workflows/[^/]*dco", fl):
            dco_hits.append(f)
        if fl in ("cla.md", ".clabot") or re.search(r"\.github/workflows/[^/]*cla", fl):
            cla_hits.append(f)
    return {"dco": bool(dco_hits), "cla": bool(cla_hits),
            "cla_evidence": None, "detected_from": dco_hits + cla_hits}


# ---------------------------------------------------------------------------
# GitHub access (used by memory init/refresh/ingest, batch and --live)
# ---------------------------------------------------------------------------

def api_base() -> str:
    return os.environ.get("GH_API_URL") or "https://api.github.com"


def api_get(path: str):
    """GET -> parsed JSON. curl-free: urllib with GH_TOKEN, else gh api."""
    token = os.environ.get("GH_TOKEN")
    if token:
        req = urllib.request.Request(api_base() + path, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise Refused("GET %s returned HTTP %s" % (path.split("?")[0], exc.code))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise Refused("GET %s failed: %s" % (path.split("?")[0], exc))
    gh = os.environ.get("GH_BIN") or "gh"
    try:
        proc = subprocess.run([gh, "api", path], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("gh api %s failed: %s" % (path.split("?")[0], exc))
    if proc.returncode != 0:
        raise Refused("gh api %s exited %d: %s" % (path.split("?")[0], proc.returncode, proc.stderr[:200]))
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise Refused("gh api %s did not return JSON: %s" % (path.split("?")[0], exc))


def api_get_all(path: str, label: str, cap_pages: int = 30) -> list:
    """Explicit paging; never --paginate (object-concatenation trap)."""
    out: list = []
    sep = "&" if "?" in path else "?"
    for page in range(1, cap_pages + 1):
        chunk = api_get("%s%sper_page=100&page=%d" % (path, sep, page))
        if not isinstance(chunk, list):
            raise Refused("%s page %d is not a JSON array" % (label, page))
        out.extend(chunk)
        if len(chunk) < 100:
            return out
    raise Refused("%s exceeds %d pages (paging cap)" % (label, cap_pages))


def fetch_file(repo: str, path: str, ref: str | None = None) -> "tuple[str, str]":
    """(text, blob_sha) of a repo file; Refused when unreadable."""
    import base64
    q = "?ref=%s" % ref if ref else ""
    meta = api_get("/repos/%s/contents/%s%s" % (repo, path, q))
    if not isinstance(meta, dict) or "content" not in meta:
        raise Refused("contents/%s on %s is not a file" % (path, repo))
    return base64.b64decode(meta["content"]).decode("utf-8", "replace"), meta.get("sha", "")


RECEIPT_MARK = "upstream-gate:receipt"
RECEIPT_STATES = ("success", "failure", "pending", "error")


def whoami() -> str:
    """Login of the identity that writes and is trusted for receipts."""
    login = os.environ.get("UPSTREAM_RECEIPT_AUTHOR")
    if login:
        return login
    me = api_get("/user")
    if not isinstance(me, dict) or not me.get("login"):
        raise Refused("cannot resolve our GitHub login (set UPSTREAM_RECEIPT_AUTHOR)")
    return me["login"]


def receipt_body(context: str, state: str, head: str, description: str) -> str:
    """A PR comment: one machine line (JSON in an HTML comment) and a human line."""
    payload = json.dumps({"context": context, "state": state, "head": head, "description": description[:200]},
                         ensure_ascii=True, separators=(",", ":"))
    payload = payload.replace("-", "\\u002d").replace(">", "\\u003e")  # nothing can close the comment early
    return "<!-- %s %s -->\n**%s**: %s on `%s`\n\n`%s`\n" % (
        RECEIPT_MARK, payload, context, state, head[:12], description[:200])


def receipts_from_comments(comments: list, author: str) -> list:
    """Receipts written by `author` (and nobody else) on the mirror PR, oldest first.

    A receipt is a PR comment, not a commit status: the credential agents hold
    cannot write statuses (HTTP 403), but it can comment. Anyone can comment on a
    public fork PR, so the author check is the trust boundary."""
    out = []
    for c in comments or []:
        if ((c.get("user") or {}).get("login") or "").lower() != author.lower():
            continue
        m = re.search(r"<!-- %s (\{.*?\}) -->" % re.escape(RECEIPT_MARK), c.get("body") or "", re.S)
        if not m:
            continue
        try:
            r = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("context") and r.get("state") in RECEIPT_STATES and r.get("head"):
            r["created_at"] = c.get("created_at") or c.get("updated_at") or ""
            r["id"] = c.get("id") or 0
            out.append(r)
    out.sort(key=lambda r: (r["created_at"], r["id"]))
    return out


def post_receipt(repo: str, pr: int, head: str, context: str, state: str, description: str) -> None:
    """Record a receipt as a comment on the mirror PR. Refused when it cannot be written."""
    if not re.fullmatch(r"[0-9a-f]{40}", head or ""):
        raise Refused("refusing to record a receipt for a non-SHA head %r" % head)
    if state not in RECEIPT_STATES:
        raise Refused("bad receipt state %r" % state)
    if not pr:
        raise Refused("no mirror PR number to record the %s receipt on" % context)
    payload = {"body": receipt_body(context, state, head, description)}
    token = os.environ.get("GH_TOKEN")
    if token:
        req = urllib.request.Request("%s/repos/%s/issues/%d/comments" % (api_base(), repo, int(pr)),
                                     data=json.dumps(payload).encode("utf-8"), method="POST", headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except (urllib.error.URLError, OSError) as exc:
            raise Refused("could not record the %s receipt on %s#%s: %s" % (context, repo, pr, exc))
        return
    gh = os.environ.get("GH_BIN") or "gh"
    proc = subprocess.run([gh, "api", "-X", "POST", "/repos/%s/issues/%d/comments" % (repo, int(pr)),
                           "-f", "body=" + payload["body"]], capture_output=True, text=True)
    if proc.returncode != 0:
        raise Refused("could not record the %s receipt on %s#%s: %s" % (context, repo, pr, proc.stderr[:200]))


def is_missing_api_resource(exc: Exception) -> bool:
    return bool(re.search(r"\bHTTP\s+404\b", str(exc)))


# Internal minimum for template-less repos: 7 sections mirroring our own
# public template shape, 6 verbatim checkboxes. Honestly labeled, never
# presented as upstream maintainer input.
INTERNAL_MINIMUM_SECTIONS = ["Thinking Path", "Linked Issues or Issue Description",
                             "What Changed", "Verification", "Risks", "Model Used",
                             "Checklist"]
INTERNAL_MINIMUM_CHECKBOXES = [
    "I wrote a thinking path that runs from the project to this change",
    "I named the model used, with its version",
    "I searched for duplicate or related PRs and linked them above",
    "I ran the tests locally and they pass",
    "I added or updated tests where applicable",
    "I updated the documentation this change touches",
]
TEMPLATE_CANDIDATES = [".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md",
                       "pull_request_template.md", "docs/pull_request_template.md"]


def discover_signoff(repo: str, base: str) -> dict:
    """DCO/CLA facts from CONTRIBUTING + workflow/root listings (404-tolerant)."""
    try:
        contributing, _ = fetch_file(repo, "CONTRIBUTING.md", base)
    except Refused as exc:
        if not is_missing_api_resource(exc):
            raise Refused("cannot read signoff policy from CONTRIBUTING.md; memory unchanged: %s" % exc)
        contributing = ""
    names: list = []
    for path, prefix in ((".github", ".github/"), (".github/workflows", ".github/workflows/")):
        try:
            listing = api_get("/repos/%s/contents/%s" % (repo, path))
        except Refused as exc:
            if is_missing_api_resource(exc):
                continue
            raise Refused("cannot read signoff policy paths; memory unchanged: %s" % exc)
        if not isinstance(listing, list):
            raise Refused("signoff policy listing %s is not a JSON array; memory unchanged" % path)
        names += [prefix + e["name"] for e in listing if isinstance(e, dict) and e.get("name")]
    try:
        listing = api_get("/repos/%s/contents" % repo)
    except Refused as exc:
        if not is_missing_api_resource(exc):
            raise Refused("cannot read repository signoff policy paths; memory unchanged: %s" % exc)
        listing = []
    if not isinstance(listing, list):
        raise Refused("repository signoff policy listing is not a JSON array; memory unchanged")
    root = [e["name"] for e in listing if isinstance(e, dict) and e.get("name")]
    return detect_signoff(contributing, names + root)


def apply_signoff(mem: dict, det: dict, refresh: bool) -> None:
    if not refresh:
        mem["signoff"] = det
    else:  # a recorded CLA evidence survives a refresh
        mem["signoff"]["dco"] = det["dco"]
        mem["signoff"]["detected_from"] = det["detected_from"]
        mem["signoff"]["cla"] = det["cla"] or mem["signoff"].get("cla", False)


def memory_init_absence(repo: str, mdir: str | None, mem: dict) -> str:
    """Write the honestly-labeled internal-minimum memory for a proven-404 repo."""
    import datetime
    base = mem.get("default_base") or "main"
    commit_sha = None
    try:
        ref = api_get("/repos/%s/commits/%s" % (repo, base))
        if isinstance(ref, dict):
            commit_sha = ref.get("sha")
    except Refused:
        commit_sha = None
    if not commit_sha:
        try:
            gref = api_get("/repos/%s/git/ref/heads/%s" % (repo, base))
            if isinstance(gref, dict):
                commit_sha = (gref.get("object") or {}).get("sha")
        except Refused as exc:
            raise Refused("cannot resolve default base %s for %s; memory unchanged: %s" % (base, repo, exc))
    if not commit_sha:
        raise Refused("cannot resolve default base %s for %s; memory unchanged" % (base, repo))
    cands = [mem["template"].get("path") or TEMPLATE_CANDIDATES[0]] + TEMPLATE_CANDIDATES[1:]
    det = discover_signoff(repo, base)
    apply_signoff(mem, det, refresh=False)
    tpl = mem["template"]
    tpl.update({"path": "", "blob_sha": "", "sections": list(INTERNAL_MINIMUM_SECTIONS),
                "checkboxes": [norm(b) for b in INTERNAL_MINIMUM_CHECKBOXES],
                "pending_ok_at_first_push": ["greptile", "ci gates are green"],
                "section_rules": [],
                "absent": True,
                "origin": "internal-minimum — NOT upstream maintainer input",
                "absence_evidence": {
                    "candidates": [{"candidate": c, "outcome": "HTTP 404"} for c in cands],
                    "source": "GET /repos/%s/contents/<cand>?ref=%s" % (repo, commit_sha),
                    "revision": "%s@%s" % (base, commit_sha),
                    "checked_at": datetime.date.today().isoformat()}})
    mem["updated"] = datetime.date.today().isoformat()
    return save_memory(repo, mem, mdir)


def memory_init(repo: str, mdir: str | None, refresh: bool) -> str:
    """Create or refresh the template/signoff facts from the live repo."""
    info = api_get("/repos/%s" % repo)
    mem = default_memory(repo)
    if refresh:
        mem = load_memory(repo, mdir)
    mem["default_base"] = info.get("default_branch") or mem["default_base"]
    tpath = mem["template"].get("path") or TEMPLATE_CANDIDATES[0]
    found = None
    non_file = None
    for cand in [tpath] + [c for c in TEMPLATE_CANDIDATES if c != tpath]:
        try:
            text, blob = fetch_file(repo, cand, mem["default_base"])
            found = (cand, text, blob)
            break
        except Refused as exc:
            # Fall back only when the candidate is verifiably absent. An
            # unreadable one (503, network) must not select a weaker template.
            if not (is_missing_api_resource(exc) or "is not a file" in str(exc)):
                raise Refused("cannot read PR template %s; memory unchanged: %s" % (cand, exc))
            if "is not a file" in str(exc) and not is_missing_api_resource(exc):
                # A non-file candidate never proves absence, but a real
                # template later in the list is still adopted.
                if non_file is None:
                    non_file = (cand, str(exc))
            continue
    if found is None and non_file is not None:
        raise Refused("cannot verify PR template %s (%s); memory unchanged" % (
            non_file[0], non_file[1]))
    if found is None:
        return memory_init_absence(repo, mdir, mem)
    parsed = parse_template(found[1])
    tpl = mem["template"]
    if tpl.get("absent"):
        # A live template invalidates a recorded absence: drop the
        # internal-minimum markers so the facts describe the maintainer
        # template. pending_ok/section_rules reset to unknown (fail-closed:
        # nothing is pre-approved as pending for a template never reviewed),
        # otherwise the freshness REFUSED could never be recovered by refresh.
        tpl.pop("absent", None)
        tpl.pop("origin", None)
        tpl.pop("absence_evidence", None)
        tpl["pending_ok_at_first_push"] = []
        tpl["section_rules"] = []
    tpl.update({"path": found[0], "blob_sha": found[2], "sections": parsed["sections"],
                "checkboxes": parsed["checkboxes"]})
    det = discover_signoff(repo, mem["default_base"])
    apply_signoff(mem, det, refresh)
    import datetime
    mem["updated"] = datetime.date.today().isoformat()
    return save_memory(repo, mem, mdir)


CATEGORY_WORDS = [
    ("process", r"template|checklist|branch name|commit message|\bpr title\b|rebase|squash|duplicate pr|roadmap|pass all check"),
    ("scope", r"too large|too big|split (?:this|the)|separate pr|unrelated|out of scope|smaller pr"),
    ("security", r"security|secret|credential|injection|xss|csrf|\bauth|permission|sanitiz|redact|leak"),
    ("tests", r"\btests?\b|coverage|assertion|regression test|flak(?:e|y)|drift"),
    ("error-handling", r"error|exception|throw|catch|fail(?:s|ure)?\b|retry|timeout|startup|unfinished|blocks"),
    ("types", r"\btypes?\b|typescript|\bany\b|\bcast\b|generic|nullable|undefined"),
    ("performance", r"performance|slow|n\+1|latency|\bmemory\b|\bcache\b|backlog|grow"),
    ("docs", r"\bdocs?\b|documentation|readme|changelog|docstring|jsdoc"),
    ("naming", r"\bnam(?:e|ing)\b|rename|convention|style|lint|format"),
]


def categorize(text: str) -> str:
    low = (text or "")[:240].lower()
    for cat, rx in CATEGORY_WORDS:
        if re.search(rx, low):
            return cat
    return "other"


def clean_feedback(body: str) -> "tuple[str, str]":
    """(plain text, severity) of a review comment; Greptile wraps titles in badge HTML."""
    sev = ""
    m = re.search(r'alt="(P[0-3])"|badges/(p[0-3])\.svg', body or "")
    if m:
        sev = (m.group(1) or m.group(2)).upper()
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\*\*|__", "", text)
    return re.sub(r"\s+", " ", text).strip(), sev


def ingest_feedback(repo: str, pr: int, mdir: str | None, me: str | None,
                    fixtures: str | None = None, any_author: bool = False, dry_run: bool = False) -> "tuple[int, int]":
    """Append maintainer and Greptile feedback from one PR. Returns (added, seen)."""
    mem = load_memory(repo, mdir)
    if fixtures:
        review = load_json(os.path.join(fixtures, "review_comments.json"))
        issue = load_json(os.path.join(fixtures, "issue_comments.json"))
        reviews = load_json(os.path.join(fixtures, "reviews.json"))
        prj = load_json(os.path.join(fixtures, "pr.json"))
    else:
        review = api_get_all("/repos/%s/pulls/%d/comments" % (repo, pr), "review comments")
        issue = api_get_all("/repos/%s/issues/%d/comments" % (repo, pr), "issue comments")
        reviews = api_get_all("/repos/%s/pulls/%d/reviews" % (repo, pr), "reviews")
        prj = api_get("/repos/%s/pulls/%d" % (repo, pr))
    author = ((prj or {}).get("user") or {}).get("login") or ""
    if me and author and author.lower() != me.lower() and not any_author:
        return 0, 0  # someone else's PR: not our feedback (use --any-author to learn from it)
    mine = {x.lower() for x in (me, author) if x}
    have = {f.get("id") for f in mem["feedback"]}
    added = seen = 0

    def take(kind: str, cid, who: str, body: str, url: str, date: str, path: str | None,
             association: str | None = None):
        nonlocal added, seen
        seen += 1
        # Only a repository OWNER or MEMBER speaks as the maintainer. Any other
        # human commenter is kept as a "commenter": useful feedback, but never
        # policy (a visitor must not be able to tighten the PR budget).
        if kind == "maintainer" and (association or "").upper() not in MAINTAINER_ASSOCIATIONS:
            kind = "commenter"
        fid = "%s-%d-%s" % (kind, pr, cid)
        if fid in have or (kind == "commenter" and "maintainer-%d-%s" % (pr, cid) in have):
            return
        text, sev = clean_feedback(body)
        if not text:
            return
        mem["feedback"].append({
            "id": fid, "kind": kind, "pr": pr, "url": url, "author": who,
            "association": (association or "").upper() or None,
            "date": (date or "")[:10], "category": categorize(text), "severity": sev,
            "excerpt": text[:300], "paths": [path] if path else [], "rule": None,
            "split_request": kind == "maintainer" and bool(
                re.search(r"split|separate pr|too large|smaller pr|break (?:this )?up", text, re.I)),
            "status": "recorded"})
        have.add(fid)
        added += 1

    def is_greptile(login: str) -> bool:
        return login.lower() in ("greptile-apps[bot]", "greptile[bot]")

    for c in review:
        who = (c.get("user") or {}).get("login") or ""
        if not who or who.lower() in mine:
            continue
        kind = "greptile" if is_greptile(who) else ("maintainer" if not who.endswith("[bot]") else None)
        if kind == "greptile" and c.get("in_reply_to_id"):
            continue  # Greptile answering our reply is not new feedback
        if kind:
            take(kind, c.get("id"), who, c.get("body"), c.get("html_url", ""), c.get("created_at", ""), c.get("path"),
                 c.get("author_association"))
    for c in issue:
        who = (c.get("user") or {}).get("login") or ""
        if not who or who.lower() in mine or who.endswith("[bot]"):
            continue  # Greptile's issue comment is the score summary, tracked by the gate
        take("maintainer", c.get("id"), who, c.get("body"), c.get("html_url", ""), c.get("created_at", ""), None,
             c.get("author_association"))
    for r in reviews:
        who = (r.get("user") or {}).get("login") or ""
        if not who or who.lower() in mine or who.endswith("[bot]"):
            continue
        if (r.get("body") or "").strip():
            take("maintainer", "r%s" % r.get("id"), who, r.get("body"), r.get("html_url", ""), r.get("submitted_at", ""), None,
                 r.get("author_association"))
    if added and not dry_run:
        import datetime
        mem["updated"] = datetime.date.today().isoformat()
        save_memory(repo, mem, mdir)
    return added, seen


# ---------------------------------------------------------------------------
# the lint engine
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    def add(self, status: str, item: str, detail: str) -> None:
        self.items.append((status, item, detail))

    @property
    def failed(self) -> bool:
        return any(s == "FAIL" for s, _, _ in self.items)

    @property
    def refused(self) -> bool:
        return any(s == "REFUSED" for s, _, _ in self.items)


def text_hits(text: str, patterns) -> "list[str]":
    hits = []
    for rx in patterns:
        m = rx.search(text or "")
        if m:
            hits.append(m.group(0)[:60])
    return hits


def check_title(rep: Report, title: str, mem: dict) -> None:
    max_len = int((mem.get("title") or {}).get("max_len") or 100)
    custom = (mem.get("title") or {}).get("pattern")
    problems = []
    if not (re.match(custom, title) if custom else CC_HEADER.match(title or "")):
        problems.append("not a Conventional Commits header 'type[(scope)]: summary' (scope is optional)")
    if len(title or "") > max_len:
        problems.append("%d chars, limit %d" % (len(title), max_len))
    if (title or "").rstrip().endswith("."):
        problems.append("trailing period")
    if re.search(r"\bTOG-|\bPAP", title or ""):
        problems.append("holds an internal id")
    rep.add("FAIL" if problems else "PASS", "title", "; ".join(problems) or "Conventional Commits header")


def check_commits(rep: Report, commits: list, mem: dict, stage: str) -> None:
    probs = []
    so = mem.get("signoff") or {}
    dco, cla = bool(so.get("dco")), bool(so.get("cla"))
    sign_probs, sign_checks = [], []
    for c in commits:
        msg = ((c.get("commit") or {}).get("message")) or c.get("message") or ""
        sha = (c.get("sha") or "?")[:10]
        subject = patch_lines(msg)[0] if msg else ""
        if len((c.get("parents") or [])) > 1:
            probs.append("%s is a merge commit" % sha)
            continue
        if not CC_HEADER.match(subject):
            probs.append("%s subject not Conventional Commits: %r" % (sha, subject[:60]))
        if re.match(r"^(?:fixup!|squash!|wip\b)", subject, re.I):
            probs.append("%s is a %s commit" % (sha, subject.split()[0]))
        if len(subject) > 100:
            probs.append("%s subject over 100 chars" % sha)
        if dco:
            author = ((c.get("commit") or {}).get("author") or {})
            email = (author.get("email") or "").lower()
            trailers = re.findall(r"(?im)^Signed-off-by:\s*(.+?)\s*<([^>]+)>\s*$", msg)
            if not trailers:
                sign_probs.append("%s has no Signed-off-by" % sha)
            elif email and email not in {t[1].lower() for t in trailers}:
                sign_probs.append("%s Signed-off-by does not match author %s" % (sha, email))
    if not commits:
        probs.append("no commits to push")
        if dco:
            sign_probs.append("no commits to verify DCO signoff")
    rep.add("FAIL" if probs else "PASS", "commits", "; ".join(probs) or "%d commit(s), all Conventional Commits" % len(commits))
    if dco:
        sign_checks.append("DCO Signed-off-by on every commit")
    if cla:
        ev = so.get("cla_evidence")
        if ev:
            sign_checks.append("CLA recorded: %s" % ev)
        else:
            sign_probs.append("repo requires a CLA and memory records no signed-CLA evidence (set signoff.cla_evidence to the public CLA record)")
    if not dco and not cla:
        sign_checks.append("no DCO or CLA required (per memory)")
    rep.add("FAIL" if sign_probs else "PASS", "signoff", "; ".join(sign_probs or sign_checks))


def check_branch(rep: Report, branch: str) -> None:
    if not branch:
        rep.add("REFUSED", "branch", "no branch name to check")
        return
    probs = []
    if not BRANCH_OK.match(branch):
        probs.append("not descriptive kebab-case (lowercase letters, digits, - . _ and one optional type/ prefix)")
    if re.search(r"(?i)\bTOG-|\bPAPA?-?\d|^(?:agent|run)[-/]", branch):
        probs.append("holds an internal ticket id or instance-derived name")
    if len(branch) > 60:
        probs.append("%d chars, keep it short" % len(branch))
    rep.add("FAIL" if probs else "PASS", "branch", "; ".join(probs) or "descriptive branch name")


def section_stats(text: str) -> dict:
    t = strip_comments(text)
    bullets = [ln for ln in t.splitlines() if re.match(r"^\s*(?:>\s*)?[-*]\s+\S", ln)]
    return {"bullets": len(bullets), "has_code": bool(re.search(r"`[^`\n]+`|```", t)),
            "chars": len(t.strip())}


def live_absence_probe(repo: str, mem: dict) -> "str | None":
    """Refusal detail when a recorded absence no longer holds live, else None."""
    base = mem.get("default_base") or "main"
    for cand in TEMPLATE_CANDIDATES:
        try:
            fetch_file(repo, cand, base)
            return "upstream gained a PR template (%s); run memory refresh --repo %s" % (cand, repo)
        except Refused as exc:
            if is_missing_api_resource(exc):
                continue
            return "cannot confirm the live PR template: %s" % exc
    det = discover_signoff(repo, base)
    rec = mem.get("signoff") or {}
    if (det.get("dco") and not rec.get("dco")) or (det.get("cla") and not rec.get("cla")):
        return "upstream gained a signoff policy (%s); run memory refresh --repo %s" % (
            "; ".join(det.get("detected_from") or []) or "DCO/CLA marker", repo)
    return None


def check_template(rep: Report, title: str, body: str, mem: dict, stage: str, nobody: bool) -> None:
    if nobody:
        rep.add("PASS", "template", "skipped (no PR text yet; checked at publish and ready)")
        return
    tpl = mem.get("template") or {}
    # Absence-memory lines are tagged internal-minimum so gate output never
    # implies maintainer authorship. The tag contains "template" so existing
    # template-failure assertions keep matching.
    item = "template(internal-minimum)" if tpl.get("absent") else "template"
    want = tpl.get("sections") or []
    if not want:
        rep.add("REFUSED", item, "memory records no template sections; run memory refresh")
        return
    secs = split_sections(body or "")
    reqs = {s["name"]: s for s in tpl.get("section_rules", []) if isinstance(s, dict)}
    probs = []
    for name in want:
        if name == "Checklist":
            continue
        if name not in secs:
            probs.append("missing section '%s'" % name)
            continue
        content = strip_comments(secs[name]).strip()
        if not content or PLACEHOLDER.search(content):
            probs.append("'%s' is empty or still holds template placeholder text" % name)
            continue
        st = section_stats(secs[name])
        rule = reqs.get(name, {})
        if st["bullets"] < int(rule.get("min_bullets", 0)):
            probs.append("'%s' needs at least %s bullet(s)" % (name, rule["min_bullets"]))
        if rule.get("require_code") and not st["has_code"]:
            probs.append("'%s' must name the command or file you ran, in backticks" % name)
        if st["chars"] < int(rule.get("min_chars", 8)):
            probs.append("'%s' is too short (%d chars)" % (name, st["chars"]))
    mu = secs.get("Model Used", "")
    if "Model Used" in want and mu:
        mtext = strip_comments(mu)
        if not re.search(r"None\s*[-—–]+\s*human[- ]authored", mtext, re.I):
            if not re.search(r"\b[A-Za-z][A-Za-z0-9._-]*\d[A-Za-z0-9._-]*\b", mtext):
                probs.append("'Model Used' names no exact model id")
            if INTERNAL_MODEL_ALIASES.search(mtext):
                probs.append("'Model Used' names an internal routing alias")
            provider_fams = detect_families(re.sub(r"[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*", " ", mtext))
            id_fams = detect_families(" ".join(re.findall(r"[A-Za-z][A-Za-z0-9._-]*\d[A-Za-z0-9._-]*", mtext)))
            if provider_fams and id_fams and not (provider_fams & id_fams):
                probs.append("'Model Used' provider (%s) does not match the model id family (%s)" % (
                    "/".join(sorted(provider_fams)), "/".join(sorted(id_fams))))
    # Checklist boxes.
    if "Checklist" in want:
        chk = secs.get("Checklist")
        boxes = []
        if chk is None:
            probs.append("missing section 'Checklist'")
        else:
            for ln in strip_comments(chk).splitlines():
                m = re.match(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$", ln)
                if m:
                    boxes.append((m.group(1).lower() == "x", norm(m.group(2))))
            have = [t for _, t in boxes]
            lost = [w for w in tpl.get("checkboxes", []) if not any(h.startswith(w[:60]) for h in have)]
            if lost:
                probs.append("%d template checkbox(es) deleted, e.g. %r" % (len(lost), lost[0][:50]))
            pend_ok = [re.compile(p, re.I) for p in tpl.get("pending_ok_at_first_push", [])]
            open_boxes = [t for ticked, t in boxes if not ticked]
            if stage == "ready":
                if open_boxes:
                    probs.append("%d unticked checkbox(es) with maintainers about to be asked" % len(open_boxes))
            else:
                bad = [t for t in open_boxes if not any(p.search(t) for p in pend_ok)]
                if bad:
                    probs.append("%d unticked checkbox(es) not on the pending list, e.g. %r" % (len(bad), bad[0][:50]))
    leftover = [c for c in re.findall(r"<!--(.*?)-->", body or "", flags=re.S)
                if re.search(r"\bRequired\b|Pick ONE|Bullet list of concrete", c)]
    if leftover:
        rep.add("WARN", "template-comments", "%d template guidance comment(s) left in the body" % len(leftover))
    rep.add("FAIL" if probs else "PASS", item, "; ".join(probs) or "all %d template sections present and filled" % len(want))


EMPTY_BLOB_SHAS = frozenset({
    "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
    "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813"})


def changed_without_patch(f: dict) -> bool:
    """A changed file whose content cannot be read from a missing patch.

    Zero textual counters do not prove "unchanged": GitHub reports an added or
    modified binary as 0/0 with no patch. A pure rename or mode change, which
    carries no new content, is the only patchless zero-count file that passes.
    """
    if f.get("sha") in EMPTY_BLOB_SHAS:  # an empty file (`__init__.py`) has no content to scan
        return False
    return (f.get("changes") or 0) > 0 or f.get("status") in ("added", "modified")


def check_internal(rep: Report, title: str, body: str, branch: str, commits: list, files: list,
                   nobody: bool) -> None:
    prose: dict[str, str] = {}
    if not nobody:
        prose["title"], prose["body"] = title or "", body or ""
    codeish: dict[str, str] = {"branch": branch or ""}
    hits, unscanned = [], []
    for c in commits:
        sha = (c.get("sha") or "?")[:10]
        msg = ((c.get("commit") or {}).get("message")) or c.get("message") or ""
        codeish["commit %s" % sha] = msg
        for f in c.get("added_files", []):
            name = f.get("filename") or ""
            # A path published in any commit stays in history, even if a later
            # commit deletes the file: scan the name of every touched path.
            for h in text_hits(name, CODE_PATTERNS):
                hits.append("path %s in commit %s holds %r" % (name, sha, h))
            patch = f.get("patch")
            if patch is None:
                ext = name.rsplit(".", 1)[-1].lower()
                if changed_without_patch(f) and ext not in BINARY_EXT and not GENERATED_PATH.search(name):
                    unscanned.append("%s in commit %s" % (name, sha))
                continue
            for ln in added_lines(patch):
                for h in text_hits(ln, CODE_PATTERNS):
                    hits.append("added line in commit %s %s holds %r" % (sha, name, h))
    for label, text in prose.items():
        for h in text_hits(text, TEXT_PATTERNS):
            hits.append("%s holds %r" % (label, h))
    for label, text in codeish.items():
        for h in text_hits(text, CODE_PATTERNS):
            hits.append("%s holds %r" % (label, h))
    for f in files:
        name = f.get("filename") or ""
        for h in text_hits(name, CODE_PATTERNS):
            hits.append("path %s holds %r" % (name, h))
        if f.get("status") == "removed":
            continue
        patch = f.get("patch")
        if patch is None:
            ext = name.rsplit(".", 1)[-1].lower()
            if changed_without_patch(f) and ext not in BINARY_EXT and os.path.basename(name) not in LOCK_NAMES \
                    and not GENERATED_PATH.search(name):
                unscanned.append(name)
            continue
        for ln in added_lines(patch):
            for h in text_hits(ln, CODE_PATTERNS):
                hits.append("added line in %s holds %r" % (name, h))
    if unscanned:
        rep.add("REFUSED", "internal", "cannot scan %d changed file(s) with no patch text (too large or binary?): %s" % (
            len(unscanned), ", ".join(unscanned[:3])))
        return
    rep.add("FAIL" if hits else "PASS", "internal",
            "; ".join(sorted(set(hits))[:8]) or "no internal ids, hosts or secrets in title, body, branch, commits, paths or added lines")


def effective_budget(mem: dict) -> "tuple[int, int, str]":
    b = mem.get("budget") or {}
    lines, files = int(b.get("max_changed_lines", 1500)), int(b.get("max_files", 40))
    asked = [f for f in mem.get("feedback", [])
             if f.get("kind") == "maintainer" and f.get("split_request") and f.get("status") != "resolved"]
    if asked:
        lines = min(lines, int(b.get("split_max_changed_lines", 400)))
        files = min(files, int(b.get("split_max_files", 12)))
        return lines, files, "maintainers asked for smaller PRs (%s)" % asked[0].get("id")
    return lines, files, "default budget (fewer, complete PRs)"


def check_budget(rep: Report, files: list, mem: dict) -> None:
    lines = nfiles = 0
    for f in files:
        if classify(f.get("filename") or "", mem) in ("lock", "generated", "asset"):
            continue
        nfiles += 1
        lines += int(f.get("additions") or 0) + int(f.get("deletions") or 0)
    max_lines, max_files, why = effective_budget(mem)
    probs = []
    if lines > max_lines:
        probs.append("%d changed lines, budget %d" % (lines, max_lines))
    if nfiles > max_files:
        probs.append("%d files, budget %d" % (nfiles, max_files))
    rep.add("FAIL" if probs else "PASS", "budget",
            ("; ".join(probs) + " [%s]" % why) if probs else "%d lines, %d files within budget %d/%d [%s]" % (
                lines, nfiles, max_lines, max_files, why))


def check_tests(rep: Report, files: list, mem: dict) -> None:
    tcfg = mem.get("tests") or {}
    if tcfg.get("required") is False:
        rep.add("PASS", "tests", "memory records no test requirement for this repo")
        return
    src, tst = [], []
    for f in files:
        cls = classify(f.get("filename") or "", mem)
        if cls == "source":
            src.append(f)
        elif cls == "test":
            tst.append(f)
    if not src:
        rep.add("PASS", "tests", "no behavior change (docs, tests, CI or meta files only)")
        return
    if all(f.get("status") == "removed" for f in src):
        rep.add("PASS", "tests", "source changes are pure deletions")
        return
    t_by_pkg: dict[str, int] = {}
    asserted_tests: list[str] = []
    ambiguous: list[str] = []
    unscanned_tests: list[str] = []
    for f in tst:
        patch = f.get("patch")
        if patch is None:
            # No patch text (GitHub drops it on huge diffs): assertions cannot be
            # measured, so this is a refusal like check_internal's unscanned
            # files, never a pass. A patch-less "test" must not waive the
            # same-package gate. A pure rename (changes == 0, no patch) is not
            # unscanned: like check_internal, only changed files can refuse.
            if f.get("status") != "removed" and changed_without_patch(f):
                unscanned_tests.append(f["filename"])
            continue
        code, dropped = scan_test_code(patch, f["filename"])
        if not any(TEST_ASSERT.search(ln) for ln in code):
            if dropped:
                ambiguous.append(f["filename"])
            continue
        asserted_tests.append(f["filename"])
        t_by_pkg[package_of(f["filename"])] = t_by_pkg.get(package_of(f["filename"]), 0) + 1
    if unscanned_tests:
        rep.add("REFUSED", "tests", "cannot verify assertions in %d test file(s) with no patch text (too large?): %s" % (
            len(unscanned_tests), ", ".join(unscanned_tests[:3])))
        return
    global_dirs = tcfg.get("global_test_globs") or []
    missing = {}
    for f in src:
        if f.get("status") == "removed":
            continue
        pkg = package_of(f["filename"])
        if pkg in t_by_pkg:
            continue
        # Like package coverage, global coverage needs an added assertion: a
        # deleted, comment-only or assertion-free global test waives nothing.
        if any(any_glob(name, global_dirs) for name in asserted_tests):
            continue
        missing.setdefault(pkg, []).append(f["filename"])
    if missing:
        rep.add("FAIL", "tests", "behavior change without a test in the same package: " + "; ".join(
            "%s (%s)" % (p, fs[0]) for p, fs in sorted(missing.items())[:4])
            + ("; an unclosed multi-line literal hides assertions in a test hunk: %s" % ", ".join(
                ambiguous[:3]) if ambiguous else ""))
    else:
        rep.add("PASS", "tests", "%d source file(s) changed, tests added in %s" % (
            len(src), ", ".join(sorted(t_by_pkg)) or "global tests"))


def check_claims(rep: Report, title: str, body: str, mem: dict, nobody: bool) -> None:
    if nobody:
        rep.add("PASS", "claims", "skipped (no PR text yet; checked at publish and ready)")
        return
    allow = {a.lower() for a in (mem.get("claims") or {}).get("allow", [])}
    secs = split_sections(body or "")
    prose = [title or ""] + [ln for name, text in secs.items() if name != "Checklist" for ln in prose_lines(text)]
    prose += prose_lines(body.split("## ")[0]) if body and not body.lstrip().startswith("##") else []
    hits, warns = [], []
    for ln in prose:
        for rx, label in BANNED_CLAIMS:
            if label.lower() in allow:
                continue
            for m in re.finditer(rx, ln, re.I):
                # "not a general guarantee" limits a claim, it does not make one.
                if label not in NEGATION_PROOF and NEGATOR.search(ln[max(0, m.start() - 30):m.start()]):
                    continue
                hits.append("over-promise %r" % label)
                break
        for rx, label in WARN_TONE:
            if re.search(rx, ln, re.I):
                warns.append(label)
        if re.search(r"(?<![=!<>])!(?![=\[])", ln):
            hits.append("exclamation mark")
        if EMOJI.search(ln):
            hits.append("emoji")
        for w in SHOUT.findall(ln):
            if w not in SHOUT_OK and w.lower() not in allow:
                hits.append("shouting %r" % w)
        if not ln.lstrip().startswith("|") and any(
                len(sent.split()) > 40 for sent in re.split(r"(?<=[.!?])\s+", ln)):
            warns.append("sentence over 40 words (ASD-STE100 wants short sentences)")
    rep.add("FAIL" if hits else "PASS", "claims",
            "; ".join(sorted(set(hits))[:8]) or "no over-promising, puffery, shouting or emoji")
    if warns:
        rep.add("WARN", "tone", "; ".join(sorted(set(warns))[:4]))


def rule_scopes(rule: dict, title: str, body: str, branch: str, commits: list, files: list) -> "list[str]":
    scope = rule.get("scope") or "body"
    if scope == "title":
        return [title or ""]
    if scope == "body":
        return [body or ""]
    if scope == "branch":
        return [branch or ""]
    if scope == "commits":
        return [((c.get("commit") or {}).get("message")) or c.get("message") or "" for c in commits]
    if scope == "paths":
        return [f.get("filename") or "" for f in files]
    if scope == "added":
        return ["\n".join(added_lines(f.get("patch") or "")) for f in files if f.get("status") != "removed"]
    if scope.startswith("section:"):
        return [split_sections(body or "").get(scope.split(":", 1)[1], "")]
    raise Refused("memory rule %s has unknown scope %r" % (rule.get("id"), scope))


def check_rules(rep: Report, title: str, body: str, branch: str, commits: list, files: list,
                mem: dict, nobody: bool) -> None:
    probs, warns, ran = [], [], 0
    for rule in mem.get("rules", []):
        rid = rule.get("id", "?")
        if rule.get("applies_when_paths") and not any(
                any_glob(f.get("filename") or "", rule["applies_when_paths"]) for f in files):
            continue
        scope = rule.get("scope") or "body"
        if nobody and (scope in ("title", "body") or scope.startswith("section:")):
            continue
        texts = rule_scopes(rule, title, body, branch, commits, files)
        flags = re.I if "i" in (rule.get("flags") or "") else 0
        flags |= re.M if "m" in (rule.get("flags") or "") else 0
        kind = rule.get("kind")
        try:
            if kind == "forbid":
                bad = any(re.search(rule["pattern"], t, flags) for t in texts)
                ok = not bad
            elif kind == "require":
                ok = any(re.search(rule["pattern"], t, flags) for t in texts) if texts else False
            elif kind == "require_any":
                ok = False
                for alt in rule.get("alternatives", []):
                    mn = int(alt.get("min_count", 1))
                    if any(len(re.findall(alt["pattern"], t, flags)) >= mn for t in texts):
                        ok = True
                        break
            elif kind == "max_len":
                ok = all(len(t) <= int(rule["max"]) for t in texts)
            else:
                raise Refused("memory rule %s has unknown kind %r" % (rid, kind))
        except re.error as exc:
            raise Refused("memory rule %s has a bad pattern: %s" % (rid, exc))
        ran += 1
        if not ok:
            msg = "%s: %s" % (rid, rule.get("message") or "rule failed")
            (warns if rule.get("severity") == "warn" else probs).append(msg)
    rep.add("FAIL" if probs else "PASS", "rules", "; ".join(probs) or "%d maintainer-memory rule(s) satisfied" % ran)
    for w in warns:
        rep.add("WARN", "rules", w)


def check_advisory(rep: Report, files: list, mem: dict) -> None:
    changed = [f.get("filename") or "" for f in files]
    hits = []
    for fb in mem.get("feedback", []):
        if fb.get("status") == "resolved":
            continue
        if any(any(glob_match(c, p) or c == p for c in changed) for p in fb.get("paths", [])):
            hits.append("%s [%s] %s" % (fb.get("id"), fb.get("category"), (fb.get("excerpt") or "")[:90]))
    cats: dict[str, int] = {}
    for fb in mem.get("feedback", []):
        cats[fb.get("category", "other")] = cats.get(fb.get("category", "other"), 0) + 1
    recur = ["%s x%d" % (c, n) for c, n in sorted(cats.items()) if n >= 2 and c != "other"]
    if hits:
        rep.add("WARN", "advisory", "past feedback touches changed paths: " + " | ".join(hits[:4]))
    if recur:
        rep.add("WARN", "advisory", "recurring maintainer/Greptile feedback categories: " + ", ".join(recur))


def lint(repo: str, title: str, body: str, branch: str, commits: list, files: list,
         stage: str = "first-push", mdir: str | None = None, nobody: bool = False,
         live: bool = False) -> Report:
    rep = Report()
    try:
        mem = load_memory(repo, mdir)
    except Refused as exc:
        rep.add("REFUSED", "memory", str(exc))
        return rep
    rep.add("PASS", "memory", "%s memory loaded (%d feedback entries, %d rules, updated %s)" % (
        repo, len(mem.get("feedback", [])), len(mem.get("rules", [])), mem.get("updated") or "?"))
    if live:
        try:
            tpl = mem.get("template") or {}
            if tpl.get("absent"):
                gained = live_absence_probe(repo, mem)
                if gained is not None:
                    rep.add("REFUSED", "template-fresh", gained)
                    return rep
                rep.add("PASS", "template-fresh",
                        "upstream template absence confirmed live for %s" % repo)
            else:
                _, blob = fetch_file(repo, tpl.get("path", ".github/PULL_REQUEST_TEMPLATE.md"), mem.get("default_base"))
                if tpl.get("blob_sha") and blob != tpl["blob_sha"]:
                    rep.add("REFUSED", "template-fresh",
                            "upstream PR template changed (%s -> %s); run upstream_lint.py memory refresh --repo %s" % (
                                tpl["blob_sha"][:10], blob[:10], repo))
                    return rep
                rep.add("PASS", "template-fresh", "upstream template unchanged since memory was recorded")
        except Refused as exc:
            rep.add("REFUSED", "template-fresh", "cannot confirm the live PR template: %s" % exc)
            return rep
    steps = [
        lambda: check_title(rep, title, mem) if not nobody else rep.add("PASS", "title", "skipped (no PR text yet)"),
        lambda: check_commits(rep, commits, mem, stage),
        lambda: check_branch(rep, branch),
        lambda: check_template(rep, title, body, mem, stage, nobody),
        lambda: check_internal(rep, title, body, branch, commits, files, nobody),
        lambda: check_tests(rep, files, mem),
        lambda: check_budget(rep, files, mem),
        lambda: check_claims(rep, title, body, mem, nobody),
        lambda: check_rules(rep, title, body, branch, commits, files, mem, nobody),
        lambda: check_advisory(rep, files, mem),
    ]
    for step in steps:
        try:
            step()
        except Refused as exc:
            rep.add("REFUSED", "lint", str(exc))
            return rep
    return rep


# ---------------------------------------------------------------------------
# batch policy: fewer, complete PRs
# ---------------------------------------------------------------------------

_SPLIT_COMMENT_URL = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:pull|issues)/"
    r"(?P<issue>[1-9]\d*)#issuecomment-(?P<comment>[1-9]\d*)"
)
_SPLIT_REQUEST = re.compile(
    r"\b(?:please|could you|can you|would you)\b.{0,120}\b(?:split|separate|break)\b|"
    r"\b(?:please\s+)?(?:split|separate)\s+(?:this|the|your)\s+(?:pr|pull request|change|patch)\b|"
    r"\b(?:this|the|your)\s+(?:pr|pull request|change|patch)\s+(?:should|needs? to|must)\s+be\s+(?:split|separated)\b|"
    r"\b(?:please\s+)?break\s+(?:this|the|your|it)\s+up\b",
    re.I,
)
_SPLIT_NEGATION = re.compile(
    r"\b(?:do not|don't|should not|shouldn't|must not|mustn't|never|avoid|"
    r"does not need to|doesn't need to|do not need to|don't need to)\s+(?:split|separate|break)\b",
    re.I,
)


def verified_split_request(repo: str, url: str | None, overlapping_prs: set[str]) -> bool:
    """Trust only a real split request on an overlapping PR from an owner or org member."""
    match = _SPLIT_COMMENT_URL.fullmatch(url or "")
    if not match:
        return False
    linked_repo = "%s/%s" % (match.group("owner"), match.group("repo"))
    if linked_repo.casefold() != repo.casefold():
        return False

    issue_number = int(match.group("issue"))
    if str(issue_number) not in overlapping_prs:
        return False
    comment_id = int(match.group("comment"))
    comment = api_get("/repos/%s/issues/comments/%d" % (repo, comment_id))
    if not isinstance(comment, dict) or type(comment.get("id")) is not int or comment["id"] != comment_id:
        return False
    issue_url = comment.get("issue_url")
    user = comment.get("user")
    association = comment.get("author_association")
    if not isinstance(issue_url, str) or not isinstance(user, dict) or not isinstance(user.get("login"), str):
        return False
    if not user["login"].strip() or not isinstance(association, str):
        return False
    try:
        expected = urllib.parse.urlsplit(
            "%s/repos/%s/issues/%d" % (api_base().rstrip("/"), repo, issue_number))
        actual = urllib.parse.urlsplit(issue_url)
    except ValueError:
        return False
    if (actual.scheme.casefold(), actual.netloc.casefold(), actual.path.rstrip("/").casefold()) != (
            expected.scheme.casefold(), expected.netloc.casefold(), expected.path.rstrip("/").casefold()):
        return False
    if association.upper() not in MAINTAINER_ASSOCIATIONS:
        return False
    body = comment.get("body")
    if not isinstance(body, str):
        return False
    body = " ".join(body.split())
    return not _SPLIT_NEGATION.search(body) and bool(_SPLIT_REQUEST.search(body))


def batch_check(repo: str, branch: str, title: str, files: list, open_prs: list, mem: dict,
                split_url: str | None) -> Report:
    """open_prs: [{number, title, head_ref, files: [path...]}] -- OUR open PRs in the repo."""
    rep = Report()
    mine = [p for p in open_prs if p.get("head_ref") != branch]
    cap = int((mem.get("batch") or {}).get("max_open_prs", 5))
    mypaths = {f.get("filename") for f in files}
    overlap = []
    overlap_numbers = set()
    for p in mine:
        shared = sorted(mypaths & set(p.get("files") or []))
        if shared:
            overlap.append("#%s (%s)" % (p.get("number"), shared[0]))
            overlap_numbers.add(str(p.get("number")))
    asked = verified_split_request(repo, split_url, overlap_numbers) if split_url else False
    if split_url and not asked:
        rep.add("FAIL", "batch", "--maintainer-split-url must identify a split request by a GitHub OWNER or MEMBER on an overlapping PR in this repo")
        return rep
    probs = []
    if overlap and not asked:
        probs.append("our open PR(s) already touch the same files: %s; fold this change into one of them (fewer, complete PRs) "
                     "or pass --maintainer-split-url with the maintainer request to split" % ", ".join(overlap[:3]))
    if len(mine) >= cap:
        probs.append("%d of our PRs are already open in %s (limit %d)" % (len(mine), repo, cap))
    if probs:
        rep.add("FAIL", "batch", "; ".join(probs))
    else:
        rep.add("PASS", "batch", "%d other open PR(s), no overlap%s" % (
            len(mine), " (split allowed: maintainer request recorded)" if asked else ""))
    return rep


def fetch_open_prs(repo: str, me: str) -> list:
    out = []
    for pr in api_get_all("/repos/%s/pulls?state=open" % repo, "open pulls", cap_pages=10):
        if ((pr.get("user") or {}).get("login") or "").lower() != me.lower():
            continue
        fl = api_get_all("/repos/%s/pulls/%d/files" % (repo, pr["number"]), "pr files")
        out.append({"number": pr["number"], "title": pr.get("title"),
                    "head_ref": (pr.get("head") or {}).get("ref"),
                    "files": [f.get("filename") for f in fl]})
    return out


# ---------------------------------------------------------------------------
# layouts and output
# ---------------------------------------------------------------------------

def layout_from_dir(d: str) -> dict:
    pr = load_json(os.path.join(d, "pr.json"))
    commits = load_json(os.path.join(d, "commits.json"))
    files_p = os.path.join(d, "files.json")
    if not os.path.isfile(files_p):
        raise Refused("layout %s has no files.json (the diff cannot be measured)" % d)
    files = load_json(files_p)
    if isinstance(commits, dict):
        commits = [commits]
    return {"title": pr.get("title") or "", "body": pr.get("body") or "",
            "branch": (pr.get("head") or {}).get("ref") or "", "commits": commits, "files": files,
            "head": (pr.get("head") or {}).get("sha") or ""}


def git(repo_dir: str, *args: str) -> str:
    # GIT_BIN is a seam: on agent hosts the PATH git is a credential-injecting
    # shim that costs seconds per call, so the call count here is kept small.
    # gc.auto=0 / maintenance.auto=false: linting only reads the repo, so it
    # must never detach a background gc/maintenance writer into it.
    proc = subprocess.run([os.environ.get("GIT_BIN") or "git", "-C", repo_dir, "-c", "core.quotepath=off",
                           "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
                          capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise Refused("git %s failed: %s" % (" ".join(args[:2]), stderr[:200]))
    return proc.stdout.decode("utf-8", "surrogateescape")


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_c(text: str) -> str:
    """Undo Git's C-style quoting of a path (the text between the double quotes)."""
    raw = text.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] != 92:  # a backslash
            out.append(raw[i])
            i += 1
        elif raw[i + 1:i + 2].isdigit() and len(raw[i + 1:i + 4]) == 3:
            out.append(int(raw[i + 1:i + 4], 8) & 0xFF)
            i += 4
        elif raw[i + 1:i + 2] and chr(raw[i + 1]) in _C_ESCAPES:
            out.append(_C_ESCAPES[chr(raw[i + 1])])
            i += 2
        else:
            raise Refused("cannot parse a quoted Git path")
    return out.decode("utf-8", "surrogateescape")


def diff_header_path(rest: str) -> str:
    """Path named by `diff --git a/P b/P` (the text after `diff --git `).

    Run with --no-renames and forced a/ b/ prefixes, both sides name one path. A
    path with a space is not quoted by Git, so it cannot be split at the space:
    the two halves are compared instead.
    """
    quoted = re.fullmatch(r'"((?:\\.|[^"\\])*)" "((?:\\.|[^"\\])*)"', rest)
    if quoted:
        left, right = unquote_c(quoted.group(1)), unquote_c(quoted.group(2))
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            return left[2:]
    elif len(rest) >= 5 and (len(rest) - 5) % 2 == 0:
        n = (len(rest) - 5) // 2
        name = rest[2:2 + n]
        if rest == "a/%s b/%s" % (name, name):
            return name
    raise Refused("cannot parse Git diff header")


def split_diff(full: str, paths: list[str]) -> "dict[str, str]":
    """Map patch sections to NUL-delimited numstat paths in Git's output order.

    A type change (a file replaced by a symlink) is two `diff --git` sections for
    one numstat entry; both are kept under that path.
    """
    out: "dict[str, list[str]]" = {}
    cur = -1
    collecting = False
    for ln in patch_lines(full):
        if ln.startswith("diff --git "):
            name = diff_header_path(ln[len("diff --git "):])
            if cur < 0 or name != paths[cur]:
                cur += 1
                if cur >= len(paths):
                    raise Refused("Git patch has more files than its numstat output")
                if name != paths[cur]:
                    raise Refused("Git patch section %s does not match numstat path %s" % (name, paths[cur]))
                out[paths[cur]] = []
            collecting = False
        elif ln.startswith("@@"):
            collecting = True
            out[paths[cur]].append(ln)
        elif collecting:
            out[paths[cur]].append(ln)
    if cur + 1 != len(paths):
        raise Refused("Git patch has %d file section(s), numstat has %d" % (cur + 1, len(paths)))
    return {name: "\n".join(lines) for name, lines in out.items()}


def split_log_records(full: str, shas: list[str]) -> dict[str, str]:
    """Map each commit SHA to its log payload using a NUL-delimited format marker."""
    out: dict[str, str] = {}
    cursor = 0
    for index, sha in enumerate(shas):
        marker = sha + "\x00"
        start = full.find(marker, cursor)
        if start < 0:
            raise Refused("git log output is missing commit %s" % sha[:10])
        start += len(marker)
        if index + 1 < len(shas):
            next_marker = shas[index + 1] + "\x00"
            end = full.find(next_marker, start)
            if end < 0:
                raise Refused("git log output is missing commit %s" % shas[index + 1][:10])
        else:
            end = len(full)
        out[sha] = full[start:end]
        cursor = end
    return out


def numstat_entries(segment: str) -> list[dict]:
    out = []
    for record in segment.split("\0"):
        if not record.strip():
            continue
        fields = record.split("\t", 2)
        if len(fields) != 3 or not fields[2]:
            raise Refused("cannot parse NUL-delimited Git numstat output")
        added, deleted, name = fields
        added, deleted = added.strip(), deleted.strip()
        binary = added == "-" or deleted == "-"
        try:
            additions, deletions = (0, 0) if binary else (int(added), int(deleted))
        except ValueError:
            raise Refused("cannot parse Git numstat counts for %s" % name)
        out.append({"filename": name, "binary": binary, "additions": additions,
                    "deletions": deletions, "changes": 1 if binary else additions + deletions})
    return out


def parse_name_status_z(output: str) -> dict[str, str]:
    fields = output.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise Refused("cannot parse NUL-delimited Git name-status output")
    state = {}
    for status, name in zip(fields[::2], fields[1::2]):
        if not status or not name:
            raise Refused("cannot parse NUL-delimited Git name-status output")
        state[name] = {"A": "added", "D": "removed"}.get(status[:1], "modified")
    return state


def layout_from_git(repo_dir: str, base: str, head: str, title: str, body: str) -> dict:
    sep = "\x1e"
    rng_range = "%s..%s" % (base, head)
    log = git(repo_dir, "log", "--reverse", "--format=%H\x1f%P\x1f%an\x1f%ae\x1f%B" + sep, rng_range)
    commits = []
    for rec in [r for r in log.split(sep) if r.strip()]:
        h, parents, an, ae, msg = rec.lstrip("\n").split("\x1f", 4)
        commits.append({"sha": h, "parents": parents.split(),
                        "commit": {"message": msg.rstrip("\n"), "author": {"name": an, "email": ae}}})
    shas = [c["sha"] for c in commits]
    patch_args = ("--no-renames", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
                  "--src-prefix=a/", "--dst-prefix=b/")
    commit_patches = git(repo_dir, "log", "--reverse", "-p", *patch_args,
                         "--format=%H%x00", rng_range)
    commit_numstat = git(repo_dir, "log", "--reverse", "--no-renames", "--numstat", "--no-textconv", "-z",
                         "--format=%H%x00", rng_range)
    patches_by_commit = split_log_records(commit_patches, shas)
    numstat_by_commit = split_log_records(commit_numstat, shas)
    for c in commits:
        sha = c["sha"]
        entries = numstat_entries(numstat_by_commit[sha])
        patches = split_diff(patches_by_commit[sha], [f["filename"] for f in entries])
        for entry in entries:
            name = entry["filename"]
            patch = None if entry["binary"] else patches[name]
            if not entry["binary"] and entry["changes"] > 0 and not patch:
                raise Refused("Git patch has no parsed hunk for changed file %s" % name)
            c.setdefault("added_files", []).append(
                {"filename": name, "patch": patch, "changes": entry["changes"]})
    rng = "%s...%s" % (base, head)
    entries = numstat_entries(git(repo_dir, "diff", "--no-renames", "--numstat", "--no-textconv", "-z", rng))
    state = parse_name_status_z(git(repo_dir, "diff", "--no-renames", "--name-status", "-z", rng))
    patches = split_diff(git(repo_dir, "diff", *patch_args, rng), [f["filename"] for f in entries])
    files = []
    for item in entries:
        name = item["filename"]
        ext = name.rsplit(".", 1)[-1].lower()
        if item["binary"] and ext not in BINARY_EXT and not GENERATED_PATH.search(name):
            raise Refused("cannot measure changed binary or unknown file %s" % name)
        entry = {"filename": name, "status": state.get(name, "modified"),
                 "additions": item["additions"], "deletions": item["deletions"],
                 "changes": item["changes"]}
        if not item["binary"]:
            entry["patch"] = patches[name]
            if item["changes"] > 0 and not entry["patch"]:
                raise Refused("Git patch has no parsed hunk for changed file %s" % name)
        files.append(entry)
    try:
        branch = git(repo_dir, "rev-parse", "--abbrev-ref", head).strip()
    except Refused:
        branch = ""
    return {"title": title, "body": body, "branch": branch, "commits": commits, "files": files,
            "head": git(repo_dir, "rev-parse", head).strip(), "names": sorted(state)}


def render(rep: Report, as_json: bool, gate_line: bool, repo: str, head: str, extra: dict | None = None,
           label: str = "lint") -> int:
    code = EXIT_REFUSED if rep.refused else (EXIT_FAIL if rep.failed else EXIT_PASS)
    if gate_line:
        bad = [i for s, i, _ in rep.items if s in ("FAIL", "REFUSED")]
        if rep.refused:
            first = next(d for s, _, d in rep.items if s == "REFUSED")
            print("REFUSED\t%s" % first)
        elif rep.failed:
            print("FAIL\t%s failed on %s: %s" % (label, head[:12], "; ".join(
                "%s: %s" % (i, d) for s, i, d in rep.items if s == "FAIL")))
        else:
            n = sum(1 for s, _, _ in rep.items if s in ("PASS", "WARN"))
            print("PASS\t%s passed on %s: %d checks%s" % (label, head[:12], n, (
                ", %d warning(s)" % sum(1 for s, _, _ in rep.items if s == "WARN"))
                if any(s == "WARN" for s, _, _ in rep.items) else ""))
        return code
    if as_json:
        out = {"repo": repo, "head": head, "pass": code == EXIT_PASS,
               "refused": rep.refused, "items": [{"status": s, "item": i, "detail": d} for s, i, d in rep.items]}
        out.update(extra or {})
        print(json.dumps(out))
        return code
    for s, i, d in rep.items:
        print("%-7s %-16s %s" % (s, i, d))
    print("LINT: %s" % ("REFUSED" if rep.refused else "FAIL" if rep.failed else "PASS") + (" @ " + head[:12] if head else ""))
    return code


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_lint(a) -> int:
    try:
        if a.layout:
            lay = layout_from_dir(a.layout)
            title, body = lay["title"], lay["body"]
            if a.title is not None:
                title = a.title
            if a.body_file:
                body = open(a.body_file, encoding="utf-8").read()
        else:
            if not (a.git_range and a.repo_dir):
                print("lint needs --layout DIR, or --repo-dir with --git-range BASE..HEAD", file=sys.stderr)
                return EXIT_USAGE
            base, _, head = a.git_range.partition("..")
            if not head:
                print("--git-range must be BASE..HEAD", file=sys.stderr)
                return EXIT_USAGE
            body = open(a.body_file, encoding="utf-8").read() if a.body_file else ""
            lay = layout_from_git(a.repo_dir, base, head, a.title or "", body)
            title = a.title or ""
        nobody = bool(a.no_pr_text)
        if not nobody and not a.layout and not (a.title and a.body_file):
            print("lint needs --title and --body-file (or --no-pr-text for a commits-and-diff-only run)", file=sys.stderr)
            return EXIT_USAGE
        branch = a.branch or lay["branch"]
        rep = lint(a.repo, title, body, branch, lay["commits"], lay["files"], a.stage,
                   a.memory_dir, nobody, a.live)
        if a.open_prs_file and not rep.refused:
            mem = load_memory(a.repo, a.memory_dir)
            br = batch_check(a.repo, branch, title, lay["files"], load_json(a.open_prs_file), mem, a.maintainer_split_url)
            rep.items.extend(br.items)
    except Refused as exc:
        rep = Report()
        rep.add("REFUSED", "lint", str(exc))
        lay = {"head": ""}
    return render(rep, a.json, a.gate_line, a.repo, lay.get("head", ""))


def cmd_batch(a) -> int:
    try:
        lay = layout_from_dir(a.layout)
        mem = load_memory(a.repo, a.memory_dir)
        if a.open_prs_file:
            opens = load_json(a.open_prs_file)
        else:
            me = a.me or os.environ.get("UPSTREAM_AUTHOR_LOGIN") or api_get("/user").get("login")
            if not me:
                raise Refused("cannot resolve our GitHub login (set UPSTREAM_AUTHOR_LOGIN)")
            opens = fetch_open_prs(a.repo, me)
        rep = batch_check(a.repo, lay["branch"], lay["title"], lay["files"], opens, mem, a.maintainer_split_url)
    except Refused as exc:
        rep = Report()
        rep.add("REFUSED", "batch", str(exc))
    return render(rep, a.json, a.gate_line, a.repo, "", label="batch")


def cmd_memory(a) -> int:
    try:
        if a.action in ("init", "refresh"):
            if a.action == "init" and os.path.exists(memory_path(a.repo, a.memory_dir)) and not a.force:
                print("memory for %s already exists; use refresh (or init --force)" % a.repo, file=sys.stderr)
                return EXIT_USAGE
            path = memory_init(a.repo, a.memory_dir, a.action == "refresh")
            print("wrote %s" % path)
        elif a.action == "show":
            mem = load_memory(a.repo, a.memory_dir)
            cats: dict = {}
            for f in mem["feedback"]:
                cats[f["category"]] = cats.get(f["category"], 0) + 1
            print(json.dumps({"repo": mem["repo"], "updated": mem["updated"], "signoff": mem["signoff"],
                              "budget": effective_budget(mem), "rules": [r["id"] for r in mem["rules"]],
                              "feedback": len(mem["feedback"]), "categories": cats}, indent=2))
        elif a.action == "ingest":
            if not a.pr:
                print("ingest needs --pr N", file=sys.stderr)
                return EXIT_USAGE
            me = a.me or os.environ.get("UPSTREAM_AUTHOR_LOGIN")
            added, seen = ingest_feedback(a.repo, a.pr, a.memory_dir, me, a.fixtures, a.any_author, a.dry_run)
            print("ingest %s#%d: %d new%s, %d seen" % (a.repo, a.pr, added, " (dry run, nothing written)" if a.dry_run else "", seen))
    except Refused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_PASS


HOOK_MARK = "# upstream-lint-pre-push (managed by upstream_lint.py install-hook)"


def cmd_receipt_post(a) -> int:
    try:
        post_receipt(a.repo, a.pr, a.head, a.context, a.state, a.description)
    except Refused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    print("recorded %s=%s on %s#%s @ %s" % (a.context, a.state, a.repo, a.pr, a.head[:12]))
    return EXIT_PASS


def cmd_install_hook(a) -> int:
    d = os.path.abspath(a.repo_dir)
    hook_dir = git(d, "rev-parse", "--git-path", "hooks").strip()
    hook_dir = hook_dir if os.path.isabs(hook_dir) else os.path.join(d, hook_dir)
    path = os.path.join(hook_dir, "pre-push")
    if os.path.exists(path) and HOOK_MARK not in open(path, encoding="utf-8").read() and not a.force:
        print("refusing to overwrite an existing pre-push hook at %s (use --force)" % path, file=sys.stderr)
        return EXIT_REFUSED
    os.makedirs(hook_dir, exist_ok=True)
    script = os.path.join(HERE, "upstream_prepush_hook.sh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\n%s\nexec %s \"$@\"\n" % (HOOK_MARK, shlex.quote(script)))
    os.chmod(path, 0o755)
    print("installed %s -> %s" % (path, script))
    return EXIT_PASS


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse exits 2 by default, which here means REFUSED
        self.print_usage(sys.stderr)
        print("%s: usage error: %s" % (self.prog, message), file=sys.stderr)
        sys.exit(EXIT_USAGE)


def main(argv=None) -> int:
    p = _Parser(prog="upstream_lint.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)
    pl = sub.add_parser("lint")
    pl.add_argument("--repo", required=True, help="upstream owner/repo")
    pl.add_argument("--layout")
    pl.add_argument("--repo-dir")
    pl.add_argument("--git-range", help="BASE..HEAD of the commits to push")
    pl.add_argument("--title")
    pl.add_argument("--body-file")
    pl.add_argument("--branch")
    pl.add_argument("--stage", choices=["first-push", "ready"], default="first-push")
    pl.add_argument("--no-pr-text", action="store_true", help="lint commits, branch and diff only")
    pl.add_argument("--live", action="store_true", help="also confirm the upstream PR template is unchanged")
    pl.add_argument("--open-prs-file")
    pl.add_argument("--maintainer-split-url")
    pl.add_argument("--memory-dir")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--gate-line", action="store_true")
    pb = sub.add_parser("batch")
    pb.add_argument("--repo", required=True)
    pb.add_argument("--layout", required=True)
    pb.add_argument("--open-prs-file")
    pb.add_argument("--me")
    pb.add_argument("--maintainer-split-url")
    pb.add_argument("--memory-dir")
    pb.add_argument("--json", action="store_true")
    pb.add_argument("--gate-line", action="store_true")
    pm = sub.add_parser("memory")
    pm.add_argument("action", choices=["init", "refresh", "show", "ingest"])
    pm.add_argument("--repo", required=True)
    pm.add_argument("--pr", type=int)
    pm.add_argument("--me")
    pm.add_argument("--fixtures")
    pm.add_argument("--any-author", action="store_true")
    pm.add_argument("--dry-run", action="store_true", help="ingest: count unseen feedback, write nothing")
    pm.add_argument("--force", action="store_true")
    pm.add_argument("--memory-dir")
    ps = sub.add_parser("receipt-post", help="record a receipt as a comment on the fork mirror PR")
    ps.add_argument("--repo", required=True, help="the fork repo that holds the mirror PR")
    ps.add_argument("--pr", type=int, required=True, help="mirror PR number")
    ps.add_argument("--head", required=True)
    ps.add_argument("--context", required=True)
    ps.add_argument("--state", required=True)
    ps.add_argument("--description", required=True)
    ph = sub.add_parser("install-hook")
    ph.add_argument("--repo-dir", required=True)
    ph.add_argument("--force", action="store_true")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_usage(sys.stderr)
        print("upstream_lint.py: no command given; nothing was measured", file=sys.stderr)
        return EXIT_REFUSED
    return {"lint": cmd_lint, "batch": cmd_batch, "memory": cmd_memory, "install-hook": cmd_install_hook,
            "receipt-post": cmd_receipt_post}[a.cmd](a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # fail closed: a crash is a refusal, never a pass
        print("upstream_lint.py: REFUSED: internal error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(EXIT_REFUSED)
