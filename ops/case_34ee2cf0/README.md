# Hindsight recall module

Source-only registry entries:

- `hindsight_recall-canary` / `hindsight/recall-canary`.
- `hindsight_recall-latency` / `hindsight/recall-latency`.

Fragment: `gatus-endpoints.yaml`. Probe: `push-hindsight-recall.sh`.
Triage: [recall-canary](OPERATOR.md#recall-canary) and
[recall-latency](OPERATOR.md#recall-latency). Synthetic inputs live in `fixtures/`.
Run `bash test_case_79158b71_hindsight_recall.sh` from the repository root.

This module contains no live service address, bearer, memory content or
installation claim.
