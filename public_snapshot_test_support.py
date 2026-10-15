"""Narrow test-only boundary for historical private artifacts, never runtime policy."""
from pathlib import Path
import os


PRIVATE_ARTIFACTS = frozenset({
    "ops/case_c542accd/job_baselines.json",
    "ops/case_30b51a0f/host-timer-registry.json",
    "plugins/model-selection/config/reviewed-roster.json",
    "tests/auth-denial-incident-2026-09-22.json",
    "tests/dead-feed-2026-08-26.jsonl",
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
