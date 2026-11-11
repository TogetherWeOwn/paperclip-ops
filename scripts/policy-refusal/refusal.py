#!/usr/bin/env python3
"""Report an unqualified result without executing a scenario."""
import json


def main():
    print(json.dumps({
        "qualification": "POLICY_ONLY",
        "stock_reference": "UNESTABLISHED",
        "upstream_defect_eligible": False,
        "scenario_observation": "NOT_RUN",
        "verdict": "ERROR",
    }, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
