#!/usr/bin/env bash
set -euo pipefail

# Paperclip Enterprise Org Bootstrap — LOCAL ADAPTATION
#
# Adapted from bootstrap_paperclip_enterprise_company.sh (unchanged reference)
# for the Paperclip installation actually present on this machine:
#
#   deployment ... rootless Podman + systemd --user Quadlet
#                  (~/.config/containers/systemd/paperclip{,-db}.container)
#   server ....... container "paperclip", ghcr.io/paperclipai/paperclip
#                  v2026.817.0 commit 213dabab4f8e1f3bb1803a2924c0fea1289fcd4c
#   api .......... http://127.0.0.1:3100 (loopback publish only)
#   auth ......... PAPERCLIP_DEPLOYMENT_MODE=authenticated
#                  -> board bearer auth required; no local-trusted implicit board.
#                  Uses the instance-admin credential already stored in
#                  ~/.paperclip/auth.json. No token is ever placed in argv.
#   database ..... containerized PostgreSQL 17 in container "paperclip-db",
#                  reachable only on the internal podman network
#                  (paperclip-db:5432/paperclip). NOT embedded, NOT on 54329,
#                  and there is no psql on the host.
#
# The ORGANIZATION DESIGN IS UNCHANGED from the operating model report:
# role IDs, reporting lines, permission profiles, subtree scopes, protected
# assignment, default-grant replacement semantics and heartbeat-disabled state
# are byte-for-byte the report's specification.
#
# Deltas vs. the reference script are marked with [DELTA n] and summarized in
# the deployment report. Every delta is a local-mechanics or fail-closed
# hardening change; none widens any agent's authority.
#
# Required environment:
#   COMPANY_NAME
#   COMPANY_GOAL
#
# Optional environment:
#   PAPERCLIP_API_URL       default http://127.0.0.1:3100
#   PAPERCLIP_CLI           explicit path to a version-matched paperclipai CLI
#   PAPERCLIP_SERVER_CTR    default "paperclip"
#   PAPERCLIP_DB_CTR        default "paperclip-db"
#   PAPERCLIP_BACKUP_REF    pre-existing verified backup; else one is created
#   COMPANY_DESCRIPTION
#   COMPANY_BUDGET_CENTS    default 0
#   ADAPTER_TYPE            default claude_local
#   AGENT_BUDGET_CENTS      default 0
#   AGENT_CWD               optional cwd passed to local adapters
#   REPORT_PATH             container-visible path recorded in agent metadata
#   DRY_RUN=1               preflight + plan only, no mutation

: "${COMPANY_NAME:?Set COMPANY_NAME before running this script}"
: "${COMPANY_GOAL:?Set COMPANY_GOAL before running this script}"

PAPERCLIP_API_URL="${PAPERCLIP_API_URL:-http://127.0.0.1:3100}"
PAPERCLIP_SERVER_CTR="${PAPERCLIP_SERVER_CTR:-paperclip}"
PAPERCLIP_DB_CTR="${PAPERCLIP_DB_CTR:-paperclip-db}"
COMPANY_DESCRIPTION="${COMPANY_DESCRIPTION:-Autonomous company governed by the Paperclip enterprise operating model: owner-controlled root, President/COO enterprise operator, independent Chief of Staff and Audit/Risk functions, functional chiefs, and least-privilege authority taper.}"
COMPANY_BUDGET_CENTS="${COMPANY_BUDGET_CENTS:-0}"
ADAPTER_TYPE="${ADAPTER_TYPE:-claude_local}"
AGENT_BUDGET_CENTS="${AGENT_BUDGET_CENTS:-0}"
AGENT_CWD="${AGENT_CWD:-}"
DRY_RUN="${DRY_RUN:-0}"

# ---------------------------------------------------------------------------
# [DELTA 1] Host tooling. The reference script requires psql on the host; this
# machine has none, and the database is not reachable from the host at all.
# podman is required instead.
# ---------------------------------------------------------------------------
for bin in jq curl podman; do
  command -v "$bin" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $bin" >&2
    exit 1
  }
done

# ---------------------------------------------------------------------------
# [DELTA 2] CLI resolution. The reference script falls back to
# `npx --yes paperclipai`, which would resolve whatever version npm's registry
# currently serves. This pins a locally present CLI and HARD-FAILS unless its
# version equals the running server's version.
# ---------------------------------------------------------------------------
resolve_cli() {
  if [[ -n "${PAPERCLIP_CLI:-}" ]]; then
    echo "$PAPERCLIP_CLI"
    return
  fi
  if command -v paperclipai >/dev/null 2>&1; then
    command -v paperclipai
    return
  fi
  local cand
  # npx-cached CLIs are symlinks, so do not filter on -type f.
  cand="$(find "$HOME/.npm/_npx" -maxdepth 4 -path '*/node_modules/.bin/paperclipai' 2>/dev/null | head -1)"
  if [[ -n "$cand" ]]; then
    echo "$cand"
    return
  fi
  echo "ERROR: no local paperclipai CLI found. Set PAPERCLIP_CLI." >&2
  echo "       Refusing to 'npx --yes paperclipai' against a running server." >&2
  exit 1
}

