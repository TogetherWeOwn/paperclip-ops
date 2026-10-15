#!/usr/bin/env python3
"""case_a84d4bc9 stager: build blinded reviewer bundles from git objects only.

Scorer-side tool. Reviewer-visible output goes to ARENA/<C-ID>/ and contains only:
  reviewer_view.json  title (+ no description)       (frozen prompt reads this)
  pr.diff             merge-base..last-reviewed head, `index` lines stripped
  tree/               the full head tree, minus files that trip the leak screen
Everything else (C-ID map, Greptile data, held-out phrases, provenance) stays under
SCORER and is never written into ARENA.

Usage: stage.py [--only C-01,C-02] [--map map.json]
"""
import json, os, re, subprocess, sys, shutil, argparse, hashlib, pathlib, stat, tarfile

SCORER = "/paperclip/instances/default/workspaces/00000000-0000-4000-8000-000000000121/case_79eba354"
ARENA = "/paperclip/instances/default/workspaces/00000000-0000-4000-8000-000000000121/arena"
UPSTREAM = SCORER + "/upstream"
GREPTILE = SCORER + "/evidence/case_79eba354_greptile.json"

# C-ID -> upstream PR (scorer-only; never reviewer-visible)
MAP = {"C-01": 15049, "C-02": 15024, "C-03": 15015, "C-04": 14961, "C-05": 14918,
       "C-06": 15007, "C-07": 14979, "C-08": 15020, "C-09": 14863, "C-10": 14996}

# Diff/title/view screen: the vendor name plus phrases lifted from the other tool's output.
GLOBAL = [r"greptile", r"retrigger", r"confidence score", r"safe to merge",
          r"last reviewed commit", r"reviewer g\b"]
# Tree screen: only the vendor name. The generic phrases occur in ordinary prose all over a
# repo and carry no signal there; the diff/title screen above is where they would leak.
TREE_GLOBAL = [r"greptile"]


def sh(args, cwd=None, inp=None, check=True):
    r = subprocess.run(args, cwd=cwd, input=inp, capture_output=True, text=False)
    if check and r.returncode != 0:
        raise RuntimeError(f"{args[:3]} failed: {r.stderr.decode()[:300]}")
    return r.stdout


def last_reviewed(pr):
    sums = pr["greptile"]["summaries"]
    sums = sorted(sums, key=lambda s: s["created_at"])
    for s in reversed(sums):
        m = re.findall(r"commit/([0-9a-f]{40})", s["body"])
        if m:
            return m[-1]
    return None


def own_patterns(n):
    return [rf"(?<![0-9A-Za-z]){n}(?![0-9A-Za-z])", rf"pull/{n}\b"]


def finding_phrases(pr):
    out = []
    for f in pr["greptile"].get("findings", []):
        t = (f.get("title") or "").strip().lower()
        if len(t.split()) >= 3 and len(t) >= 16:
            out.append(t)
    return sorted(set(out))


class GateError(RuntimeError):
    """The bundle was not completely measured; never a release pass."""


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 50000


def bundle_entries(root):
    """Inspect names/types before reading; symlinks and scanner errors fail closed."""
    root = pathlib.Path(root)
    if root.is_symlink() or not root.is_dir():
        raise GateError("bundle root is not an ordinary directory")
    count, total = 0, 0
    def failed(error):
        raise GateError("cannot enumerate bundle") from error
    try:
        for dp, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            for name in sorted(dirs + files):
                p = pathlib.Path(dp) / name
                mode = p.lstat().st_mode
                count += 1
                if count > MAX_ENTRIES:
                    raise GateError("bundle entry budget exceeded")
                if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise GateError("symlink or special entry in bundle")
                if stat.S_ISREG(mode):
                    size = p.stat().st_size
                    total += size
                    if size > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
                        raise GateError("bundle byte budget exceeded")
                yield p.relative_to(root).as_posix(), p, stat.S_ISDIR(mode)
    except OSError as e:
        raise GateError("cannot inspect bundle") from e


def grep_files(root, patterns, fixed=False):
    """Screen every name/byte; unscreenable assets are hits, not silent skips."""
    hits = set()
    for rel, p, is_dir in bundle_entries(root):
        if screen_text(rel, patterns, fixed=fixed):
            hits.add(rel)
        if is_dir:
            continue
        try:
            raw = p.read_bytes()
            # Images/other binary assets are omitted or cause exclusion, not exposed to Read.
            text = raw.decode("utf-8", "strict")
        except UnicodeError:
            hits.add(rel)
            continue
        except OSError as e:
            raise GateError("cannot read bundle entry") from e
        if b"\0" in raw or screen_text(text, patterns, fixed=fixed):
            hits.add(rel)
    return hits


