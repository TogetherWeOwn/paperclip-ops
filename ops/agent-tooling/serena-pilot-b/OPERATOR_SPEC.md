# Operator spec: Serena 2.0.0 Pilot B agent-side CLI (single host step)

The designated host executor installs from the merged pilot commit after
same-head CI and Paperclip Review 5/5. No extra approval or queue hop; never
create a second host-step card. Builders do not perform this host action.

## Scope

Install serena-agent 2.0.0 into a container venv plus the `code-nav` wrapper
from this directory. Agents run the wrapper from their own worktree through
Bash: it starts the Serena MCP server as a child in that cwd, makes one tool
call over stdio, prints the result and exits.

No gateway connection, grant, binding or DB state. Telemetry off. Dashboard
off. Exactly 3 tools via the read-only context in this directory. Nothing is
written into repos (see step 5 and the pilot global config).

A gateway `local_stdio` path was considered and dropped: a gateway-spawned
server has no task cwd and a restricted env, so `--project-from-cwd` cannot
find the builder's worktree. The agent-side CLI avoids all of that, following
the live `context-pack` pattern.

## ACT

All paths are in the paperclip container. Run from a checkout of this repo
at the merged pilot commit; `PILOT` below is
`ops/agent-tooling/serena-pilot-b` in that checkout.

1. Create the venv with container python3.13 and install pinned:
   `python3.13 -m venv /paperclip/.local/opt/serena-2.0.0/venv`
   `/paperclip/.local/opt/serena-2.0.0/venv/bin/pip install --require-hashes --requirement PILOT/requirements-serena-2.0.0-lock.txt`
   (Lock: all 74 packages in the resolved closure, including transitives,
   with committed PyPI SHA256s; Linux x86_64 / CPython 3.13 only. The
   `serena-agent==2.0.0` Python range is `>=3.11,<3.15`.) Never regenerate
   the lock or hashes at install time. A hash/resolution failure is a STOP.
   If `venv` fails with "ensurepip is not available", the host executor
   installs `python3.13-venv` and retries. Only bundled `ensurepip` may
   bootstrap pip; no downloaded bootstrap scripts.
2. Install the pilot files:
   `cp PILOT/code_nav.py PILOT/serena-config.pilot.yml /paperclip/.local/opt/serena-2.0.0/`
   `cp PILOT/contexts/builder-readonly.yml /paperclip/.local/opt/serena-2.0.0/`
   `cp PILOT/code-nav /paperclip/.local/bin/code-nav && chmod 755 /paperclip/.local/bin/code-nav`
3. Preconditions on PATH for agent shells (verify, do not install here):
   `node`, `npm` (typescript-language-server and Svelte LS bootstrap via
   npm on first use for that language) and a working `rust-analyzer`.
   Verify `rust-analyzer --version` actually runs; a rustup shim that errors
   is a failed precondition, not a pass.
4. Prime each language once (network allowed here, not at call time). With
   the exact agent env, from a scratch checkout (NOT a builder worktree):
   `SERENA_HOME=/tmp/serena-home /paperclip/.local/bin/code-nav --timeout 600 outline <a-.ts-file>`
   `SERENA_HOME=/tmp/serena-home /paperclip/.local/bin/code-nav --timeout 600 outline <a-.rs-file>`
   Svelte best-effort if a Svelte checkout is handy. First use per language
   downloads its LS into `$SERENA_HOME/.solidlsp`; later calls are offline
   until a /tmp prune, after which the next call re-downloads (slow once,
   then fast again). Record which languages primed OK.
5. Health check (same scratch checkout, default 120 s timeout):
   - `code-nav outline <file>` prints a symbol outline.
   - `code-nav symbol <NameInThatFile> --in <file> --body` prints the body.
   - `code-nav refs <NameInThatFile> <file>` prints referencing symbols.
   - Before calling, record `git status --porcelain` and a recursive hash
     inventory of an existing scratch `.serena/` (if absent, create one
     with a sentinel `project.yml` containing a harmless activation command
     that writes a scratch marker). After all three calls, status and the
     inventory must be unchanged and the marker absent. Repeat in a fresh
     scratch checkout without `.serena/`; none may be created. Any repo
     write or marker is a failed install, not a warning.
   - The pilot config explicitly sets `trusted_project_path_patterns: []`.
     It renders the effective home and pre-creates the external project
     folder before Serena starts. With `SERENA_HOME=<custom-private-dir>`,
     verify state stays in `<custom-private-dir>/projects/<basename>` and
     does not appear in `/tmp/serena-home/projects/<basename>`.
   - Existing global configs are parsed on every call; unsafe trust or
     redirect values exit 2 before startup. For an older pilot seed, inspect
     and move only `serena_config.yml` aside to a private backup, then rerun
     to seed the reviewed config. Do not overwrite it blindly. Harmless
     Serena normalization, project entries and auth_secret are preserved.
   - `SERENA_HOME` is never under `/paperclip`: that tree is world-writable
     (same reason context-pack keeps HOME in /tmp). `code-nav` also
     redirects the server child's HOME there when the login HOME is under
     `/paperclip`.
6. Report back on the host-step card: install paths, `serena --version`
   output, primed languages, the three health-check outputs (trimmed), and
   the `git status` evidence from step 5. Run every `serena` invocation
   with `SERENA_HOME=/tmp/serena-home` in the environment -- even
   `serena --version` creates directories under the default home, which
   resolves to world-writable `/paperclip/.serena` on this fleet.

## WHY

Owner-approved Pilot B: Stage-0 measured 55-162x fewer tokens than
whole-file reads on 300k+ token files, with exact language-server
references. Builders re-read big files hundreds of times per week. The CLI
shape keeps the savings with zero gateway state and zero per-request
tool-schema cost.

## UNDO (rollback)

1. `rm -rf /paperclip/.local/opt/serena-2.0.0 /paperclip/.local/bin/code-nav`
2. `rm -rf /tmp/serena-home /tmp/code-nav-locks`
3. Report: wrapper gone (`command -v code-nav` empty), venv gone. No
   server, gateway or DB state was ever touched. Rollback is triggered on
   canary failure (see CANARY.md) or on any write into a repo or outbound
   call observed from the server after priming.

## Flags for the record (not blockers)

- License is GPL-3.0-or-later (internal container use; no distribution).
  CISO awareness only; no new external dependency at call time once primed
  (re-verify in canary).
- Stage-0 RSS was ~1.9 GB per active language server; `code-nav` serializes
  concurrent calls per repo with a flock so they queue instead of stacking.
- Pilot limitation: Serena project data folders under `/tmp/serena-home`
  are keyed by repo directory basename (the only per-project placeholder
  Serena 2.0.0 supports). Two different repos with the same basename would
  share one. Builder worktree directories are card-unique, so this is
  acceptable for the canary.
- Evidence files (Stage-0 config snippet, 7-day baselines) were not in this
  repo at the spec head; context/pin authored from the 2.0.0 sdist and PyPI
  metadata. Reconcile with the snippet when located (gap G1 in CANARY.md).