PC_CLI="$(resolve_cli)"

# CLI helper. The stored board credential in ~/.paperclip/auth.json is keyed by
# API base, so --api-base must always be passed or the CLI 401s.
# [DELTA 3] --api-key is deliberately NOT passed: the stored credential is used
# instead, keeping the bearer token out of argv and out of `ps` output.
pc() {
  "$PC_CLI" "$@" --api-base "$PAPERCLIP_API_URL"
}

# ---------------------------------------------------------------------------
# [DELTA 4] Database access. All SQL goes through the paperclip-db container.
# Values are passed as inherited environment variables (podman exec -e VAR with
# no '='), never in argv; SQL arrives on stdin. Credentials stay inside the
# container and are never read, echoed, or written to disk by this script.
# ---------------------------------------------------------------------------
pcsql() {
  # usage: pcsql [psql-args...]  <<< SQL-on-stdin
  podman exec -i \
    -e PGV_COMPANY_ID -e PGV_AGENT_ID -e PGV_TEXT -e PGV_GRANTS \
    "$PAPERCLIP_DB_CTR" sh -c '
      exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -v ON_ERROR_STOP=1 \
        -v company_id="${PGV_COMPANY_ID:-}" \
        -v agent_id="${PGV_AGENT_ID:-}" \
        -v text="${PGV_TEXT:-}" \
        -v grants="${PGV_GRANTS:-[]}" \
        "$@" -f -
    ' _ "$@"
}

say() { echo "==> $*"; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
say "Preflight: containers"
podman container exists "$PAPERCLIP_SERVER_CTR" \
  || { echo "ERROR: container $PAPERCLIP_SERVER_CTR not found" >&2; exit 1; }
podman container exists "$PAPERCLIP_DB_CTR" \
  || { echo "ERROR: container $PAPERCLIP_DB_CTR not found" >&2; exit 1; }

say "Preflight: Paperclip API"
# The unauthenticated /api/health payload carries status/commit/mode but no
# version; the version is only exposed on the authenticated health surface, so
# the version gate runs after board auth below.
HEALTH_JSON="$(curl -fsS "$PAPERCLIP_API_URL/api/health")"
SERVER_COMMIT="$(jq -r '.commit // empty' <<<"$HEALTH_JSON")"
DEPLOY_MODE="$(jq -r '.deploymentMode // empty' <<<"$HEALTH_JSON")"
[[ "$(jq -r '.status' <<<"$HEALTH_JSON")" == "ok" ]] \
  || { echo "ERROR: API health not ok" >&2; exit 1; }
echo "    server commit=$SERVER_COMMIT mode=$DEPLOY_MODE"

say "Preflight: board authentication"
WHOAMI_JSON="$(pc whoami --json 2>/dev/null)" \
  || { echo "ERROR: board authentication failed against $PAPERCLIP_API_URL" >&2; exit 1; }
[[ "$(jq -r '.isInstanceAdmin' <<<"$WHOAMI_JSON")" == "true" ]] \
  || { echo "ERROR: authenticated principal is not an instance admin" >&2; exit 1; }
echo "    authenticated as userId=$(jq -r '.userId' <<<"$WHOAMI_JSON") (instance admin)"

say "Preflight: CLI/server version match"
AUTH_HEALTH_JSON="$(pc health --json 2>/dev/null)"
SERVER_VERSION="$(jq -r '.serverVersion // .version // empty' <<<"$AUTH_HEALTH_JSON")"
CLI_VERSION="$("$PC_CLI" --version 2>/dev/null | tr -d '[:space:]')"
echo "    cli=$PC_CLI version=$CLI_VERSION"
echo "    server version=$SERVER_VERSION"
if [[ -z "$SERVER_VERSION" ]]; then
  echo "ERROR: could not read server version; refusing to proceed unverified." >&2
  exit 1
fi
if [[ "$CLI_VERSION" != "$SERVER_VERSION" ]]; then
  echo "ERROR: CLI version ($CLI_VERSION) != server version ($SERVER_VERSION)." >&2
  echo "       Refusing to run a mismatched CLI against this server." >&2
  exit 1
fi
if [[ -n "$SERVER_COMMIT" && "$(jq -r '.commit // empty' <<<"$AUTH_HEALTH_JSON")" != "$SERVER_COMMIT" ]]; then
  echo "ERROR: health commit mismatch between auth surfaces." >&2
  exit 1
fi

say "Preflight: database reachability and schema"
DB_IDENT="$(pcsql -Atq <<<'SELECT current_database() || $$ / $$ || current_user;')"
echo "    database=$DB_IDENT"
MISSING_TABLES="$(pcsql -Atq <<'SQL'
SELECT string_agg(t, ', ')
FROM (VALUES ('companies'),('agents'),('company_memberships'),('principal_permission_grants')) AS v(t)
WHERE to_regclass('public.' || t) IS NULL;
SQL
)"
if [[ -n "$MISSING_TABLES" ]]; then
  echo "ERROR: required tables missing from installed schema: $MISSING_TABLES" >&2
  exit 1
