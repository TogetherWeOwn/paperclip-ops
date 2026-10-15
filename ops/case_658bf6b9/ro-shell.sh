#!/bin/sh
# ro-shell.sh — case_658bf6b9 forced-command wrapper (phase-1 artifact).
#
# Installed by OPERATOR.md §5.3 for the single phase-1 SSH host (node7b1ff338):
# phase 1 configures EXACTLY ONE SSH host, confined to this allow-list.
# Investigation is read-only; no fix proposal is approved during the pilot.
# More hosts / more arms wait for the phase-2 go decision, which authorizes
# the operator to extend this file (by patch + re-review, never by wildcard)
# and install it per host.
#
# Install (OPERATOR.md §5.3, as root):
#   install -m 0755 -o root -g root ro-shell.sh /usr/local/bin/case_60330d6f-ro-shell
#   # in the Akmatori key's authorized_keys line (key minted host-side):
#   command="/usr/local/bin/case_60330d6f-ro-shell",no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding <key>
#
# Enforcement: exact-match allow-list on $SSH_ORIGINAL_COMMAND. Anything else
# — including an empty command (interactive ssh), scp/sftp subsystems, and
# command-chaining (`uptime; evil`) — is refused and logged. Each arm
# dispatches with explicit quoting: the client's quote characters are matched
# literally in the case pattern, then this shell re-issues the command with
# proper quoting — never `exec $SSH_ORIGINAL_COMMAND`, whose word-splitting
# keeps the quotes literal and shatters patterns (the journalctl `-g` arm
# misfired exactly this way). Parameterized variants (e.g. `df -h /var`) are
# refused until deliberately added here and re-reviewed: the list grows by
# patch, never by wildcard.
# -e is fail-closed here: every fallible command is explicitly guarded
# (|| deny, &&/|| true), so an unexpected failure refuses instead of
# falling through the case to a zero exit (case_66436e78 slice 6).
set -eu

deny() {
  command -v logger >/dev/null 2>&1 && logger -t case_60330d6f-ro-shell "refused: ${SSH_ORIGINAL_COMMAND:-<empty>}" || true
  echo "case_60330d6f-ro-shell: refused" >&2
  exit 1
}

