#!/usr/bin/env python3
# ===========================================================================
# kofra_preview_parity.py — does the checked-in staging Worker config still
# match the checked-in prod default?
#
# WHY THIS EXISTS (case_db6cb3f2, preview slice of case_6454e394). Kofra's preview
# deploys aim disposable `kofra-pr-N` workers from one checked-in file,
# `serverless/wrangler.jsonc`: the `staging` env block is the staging config
# and the TOP-LEVEL fields are the prod default wrangler deploys when no
# `--env` is passed (and the defaults every env inherits). A slice that adds a
# var, a binding or a route to staging and forgets prod — or lands prod-only
# hardening nobody replays to staging — ships a preview the owner cannot
# trust to represent prod. case_75ce2b76 proved the staging surface hygienic; this
# proves the two sides still agree with each other.
#
# WHAT IT COMPARES. The deployment-bearing surface only: worker name, entry
# point, compatibility_date, ingress (workers_dev, preview_urls, routes), var
# NAMES, and resource bindings. Var VALUES are never compared and never
# printed: checked-in values are placeholders by policy, and a tool that
# prints values trains operators to paste real ones into the file it reads.
# Instead a non-empty checked-in value is itself a DRIFT finding named by var
# name alone — secrets reach the Worker through `wrangler secret put`, never
# through this file.
#
# WHY THE WORKER NAME IS A SEMANTIC CHECK, NOT A DIFF ROW. Staging MUST carry
# its own worker name: equal names mean a staging deploy lands on the prod
# worker, which is the incident, not parity. So equal names are drift by
# construction, differing names are the required state (never a diff row),
# and worker.name can never be allowlisted — the script REFUSES (exit 2) on
# such an entry rather than honouring it. (An allowlist entry is a path the
# detector stops watching; a path the detector must never stop watching
# does not belong in one, and silently ignoring the entry would be the same
# hole with better paperwork.)
#
# WHY THE PROD SIDE IS TOP-LEVEL, AND WHEN IT STOPS BEING THAT. The checked-in
# config defines no `env.production` block, so the prod deploy surface IS the
# top-level fields. If a later slice adds `env.production`, this script takes
# that block as the prod side instead — and the doc's baseline section says
# so, so the switch is a reviewed event rather than a silent redefinition.
#
# EXIT CODES are the interface; callers gate on these, never on printed text.
#   0  parity: every surface path matches (allowlisted rows named ALLOWED)
#   1  drift: at least one path differs, a semantic gate tripped, or a
#      checked-in var carries a value. 1 is the conventional diff-found code,
#      so `set -e` and CI treat it as failure without translation.
#   2  refused: measured nothing (unreadable ref, missing file, unparseable
#      JSONC, missing staging env, broken allowlist). NEVER conflated with 0.
#
# SUBCOMMANDS.
#   check   compare staging vs prod, exit 0/1/2 as above.
#   surface print the flattened surface both sides are compared on (evidence).
#
# Both read the config out of a git ref with `git show` (default
# `--ref origin/main`), so the comparison is against what is COMMITTED, not
# what is in the working tree. `--staging-ref` / `--prod-ref` compare across
# refs (e.g. a staging branch against main). `--config FILE` reads one file
# directly for tests and for working-tree pre-checks; it is NOT the evidence
# path, because an uncommitted file is not what deploys.
#
# MACHINE-READABLE VERDICTS (`check --json`, schema_version 1). Exit codes
# stay the interface, but agents scoping a fix need WHICH paths drifted
# without scraping the prose above. `--json` prints exactly one JSON
# document on stdout (one trailing newline, nothing else): schema_version,
# tool, command, verdict (parity/drift/refused), prod_side label, per-path
# rows (stable surface-path IDs, canonical side values or null, row verdict
# match/drift/allowed/refused), semantic_gates (worker.name + valued vars,
# pass/drift with redacted detail), summary tallies, and error (null unless
# refused: stable code, retryable, next_action). Refusals (exit 2) still
# print a parseable envelope — verdict refused, error populated — so a
# gating agent never scrapes REFUSED prose for the code; the prose still
# goes to stderr for humans. Var VALUES never appear in JSON bytes (rows
# carry '"present"', gates name the var only). Text output is byte-identical
# when `--json` is absent. Stable error codes: config_unreadable,
# config_unparseable, config_missing_staging, allowlist_unreadable,
# allowlist_malformed, allowlist_forbidden_path, allowlist_stale_entry.
#
# NO PLAN MODE (`--dry-run` is refused). Any exit-0 output means parity was
# measured; a `--dry-run` that exits 0 without measuring would be a gating
# lie. So `--dry-run` exits 2 naming the refusal, on both subcommands. The
# offline pre-check equivalent is `--config` (reads one file, still
# measures). Unknown flags refuse with usage + exit 2 (argparse).
# ===========================================================================
import argparse
import json
import re
import subprocess
import sys

