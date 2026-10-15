# paperclip-ops — fast local checks (case_a2b959d4).
#
# `make verify-fast` is the ONE fast-tier command. CI's Offline suites job
# calls the same script, so local and CI run the identical thing.
# Heavy tier (Postgres, browser, staging e2e, -race, mutation) is CI-only;
# the PR readiness block reports it from CI — never claim it locally.
.PHONY: verify-fast verify-fast-parse setup-githooks

verify-fast:
	./scripts/verify-fast.sh

verify-fast-parse:
	./scripts/verify-fast.sh --parse-only

setup-githooks:
	./scripts/setup-githooks.sh