fi
echo "    companies, agents, company_memberships, principal_permission_grants present"

say "Preflight: permission keys exist in installed server"
# Guard against deploying a profile that references a key this build dropped.
REQUIRED_KEYS='["agents:create","agents:configure","agents:suggest-changes","skills:create","skills:suggest-changes","environments:manage","tools:admin","tools:manage_connections","tools:view_audit","audit:view_agent_actions","tools:use","tools:manage_runtime","users:invite","users:manage_permissions","tasks:assign","tasks:assign_scope","tasks:manage_active_checkouts","pipelines:write","joins:approve"]'
SERVER_KEYS="$(curl -fsS "$PAPERCLIP_API_URL/api/openapi.json" \
  | jq -c '[.paths["/api/companies/{companyId}/members/{memberId}/permissions"].patch.requestBody.content["application/json"].schema.properties.grants.items.properties.permissionKey.enum // []] | .[0]')"
MISSING_KEYS="$(jq -rn --argjson need "$REQUIRED_KEYS" --argjson have "$SERVER_KEYS" \
  '[$need[] | select(. as $k | ($have | index($k)) | not)] | join(", ")')"
if [[ -n "$MISSING_KEYS" ]]; then
  echo "ERROR: installed server is missing required permission keys: $MISSING_KEYS" >&2
  exit 1
fi
echo "    all 19 required permission keys present"

# ---------------------------------------------------------------------------
# [DELTA 9] Preflight: the companion operating model must be present.
#
# case_ad767aaf. Every agent this script creates gets the container path of this
# document written into `metadata.operatingModelReport` and into its AGENTS.md,
# which states "The canonical org/permission design is documented in: <path>".
# So it is the cited canonical source for the org design, the permission
# profiles and the delegation ceilings.
#
# This check used to be an `if [[ -f "$REPORT_SRC" ]]` wrapped around the
# staging step further down. That tolerated the file's absence: a bootstrap run
# on a host without it SUCCEEDED and produced a full company whose every agent
# cited a document that was never staged. Nothing printed, nothing failed, and
# the damage was only visible if someone opened the path an agent was told to
# trust. Refusing is the only correct behaviour — the document is not optional
# decoration, it is the authority the generated instructions defer to.
#
# It lives HERE, in preflight, and not at the staging site, for two reasons:
# `DRY_RUN=1` reaches this line and so can report the gap for free, and this
# runs before the backup and before any mutation, so a host missing the file is
# turned away rather than left holding a half-built company.
REPORT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/paperclipai_enterprise_org_operating_model.md"
say "Preflight: companion operating model document"
if [[ ! -f "$REPORT_SRC" ]]; then
  echo "ERROR: operating model document not found next to this script:" >&2
  echo "         $REPORT_SRC" >&2
  echo "       Every agent's metadata and AGENTS.md cite this file as the canonical" >&2
  echo "       org/permission design, so bootstrapping without it produces agents" >&2
  echo "       pointing at a document that does not exist. It is committed beside" >&2
  echo "       the script in host-bootstrap/; copy it next to this file and re-run." >&2
  exit 1
fi
if [[ ! -s "$REPORT_SRC" ]]; then
  echo "ERROR: operating model document is empty: $REPORT_SRC" >&2
  exit 1
fi
REPORT_SRC_BYTES="$(wc -c <"$REPORT_SRC" | tr -d '[:space:]')"
echo "    $REPORT_SRC ($REPORT_SRC_BYTES bytes)"