ME = "kofra_preview_parity.py"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_REFUSED = 2

SCHEMA_VERSION = 1
TOP_LEVEL_PROD = "top-level (no env.production block)"
ENV_PROD = "env.production"


class Refusal(Exception):
    # A measured-nothing exit-2 with a stable machine-readable code. Helpers
    # raise this instead of printing: cmd_check catches it and either prints
    # the historical REFUSED prose (text mode, byte-identical) or the prose
    # plus a parseable JSON envelope (JSON mode). `path` names the offending
    # surface path when the refusal is about one (forbidden/stale allowlist
    # entries); `next_action` never carries config values.
    def __init__(self, code, message, next_action, path=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action
        self.path = path


def refuse(code, message, next_action, path=None):
    raise Refusal(code, message, next_action, path)

# Resource bindings that cost money, need product activation, or reach
# customer-adjacent state. Mirrors DENIED_RESOURCE_BINDINGS in Kofra's
# serverless/scripts/lib.mjs: the comparison stays meaningful when slices
# add bindings, because any binding key either side carries is surfaced by
# exact key — this list exists so the report can name which ones are
# deployment-bearing, not to enumerate what may exist.
KNOWN_BINDINGS = (
    "d1_databases",
    "r2_buckets",
    "kv_namespaces",
    "queues",
    "workflows",
    "durable_objects",
    "containers",
    "ai",
    "hyperdrive",
    "vectorize",
    "pipelines",
    "browser",
    "dispatch_namespaces",
)

DEFAULT_ALLOWLIST = "kofra_preview_parity_allowlist.txt"


def die(msg):
    print("REFUSED %s :: %s" % (ME, msg), file=sys.stderr)
    sys.exit(EXIT_REFUSED)


def strip_jsonc_comments(text):
    # String-aware comment strip, same algorithm as Kofra's stripJsoncComments
    # in serverless/scripts/lib.mjs. A regex-only strip turns "https://..."
    # into "https:" and then blames JSON parsing; the scanner below tracks
    # string state and escapes instead.
    out = []
    i, n, in_string = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_config_text(repo, ref):
    cmd = ["git", "-C", repo, "show", "%s:serverless/wrangler.jsonc" % ref]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        refuse("config_unreadable",
               "cannot run git show for %s in %s: %s" % (ref, repo, e),
               "check the --ref/--staging-ref/--prod-ref value and the repo "
               "path, then retry with a readable ref")
    if p.returncode != 0:
        refuse("config_unreadable",
               "cannot read serverless/wrangler.jsonc at %s in %s: %s"
               % (ref, repo, p.stderr.strip().splitlines()[:1]),
               "check the ref exists and carries serverless/wrangler.jsonc, "
               "then retry")
    return p.stdout


def parse_config(text, label):
    try:
        cfg = json.loads(strip_jsonc_comments(text))
    except json.JSONDecodeError as e:
        refuse("config_unparseable",
               "%s is not valid JSONC: %s" % (label, e),
               "fix the JSONC syntax error at the reported offset so the "
               "config parses to an object")
    if not isinstance(cfg, dict):
        refuse("config_unparseable",
               "%s parses to %s, not an object" % (label, type(cfg).__name__),
               "fix the config so it parses to a top-level object with an "
               "env.staging block")
    return cfg


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def resolve_sides(cfg, label):
    # Prod side: an explicit env.production block wins when present; else the
    # top-level fields ARE the prod deploy surface (wrangler deploys top-level
    # when no --env is passed). The switch is structural, read from the file,
    # never a flag — a flag would let a run redefine "prod" until it matches.
    envs = cfg.get("env")
    if not isinstance(envs, dict) or "staging" not in envs:
        refuse("config_missing_staging",
               "%s defines no env.staging — nothing staging-side to compare"
               % label,
               "add an env.staging object to the config — without it there "
               "is nothing staging-side to compare")
    staging = envs["staging"]
    if not isinstance(staging, dict):
        refuse("config_missing_staging",
               "%s env.staging is not an object" % label,
               "fix env.staging so it is an object carrying the staging "
               "Worker config")
    if isinstance(envs.get("production"), dict):
        prod, prod_label = envs["production"], ENV_PROD
    else:
        prod, prod_label = cfg, TOP_LEVEL_PROD
    if not isinstance(prod, dict):
        refuse("config_unparseable",
               "%s prod side is not an object" % label,
               "fix the prod side (top-level or env.production) so it is "
               "an object")
    return staging, prod, prod_label


def worker_name_of(node, cfg_name):
    own = node.get("name")
    if isinstance(own, str) and own:
        return own
    return cfg_name


def flatten(name, node, cfg_name, top=None):
    # One sorted {path: canonical-value} map per side. Vars flatten to a
    # presence marker plus a has-value flag held OUT of the compared map: the
    # value itself never enters output, on any path, in any mode.
    #
    # main/compatibility_date INHERIT: wrangler resolves a missing env-level
    # key from top-level, so an env that omits them deploys WITH them. Reading
    # the env node alone reports "staging has no entrypoint" on every real
    # config — a permanent false drift. The fallback below mirrors the deploy
    # resolution; `top` is None only when the node IS top-level.
    base = top if isinstance(top, dict) else node
    surface = {}
    valued_vars = []
    surface["worker.name"] = canon(worker_name_of(node, cfg_name))
    surface["entry.main"] = canon(
        node.get("main", base.get("main")) if isinstance(node, dict) else None)
    surface["compat.date"] = canon(
        node.get("compatibility_date", base.get("compatibility_date"))
        if isinstance(node, dict) else None)
    surface["ingress.workers_dev"] = canon(node.get("workers_dev"))
    surface["ingress.preview_urls"] = canon(node.get("preview_urls"))
    surface["ingress.routes"] = canon(node.get("routes"))
    varnode = node.get("vars")
    varnames = sorted(varnode.keys()) if isinstance(varnode, dict) else []
    for v in varnames:
        surface["vars.%s" % v] = canon("present")
        if varnode[v] != "":
            valued_vars.append(v)
    for b in sorted(set(KNOWN_BINDINGS) | set(
            k for k in node.keys() if k not in (
                "name", "main", "compatibility_date", "workers_dev",
                "preview_urls", "routes", "vars", "env", "$schema"))):
        if b in node and node[b] not in (None, [], {}):
            surface["bindings.%s" % b] = canon(node[b])
    return surface, valued_vars


# Surface paths that can never be allowlisted. worker.name is compared
# NOWHERE (see compare): an entry naming it would be a standing permission
# for staging to aim at prod. load_allowlist REFUSES on it rather than
# honouring it — and rather than silently ignoring it, which would be the
# same hole with better paperwork.
NEVER_ALLOWLISTED = frozenset({"worker.name"})


def load_allowlist(path):
    # Format: <surface-path><TAB><reason>. A reason is MANDATORY: an
    # unreasoned exemption is indistinguishable from silencing the detector.
    # Blank lines and #-comments are ignored. Returns {path: reason}.
    allowed = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        refuse("allowlist_unreadable",
               "cannot read allowlist %s: %s" % (path, e),
               "check the --allowlist path and retry with a readable file")
    for lineno, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            refuse("allowlist_malformed",
                   "allowlist %s line %d has no <path><TAB><reason> — "
                   "refusing rather than honouring a silent exemption"
                   % (path, lineno),
                   "fix allowlist %s line %d to <surface-path><TAB><reason> "
                   "— every entry needs a reason" % (path, lineno))
        if parts[0].strip() in NEVER_ALLOWLISTED:
            refuse("allowlist_forbidden_path",
                   "allowlist %s line %d names %s, which can never be "
                   "allowlisted — equal worker names are an incident, not parity"
                   % (path, lineno, parts[0].strip()),
                   "remove %s from allowlist %s — it can never be "
                   "allowlisted, equal worker names are an incident"
                   % (parts[0].strip(), path),
                   path=parts[0].strip())
        allowed[parts[0].strip()] = parts[1].strip()
    return allowed


def read_config_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        refuse("config_unreadable",
               "cannot read config file %s: %s" % (path, e),
               "check the --config path and retry with a readable file")


# worker.name is compared NOWHERE below: differing names are the required
# state (a staging deploy must not land on prod), equal names are the
# semantic finding above. A diff row on this path would force operators to
# allowlist the one divergence that must never be silenced.
NON_DIFF_PATHS = frozenset({"worker.name"})


def compare(staging_surface, prod_surface, staging_valued, prod_valued,
            staging_name, prod_name, allowed):
    drift, semantic, reported_allowed = [], [], []
    if staging_name == prod_name:
        semantic.append("worker.name :: staging and prod resolve to the same "
                        "worker name — a staging deploy would land on prod")
    for v in sorted(set(staging_valued) | set(prod_valued)):
        semantic.append("vars.%s :: checked-in var carries a value "
                        "(secrets reach the Worker via secret put, never via config)" % v)
    for path in sorted(set(staging_surface) | set(prod_surface)):
        if path in NON_DIFF_PATHS:
            continue
        s = staging_surface.get(path)
        p = prod_surface.get(path)
        if s == p:
            continue
        if path in allowed:
            reported_allowed.append(path)
        else:
            drift.append((path, s, p))
    return drift, semantic, reported_allowed


def refuse_dry_run(subcommand):
    # Decided by the acceptance card: there is no plan mode. Any exit-0
    # output means parity was measured; `--config` is the offline
    # equivalent for pre-checks. Text-only refusal on purpose: no verdict
    # and no JSON on stdout, so no caller can mistake it for measured
    # parity. Names --dry-run, the refusal and usage on stderr.
    print("usage: %s %s --config FILE --allowlist FILE [--json]"
          % (ME, subcommand), file=sys.stderr)
    print("REFUSED %s :: --dry-run is refused: any exit-0 output means "
          "parity was measured, and there is no plan mode (use --config "
          "for working-tree pre-checks)" % ME, file=sys.stderr)
    sys.exit(EXIT_REFUSED)


def build_rows(staging_surface, prod_surface, drift_paths, allowed_paths,
               refused_paths=()):
    # Every compared surface path exactly once, sorted, with canonical side
    # values (identical bytes to the `surface` TSV column) or null when the
    # path is absent on that side. worker.name is compared nowhere, so it
    # never appears here — refusal envelopes synthesize it separately.
    refused = set(refused_paths)
    rows = []
    for path in sorted(set(staging_surface) | set(prod_surface)):
        if path in NON_DIFF_PATHS:
            continue
        if path in refused:
            verdict = "refused"
        elif path in drift_paths:
            verdict = "allowed" if path in allowed_paths else "drift"
        else:
            verdict = "match"
        rows.append({"id": path,
                     "staging": staging_surface.get(path),
                     "prod": prod_surface.get(path),
                     "verdict": verdict})
    return rows


def build_gates(staging_worker, prod_worker, staging_valued, prod_valued):
    # Worker name always gates (differing names are the required state);
    # each value-carrying var gates by name alone. Details never carry
    # checked-in values — the valued-var detail names the var only.
    if staging_worker == prod_worker:
        worker = {"id": "worker.name", "outcome": "drift",
                  "detail": "worker.name :: staging and prod resolve to the "
                            "same worker name — a staging deploy would land "
                            "on prod"}
    else:
        worker = {"id": "worker.name", "outcome": "pass",
                  "detail": "worker.name :: staging and prod worker names "
                            "differ as required (staging must not aim at "
                            "prod)"}
    gates = [worker]
    for v in sorted(set(staging_valued) | set(prod_valued)):
        gates.append({"id": "vars.%s" % v, "outcome": "drift",
                      "detail": "vars.%s :: checked-in var carries a value "
                                "(secrets reach the Worker via secret put, "
                                "never via config)" % v})
    return gates


def summarize(rows, gates):
    summary = {"match": 0, "drift": 0, "allowed": 0, "refused": 0,
               "semantic_drift": 0}
    for row in rows:
        summary[row["verdict"]] += 1
    summary["semantic_drift"] = sum(
        1 for g in gates if g["outcome"] == "drift")
    return summary


def emit_envelope(env):
    # Stdout is exactly one JSON document plus one trailing newline:
    # sorted keys and compact separators keep bytes deterministic across
    # runs so agents can diff or hash them.
    print(json.dumps(env, sort_keys=True, separators=(",", ":")))


def build_success_envelope(prod_label, staging_surface, prod_surface,
                           staging_valued, prod_valued,
                           staging_worker, prod_worker,
                           drift, reported_allowed):
    # reported_allowed paths diverge too (they are just excused): include
    # them in drift_paths so build_rows reaches the allowed branch instead
    # of falling through to match.
    drift_paths = {path for path, _, _ in drift} | set(reported_allowed)
    allowed_paths = set(reported_allowed)
    rows = build_rows(staging_surface, prod_surface,
                      drift_paths, allowed_paths)
    gates = build_gates(staging_worker, prod_worker,
                        staging_valued, prod_valued)
    n = len(drift) + sum(1 for g in gates if g["outcome"] == "drift")
    return {"schema_version": SCHEMA_VERSION,
            "tool": ME,
            "command": "check",
            "verdict": "drift" if n else "parity",
            "prod_side": prod_label,
            "rows": rows,
            "semantic_gates": gates,
            "summary": summarize(rows, gates),
            "error": None}


def build_refusal_envelope(err, surfaces):
    # Exit-2 runs still print a parseable envelope so gating agents never
    # scrape REFUSED prose for the code. Parse/config failures carry empty
    # rows and gates (nothing was measured); allowlist failures keep the
    # measured surface with the refused path rewritten to "refused" (or, for
    # worker.name — compared nowhere — a synthesized row carrying both
    # worker names). retryable is always false: every refusal here is a
    # file-content problem, never transient.
    error = {"code": err.code, "retryable": False,
             "next_action": err.next_action}
    if surfaces is None:
        rows, gates, prod_label = [], [], TOP_LEVEL_PROD
    else:
        (staging_surface, prod_surface, staging_valued, prod_valued,
         staging_worker, prod_worker, prod_label, drift_paths,
         allowed_paths) = surfaces
        refused_paths = {err.path} if err.path else set()
        rows = build_rows(staging_surface, prod_surface,
                          drift_paths, allowed_paths, refused_paths)
        if err.path and err.path in NON_DIFF_PATHS:
            rows.append({"id": err.path,
                         "staging": staging_worker,
                         "prod": prod_worker,
                         "verdict": "refused"})
            rows.sort(key=lambda r: r["id"])
        elif err.path and err.path not in {r["id"] for r in rows}:
            rows.append({"id": err.path, "staging": None, "prod": None,
                         "verdict": "refused"})
            rows.sort(key=lambda r: r["id"])
        gates = build_gates(staging_worker, prod_worker,
                            staging_valued, prod_valued)
    return {"schema_version": SCHEMA_VERSION,
            "tool": ME,
            "command": "check",
            "verdict": "refused",
            "prod_side": prod_label,
            "rows": rows,
            "semantic_gates": gates,
            "summary": summarize(rows, gates),
            "error": error}


def cmd_surface(args):
    if args.dry_run:
        refuse_dry_run("surface")
    try:
        text = (read_config_file(args.config) if args.config
                else load_config_text(args.repo, args.ref))
        cfg = parse_config(text, args.config or args.ref)
        staging, prod, prod_label = resolve_sides(cfg, args.config or args.ref)
    except Refusal as e:
        die(e.message)
    cfg_name = cfg.get("name") if isinstance(cfg.get("name"), str) else ""
    for side, node in (("staging", staging), ("prod", prod)):
        surface, valued = flatten(side, node, cfg_name, top=cfg)
        for path in sorted(surface):
            print("%s\t%s\t%s" % (side, path, surface[path]))
        for v in sorted(valued):
            print("%s\tvars.%s\tHAS-CHECKED-IN-VALUE" % (side, v))
    print("# prod side: %s" % prod_label)


def cmd_check(args):
    if args.dry_run:
        refuse_dry_run("check")
    # surfaces_for_refusal stays None until both sides are flattened: config
    # failures refuse with an empty envelope, allowlist failures with the
    # measured surface. The tuple shape matches build_refusal_envelope.
    surfaces_for_refusal = None
    try:
        if args.config:
            text = read_config_file(args.config)
            label = args.config
            cfg = parse_config(text, label)
            staging, prod, prod_label = resolve_sides(cfg, label)
            ref_note = "file %s" % args.config
        else:
            s_ref = args.staging_ref or args.ref
            p_ref = args.prod_ref or args.ref
            s_cfg = parse_config(load_config_text(args.repo, s_ref), s_ref)
            p_cfg = parse_config(load_config_text(args.repo, p_ref), p_ref)
            s_staging, _, _ = resolve_sides(s_cfg, s_ref)
            _, p_prod, prod_label = resolve_sides(p_cfg, p_ref)
            staging, prod = s_staging, p_prod
            ref_note = "staging=%s prod=%s repo=%s" % (s_ref, p_ref, args.repo)
        s_name = worker_name_of(staging, "")
        p_name = worker_name_of(prod, "")
        # Top-level inheritance mirrors wrangler: a staging env that omits
        # main/compatibility_date deploys WITH the top-level values. Each side
        # falls back to its own file's top level (cross-ref mode keeps each
        # side's own inheritance — borrowing the other file's top level would
        # compare staging's resolved values against the wrong defaults).
        s_top = s_cfg if not args.config else cfg
        p_top = p_cfg if not args.config else cfg
        staging_surface, staging_valued = flatten(
            "staging", staging, s_name or p_name, top=s_top)
        prod_surface, prod_valued = flatten(
            "prod", prod, p_name or s_name, top=p_top)
        prod_worker = prod_surface.get("worker.name", "?")
        staging_worker = staging_surface.get("worker.name", "?")
        try:
            allowed = load_allowlist(args.allowlist)
        except Refusal as e:
            # The allowlist is unusable, so no path can be ALLOWED — but the
            # surface was measured, so the JSON envelope keeps it with
            # match/drift verdicts and the refused path marked.
            drift_paths = {path for path in
                           set(staging_surface) | set(prod_surface)
                           if path not in NON_DIFF_PATHS
                           and staging_surface.get(path)
                           != prod_surface.get(path)}
            surfaces_for_refusal = (
                staging_surface, prod_surface, staging_valued, prod_valued,
                staging_worker, prod_worker, prod_label, drift_paths, set())
            raise
        drift, semantic, reported_allowed = compare(
            staging_surface, prod_surface, staging_valued, prod_valued,
            staging_worker, prod_worker, allowed)
        # A stale allowlist entry covers no divergence THIS run: the divergence
        # it once covered is fixed (or never existed) and the entry is now a
        # hole waiting for reuse. Liveness is not enough — a path present and
        # equal on both sides still covers nothing. Every entry must be consumed
        # by an actual divergence, or the run refuses.
        consumed = set(reported_allowed)
        for path in sorted(allowed):
            if path not in consumed:
                drift_paths = {p for p, _, _ in drift} | consumed
                surfaces_for_refusal = (
                    staging_surface, prod_surface, staging_valued,
                    prod_valued, staging_worker, prod_worker, prod_label,
                    drift_paths, consumed)
                refuse("allowlist_stale_entry",
                       "allowlist entry %s covers no divergence in this run — "
                       "remove it rather than carrying a standing exemption"
                       % path,
                       "remove allowlist entry %s — it covers no divergence "
                       "in this run, so it is a standing exemption waiting "
                       "for reuse" % path,
                       path=path)
    except Refusal as e:
        if args.json:
            emit_envelope(build_refusal_envelope(e, surfaces_for_refusal))
        die(e.message)
    if args.json:
        emit_envelope(build_success_envelope(
            prod_label, staging_surface, prod_surface,
            staging_valued, prod_valued, staging_worker, prod_worker,
            drift, reported_allowed))
        n = len(drift) + len(semantic)
        sys.exit(EXIT_DRIFT if n else EXIT_OK)
    print("%s: %s" % (ME, ref_note))
    print("%s: staging worker %s :: prod worker %s (%s)"
          % (ME, staging_worker, prod_worker, prod_label))
    for path in sorted(reported_allowed):
        print("ALLOWED %s" % path)
    for item in semantic:
        print("DRIFT %s" % item)
    for path, s, p in drift:
        print("DRIFT %s :: staging=%s prod=%s" % (path, s, p))
    n = len(drift) + len(semantic)
    if n:
        print("%s: DRIFT FOUND (%d item%s)" % (ME, n, "" if n == 1 else "s"))
        sys.exit(EXIT_DRIFT)
    print("%s: PARITY OK" % ME)


def main(argv=None):
    ap = argparse.ArgumentParser(prog=ME, description="staging vs prod Worker config parity")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("check", cmd_check), ("surface", cmd_surface)):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--ref", default="origin/main")
        p.add_argument("--staging-ref", default=None)
        p.add_argument("--prod-ref", default=None)
        p.add_argument("--config", default=None)
        p.add_argument("--allowlist", default=DEFAULT_ALLOWLIST)
        p.add_argument("--dry-run", action="store_true",
                       help="refused: this tool has no plan mode, so "
                            "--dry-run always exits 2 (use --config for "
                            "working-tree pre-checks)")
        if name == "check":
            p.add_argument("--json", action="store_true",
                           help="emit a versioned JSON verdict envelope "
                                "(schema_version 1) on stdout; exit codes "
                                "unchanged, text mode byte-identical when "
                                "absent")
        p.set_defaults(func=fn)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
