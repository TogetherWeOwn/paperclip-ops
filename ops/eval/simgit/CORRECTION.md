# One defect-justified correction

Initial evaluated head: `7961a2215e49a1fdda9b49a91aeedd1c8a00a231`.
[Initial Actions run](https://github.com/TogetherWeOwn/paperclip-ops/actions/runs/42565145617)
was workflow run number 1, attempt 1. It completed all eight matrix scenarios
and eight contract tests. Preserve that artifact unchanged; it is not the
corrected-head evidence.

One independent read-only fresh-context source review identified these concrete
failure scenarios. They are accepted and batched in the only correction round:

1. An exited launcher could leave its waiting child alive. Always stop the owned
   process group; retain/check the child's identity and report any survivor.
2. Repair could run before SIGKILL had disconnected FUSE. Observe daemon exit
   and an inaccessible upper marker/failed Git access before invoking repair
   exactly once. Initial recovery evidence is provisional until this correction
   executes, not silently upgraded to a proved post-disconnection recovery.
3. Any nonzero exit could masquerade as safe refusal. Require native exit 1,
   the expected diagnostic, empty stdout and preserved contents; a crash is not
   a refusal. Apply this to dirty remove and live-owner unlock.
4. The aggregate provider case was always UNRUN. Record actual startup and all
   allocation validation evidence explicitly; negative contracts remain in the
   independently visible test step.
5. Timed-out commands disappeared from audit evidence. Persist the attempt before
   execution and finalize partial output, timeout/error outcome and duration.
6. Directory symlink inodes were omitted from block accounting. Count their own
   blocks without following the target. The initial generated payload has no
   directory symlinks, so this omission is not a claimed cause of its savings.
7. The ordinary ten-minute soft timeout bypassed Python finalizers. Handle
   SIGTERM as a catchable interruption, bound cleanup commands and allow 90
   seconds before hard termination. Add a contained dirty-worker-exception probe
   to exercise native failure cleanup; this is one lifecycle case after the
   matrix, not a second allocation matrix. Hard termination remains UNKNOWN.
8. Further pushes could automatically renew the experiment. Limit the workflow
   lifetime to run numbers 1 and 2 and attempt 1 only, with this committed reason
   required by the second run. No third matrix or manual retry is authorized.

The initial PR lint job also failed the repository's CI path-filter policy.
This experiment produces no required repository check, so mark it using the
existing documented `ci-policy: non-required` mechanism with a reason; no
required check or ruleset is removed or weakened.

Add focused regression fixtures for command timeout auditing, crash-versus-refusal,
directory-symlink accounting, exited-launcher group cleanup, soft interruption,
scenario-failure finalization and the finite workflow budget. Tests were not run
locally; corrected-head Actions must provide the evidence. No red-before test
execution is claimed. Do not start another fresh-review loop or another matrix
if the one correction is incomplete: preserve limitations and report NO-GO.
