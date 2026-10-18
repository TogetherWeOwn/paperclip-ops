"""Narrow test-only boundary for historical private artifacts, never runtime policy."""
from pathlib import Path
import os


# Public files approved to coexist with the private upstream bundle directory
# without constituting the private disclosure packet. The compatibility
# contract is the first such file (its only public location). New public
# files here require explicit review addition to this allowlist; any other
# content keeps the bundle present (fail-closed).
DOCS_UPSTREAM_PUBLIC = frozenset({
    "compatibility-contract.md",
})

# The private disclosure packet gated by upstream-bundle-filing-gate.sh and
# upstream-bundle-filing-strip-gate.sh: exactly their shared SENDABLE plus
# the filing README. Presence of any of these in a public snapshot means
# the bundle is present and must be gated, not skipped.
DOCS_UPSTREAM_PRIVATE = frozenset({
    "agent-run-credential-isolation.md",
    "hindsight-company-config-bootstrap.md",
    "interaction-comment-supersession-default.md",
    "manifest-refresh-escalation.md",
    "omniroute-false-cliproxy-terminal-state.md",
    "omniroute-truncated-reasoning-false-502.md",
    "plugin-auth-surface.md",
    "discord-plugin-proactive-job-scope.md",
    "README.md",
})

PRIVATE_ARTIFACTS = frozenset({
    ".github/workflows/runner-disk-pressure.yml",
    ".github/workflows/tier-0-quality-gate.yml",
    "semgrep-rules",
    "deploy/paperclip-immutable/generated/board-quadlet-render.json",
    "ops/case_c542accd/job_baselines.json",
    "ops/case_30b51a0f/host-timer-registry.json",
    "plugins/model-selection/config/reviewed-roster.json",
    "tests/auth-denial-incident-2026-09-22.json",
    "tests/dead-feed-2026-08-26.jsonl",
    "tool_grant_expectations.json",
    "operator_runbook_classification.json",
    "docs/OPERATOR-RUNBOOK.md",
    "docs/upstream",
    "verification/fixtures/case_4d10ac5e-prefix-readme.md",
})


def is_public_snapshot(root):
    root = Path(root)
    marker = root / "README.md"
    return ((root / ".github/scripts/public_safety.py").is_file()
            and marker.is_file()
            and "## Fresh-history publication candidate" in marker.read_text().splitlines())


def private_artifact_absent(root, relative):
    """Skip only a named private artifact absent in the marked public snapshot.

    Existing partial/empty files, directories and dangling symlinks must still
    reach the original assertions. Nonpublic copies must fail when data is lost.

    docs/upstream hosts one allowlisted public file (compatibility-contract.md)
    alongside the private disclosure packet. The directory existing for that
    public file alone still counts as absent; any private packet member or any
    unreviewed entry keeps it present (fail-closed).
    """
    if relative not in PRIVATE_ARTIFACTS:
        raise ValueError("not a reviewed private-artifact test boundary")
    if relative == "docs/upstream":
        if not is_public_snapshot(root):
            return False
        base = Path(root) / relative
        if not os.path.lexists(base):
            return True
        if not base.is_dir() or base.is_symlink():
            return False
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return False
        if not entries:
            return False
        if any(name in DOCS_UPSTREAM_PRIVATE for name in entries):
            return False
        if any(name not in DOCS_UPSTREAM_PUBLIC for name in entries):
            return False
        for name in entries:
            # An allowlisted name that is not a plain file (directory,
            # symlink) cannot stand in for the public contract: staging it
            # would copy the wrong bytes or fail, so the bundle stays present.
            entry = base / name
            if entry.is_symlink() or not entry.is_file():
                return False
        return True
    return is_public_snapshot(root) and not os.path.lexists(Path(root) / relative)


def stage_private_artifact(root, destination, relative):
    """Carry the same reviewed test boundary into an isolated mutation stage.

    Copy real inputs when present; never manufacture historical observations.
    Missing nonpublic inputs and malformed existing inputs still fail staging.
    """
    import shutil
    root, destination = Path(root), Path(destination)
    absent = private_artifact_absent(root, relative)
    paths = ["public_snapshot_test_support.py"]
    paths += (["README.md", ".github/scripts/public_safety.py"]
              if absent else [relative])
    for name in paths:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    return absent


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("destination")
    parser.add_argument("relative")
    args = parser.parse_args()
    if stage_private_artifact(args.root, args.destination, args.relative):
        print("SKIP: staging excluded private history with the exact public test boundary")