# ---------------------------------------------------------------------------
# [DELTA 5] Backup. The reference script has no backup step and assumes host
# pg_dump would be possible. Use Paperclip's own supported server-side backup.
# ---------------------------------------------------------------------------
BACKUP_DIR_HOST="$HOME/.local/share/paperclip/instances/default/data/backups"
if [[ -n "${PAPERCLIP_BACKUP_REF:-}" ]]; then
  [[ -s "$PAPERCLIP_BACKUP_REF" ]] \
    || { echo "ERROR: PAPERCLIP_BACKUP_REF does not exist or is empty" >&2; exit 1; }
  gzip -t "$PAPERCLIP_BACKUP_REF" \
    || { echo "ERROR: PAPERCLIP_BACKUP_REF failed gzip integrity check" >&2; exit 1; }
  say "Backup: using verified pre-existing backup"
  echo "    $PAPERCLIP_BACKUP_REF"
else
  say "Backup: creating via supported instance backup API"
  pc instance database-backup --json >/dev/null
  PAPERCLIP_BACKUP_REF="$(ls -1t "$BACKUP_DIR_HOST"/*.sql.gz 2>/dev/null | head -1)"
  [[ -s "$PAPERCLIP_BACKUP_REF" ]] \
    || { echo "ERROR: no backup produced; refusing to mutate" >&2; exit 1; }
  gzip -t "$PAPERCLIP_BACKUP_REF" \
    || { echo "ERROR: fresh backup failed gzip integrity check" >&2; exit 1; }
  echo "    $PAPERCLIP_BACKUP_REF"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  say "DRY_RUN=1 — preflight complete, no mutation performed."
  exit 0
fi

# ---------------------------------------------------------------------------
# Grant helpers
# ---------------------------------------------------------------------------
scope_self() {
  jq -cn --arg id "$1" '{subtreeRootAgentId:$id}'
}

grant() {
  jq -cn --arg permission "$1" --argjson scope "${2:-null}" \
    '{permissionKey:$permission,scope:$scope}'
}

grants_from_lines() {
  jq -cs '.'
}

# Replace a target agent's full explicit grant set. Replacement (not additive)
# semantics are required so the default tasks:assign grant that this server
# version applies at creation time cannot survive.
#
# [DELTA 6] Path A is attempted first: the native member-permissions API. On
# this build it rejects agent principals (route protection returns early for
# principal_type <> 'user'), so we fall back to the report's narrow DB grant
# step. The attempt is kept so the fallback is evidence-based per run rather
# than assumed, and so a future build that gains native support is used
# automatically.
NATIVE_GRANT_API_USABLE="unknown"

try_native_grants() {
  local agent_id="$1" grants_json="$2"
  local member_id
  member_id="$(PGV_COMPANY_ID="$COMPANY_ID" PGV_AGENT_ID="$agent_id" pcsql -Atq <<'SQL'
SELECT id FROM company_memberships
WHERE company_id = :'company_id'::uuid
  AND principal_type = 'agent'
  AND principal_id = :'agent_id';
SQL
)"
  [[ -n "$member_id" ]] || return 1
  # curl reads its whole config (including the bearer token) from stdin, so the
  # credential never appears in argv / `ps` output.
  {
    printf 'url = "%s"\n' "$PAPERCLIP_API_URL/api/companies/$COMPANY_ID/members/$member_id/permissions"
    printf 'request = "PATCH"\n'
    printf 'header = "content-type: application/json"\n'
    printf 'header = "authorization: Bearer %s"\n' \
      "$(jq -r --arg b "$PAPERCLIP_API_URL" '.credentials[$b].token // empty' "$HOME/.paperclip/auth.json")"
    printf 'data-binary = "%s"\n' \
      "$(jq -cn --argjson g "$grants_json" '{grants:$g}' | sed 's/"/\\"/g')"
  } | curl -fsS -o /dev/null -K - 2>/dev/null
}

apply_grants() {
  local agent_id="$1" grants_json="$2"

  if [[ "$NATIVE_GRANT_API_USABLE" == "unknown" ]]; then
    if try_native_grants "$agent_id" "$grants_json"; then
      NATIVE_GRANT_API_USABLE="yes"
      echo "    native member-permissions API accepted an agent principal; using Path A"
      return
    fi
    NATIVE_GRANT_API_USABLE="no"
    echo "    native member-permissions API rejects agent principals; using Path B (narrow DB grant step)"
  elif [[ "$NATIVE_GRANT_API_USABLE" == "yes" ]]; then
    try_native_grants "$agent_id" "$grants_json"
    return
  fi

  PGV_COMPANY_ID="$COMPANY_ID" PGV_AGENT_ID="$agent_id" PGV_GRANTS="$grants_json" \
    pcsql -q >/dev/null <<'SQL'
BEGIN;

INSERT INTO company_memberships (
  company_id, principal_type, principal_id, status, membership_role
)
VALUES (
  :'company_id'::uuid, 'agent', :'agent_id', 'active', 'member'
)
ON CONFLICT (company_id, principal_type, principal_id)
DO UPDATE SET status = 'active', updated_at = NOW();

DELETE FROM principal_permission_grants
WHERE company_id = :'company_id'::uuid
  AND principal_type = 'agent'
  AND principal_id = :'agent_id';

INSERT INTO principal_permission_grants (
  company_id, principal_type, principal_id, permission_key, scope
)
SELECT
  :'company_id'::uuid,
  'agent',
  :'agent_id',
  g->>'permissionKey',
  CASE WHEN g->'scope' IS NULL OR g->'scope' = 'null'::jsonb
       THEN NULL ELSE g->'scope' END
FROM jsonb_array_elements(:'grants'::jsonb) AS g;

COMMIT;
SQL
}