def extract_archive(fileobj, destination):
    """Reject links/traversal before extraction; data_filter alone permits some links.

    Python 3.13: https://docs.python.org/3.13/library/tarfile.html#extraction-filters
    """
    seen, total = set(), 0
    with tarfile.open(fileobj=fileobj, mode="r|") as archive:
        for member in archive:
            name = pathlib.PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise GateError("unsafe archive name")
            if not (member.isdir() or member.isfile()) or member.name in seen:
                raise GateError("link, special or duplicate archive entry")
            seen.add(member.name)
            total += member.size
            if (len(seen) > MAX_ENTRIES or member.size > MAX_FILE_BYTES
                    or total > MAX_TREE_BYTES):
                raise GateError("archive budget exceeded")
            archive.extract(member, destination, filter="data")


def snapshot_bundle(root):
    entries = {}
    for rel, p, is_dir in bundle_entries(root):
        entries[rel] = {"kind": "directory"} if is_dir else {
            "kind": "file", "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    return entries


def manifest_path(cid):
    if cid not in MAP:
        raise GateError("unknown cell")
    return pathlib.Path(SCORER) / "evidence" / "bundle-manifests" / (cid + ".json")


def corpus_pr(cid):
    if cid not in MAP:
        raise GateError("unknown cell")
    try:
        data = json.loads(pathlib.Path(GREPTILE).read_text())
        return next(pr for pr in data["prs"] if pr["number"] == MAP[cid])
    except (OSError, ValueError, KeyError, StopIteration) as e:
        raise GateError("corpus screen unavailable") from e


def verify_bundle(cid):
    """Re-screen and require exact frozen bytes plus scorer-only reviewed-head binding."""
    try:
        manifest = json.loads(manifest_path(cid).read_text())
        root = pathlib.Path(ARENA) / cid
        pr = corpus_pr(cid)
        head = last_reviewed(pr)
        if (manifest.get("schema") != "blinded-bundle-v1" or manifest.get("cid") != cid
                or not head or manifest.get("reviewed_head") != head
                or not manifest.get("entries")
                or snapshot_bundle(root) != manifest["entries"]
                or not {"pr.diff", "reviewer_view.json", "tree"}.issubset(manifest["entries"])):
            raise GateError("bundle differs from its frozen manifest/head")
        own, phrases = own_patterns(MAP[cid]), finding_phrases(pr)
        hits = grep_files(root, TREE_GLOBAL + own) | grep_files(root, phrases, fixed=True)
        for name in ("pr.diff", "reviewer_view.json"):
            text = (root / name).read_text(encoding="utf-8", errors="strict")
            if screen_text(text, GLOBAL + own) or screen_text(text, phrases, fixed=True):
                hits.add(name)
        if hits:
            raise GateError("frozen bundle no longer passes its current held-out screen")
    except (OSError, ValueError) as e:
        raise GateError("bundle manifest missing or unreadable") from e
    return manifest


def screen_text(text, patterns, fixed=False):
    low = text.lower()
    hit = []
    for p in patterns:
        if fixed:
            if p in low:
                hit.append(p)
        elif re.search(p, text, re.I):
            hit.append(p)
    return hit


def build(cid, num, data):
    pr = next(p for p in data["prs"] if p["number"] == num)
    head = last_reviewed(pr)
    rec = {"cid": cid, "pr": num, "head": head, "recorded_base": pr["base_sha"], "status": None}
    if not head:
        rec["status"] = "EXCLUDED: no last-reviewed commit"; return rec
    for sha in (head, pr["base_sha"]):
        if subprocess.run(["git", "cat-file", "-e", sha], cwd=UPSTREAM).returncode != 0:
            rec["status"] = f"EXCLUDED: object {sha[:12]} missing locally"; return rec
    mb = sh(["git", "merge-base", pr["base_sha"], head], cwd=UPSTREAM).decode().strip()
    rec["merge_base"] = mb
    diff = sh(["git", "diff", mb, head], cwd=UPSTREAM).decode("utf-8", "replace")
    diff = "".join(l for l in diff.splitlines(True) if not l.startswith("index "))
    own = own_patterns(num)
    phrases = finding_phrases(pr)
    d_hits = screen_text(diff, GLOBAL + own) + screen_text(diff, phrases, fixed=True)
    if d_hits:
        rec["status"] = f"EXCLUDED: diff hit {d_hits[:4]}"; return rec
    title = pr["title"]
    t_hits = screen_text(title, GLOBAL + own) + screen_text(title, phrases, fixed=True)
    if t_hits:
        rec["status"] = f"EXCLUDED: title hit {t_hits[:4]}"; return rec
    changed = set(sh(["git", "diff", "--name-only", mb, head], cwd=UPSTREAM).decode().splitlines())
    out = os.path.join(ARENA, cid)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out + "/tree")
    archive = subprocess.Popen(["git", "archive", head], cwd=UPSTREAM,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        extract_archive(archive.stdout, out + "/tree")
        if archive.wait() != 0:
            raise GateError("git archive failed")
    except (GateError, OSError, tarfile.TarError):
        shutil.rmtree(out, ignore_errors=True)
        rec["status"] = "EXCLUDED: archive not completely measured"
        return rec
    finally:
        archive.stdout.close()
        if archive.poll() is None:
            archive.kill()
            archive.wait()
    tree = out + "/tree"
    hits = grep_files(tree, TREE_GLOBAL + own) | grep_files(tree, phrases, fixed=True)
    # A changed doc may be redacted in place (line numbers preserved); a changed non-doc
    # file may not, so the PR is excluded instead.
    redacted = []
    for h in sorted(x for x in hits if x in changed):
        if not h.endswith(".md"):
            shutil.rmtree(out, ignore_errors=True)
            rec["status"] = f"EXCLUDED: changed non-doc file hit {h}"; return rec
        fp = os.path.join(tree, h)
        txt = open(fp, encoding="utf-8", errors="replace").read()
        for pat in TREE_GLOBAL + own:
            txt = re.sub(pat, "[redacted]", txt, flags=re.I)
        for ph in phrases:
            txt = re.sub(re.escape(ph), "[redacted]", txt, flags=re.I)
        open(fp, "w", encoding="utf-8").write(txt)
        redacted.append(h)
    dropped = sorted(h for h in hits if h not in changed)
    for h in dropped:
        p = os.path.join(tree, h)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.lexists(p):
            os.remove(p)
    open(out + "/pr.diff", "w").write(diff)
    json.dump({"title": title, "description": ""}, open(out + "/reviewer_view.json", "w"), indent=2)
    # final whole-bundle re-scan (reviewer-visible bytes only)
    left = grep_files(out, TREE_GLOBAL + own) | grep_files(out, phrases, fixed=True)
    for vf in ("pr.diff", "reviewer_view.json"):
        txt = open(os.path.join(out, vf), encoding="utf-8", errors="replace").read()
        if screen_text(txt, GLOBAL + own) + screen_text(txt, phrases, fixed=True):
            left.add(vf)
    if left:
        shutil.rmtree(out, ignore_errors=True)
        rec["status"] = f"EXCLUDED: residual hit after redaction {sorted(left)[:3]}"; return rec
    manifest = {"schema": "blinded-bundle-v1", "cid": cid, "reviewed_head": head,
                "entries": snapshot_bundle(out)}
    mf = manifest_path(cid)
    mf.parent.mkdir(parents=True, exist_ok=True)
    temporary = mf.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    temporary.replace(mf)
    nfiles = sum(len(f) for _, _, f in os.walk(tree))
    rec.update(status="OK", diff_lines=diff.count("\n"), changed_files=len(changed),
               tree_files=nfiles, dropped_files=len(dropped), dropped_sample=dropped[:8],
               redacted_changed_docs=redacted,
               finding_phrases=len(phrases), manifest_schema=manifest["schema"])
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    a = ap.parse_args()
    data = json.load(open(GREPTILE))
    os.makedirs(ARENA, exist_ok=True)
    only = set(a.only.split(",")) if a.only else None
    if only and not only.issubset(MAP):
        ap.error("unknown cell in --only")
    report, measurement_failed = [], False
    for cid, num in MAP.items():
        if only and cid not in only:
            continue
        try:
            rec = build(cid, num, data)
        except (GateError, OSError, UnicodeError) as e:
            measurement_failed = True
            shutil.rmtree(os.path.join(ARENA, cid), ignore_errors=True)
            manifest_path(cid).unlink(missing_ok=True)
            rec = {"cid": cid, "pr": num, "status": "EXCLUDED: scanner/manifest failure", "cause": str(e)}
        report.append(rec)
        print(json.dumps({k: v for k, v in rec.items() if k != "dropped_sample"}))
    json.dump(report, open(SCORER + "/evidence/stage_report_v4.json", "w"), indent=1)
    if measurement_failed:
        sys.exit(3)


if __name__ == "__main__":
    main()
