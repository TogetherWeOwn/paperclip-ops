#!/usr/bin/env python3
"""Check the source-owned sections -> detectors static import prohibition.

Only the two watchdog modules are parsed; inspected source is never executed.
Dynamic imports, transitive dependencies and unrelated imports are out of scope.
"""

import argparse
import ast
import json
from pathlib import Path
import stat


SCHEMA = "watchdog-module-boundary/v1"
RULE = "watchdog-sections-must-not-import-detectors"
SECTIONS = "watchdog/sections.py"
DETECTORS = "watchdog/detectors.py"


def imports_module(node, module):
    """Recognize package, sibling-relative and standalone import spellings."""
    names = {module, f"watchdog.{module}"}
    if isinstance(node, ast.Import):
        return any(alias.name in names for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.level == 0 and node.module in names:
            return True
        if node.level == 1 and node.module == module:
            return True
        if (node.level == 0 and node.module == "watchdog") or (
            node.level == 1 and node.module is None
        ):
            return any(alias.name == module for alias in node.names)
    return False


def diagnostic(code, source, error):
    return {
        "code": code,
        "source": source,
        "error": type(error).__name__,
        "line": error.lineno if isinstance(error, SyntaxError) else None,
    }


def check(root):
    result = {
        "schema": SCHEMA,
        "rule": RULE,
        "status": "error",
        "checkedFiles": [],
        "checkedFileCount": 0,
        "allowedEdgeCount": None,
        "violationCount": None,
        "violations": [],
        "errors": [],
    }
    try:
        if not stat.S_ISDIR(root.stat().st_mode):
            raise NotADirectoryError()
    except (OSError, ValueError) as error:
        result["errors"].append(diagnostic("missing-root", ".", error))
        return result

    trees = {}
    for source in sorted((SECTIONS, DETECTORS)):
        try:
            text = (root / source).read_text(encoding="utf-8")
            trees[source] = ast.parse(text, filename=source)
        except FileNotFoundError as error:
            result["errors"].append(diagnostic("missing-file", source, error))
        except OSError as error:
            result["errors"].append(diagnostic("unreadable-file", source, error))
        except UnicodeDecodeError as error:
            result["errors"].append(diagnostic("invalid-encoding", source, error))
        except SyntaxError as error:
            result["errors"].append(diagnostic("invalid-python", source, error))

    result["checkedFiles"] = sorted(trees)
    result["checkedFileCount"] = len(trees)
    result["errors"].sort(key=lambda error: (error["source"], error["code"], error["error"]))
    if result["errors"]:
        return result

    result["allowedEdgeCount"] = sum(
        imports_module(node, "sections") for node in ast.walk(trees[DETECTORS])
    )
    result["violations"] = sorted(
        (
            {
                "rule": RULE,
                "source": SECTIONS,
                "target": DETECTORS,
                "line": node.lineno,
                "kind": "import" if isinstance(node, ast.Import) else "from-import",
            }
            for node in ast.walk(trees[SECTIONS])
            if imports_module(node, "detectors")
        ),
        key=lambda edge: (edge["source"], edge["line"], edge["kind"], edge["target"]),
    )
    result["violationCount"] = len(result["violations"])
    result["status"] = "violation" if result["violations"] else "pass"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    result = check(args.root)
    print(json.dumps(result, sort_keys=True))
    return {"pass": 0, "violation": 1, "error": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