# Resets legacy/simple permissions before exact principal grants are written.
# This prevents legacy canCreateAgents / canAssignTasks from widening authority,
# and installs the protected assignment policy.
#
# ORDER IS LOAD-BEARING on this build: PATCH /agents/:id/permissions itself
# writes a tasks:assign grant derived from (role=='ceo' || canCreateAgents ||
# canAssignTasks). It must therefore run BEFORE apply_grants, never after.
reset_legacy_permissions() {
  pc agent permissions:update "$1" \
    --payload-json '{
      "canCreateAgents": false,
      "canCreateSkills": false,
      "canAssignTasks": false,
      "authorizationPolicy": {
        "assignmentPolicy": {
          "mode": "protected"
        }
      }
    }' \
    --json >/dev/null
}

# Idempotent lookup: find an already-created agent by its org role id.
find_agent_by_role() {
  PGV_COMPANY_ID="$COMPANY_ID" PGV_TEXT="$1" pcsql -Atq <<'SQL'
SELECT id::text FROM agents
WHERE company_id = :'company_id'::uuid
  AND metadata->>'orgRoleId' = :'text'
  AND status <> 'terminated'
ORDER BY created_at
LIMIT 1;
SQL
}

create_agent() {
  local org_role_id="$1" title="$2" reports_to="$3" profile="$4" capabilities="$5"

  local existing
  existing="$(find_agent_by_role "$org_role_id")"
  if [[ -n "$existing" ]]; then
    echo "    $org_role_id already exists ($existing) — reusing" >&2
    echo "$existing"
    return
  fi

  local payload
  payload="$(
    jq -cn \
      --arg name "$title" \
      --arg title "$title" \
      --arg parent "$reports_to" \
      --arg adapter "$ADAPTER_TYPE" \
      --arg cwd "$AGENT_CWD" \
      --arg orgRoleId "$org_role_id" \
      --arg permissionProfile "$profile" \
      --arg capabilities "$capabilities" \
      --arg report "$REPORT_PATH" \
      --argjson budget "$AGENT_BUDGET_CENTS" '
      {
        name: $name,
        role: "general",
        title: $title,
        capabilities: $capabilities,
        adapterType: $adapter,
        adapterConfig: (if $cwd == "" then {} else {cwd:$cwd} end),
        # [DELTA 7] wakeOnDemand:false added. On this build heartbeat.enabled
        # only gates source=="timer"; on-demand wakes are gated by
        # wakeOnDemand, which DEFAULTS TO TRUE when unset. The instance runs
        # with HEARTBEAT_SCHEDULER_ENABLED=true, so {enabled:false} alone
        # would not actually leave these agents dormant.
        runtimeConfig: {heartbeat:{enabled:false, wakeOnDemand:false}},
        budgetMonthlyCents: $budget,
        permissions: {
          canCreateAgents:false,
          canCreateSkills:false,
          authorizationPolicy:{
            assignmentPolicy:{mode:"protected"}
          }
        },
        metadata:{
          orgRoleId:$orgRoleId,
          permissionProfile:$permissionProfile,
          operatingModelReport:$report
        },
        instructionsBundle:{
          entryFile:"AGENTS.md",
          files:{
            "AGENTS.md":(
              "# Role\nYou are the " + $title + ".\n\n" +
              "# Operating contract\n" +
              "- Follow the company-level goals and owner-reserved decision boundaries.\n" +
              "- Operate only within your Paperclip grants and external tool/data scopes.\n" +
              "- Never grant yourself or descendants authority above the approved role-template ceiling.\n" +
              "- Escalate irreversible, high-risk, legal, capital, security, or owner-reserved decisions.\n" +
              "- The canonical org/permission design is documented in: " + $report + "\n"
            )
          }
        }
      }
      + (if $parent == "" then {reportsTo:null} else {reportsTo:$parent} end)
    '
  )"

  local out
  out="$(pc agent create --company-id "$COMPANY_ID" --payload-json "$payload" --json)"
  jq -r '.id' <<<"$out"
}

# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------
say "Creating company: $COMPANY_NAME"
COMPANY_ID="$(
  PGV_TEXT="$COMPANY_NAME" pcsql -Atq <<'SQL'
