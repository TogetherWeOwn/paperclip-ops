"""Narrow test-only boundary for historical private artifacts, never runtime policy."""
from pathlib import Path
import os


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
    """
    if relative not in PRIVATE_ARTIFACTS:
        raise ValueError("not a reviewed private-artifact test boundary")
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
