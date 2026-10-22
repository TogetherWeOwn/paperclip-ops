#!/usr/bin/env bash
# ===========================================================================
# case_092cd51d-public-fork-disclosure-audit.sh -- what has this org actually
# PUBLISHED to a public vendor fork, and was each item's content gate closed
# before those bytes went public?
#
# THE DEFECT THIS EXISTS FOR
#
# case_092cd51d is the standing procedure for upstream filings. It was written when
# the only way to publish was to click New PR, so every control in it attached
# to the PR. On 2026-09-05 that stopped being true: case_0e2092cc forked
# `paperclipai/paperclip` and case_aa55625e forked `diegosouzapw/OmniRoute` into the
# `TogetherWeOwn` org, and BOTH FORKS ARE PUBLIC. A push to a public fork is
# fetchable, linkable and indexable the moment it lands -- with or without a
# PR. The gate has to attach to the PUSH.
#
# The card was revised in prose to say so at 16:16Z. Prose does not measure.
# In the same breath it asserted "Nothing of ours is published", which was
# true when written for the ONE fork it checked and FALSE 4 minutes later:
# `TogetherWeOwn/OmniRoute` carries `fix/quota-signal-errortext-threading`
# @ ba4b1927, pushed 15:44:20Z, anonymously readable. The sentence was not
# wrong through carelessness -- it was scoped to the fork the author had in
# mind, and a second fork appeared. That is exactly the class of claim that
# must be recomputed rather than re-asserted, so it lives here.
#
# WHY DISCOVERY IS BY ENUMERATION, NEVER A HARDCODED FORK LIST
#
# Hardcoding `paperclip` and `OmniRoute` would rebuild the hole this closes:
# the third fork, created tomorrow by a card nobody here has read, would be
# invisible and silence would read as "nothing published". The org's public
# repo list is enumerated at runtime and every fork in it is scored.
#
# WHY THE REF DIFF IS AGAINST THE PARENT, NOT A BRANCH NAME PATTERN
#
# A fork inherits every upstream branch at fork time -- ~820 on the paperclip
# fork, ~230 on OmniRoute. Those are not ours and flagging them would drown
# the signal. A ref is OURS when it is absent from the parent, or present but
# pointing at a different sha. Both arms matter and both are asserted: a
# NEW-REF is an obvious publication, and a DIVERGED ref is the subtler one --
# force-pushing our commits onto an inherited branch name publishes them just
# as thoroughly while the branch list looks untouched.
#
# WHY AUTHORSHIP DECIDES OURS-VS-THEIRS, AND WHY THAT IS NOT SUFFICIENT ALONE
#
# Upstream's own in-flight branches routinely diverge from what the fork
# snapshotted, so NEW-REF/DIVERGED alone over-reports. The commit author's
# email discriminates: our pushes are authored by the broker identity
# (`person-7d73569a@example.invalid`), upstream's by vendor addresses. Authorship is
# forgeable in general -- it is NOT a security control here. It is a triage
# key for a report a human reads, and every ref is printed either way, so a
# misclassification is visible rather than silent.
#
# EXIT CODES -- three, not two, deliberately
#   0  no bytes of ours are public, or every published item is DECLARED (or
#      POSTHOC-adjudicated, reported as such) below
#   1  something of ours is public that this file does not declare  <- the finding
#   2  cannot evaluate (no network, GitHub API unreachable/rate-limited)
#
# 2 is separate from 1 because a gate that cannot see the forks must not report
# "clean". An unreachable API and an empty fork are the same silence; conflating
# them is the fail-open this repo exists to prevent.
#
# CI: this gate needs outbound network to api.github.com and is therefore NOT
# registered in ci.yml -- see CONTRIBUTING rule 8 ("if you add a suite CI cannot
# run, say so and say why"). It is an operator/agent-run check. Running it in CI
# would either need a credential CI does not hold or would fail open on the
# sandbox's blocked egress, and a check that goes green when it measured nothing
# is worse than no check.
# ===========================================================================
set -Eeuo pipefail

ORG="${DISCLOSURE_AUDIT_ORG:-TogetherWeOwn}"
API="https://api.github.com"
# Our broker identity. MEASURED, not guessed -- the first draft of this gate
# used `togetherweown|paperclip\.ing` and produced SIX false positives, because
# `paperclip.ing` is the VENDOR's own domain (nicky@paperclip.ing has 13 commits
# on `paperclipai/paperclip` master). Matching the vendor we forked FROM flags
# their in-flight branches as our disclosures. Keep this anchored to domains we
# actually push under; widening it to anything that "looks like us" re-creates
# that bug, and a gate that cries wolf 6 times gets ignored on the 7th -- which
# is the one that matters.
#
# case_c04b8883: that first draft was ALSO too narrow, in the direction that actually
# hurts. The `@togetherweown\.(com|invalid)$` form missed the identity most of
# our pushes are made under. `/paperclip/.gitconfig` sets the container-wide git
# default to `togetherweown[bot] <togetherweown[bot]@users.noreply.github.com>`,
# so any ref pushed without an explicit per-repo identity is authored by the App
# bot -- and the bot's address is on `users.noreply.github.com`, not on a
# togetherweown domain at all. Those refs were classified "upstream-authored --
# not ours" and skipped BEFORE the DECLARED allowlist check ever ran. That is a
# FALSE NEGATIVE: the gate reports clean while a real, undeclared publication
# made under the default identity goes unexamined. A false positive costs a
# reader one minute; this costs the whole control.
#
# Measured on the 2026-09-19 run over all 3 public forks, 11 refs were skipped
# this way and are now checked against DECLARED:
#     7  togetherweown[bot]@users.noreply.github.com
#     2  319968614+togetherweown[bot]@users.noreply.github.com
#     1  244704104+togetherweown[bot]@users.noreply.github.com
#     1  person-98049fce@example.invalid
#
# Two things that census settles, both of which a guess gets wrong:
#
#   * The numeric prefix is NOT a constant. GitHub's API-authored commits carry
#     `<user-id>+<login>@users.noreply.github.com`, and we have observed TWO
#     distinct ids for the SAME bot login (319968614 and 244704104) -- the App's
#     installation id differs per repo. Pinning the digits, as the reporting
#     card proposed, would have matched neither of them on some repo tomorrow.
#     So the prefix is `([0-9]+\+)?` and the LOGIN carries the identity.
#   * `.org` is ours too. `person-98049fce@example.invalid` was missed by the
#     `(com|invalid)` list for no reason other than that nobody had pushed under
#     it when the list was written.
#
# WHY THIS DOES NOT SWALLOW AN EXTERNAL CONTRIBUTOR
#
# `users.noreply.github.com` is the shared privacy domain of EVERY GitHub user,
# so matching that domain alone would call the entire internet "ours" and turn
# the allowlist into a rubber stamp. The discriminator is therefore the LOCAL
# PART, `^`-anchored to our exact bot login, never the domain. The suffix `$`
# binds both arms so a lookalike domain cannot be appended. Verified against a
# table of real external identities taken from this org's own forks -- run
# `--self-test` to re-check it; it is the negative cases that matter, and the
# adversarial near-misses (`eviltogetherweown[bot]@...`,
# `togetherweown[bot]@users.noreply.github.com.evil.tld`) are in there too.
# case_c35117d4 review: both arms are `^`-anchored. The first arm used to be bound
# only by `$`, which let anything at all precede the domain -- including a
# second address, so `person-d7e4a1b4@example.invalid@togetherweown.com` classified as ours.
# `[^@]+` makes the local part exactly one local part.
OUR_AUTHOR_RE="${DISCLOSURE_AUDIT_AUTHOR_RE:-^([^@]+@togetherweown\.(com|org|invalid)|([0-9]+\+)?togetherweown\[bot\]@users\.noreply\.github\.com)$}"