SELECT id::text FROM companies WHERE name = :'text' AND status <> 'archived' ORDER BY created_at LIMIT 1;
SQL
)"
if [[ -n "$COMPANY_ID" ]]; then
  echo "    company already exists — reusing"
else
  COMPANY_JSON="$(
    pc company create \
      --payload-json "$(
        jq -cn \
          --arg name "$COMPANY_NAME" \
          --arg description "$COMPANY_DESCRIPTION" \
          --argjson budget "$COMPANY_BUDGET_CENTS" \
          '{name:$name,description:$description,budgetMonthlyCents:$budget}'
      )" \
      --json
  )"
  COMPANY_ID="$(jq -r '.id' <<<"$COMPANY_JSON")"
fi
if [[ -z "$COMPANY_ID" || "$COMPANY_ID" == "null" ]]; then
  echo "ERROR: could not obtain company id" >&2
  exit 1
fi
echo "    COMPANY_ID=$COMPANY_ID"

# [DELTA 8] The report lives on the host at a path the server container cannot
# see (only ~/.local/share/paperclip is bind-mounted, as /paperclip). Stage it
# under the company's own directory so the path recorded in agent metadata and
# instruction bundles is real from the agents' point of view.
#
# case_ad767aaf: `REPORT_SRC` is resolved and its presence PROVEN in preflight above,
# so there is no `if [[ -f ]]` here any more. Reaching this line means the file
# exists; the only remaining question is whether the copy actually landed, and
# an unverified `podman cp` is the same silent failure one layer down — agents
# would still be given a path with nothing behind it.
REPORT_DIR_HOST="$HOME/.local/share/paperclip/instances/default/companies/$COMPANY_ID/ops"
REPORT_PATH="${REPORT_PATH:-/paperclip/instances/default/companies/$COMPANY_ID/ops/paperclipai_enterprise_org_operating_model.md}"
podman exec "$PAPERCLIP_SERVER_CTR" mkdir -p "$(dirname "$REPORT_PATH")"
podman cp "$REPORT_SRC" "$PAPERCLIP_SERVER_CTR:$REPORT_PATH"
REPORT_STAGED_BYTES="$(podman exec "$PAPERCLIP_SERVER_CTR" \
  stat -c %s "$REPORT_PATH" 2>/dev/null || echo missing)"
if [[ "$REPORT_STAGED_BYTES" != "$REPORT_SRC_BYTES" ]]; then
  echo "ERROR: operating model did not stage into $PAPERCLIP_SERVER_CTR." >&2
  echo "       $REPORT_PATH reads as '$REPORT_STAGED_BYTES', expected $REPORT_SRC_BYTES bytes." >&2
  echo "       Every agent's instructions cite that path; refusing to create agents" >&2
  echo "       that point at a document the container cannot read." >&2
  exit 1
fi
say "Staged operating model at $REPORT_PATH (container-visible, $REPORT_STAGED_BYTES bytes)"

# ---------------------------------------------------------------------------
# Goal
# ---------------------------------------------------------------------------
say "Creating company goal"
GOAL_ID="$(
  PGV_COMPANY_ID="$COMPANY_ID" PGV_TEXT="$COMPANY_GOAL" pcsql -Atq <<'SQL'
SELECT id::text FROM goals
WHERE company_id = :'company_id'::uuid AND title = :'text' AND level = 'company'
ORDER BY created_at LIMIT 1;
SQL
)"
if [[ -n "$GOAL_ID" ]]; then
  echo "    goal already exists — reusing"
else
  GOAL_JSON="$(
    pc goal create \
      --company-id "$COMPANY_ID" \
      --title "$COMPANY_GOAL" \
      --description "Owner-defined north-star goal. The President/COO translates this into the enterprise operating plan and subordinate goals." \
      --level company \
      --status active \
      --json
  )"
  GOAL_ID="$(jq -r '.id' <<<"$GOAL_JSON")"
fi
echo "    GOAL_ID=$GOAL_ID"

declare -A AGENT

# ---------------------------------------------------------------------------
# O1 / O2 / O3 — report to the human Owner, so reportsTo is NULL.
# agents.reports_to is a uuid FK to agents on this build, so the Owner cannot be
# represented as an agent parent. The governance relationship is recorded in
# metadata.orgRoleId and the instruction bundle rather than faked with a
# synthetic parent agent.
# ---------------------------------------------------------------------------
say "Creating O1 President & Chief Operating Officer"
AGENT[O1]="$(create_agent \
  O1 \
  "President & Chief Operating Officer" \
  "" \
  "P1_PRESIDENT_COO" \
  "Second-in-command: converts owner goals into the enterprise operating plan, runs the executive team, allocates priorities, builds/restructures the organization, resolves cross-functional trade-offs, and maintains accountability.")"