[ -n "${SSH_ORIGINAL_COMMAND:-}" ] || deny
case "$SSH_ORIGINAL_COMMAND" in
  "uptime")
    exec uptime
    ;;
  "free -m")
    exec free -m
    ;;
  "df -h")
    exec df -h
    ;;
  "df -h /")
    exec df -h /
    ;;
  "df -h /var/lib/docker")
    exec df -h /var/lib/docker
    ;;
  # Migrated controller reports DockerRootDir=/home/docker, so the legacy
  # path above is absent there. Bounded exact-match arm for the real root;
  # added by patch + re-review (case_43fd34f1), never by wildcard.
  "df -h /home/docker")
    exec df -h /home/docker
    ;;
  # Inode exhaustion fails writes with space to spare — the sibling of the
  # disk-full class the on-call audit caught (case_66436e78). Bare form covers
  # all mounts, `/` pins the root; every other path stays refused.
  "df -i")
    exec df -i
    ;;
  "df -i /")
    exec df -i /
    ;;
  "systemctl is-failed")
    exec systemctl is-failed
    ;;
  "systemctl list-units --failed --no-pager")
    exec systemctl list-units --failed --no-pager
    ;;
  "docker stats --no-stream")
    exec docker stats --no-stream
    ;;
  "docker ps")
    exec docker ps
    ;;
  "docker system df")
    exec docker system df
    ;;
  # Named-container log tails (case_66436e78 item 8): the on-call agent sees a
  # restart loop in `docker ps` but needs the last lines to diagnose it.
  # One exact-match arm per long-running node7b1ff338 container from the OPERATOR.md
  # §3–§5 compose files, tail bound at 50 lines. No other container, no
  # other tail bound, no --follow/--since/extra flags: the list grows by
  # patch + re-review, never by wildcard.
  "docker logs --tail 50 beszel")
    exec docker logs --tail 50 beszel
    ;;
  "docker logs --tail 50 beszel-agent")
    exec docker logs --tail 50 beszel-agent
    ;;
  "docker logs --tail 50 case_60330d6f-gatus")
    exec docker logs --tail 50 case_60330d6f-gatus
    ;;
  "docker logs --tail 50 akmatori-postgres")
    exec docker logs --tail 50 akmatori-postgres
    ;;
  "docker logs --tail 50 akmatori-api")
    exec docker logs --tail 50 akmatori-api
    ;;
  "docker logs --tail 50 akmatori-mcp-gateway")
    exec docker logs --tail 50 akmatori-mcp-gateway
    ;;
  "docker logs --tail 50 akmatori-agent")
    exec docker logs --tail 50 akmatori-agent
    ;;
  "docker logs --tail 50 akmatori-frontend")
    exec docker logs --tail 50 akmatori-frontend
    ;;
  "docker logs --tail 50 akmatori-proxy")
    exec docker logs --tail 50 akmatori-proxy
    ;;
  "lxc list --format csv -c n,s")
    exec lxc list --format csv -c n,s
    ;;
  # Listening sockets answer "is the port actually bound" without process
  # detail: -ltn shows TCP listeners numerically, never resolving names or
  # touching process tables. No flags beyond -ltn are admitted.
  "ss -ltn")
    exec ss -ltn
    ;;
  "journalctl --since '-10min' -k -g 'Out of memory|oom-killer|Killed process' --no-pager -o short")
    exec journalctl --since -10min -k -g 'Out of memory|oom-killer|Killed process' --no-pager -o short
    ;;
  # Named-unit journal tails (case_66436e78 item 8): `systemctl list-units
  # --failed` names the broken unit but not why it broke — these arms show
  # its last 50 lines. One exact-match arm per native infrastructure unit
  # the on-call needs: docker (container runtime behind every restart
  # loop), cron (the 5-minute Gatus push timers). Bounded at -n 50 with
  # --no-pager -o short; no other unit, no other flag (--follow/--since/
  # -k/-g/-M/-D and vacuum/rotate stay refused): the list grows by patch
  # + re-review, never by wildcard. Note: beszel-agent runs as a Docker
  # container (beszel-agent.compose.yml, container_name: beszel-agent),
  # not a native systemd unit — it is already covered by the
  # `docker logs --tail 50 beszel-agent` arm above.
  "journalctl -u docker.service --no-pager -n 50 -o short")
    exec journalctl -u docker.service --no-pager -n 50 -o short
    ;;
  "journalctl -u cron.service --no-pager -n 50 -o short")
    exec journalctl -u cron.service --no-pager -n 50 -o short
    ;;
  # Invariant counts (case_66436e78 item 8): the on-call agent sees a breach in
  # Gatus but needs the current count to triage it. One exact-match arm per
  # case_654dd2ec read-only view, counts only — the helper prints `key: N`,
  # never row contents (error text can carry secrets, so the Gatus-push
  # disclosure posture applies here too). Each key is a literal here and
  # maps to a hardcoded view inside the helper; no other key, no flags, no
  # parameters: the list grows by patch + re-review, never by wildcard.
  "paperclip-inv agents-error-10m")
    exec paperclip-inv-count.sh agents-error-10m
    ;;
  "paperclip-inv failed-subtype-success")
    exec paperclip-inv-count.sh failed-subtype-success
    ;;
  "paperclip-inv cards-rewoken-6h")
    exec paperclip-inv-count.sh cards-rewoken-6h
    ;;
  "paperclip-inv missing-disposition-reparks")
    exec paperclip-inv-count.sh missing-disposition-reparks
    ;;
  "paperclip-inv unassigned-infocus-cards")
    exec paperclip-inv-count.sh unassigned-infocus-cards
    ;;
  "paperclip-inv db-pool-waiters")
    exec paperclip-inv-count.sh db-pool-waiters
    ;;
  *) deny ;;
esac