# ---------------------------------------------------------------------------
# --self-test -- the ONLY part of this gate CI can run.
#
# The header explains why the audit proper is not in ci.yml: it needs outbound
# network to api.github.com, and a check that goes green when it measured
# nothing is worse than no check. That reasoning is still right for the audit.
# It is NOT right for the classifier. `OUR_AUTHOR_RE` is a pure function of a
# string, it decides whether a public ref is examined or skipped, and case_c04b8883
# is the record of it being silently wrong for an unknown number of months. A
# regex nobody re-evaluates is exactly the artifact that rots, so the table
# below runs offline, deterministically, on every CI run.
#
# The OURS rows are the cheap half. The NOT-OURS rows are the point: every one
# is a real identity observed on this org's own public forks, plus four
# adversarial near-misses. If a future widening makes any NOT-OURS row match,
# the allowlist has silently become a rubber stamp and this fails loudly.
self_test() {
  # Synthetic local parts exercise the public domain grammar, not private people.
  local ours=(
    'synthetic-fixture@togetherweown.com'
    'synthetic-fixture@togetherweown.invalid'
    'synthetic-fixture@togetherweown.org'
    'togetherweown[bot]@users.noreply.github.com'
    '319968614+togetherweown[bot]@users.noreply.github.com'
    '244704104+togetherweown[bot]@users.noreply.github.com'
  )
  # Real identities from the 2026-09-19 census, then the near-misses.
  local theirs=(
    'priya@paperclip.ing' 'nicky@paperclip.ing' 'noreply@paperclip.ing'
    '8016841+diegosouzapw@users.noreply.github.com'
    'diegosouzapw@users.noreply.github.com' 'person-f09bcc07@example.invalid'
    '49699333+dependabot[bot]@users.noreply.github.com'
    'person-89f33585@example.invalid' 'person-b1f99127@example.invalid' 'person-f050e8ca@example.invalid'
    'person-58ac611a@example.invalid' 'person-f454c45b@example.invalid'
    'person-b779638a@example.invalid' 'person-bb9ab159@example.invalid'
    'person-a9b7df63@example.invalid'
    'eviltogetherweown[bot]@users.noreply.github.com'
    'togetherweownxbot@users.noreply.github.com'
    'togetherweown[bot]@users.noreply.github.com.evil.tld'
    'synthetic-fixture@togetherweown.com.example.invalid'
    'person-d7e4a1b4@example.invalid@togetherweown.com'
  )
  local t_pass=0 t_fail=0 e
  printf '== self-test: OUR_AUTHOR_RE\n   %s\n\n' "$OUR_AUTHOR_RE"
  for e in "${ours[@]}"; do
    if grep -qEi "$OUR_AUTHOR_RE" <<<"$e"; then
      t_pass=$((t_pass+1))
    else
      t_fail=$((t_fail+1))
      printf '  FAIL  ours, but NOT matched (false negative -- ref would be SKIPPED): %s\n' "$e"
    fi
  done
  for e in "${theirs[@]}"; do
    if grep -qEi "$OUR_AUTHOR_RE" <<<"$e"; then
      t_fail=$((t_fail+1))
      printf '  FAIL  external, but MATCHED (false positive -- allowlist weakened): %s\n' "$e"
    else
      t_pass=$((t_pass+1))
    fi
  done
  printf '== %s/%s identity classifications correct\n' \
    "$t_pass" "$((t_pass + t_fail))"
  [ "$t_fail" -eq 0 ] || { printf 'FAIL: OUR_AUTHOR_RE misclassifies %s identity/identities.\n' "$t_fail"; return 1; }
  printf 'PASS: every known identity classifies correctly.\n'
  return 0
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/fork-disclosure.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

api() {
  # A token is optional. Unauthenticated is 60 req/hr for the WHOLE host, which
  # this gate exhausted twice while being written -- and a shared host burns that
  # budget between runs, so the exit-2 path is reached routinely rather than
  # exceptionally. With a token it is 5000/hr and the gate is boringly reliable.
  #
  # The token goes through a 0600 curl config file, NEVER through argv (case_c35117d4).
  # `/proc/<pid>/cmdline` is world-readable and every agent on this host shares
  # uid 1000, so `-H "Authorization: Bearer $GH_TOKEN"` publishes the credential
  # to every neighbour for the life of the request -- and `bash -x`, which is how
  # this gate gets debugged, prints it verbatim into a run log. Same pattern as
  # gh_token.sh:229; asserted below by token_argv_test.
  local cfg rc
  [ -n "${GH_TOKEN:-}" ] || { curl -fsSL --max-time 30 "$@" 2>/dev/null; return $?; }
  # umask, not a post-hoc chmod alone: it closes the window between create and
  # write on any platform where mktemp is not already 0600.
  cfg="$(umask 077; mktemp "$TMP/curlcfg.XXXXXXXX")" || return 1
  chmod 0600 "$cfg"
  printf 'header = "Authorization: Bearer %s"\n' "$GH_TOKEN" > "$cfg"
  curl -fsSL --max-time 30 --config "$cfg" "$@" 2>/dev/null
  rc=$?
  rm -f "$cfg"
  return $rc
}

# The other half of what CI can run offline. The regex table above tests a pure
# function of a string; this tests that the CREDENTIAL this gate is meant to run
# with does not end up somewhere a neighbour can read it. It is network-free:
# `curl` is replaced on PATH by a stub that records its argv and exits, so no
# request is made. The positive control matters as much as the argv check -- a
# token that reached nothing at all would otherwise pass the argv assertion
# trivially, which is the shape of a test that cannot fail.
token_argv_test() {
  local probe canary t_pass=0 t_fail=0
  probe="$(umask 077; mktemp -d "$TMP/argv-probe.XXXXXX")" || return 1
  canary='CANARY-not-a-real-token-case_3892d5d3'
  cat > "$probe/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$ARGV_LOG"
cfg=''; prev=''
for a in "$@"; do [ "$prev" = '--config' ] && cfg="$a"; prev="$a"; done
if [ -n "$cfg" ]; then cat "$cfg" > "$CFG_LOG"; else : > "$CFG_LOG"; fi
exit 0
STUB
  chmod 0755 "$probe/curl"
  (
    export ARGV_LOG="$probe/argv.log" CFG_LOG="$probe/cfg.log" \
           PATH="$probe:$PATH" GH_TOKEN="$canary"
    api 'https://api.github.com/meta' >/dev/null
  ) || true

  printf '\n== self-test: GH_TOKEN placement\n\n'
  t() { if [ "$1" = ok ]; then t_pass=$((t_pass+1)); printf '  PASS  %s\n' "$2";
        else t_fail=$((t_fail+1)); printf '  FAIL  %s\n' "$2"; fi; }

  grep -qF "$canary" "$probe/argv.log" \
    && t no 'token LEAKED into curl argv (/proc/<pid>/cmdline, bash -x)' \
    || t ok  'token absent from curl argv'
  grep -qF -- '--config' "$probe/argv.log" \
    && t ok  'curl was invoked through --config' \
    || t no  'curl was not invoked through --config'
  grep -qF "Authorization: Bearer $canary" "$probe/cfg.log" \
    && t ok  'positive control: the token did reach curl, via its config file' \
    || t no  'positive control FAILED -- the token reached curl by no path at all'
  [ -z "$(find "$TMP" -maxdepth 1 -name 'curlcfg.*' -print -quit)" ] \
    && t ok  'the config file is removed after the request' \
    || t no  'a config file holding the token was left on disk'

  printf '== %s/%s credential-placement checks correct\n' "$t_pass" "$((t_pass + t_fail))"
  [ "$t_fail" -eq 0 ] || { printf 'FAIL: GH_TOKEN is not handled safely.\n'; return 1; }
  printf 'PASS: GH_TOKEN never enters argv.\n'
  return 0
}

# ---------------------------------------------------------------------------
# DECLARED PUBLICATIONS -- the allowlist.
#
# A line here says: these bytes are public ON PURPOSE, their content gate was
# closed BEFORE the push, and here is the card that closed it. Format:
#
#     <repo>|<ref>|<sha>|<card>|<gate-closed-utc>|<note>
#
# An <sha> pin is the point. If the branch is force-pushed to different bytes,
# the declaration stops matching and the gate fails -- a declaration authorizes
# a revision, never a branch name. This mirrors upstream_draft_pins.txt: the
# 2026-08-30 lesson was that an artifact named by anything softer than a digest
# is not actually named.
# ---------------------------------------------------------------------------
DECLARED=$(cat <<'EOF'
paperclip|refs/heads/fix/mcp-gateway-legacy-path-bearer|e949815ac77c96560c6787aec7c70325840a55ce|case_6359f6bc|2026-09-05T16:04:41Z|Adversarial review case_79ee8e4b closed 16:04:41Z; pushed 16:30:38Z. Narrowed class per case_327487eb. Added-line scan over the published diff: 0 TOG ids, 0 host URLs, 0 tenant data.
paperclip|refs/heads/fix/notify-soft-invocation-proactive-scope|093f53d31ca17ec2f3edbf9edcad1eb094eba439|case_d58fbfac|2026-09-28T01:10:00Z|Full upstream audit PASS (Security Engineer case_0825f073 PASS): single squashed commit on upstream master 0f14d2612, owner authorship. Fix hunk byte-identical to reviewed 4e570ef0; adds 2 regression tests + hard-entry control, all green. Added-line scan over the published diff (110 lines): 0 TOG ids, 0 host URLs, 0 tenant data. Supersedes deleted ref case_be76b9a8-notify-invocation-soft-bookkeeping@4e570ef0 (old pin pruned with this line).
EOF
)

# ---------------------------------------------------------------------------
# POST-HOC ADJUDICATIONS -- NOT the allowlist above, and never reported as it.
#
# A line here says the opposite of a DECLARED line: these bytes were ALREADY
# public when anyone looked, no content gate closed before the push, and a
# card adjudicated them afterwards. Putting them in DECLARED with a "POST-HOC"
# note would make this gate print `PUBLIC, declared (card)` -- pre-push
# approval -- for a publication that never had any (CISO case_7fb82b3c Q3). So
# they live in their own list, print their own verdict, and carry their own
# count in the summary. Format:
#
#     <repo>|<ref>|<sha>|<card>|<first-public-utc or unknown>|<adjudicated-utc>|<note>
#
# Same full 40-hex sha pin as DECLARED: a ruling covers one revision, so a
# branch that moves after it (master moves on every merge -- accepted for a
# non-gating audit) fails as undeclared until it is adjudicated again. It is
# never "promoted" to DECLARED: the gate did not close first, and no later
# ruling changes that. `--self-test` checks every line's shape offline.
#
# case_fcdcc723: CEO decision 04daa237 (2026-10-02T20:53:55Z) on the CTO per-ref
# recommendation rev 2. First publication times were not recoverable.
#
# case_aee42a7a: CISO per-ref POSTHOC adjudication (2026-10-05T11:22:04Z), 4 refs
# admitted; supersedes the case_fcdcc723 pins for
# release/tog.5-build-preparation and fix/mcp-alias-unique-suffix (pruned
# alongside). First publication times unknown.
# ---------------------------------------------------------------------------
POSTHOC=$(cat <<'EOF'
paperclip|refs/heads/master|2ff804e873462c44b45532e85e694b9b198e55d0|case_fcdcc723|unknown|2026-10-02T20:53:55Z|release line (pin goes stale on next merge -- accepted)
paperclip|refs/heads/release/v2026.1001.0|14f66a7cf6422b43fe87d1d747ceabdf9f23b583|case_fcdcc723|unknown|2026-10-02T20:53:55Z|1001 line
paperclip|refs/heads/release/tog.2-epipe|88dc42c29952a97949e39f35457899f597fb09ba|case_fcdcc723|unknown|2026-10-02T20:53:55Z|POSTHOC until deployed line confirmed; delete if superseded
paperclip|refs/heads/tog/v2026.916.1-e2big-14092|598f924f24bc6e060410ef1b1200d75d533e5e15|case_fcdcc723|unknown|2026-10-02T20:53:55Z|POSTHOC until confirmed superseded; then delete
paperclip|refs/heads/release/tog.5-build-preparation|78db40a12beb826ad52dba2662b3840a1045a137|case_aee42a7a|unknown|2026-10-05T11:22:04Z|release line, stays live; #20 squash strict scan zero (no creds/hosts/triggers)
paperclip|refs/heads/ci/immutable-release-builder|80ef2164b9a2725a3ae604ff554952f7453dfd2b|case_fcdcc723|unknown|2026-10-02T20:53:55Z|in-flight #20; delete on merge/close
paperclip|refs/heads/fix/nested-pool-deadlock-wake|f8e24a2696711adc303b0f4ffa1b5cbe58f92ac4|case_fcdcc723|unknown|2026-10-02T20:53:55Z|in-flight #18
paperclip|refs/heads/fix/never-started-continuation|73e98bff97db0492821b35e46f948bca794e06a4|case_fcdcc723|unknown|2026-10-02T20:53:55Z|in-flight #21
paperclip|refs/heads/fix/case_8a1b7894-exe-dev-env-file|1ef30a3412f272ad5e1202a2370fdfea673fe8fc|case_fcdcc723|unknown|2026-10-02T20:53:55Z|in-flight #19; upstream advisory drafted separately
paperclip|refs/heads/fix-ssh-bundle-stream-truncation|27fcd5e4135bfde9581e981b8bb8c07d40b0b7be|case_fcdcc723|unknown|2026-10-02T20:53:55Z|head of owner's open upstream PR #14428 -- POSTHOC, never delete
paperclip|refs/heads/ci/pr-trusted-affected-work-draft|300dd3b66ed38feb3e404377af923dd513e7e52d|case_aee42a7a|unknown|2026-10-05T11:22:04Z|no PR (stacked intermediate); CI gating, 569 added lines scanned clean
CLIProxyAPI|refs/heads/fix/mcp-alias-unique-suffix|159e43cea3c56aeaf51313651cce425a06256939|case_aee42a7a|unknown|2026-10-05T11:22:04Z|fork PR #1 open draft; alias correctness, strict scan zero; reuses prior adjudication carries, tip unchanged
CLIProxyAPI|refs/heads/dev|e9af1ba330858c09a3d6aace3669ba087455928c|case_aee42a7a|unknown|2026-10-05T11:22:04Z|docs replay (PR template); reuses prior adjudication evidence, tip unchanged
EOF
)

pass=0; posthoc=0; fail=0; undeclared=0; unresolved=0; UNRESOLVED_LIST=()
ok()  { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
adj() { printf '  POSTHOC  %s\n' "$1"; posthoc=$((posthoc+1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail+1)); }

# The card that pins exactly <repo>|<ref>|<sha> in <list>, or nothing.
# Per-field equality, not a substring grep: `paperclip|...` must not also
# match a line for some other repo whose name merely ends in `paperclip`.
pin_card() {  # <list> <repo> <ref> <sha>
  awk -F'|' -v r="$2" -v f="$3" -v s="$4" \
    '$1==r && $2==f && $3==s && $4!="" {print $4; exit}' <<<"$1"
}

# The verdict for one public ref we authored. POSTHOC is consulted FIRST, so a
# ref that somehow sits in both lists reports the weaker claim -- a post-hoc
# ruling must never surface as `declared`. Returns 1 when neither list pins
# this exact sha; the caller reports that as UNDECLARED.
ours_verdict() {  # <repo> <ref> <sha>
  local card
  card=$(pin_card "$POSTHOC" "$1" "$2" "$3")
  if [ -n "$card" ]; then
    adj "$1 $2 @ ${3:0:10} -- PUBLIC, POST-HOC adjudicated ($card)"
    return 0
  fi
  card=$(pin_card "$DECLARED" "$1" "$2" "$3")
  if [ -n "$card" ]; then
    ok "$1 $2 @ ${3:0:10} -- PUBLIC, declared ($card)"
    return 0
  fi
  return 1
}

# --self-test, third part: a POSTHOC entry never prints as `declared`. That
# word asserts the content gate closed BEFORE the push; printing it for a
# post-hoc ruling is the false pre-push approval POSTHOC exists to prevent.
# Offline, so CI runs it. Every real entry is rendered through the same
# ours_verdict() the audit uses, plus fixtures for precedence (a ref in BOTH
# lists) and a positive control proving this test CAN see a `declared` line
# -- without it, "never prints declared" would pass on a function that
# printed nothing at all.
posthoc_test() {
  local p_pass=0 p_fail=0 p_skip=0 public_snapshot=0 line out rc repo ref sha card first adjd note
  local card_re='^TOG-[0-9]+$'
  if python3 - "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from public_snapshot_test_support import is_public_snapshot
raise SystemExit(0 if is_public_snapshot(sys.argv[1]) else 1)
PY
  then
    public_snapshot=1
  fi
  local sha_re='^[0-9a-f]{40}$'
  local utc_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
  local fx=0123456789abcdef0123456789abcdef01234567
  pt() { if [ "$1" = ok ]; then p_pass=$((p_pass+1));
         else p_fail=$((p_fail+1)); printf '  FAIL  %s\n' "$2"; fi; }

  printf '\n== self-test: POSTHOC never reports as declared\n\n'
  # Synthetic card-shape controls always run; no historical ruling is invented.
  card=TOG-""999201
  [[ $card =~ $card_re ]] && pt ok || pt no 'synthetic numeric card rejected'
  for card in case_fcdcc723 not-a-card TOG-X TOG- TOG-""999201suffix; do
    [[ $card =~ $card_re ]] && pt no 'malformed synthetic card accepted' || pt ok
  done
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    IFS='|' read -r repo ref sha card first adjd note <<<"$line"
    [ "$(awk -F'|' '{print NF}' <<<"$line")" -eq 7 ] \
      && pt ok || pt no "not 7 fields: $line"
    [[ $repo =~ ^[A-Za-z0-9._-]+$ ]] && pt ok || pt no "bad repo: $line"
    [[ $ref == refs/heads/?* ]] && pt ok || pt no "ref not under refs/heads/ (never audited): $line"
    [[ $sha =~ $sha_re ]] && pt ok || pt no "sha not full 40-hex: $line"
    if (( public_snapshot )) && [[ $card == case_fcdcc723 || $card == case_aee42a7a ]]; then
      # Only these reviewed sanitizer aliases have lost their private card
      # identity; each is documented in a stanza above its list.
      # Keep all remaining row assertions and the production provenance grammar.
      p_skip=$((p_skip + 1))
    else
      [[ $card =~ $card_re ]] && pt ok || pt no "bad card: $line"
    fi
    [[ $first == unknown || $first =~ $utc_re ]] && pt ok || pt no "bad first-public-utc: $line"
    [[ $adjd =~ $utc_re ]] && pt ok || pt no "bad adjudicated-utc: $line"
    [ -n "$note" ] && pt ok || pt no "empty note: $line"
    if out=$(ours_verdict "$repo" "$ref" "$sha"); then rc=0; else rc=$?; fi
    [ "$rc" -eq 0 ] && grep -qF "PUBLIC, POST-HOC adjudicated ($card)" <<<"$out" \
      && pt ok || pt no "real entry not reported POST-HOC: $line"
    grep -qF 'declared' <<<"$out" \
      && pt no "real POSTHOC entry printed as declared: $out" || pt ok
  done <<<"$POSTHOC"

  # Fixtures, in subshells so the real lists are untouched.
  out=$( DECLARED="fx|refs/heads/both|$fx|case_8c5fe4cc|2026-01-01T00:00:00Z|f
fx|refs/heads/decl|$fx|case_d02d0252|2026-01-01T00:00:00Z|f"
         POSTHOC="fx|refs/heads/both|$fx|case_a6dbab15|unknown|2026-01-01T00:00:00Z|f
xfx|refs/heads/sub|$fx|case_4ac9f045|unknown|2026-01-01T00:00:00Z|f"
         ours_verdict fx refs/heads/both "$fx"
         printf 'counts pass=%s posthoc=%s\n' "$pass" "$posthoc"
         ours_verdict fx refs/heads/decl "$fx"
         ours_verdict fx refs/heads/both "${fx%7}8" || printf 'moved: undeclared\n'
         ours_verdict fx refs/heads/sub "$fx" || printf 'substring: undeclared\n' )
  grep -qF 'fx refs/heads/both @ 0123456789 -- PUBLIC, POST-HOC adjudicated (case_a6dbab15)' <<<"$out" \
    && pt ok || pt no 'a ref in BOTH lists did not report POST-HOC'
  grep -qF 'declared' <<<"$(grep -F 'refs/heads/both' <<<"$out")" \
    && pt no 'a ref in BOTH lists printed as declared' || pt ok
  grep -qF 'counts pass=0 posthoc=1' <<<"$out" \
    && pt ok || pt no 'POST-HOC counted as declared/clean, or not counted'
  grep -qF 'fx refs/heads/decl @ 0123456789 -- PUBLIC, declared (case_d02d0252)' <<<"$out" \
    && pt ok || pt no 'positive control FAILED -- a DECLARED-only ref did not print declared'
  grep -qF 'moved: undeclared' <<<"$out" \
    && pt ok || pt no 'a POSTHOC ref at a different sha was not undeclared'
  grep -qF 'substring: undeclared' <<<"$out" \
    && pt ok || pt no 'a POSTHOC line for another repo matched by substring'

  if (( p_skip )); then
    printf 'SKIP: %s private historical card identities are redacted in this public snapshot; numeric-card controls and every other row assertion remain enabled\n' "$p_skip"
  fi
  printf '== %s/%s POSTHOC checks correct\n' "$p_pass" "$((p_pass + p_fail))"
  [ "$p_fail" -eq 0 ] || { printf 'FAIL: POSTHOC reporting is unsafe or malformed.\n'; return 1; }
  printf 'PASS: no POSTHOC entry reports as declared.\n'
  return 0
}

# Optional private snapshot diagnostics; NEVER a successor acceptance path.
# Both lists are audit-owned input, POSTHOC first. A consistent snapshot does
# not authenticate its records or prove its Git objects/scan bytes/delivery.
provenance_diagnostic() {  # <file> <repo> <ref> <sha>
  {
    while IFS= read -r line; do printf 'POSTHOC|%s\n' "$line"; done <<< "$POSTHOC"
    while IFS= read -r line; do printf 'DECLARED|%s\n' "$line"; done <<< "$DECLARED"
  } | python3 "$(dirname "$0")/public_fork_provenance.py" \
        --evidence "$1" --repository "$ORG/$2" --ref "$3" --tip "$4"
}

if [ "${1:-}" = '--provenance-diagnostics' ]; then
  [ "$#" -eq 5 ] || { printf 'usage: %s --provenance-diagnostics FILE REPO REF SHA\n' "$0" >&2; exit 2; }
  if provenance_diagnostic "$2" "$3" "$4" "$5"; then exit 0; else exit "$?"; fi
fi

if [ "${1:-}" = '--self-test' ]; then
  st_rc=0
  self_test || st_rc=1
  token_argv_test || st_rc=1
  posthoc_test || st_rc=1
  python3 "$(dirname "$0")/test_public_fork_provenance.py" || st_rc=1
  exit "$st_rc"
fi

provenance_unavailable=0

# Read as a STRANGER would. This host installs a broker credential helper for
# github.com (`git config credential.https://github.com.helper`), so a plain
# `git ls-remote` silently authenticates. While writing this gate that made
# the PRIVATE repo TogetherWeOwn/paperclip-ops answer a "public" read
# with a real sha -- the exact inversion this gate exists to prevent, in the
# direction that matters: it would report bytes as public that are not, and
# (worse) could report a private repo's refs as a publication.
#
# "Is it public" is only answerable with NO credential in the loop, so every
# ref read goes through here.
#
# The env vars alone are NOT sufficient, and that gap was measured, not
# reasoned about. They neutralise SYSTEM and GLOBAL config -- and on this host
# the helper is in /etc/gitconfig, so they do fix the case in front of us. But
# git also reads REPO-LOCAL config, which no environment variable suppresses.
# From inside a checkout carrying `credential.https://github.com.helper` in its
# own .git/config, the vars are set, the read still authenticates, and the
# private repo answers with a sha again. This gate is normally invoked from
# exactly such a checkout. So every anon read is additionally run from a
# NEUTRAL directory outside any repository -- there is no local config to
# inherit there. Belt and braces, because the failure is silent in the
# dangerous direction.
ANON_CWD="$TMP/anon-cwd"; mkdir -p "$ANON_CWD"
anon() {
  ( cd "$ANON_CWD" \
    && GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
       GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true \
       GIT_CONFIG_NOSYSTEM=1 "$@" )
}

# Prove the anonymity rather than asserting it. If a future host change makes
# an unauthenticated read succeed against a repo we KNOW is private, every
# "public" verdict below is worthless -- and it would fail open, silently
# reporting private refs as publications. So probe a known-private repo first
# and refuse to report anything if it answers.
#
# Skipped if the probe repo is unreachable for an unrelated reason: the probe
# must not itself become a new way for this gate to be unrunnable offline. We
# distinguish "answered with refs" (fatal) from "no answer" (fine, expected).
#
# The output is CAPTURED and then tested -- deliberately not piped into
# `grep -q`. Under `set -o pipefail`, `grep -q` exits at the first matching
# line, git dies of SIGPIPE (141), and pipefail reports the PIPELINE as
# failed -- so `if ... | grep -q .` evaluates FALSE precisely when refs WERE
# returned. That is this check inverted: it would skip itself silently in the
# one case it exists to catch. Measured, not theorised -- the piped form was
# written first and failed open 3 runs out of 3 against a repo that answers
# with 233 refs. A self-check that cannot fail is decoration.
ANON_PROBE="${DISCLOSURE_AUDIT_PRIVATE_PROBE:-$ORG/paperclip-ops}"
probe_refs="$(anon git ls-remote "https://github.com/$ANON_PROBE.git" 'refs/heads/*' 2>/dev/null || true)"
if [ -n "$probe_refs" ]; then
  printf 'cannot evaluate: the anonymous read path is NOT anonymous.\n'
  printf '   %s is private, yet an unauthenticated ls-remote returned refs.\n' "$ANON_PROBE"
  printf '   Every public/private verdict this gate makes would be unreliable,\n'
  printf '   so it refuses to make any. Two known causes, in likelihood order:\n\n'
  # Cause 1 was MEASURED on 2026-09-19 under PAPERCLIP_GITHUB_AUTH_MODE=managed
  # and is now the common one, so it leads. The old text named only cause 2,
  # which sent a reader hunting a credential.helper that is not there: the
  # helper is irrelevant when `git` itself is a wrapper. `anon()` neutralises
  # git CONFIG at all three levels, and no amount of config-neutralising
  # reaches a different binary earlier on PATH.
  printf '   1. `git` on PATH is a credential-injecting wrapper, not git.\n'
  printf '      Check:  command -v git      (currently: %s)\n' "$(command -v git)"
  printf '      On a Paperclip managed-GitHub run, BASH_ENV re-exports a PATH\n'
  printf '      whose first entry holds that wrapper -- so it comes back in\n'
  printf '      every non-interactive bash, even one you launched with a\n'
  printf '      cleaned PATH. Re-run with the wrapper genuinely out of reach:\n'
  printf "         env -u BASH_ENV PATH=/usr/bin:/bin GH_TOKEN=\"\$GH_TOKEN\" bash %s\n" "$0"
  printf '   2. A credential.helper is configured for github.com in git config\n'
  printf '      (system/global/local). Check:  git config --get-regexp credential\n'
  exit 2
fi

# api() and TMP are defined above the --self-test dispatch, so the self-test can
# exercise the real api() offline without reaching the anonymity probe below.

# The fork -> parent map is IMMUTABLE (a repo's parent never changes) and it is
# the only thing the REST API is needed for. Cache it, so a re-run costs zero
# API calls and the 60/hr unauthenticated budget stops being the binding
# constraint on how often this can be checked. Refresh by deleting the file, or
# automatically when a fork appears in the org listing that the cache lacks.
CACHE="${DISCLOSURE_AUDIT_CACHE:-$(dirname "$0")/.case_092cd51d-fork-parents.cache}"

printf '== enumerating public repos in org %s\n\n' "$ORG"

if ! api "$API/orgs/$ORG/repos?per_page=100&type=public" > "$TMP/repos.json"; then
  printf 'cannot evaluate: GitHub API unreachable or rate-limited (org listing)\n'
  # Say WHICH, because they need different responses: wait vs fix the network.
  # Unauthenticated core limit is 60/hr for the whole host, so a busy host can
  # exhaust it between runs and the operator should not go hunting a net fault.
  if curl -fsSL --max-time 15 "$API/rate_limit" > "$TMP/rl.json" 2>/dev/null; then
    python3 - "$TMP/rl.json" <<'PY' || printf '   rate limit response unparseable\n'
import json, sys, time
c = json.load(open(sys.argv[1]))["resources"]["core"]
wait = max(0, c["reset"] - int(time.time()))
print(f'   rate limit: {c["remaining"]}/{c["limit"]} remaining'
      f'{f", resets in {wait//60}m{wait%60}s" if c["remaining"] == 0 else ""}')
PY
    printf '   set GH_TOKEN and re-run, or wait for the reset, then re-run.\n'
  else
    printf '   api.github.com is unreachable from here (network, not quota).\n'
  fi
  exit 2
fi

# repo_name<TAB>parent_full_name  -- forks only; a non-fork org repo is our own
# project, not a vendor disclosure surface, and is out of scope here.
python3 - "$TMP/repos.json" > "$TMP/forks.tsv" <<'PY'
import json,sys
for r in json.load(open(sys.argv[1])):
    if r.get("fork") and r.get("visibility") == "public":
        print(f"{r['name']}\t{(r.get('parent') or {}).get('full_name') or ''}")
PY

# The org listing omits `parent`; fill it in per-repo. A fork whose parent we
# cannot resolve is exit 2, not a skip -- we cannot diff against an unknown base.
: > "$TMP/forks_resolved.tsv"
while IFS=$'\t' read -r name parent; do
  [ -n "$name" ] || continue
  # Cache hit? (immutable, so a hit is always valid)
  if [ -z "$parent" ] && [ -f "$CACHE" ]; then
    parent=$(awk -F'\t' -v n="$ORG/$name" '$1==n{print $2}' "$CACHE" | head -1)
  fi
  if [ -z "$parent" ]; then
    if ! api "$API/repos/$ORG/$name" > "$TMP/r.json"; then
      printf 'cannot evaluate: could not read %s/%s\n' "$ORG" "$name"
      printf '   (a cached parent would have avoided this call; see %s)\n' "$CACHE"
      exit 2
    fi
    parent=$(python3 -c 'import json,sys;print((json.load(open(sys.argv[1])).get("parent") or {}).get("full_name") or "")' "$TMP/r.json")
    if [ -n "$parent" ]; then
      printf '%s\t%s\n' "$ORG/$name" "$parent" >> "$CACHE" 2>/dev/null || true
    fi
  fi
  [ -n "$parent" ] || { printf 'cannot evaluate: no parent for %s/%s\n' "$ORG" "$name"; exit 2; }
  printf '%s\t%s\n' "$name" "$parent" >> "$TMP/forks_resolved.tsv"
done < "$TMP/forks.tsv"

nforks=$(wc -l < "$TMP/forks_resolved.tsv" | tr -d ' ')
if [ "$nforks" -eq 0 ]; then
  printf 'no public forks in %s -- nothing of ours can be published this way\n' "$ORG"
  exit 0
fi
printf 'public forks found: %s\n\n' "$nforks"

while IFS=$'\t' read -r name parent; do
  printf '== %s/%s  (parent %s)\n' "$ORG" "$name" "$parent"

  if ! anon git ls-remote "https://github.com/$ORG/$name.git" 'refs/heads/*' > "$TMP/fork.refs" 2>/dev/null; then
    printf 'cannot evaluate: ls-remote failed on fork %s\n' "$name"; exit 2
  fi
  if ! anon git ls-remote "https://github.com/$parent.git" 'refs/heads/*' > "$TMP/up.refs" 2>/dev/null; then
    printf 'cannot evaluate: ls-remote failed on parent %s\n' "$parent"; exit 2
  fi
  # An empty ref listing means the remote answered with nothing, which for a
  # fork of a live repo means the read failed rather than that it is empty.
  if [ ! -s "$TMP/fork.refs" ] || [ ! -s "$TMP/up.refs" ]; then
    printf 'cannot evaluate: empty ref listing for %s or %s\n' "$name" "$parent"; exit 2
  fi
  printf '   fork heads %s / parent heads %s\n' \
    "$(wc -l < "$TMP/fork.refs" | tr -d ' ')" "$(wc -l < "$TMP/up.refs" | tr -d ' ')"

  awk 'NR==FNR{u[$2]=$1;next}
       { if (!($2 in u)) print "NEW-REF\t"$2"\t"$1;
         else if (u[$2]!=$1) print "DIVERGED\t"$2"\t"$1 }' \
    "$TMP/up.refs" "$TMP/fork.refs" > "$TMP/candidates.tsv"

  ncand=$(wc -l < "$TMP/candidates.tsv" | tr -d ' ')
  if [ "$ncand" -eq 0 ]; then
    ok "$name: every head matches the parent -- nothing of ours published"
    printf '\n'; continue
  fi
  printf '   %s ref(s) differ from the parent; resolving authorship\n' "$ncand"

  # Authorship is resolved with git, NOT the REST API. The first draft made one
  # `GET /commits/{sha}` per candidate ref; on the paperclip fork that is 8 calls,
  # and unauthenticated GitHub allows 60/hour for the whole host. It exhausted the
  # budget mid-run and exited 2 with "could not read commit" -- technically the
  # correct exit code, but a message that blames the commit for a quota problem.
  # A single shallow fetch of the candidate refs costs one network round trip,
  # has no rate limit, and reads the same authorship bytes.
  cutrefs=$(cut -f2 "$TMP/candidates.tsv")
  if [ -n "$cutrefs" ]; then
    rm -rf "$TMP/bare"; git init -q --bare "$TMP/bare"
    # shellcheck disable=SC2086
    if ! anon git -C "$TMP/bare" fetch -q --depth=1 \
         "https://github.com/$ORG/$name.git" $cutrefs 2>"$TMP/fetch.err"; then
      printf 'cannot evaluate: shallow fetch of candidate refs failed on %s\n' "$name"
      sed 's/^/   /' "$TMP/fetch.err"; exit 2
    fi
  fi

  while IFS=$'\t' read -r kind ref sha; do
    [ -n "$ref" ] || continue
    if ! email=$(git -C "$TMP/bare" log -1 --format='%ae' "$sha" 2>/dev/null); then
      printf 'cannot evaluate: could not read commit %s on %s\n' "$sha" "$name"; exit 2
    fi
    subj=$(git -C "$TMP/bare" log -1 --format='%s' "$sha" 2>/dev/null | cut -c1-72)

    if ! grep -qEi "$OUR_AUTHOR_RE" <<<"$email"; then
      # case_c04b8883, second finding. Authorship is ONE signal and the header is
      # honest that it is a triage key, not a control. Fixing the bot-identity
      # blind spot above exposed a residual class it cannot reach: refs whose
      # BRANCH NAME carries one of our own card ids while the author email is
      # someone we cannot claim -- e.g. `case_afa953ab-ci-hygiene` authored by
      # `person-a9b7df63@example.invalid`, or `rollback/case_c491a77d-paperclip-*`
      # by a personal gmail address. A vendor does not name a branch after a
      # TOG card. These are probably ours, pushed under a mis-set identity.
      #
      # They are NOT auto-claimed. Widening the author regex to swallow a
      # personal gmail address is precisely the 6-false-positive mistake the
      # header records, and `host-2f973de2.example.invalid` is one edit away from the
      # vendor's own `paperclip.ing`. Provenance here has to be adjudicated by
      # a human, per ref, once -- not pattern-matched.
      #
      # So: print loudly, count separately, and leave the exit code alone.
      # Silence was the actual defect; an unresolved item that names itself in
      # the summary is not silent.
      #
      # ARMED 2026-09-20 (case_349ebdab): strict is now the DEFAULT. Every ref in
      # this class has been adjudicated (all removed from the public forks or
      # confirmed upstream), so a NEW unresolved ref is a regression that must
      # fail the gate, not warn. Set DISCLOSURE_AUDIT_STRICT_UNRESOLVED=0 to
      # opt out temporarily (e.g. mid-adjudication of a fresh finding).
      if grep -qEi 'tog-?[0-9]{3,}' <<<"$ref"; then
        unresolved=$((unresolved+1))
        UNRESOLVED_LIST+=("$name $ref @ ${sha:0:10} author=$email")
        printf '   ????? %-8s %s\n           UNRESOLVED -- author (%s) is not ours, but the ref\n' \
          "$kind" "$ref" "$email"
        printf '           name carries a TOG card id. A vendor does not name a branch\n'
        printf '           after our card. Adjudicate: declare it, or confirm it is theirs.\n'
        printf '           %s\n' "https://github.com/$ORG/$name/tree/${ref#refs/heads/}"
        continue
      fi
      printf '   ..... %-8s %s\n           upstream-authored (%s) -- not ours\n' "$kind" "$ref" "$email"
      continue
    fi

    # Ours and public. It must be declared above, or adjudicated post-hoc,
    # pinned to this exact sha.
    if ! ours_verdict "$name" "$ref" "$sha"; then
      undeclared=$((undeclared+1))
      bad "$name $ref @ ${sha:0:10} -- PUBLIC AND UNDECLARED"
      if [ -n "${DISCLOSURE_AUDIT_PROVENANCE_EVIDENCE:-}" ]; then
        printf '           provenance (diagnostic-only; exact-pin finding unchanged):\n'
        if provenance_diagnostic "$DISCLOSURE_AUDIT_PROVENANCE_EVIDENCE" "$name" "$ref" "$sha"; then
          : # Consistency is NOT acceptance; do not change any verdict/count.
        else
          diagnostic_rc=$?
          if [ "$diagnostic_rc" -ne 1 ]; then provenance_unavailable=1; fi
        fi
      fi
      printf '           author %s\n           subject %s\n' "$email" "$subj"
      printf '           %s\n' "https://github.com/$ORG/$name/tree/${ref#refs/heads/}"
      # A branch declared at a DIFFERENT sha is the force-push case; say so,
      # because "undeclared" alone would read as a brand-new branch.
      #
      # But distinguish a force-push from a MALFORMED declaration first. The
      # first draft declared this very branch with an ABBREVIATED sha and the
      # gate reported "DIFFERENT bytes (force-push?)" -- pointing at the same
      # commit it was already looking at. Failing was right (a pin must be
      # exact, per the upstream_draft_pins.txt lesson) but the DIAGNOSIS sent
      # the reader hunting a force-push that never happened. A gate that fails
      # for the right reason with the wrong explanation still costs a run.
      if grep -qF "$name|$ref|" <<<"$DECLARED"; then
        was=$(printf '%s\n' "$DECLARED" | grep -F "$name|$ref|" | cut -d'|' -f3)
        if [ ${#was} -ne 40 ]; then
          printf '           NOTE: declared sha %s is %s chars, not 40 -- MALFORMED\n' "$was" "${#was}"
          printf '                 declarations must pin the full 40-hex sha. Not a force-push.\n'
        elif [ "${sha#"$was"}" != "$sha" ] || [ "${was#"$sha"}" != "$was" ]; then
          printf '           NOTE: declared sha %s is a prefix/abbreviation -- MALFORMED, pin all 40.\n' "$was"
        else
          printf '           NOTE: declared at %s -- these are DIFFERENT bytes (force-push?)\n' "${was:0:10}"
        fi
      fi
      # Same diagnosis for a post-hoc ruling. Expected on master, which moves
      # on every merge: the ruling covered one revision, not the branch.
      was=$(awk -F'|' -v r="$name" -v f="$ref" '$1==r && $2==f {print $3; exit}' <<<"$POSTHOC")
      if [ -n "$was" ]; then
        printf '           NOTE: post-hoc adjudicated at %s -- these are DIFFERENT bytes.\n' "${was:0:10}"
        printf '                 A ruling covers one revision: adjudicate again, never move it to DECLARED.\n'
      fi
    fi
  done < "$TMP/candidates.tsv"
  printf '\n'
done < "$TMP/forks_resolved.tsv"

# A declaration for bytes that are no longer public is stale, not a failure --
# the branch may have been deleted after filing. Report it so the file gets
# pruned, but do not fail: the gate's job is to catch UNDECLARED publication.
# POSTHOC pins go stale the same way (a deleted branch, master moving on).
stale_pin_notes() {  # <label> <list>
  local drepo dref dsha dcard drest
  while IFS='|' read -r drepo dref dsha dcard drest; do
    [ -n "${drepo:-}" ] || continue
    if ! grep -q "$dsha" <<<"$(anon git ls-remote "https://github.com/$ORG/$drepo.git" "$dref" 2>/dev/null)"; then
      printf 'NOTE: %s %s %s @ %s (%s) is no longer at that sha -- prune or re-pin\n' \
        "$1" "$drepo" "$dref" "${dsha:0:10}" "$dcard"
    fi
  done <<< "$2"
}
stale_pin_notes declared "$DECLARED"
stale_pin_notes 'post-hoc adjudicated' "$POSTHOC"

printf '== %s declared/clean, %s post-hoc adjudicated, %s undeclared publication(s), %s unresolved\n' \
  "$pass" "$posthoc" "$undeclared" "$unresolved"

if [ "$provenance_unavailable" -ne 0 ]; then
  printf 'cannot evaluate: requested private provenance diagnostics were unavailable or malformed.\n'
  printf '   Exact-pin findings and counts above remain unchanged.\n'
  exit 2
fi

if [ "$unresolved" -ne 0 ]; then
  printf '\nUNRESOLVED (%s) -- ref named for a TOG card, author not ours:\n' "$unresolved"
  printf '   %s\n' "${UNRESOLVED_LIST[@]}"
  printf 'Each needs a one-time provenance ruling: add to DECLARED (with the card\n'
  printf 'whose gate closed before the push), or confirm it is genuinely upstream.\n'
  if [ "${DISCLOSURE_AUDIT_STRICT_UNRESOLVED:-1}" != "0" ]; then
    printf 'FAIL: strict mode (default; DISCLOSURE_AUDIT_STRICT_UNRESOLVED!=0) and %s ref(s) are unadjudicated.\n' "$unresolved"
    exit 1
  fi
fi

if [ "$fail" -ne 0 ]; then
  printf 'FAIL: bytes of ours are public without a declared, gate-closed provenance.\n'
  printf 'Either add a line to DECLARED naming the card whose content gate closed\n'
  printf 'BEFORE the push, or delete the branch from the public fork.\n'
  printf 'Bytes published before any gate closed go in POSTHOC, and only once a\n'
  printf 'card has adjudicated them -- never in DECLARED.\n'
  exit 1
fi
if [ "$posthoc" -ne 0 ]; then
  printf 'PASS: every public byte of ours is declared and gate-closed, except %s POST-HOC\n' "$posthoc"
  printf '      adjudicated ref(s) whose gate closed only AFTER publication.\n'
else
  printf 'PASS: every public byte of ours is declared and gate-closed.\n'
fi
exit 0