reset_legacy_permissions "${AGENT[O1]}"

P1_GRANTS="$(
  {
    for p in \
      agents:create \
      agents:configure \
      skills:create \
      environments:manage \
      tools:admin \
      tools:manage_connections \
      tools:view_audit \
      audit:view_agent_actions \
      tools:use \
      tools:manage_runtime \
      users:invite \
      users:manage_permissions \
      tasks:assign \
      tasks:manage_active_checkouts \
      pipelines:write \
      joins:approve
    do
      grant "$p"
    done
  } | grants_from_lines
)"
apply_grants "${AGENT[O1]}" "$P1_GRANTS"

say "Assigning company goal to President/COO"
pc goal update "$GOAL_ID" --owner-agent-id "${AGENT[O1]}" --json >/dev/null

say "Creating O2 Chief of Staff to Owner"
AGENT[O2]="$(create_agent \
  O2 \
  "Chief of Staff to Owner" \
  "" \
  "P2_OWNER_COS" \
  "Protects owner attention; filters and escalates information; manages executive context, decision routing, cross-functional handoffs, operating cadence, and document/process consistency.")"
reset_legacy_permissions "${AGENT[O2]}"
P2_GRANTS="$(
  {
    grant tasks:assign
    grant audit:view_agent_actions
    grant agents:suggest-changes
    grant skills:suggest-changes
  } | grants_from_lines
)"
apply_grants "${AGENT[O2]}" "$P2_GRANTS"

say "Creating O3 Chief Audit & Agent Risk Officer"
AGENT[O3]="$(create_agent \
  O3 \
  "Chief Audit & Agent Risk Officer" \
  "" \
  "P3_AUDIT_RISK" \
  "Independent assurance over agent actions, tool usage, control drift, delegation quality, and autonomy boundaries; recommends remediation without owning production.")"
reset_legacy_permissions "${AGENT[O3]}"
P3_GRANTS="$(
  {
    grant audit:view_agent_actions
    grant tools:view_audit
    grant agents:suggest-changes
    grant skills:suggest-changes
  } | grants_from_lines
)"
apply_grants "${AGENT[O3]}" "$P3_GRANTS"

# ---------------------------------------------------------------------------
# Functional chiefs
# ---------------------------------------------------------------------------
create_chief() {
  local org_id="$1" title="$2" profile="$3" capabilities="$4"

  say "Creating $org_id $title"
  AGENT["$org_id"]="$(create_agent "$org_id" "$title" "${AGENT[O1]}" "$profile" "$capabilities")"
  local id="${AGENT[$org_id]}"
  reset_legacy_permissions "$id"
  local self_scope
  self_scope="$(scope_self "$id")"

  local grants_json
  case "$profile" in
    B1_FUNCTION_CHIEF)
      grants_json="$(
        {
          grant agents:configure "$self_scope"
          grant tasks:assign_scope "$self_scope"
          grant tasks:manage_active_checkouts "$self_scope"
          grant skills:suggest-changes
        } | grants_from_lines
      )"
      ;;
    B2_TECH_CHIEF)
      grants_json="$(
        {
          grant agents:configure "$self_scope"
          grant tasks:assign_scope "$self_scope"
          grant tasks:manage_active_checkouts "$self_scope"
          grant skills:create
          grant environments:manage
          grant tools:manage_connections
          grant tools:view_audit
          grant tools:manage_runtime
        } | grants_from_lines
      )"
      ;;
    B3_SECURITY_CHIEF)
      grants_json="$(
        {
          grant agents:configure "$self_scope"
          grant tasks:assign_scope "$self_scope"
          grant tasks:manage_active_checkouts "$self_scope"
          grant tools:view_audit
          grant audit:view_agent_actions
          grant agents:suggest-changes "$self_scope"
          grant skills:suggest-changes
        } | grants_from_lines
      )"
      ;;
    B4_FINANCE_CHIEF)
      grants_json="$(
        {
          grant agents:configure "$self_scope"
          grant tasks:assign_scope "$self_scope"
          grant tasks:manage_active_checkouts "$self_scope"
          grant skills:suggest-changes
        } | grants_from_lines
      )"
      ;;
    B5_LEGAL_COMPLIANCE_CHIEF)
      grants_json="$(
        {
          grant agents:configure "$self_scope"
          grant tasks:assign_scope "$self_scope"
          grant tasks:manage_active_checkouts "$self_scope"
          grant audit:view_agent_actions
          grant agents:suggest-changes "$self_scope"
          grant skills:suggest-changes
        } | grants_from_lines
      )"
      ;;
    *)
      echo "ERROR: unsupported chief profile $profile" >&2
      exit 1
      ;;
  esac

  apply_grants "$id" "$grants_json"
}

create_chief T0 "CTO & Chief AI Officer" B2_TECH_CHIEF \
  "Owns technology strategy, architecture, engineering, AI/agent platform, data/technical quality, developer platform, and technical execution."

create_chief P0 "Chief Product Officer" B1_FUNCTION_CHIEF \
  "Owns product strategy, portfolio, discovery, roadmap, design/research, and product outcomes."

create_chief F0 "Chief Financial Officer" B4_FINANCE_CHIEF \
  "Owns financial strategy, planning, treasury, capital allocation, accounting/controls, unit economics, financial risk, and board reporting."

create_chief R0 "Chief Revenue Officer" B1_FUNCTION_CHIEF \
  "Owns bookings/revenue execution, sales, revenue operations, partnerships/business development, and sales engineering."

create_chief M0 "Chief Marketing Officer" B1_FUNCTION_CHIEF \
  "Owns market positioning, demand generation, brand, communications, growth, organic acquisition, and market intelligence."

create_chief C0 "Chief Customer Officer" B1_FUNCTION_CHIEF \
  "Owns post-sale customer value: customer success, support, implementation/professional services, retention, and customer health."

create_chief H0 "Chief People Officer" B1_FUNCTION_CHIEF \
  "Owns organization design, recruiting, people operations, learning, performance systems, change management, and people analytics."

create_chief L0 "General Counsel & Chief Compliance Officer" B5_LEGAL_COMPLIANCE_CHIEF \
  "Owns legal risk, commercial legal, privacy, regulatory policy, compliance, IP/legal operations, and escalation to qualified humans for binding determinations."

create_chief S0 "Chief Security & Trust Officer / CISO" B3_SECURITY_CHIEF \
  "Owns cybersecurity, identity/agent trust, secure tool access, application/cloud security, detection/response, and GRC controls."

create_chief G0 "Chief Strategy & Corporate Development Officer" B1_FUNCTION_CHIEF \
  "Owns enterprise strategy, strategic planning, portfolio/OKRs, competitive intelligence, corporate development, M&A/partnership analysis, and integration."

create_chief X0 "SVP Enterprise Operations & PMO" B1_FUNCTION_CHIEF \
  "Owns the enterprise operating system, PMO, process excellence, automation governance, procurement/vendor operations, and cross-functional execution."

say "Creating A0 Access & Agent Provisioning Steward"
AGENT[A0]="$(create_agent \
  A0 \
  "Access & Agent Provisioning Steward" \
  "${AGENT[O1]}" \
  "P4_PROVISIONING_STEWARD" \
  "Owns the role-template catalog, grant logs, provisioning-service health, access-request review, and least-privilege enforcement; cannot bypass delegation ceilings.")"
reset_legacy_permissions "${AGENT[A0]}"
P4_GRANTS="$(
  {
    grant audit:view_agent_actions
    grant tools:view_audit
    grant agents:suggest-changes
  } | grants_from_lines
)"
apply_grants "${AGENT[A0]}" "$P4_GRANTS"

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
echo
say "Grant sets"
PGV_COMPANY_ID="$COMPANY_ID" pcsql -P pager=off <<'SQL'
SELECT
  a.metadata->>'orgRoleId' AS org_role_id,
  a.title,
  g.permission_key,
  COALESCE(g.scope::text, 'company-wide') AS scope
FROM agents a
LEFT JOIN principal_permission_grants g
  ON g.company_id = a.company_id
 AND g.principal_type = 'agent'
 AND g.principal_id = a.id::text
WHERE a.company_id = :'company_id'::uuid
ORDER BY a.metadata->>'orgRoleId', g.permission_key;
SQL

echo
say "Org chart"
pc org get --company-id "$COMPANY_ID" --json

echo
echo "BOOTSTRAP COMPLETE"
echo "COMPANY_ID=$COMPANY_ID"
echo "GOAL_ID=$GOAL_ID"
echo "PRESIDENT_COO_ID=${AGENT[O1]}"
echo "CHIEF_OF_STAFF_ID=${AGENT[O2]}"
echo "AUDIT_RISK_ID=${AGENT[O3]}"
echo "BACKUP_REF=$PAPERCLIP_BACKUP_REF"
echo "GRANT_PATH=$([[ "$NATIVE_GRANT_API_USABLE" == "yes" ]] && echo "A (native member API)" || echo "B (narrow DB grant step)")"
echo
echo "Heartbeats remain DISABLED intentionally (enabled=false AND wakeOnDemand=false)."
echo "Next: deploy/test the constrained Org Provisioner before allowing recursive descendant creation."
