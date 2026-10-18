"""Fixture tests for scripts/quality-gates/no_pull_request_target.sh.

Each case builds a throwaway git repo holding a copy of the gate script and
one workflow fixture, then runs the gate in it. Mentions that are not trigger
positions (comments, step names, script paths) must pass; every live
pull_request_target trigger form must fail. No network.
Run: python3 -m unittest test_no_pull_request_target
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
GATE = REPO_ROOT / "scripts" / "quality-gates" / "no_pull_request_target.sh"

JOB = """\
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

PASS = {
    "header comment names the trigger": (
        "# pull_request_target is banned; see CONSTRAINTS.md\n"
        "on:\n  pull_request:\n" + JOB),
    "trailing comment names the trigger": (
        "on:\n  pull_request:\n" + JOB.replace(
            "echo hi", "echo hi  # no pull_request_target here")),
    "step name and script path contain the token": (
        "on:\n  pull_request:\n"
        "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        '      - name: "Gate 5 (HARD): no pull_request_target in workflows"\n'
        "        run: bash scripts/quality-gates/no_pull_request_target.sh\n"),
    # Quoted "on" without the banned token must not fail (no false
    # positive on the quoted-key handling).
    "quoted on without banned token": (
        '"on": [push, pull_request]\n' + JOB),
    # The token in a step script is outside the trigger block: steps-only
    # mentions must keep passing after the block-boundary fix.
    "token in run script passes": (
        "on:\n  push:\n" + JOB.replace(
            "echo hi", "echo pull_request_target is banned")),
    # Quoted `*` globs are not YAML aliases: the alias check is
    # quote-aware, so these clean workflows must pass.
    "quoted glob in paths passes": (
        "on:\n  push:\n    paths: ['**/*-lock.json']\n" + JOB),
    "quoted glob sequence item passes": (
        "on:\n  push:\n    paths:\n      - 'src/*Test.java'\n" + JOB),
    "double-quoted glob passes": (
        'on:\n  push:\n    branches: ["*-release"]\n' + JOB),
    # Paperclip Review round 5: unquoted globs in nested positions are
    # ordinary workflow content (the key check only inspects the minimum
    # indent, and `*` only counts as a value-position alias).
    "unquoted glob sequence item passes": (
        "on:\n  push:\n    paths:\n      - docs/**\n" + JOB),
    "unquoted release glob passes": (
        "on:\n  push:\n    branches:\n      - release/*\n" + JOB),
    "multiline flow glob passes": (
        "on:\n  push:\n    paths: [docs/**,\n      other]\n" + JOB),
    # An apostrophe in a plain scalar is literal text, not a quote: it must
    # neither blind the scan to a later alias nor hide a `#` comment that
    # names the trigger.
    "apostrophe in name with trigger comment in block": (
        "name: Don't deploy\non:\n  push:\n    branches: [main]  # never pull_request_target\n" + JOB),
    # Paperclip Review round 8: a legitimate BOM'd workflow whose first line
    # is `on:` must keep passing (the BOM is stripped, not treated as
    # content that hides the trigger block).
    "bom on-first workflow passes": (
        '\ufeffon:\n  push:\n' + JOB),
    # Paperclip Review round 10: the node-start / quoted-key tightening must
    # not flag clean workflows -- a real anchor before a quoted scalar, and
    # JSON-style key-colon pairs without the banned trigger, still pass.
    "anchor with quoted scalar passes": (
        "on:" + chr(10) + "  workflow_dispatch:" + chr(10)
        + "    inputs:" + chr(10) + "      x:" + chr(10)
        + "        description: &a " + chr(34) + "plain text" + chr(34)
        + chr(10) + JOB),
    "json-colon pairs without trigger pass": (
        'on: {"a":"b", "c":"d"}' + chr(10) + JOB),
}

FAIL = {
    "block mapping key": (
        "on:\n  pull_request_target:\n    types: [opened]\n" + JOB),
    "scalar trigger": "on: pull_request_target\n" + JOB,
    "flow list": "on: [push, pull_request_target]\n" + JOB,
    "trailing comment on key": (
        "on:\n  pull_request_target:  # risky\n" + JOB),
    "quoted key": 'on:\n  "pull_request_target":\n' + JOB,
    "block sequence item": (
        "on:\n  - push\n  - pull_request_target\n" + JOB),
    "multi-line flow list": (
        "on: [\n  push,\n  pull_request_target\n]\n" + JOB),
    # Reviewer bypass probes: quoted "on" and next-line flow
    # lists slipped the head revision; the audit silencer is built by
    # concatenation so this fixture file never carries a literal
    # suppression comment itself (floor guard bans new suppressions).
    "quoted on key with inline flow list": (
        '"on": [push, pull_request_target]\n' + JOB),
    "flow list on line after on": (
        "on:\n  [push, pull_request_target]\n" + JOB),
    "reviewer bypass: quoted on plus audit silencer": (
        '"on": [push, pull_request_target]'
        + (" # zizmor" + ": ignore[dangerous-triggers]")
        + "\n" + JOB),
    # Reviewer probes: a block sequence may sit at
    # the same indent as its key, so zero-indent `- ` items continue the
    # trigger block; and a uniformly indented file still has one top-level
    # mapping whose `on:` must be found off column 0.
    "zero-indent sequence": (
        "name: probe\non:\n- push\n- pull_request_target\n" + JOB),
    "minimal zero-indent sequence": "on:\n- pull_request_target\n",
    "indented root mapping": (
        "  name: probe\n  on:\n    pull_request_target:\n"
        "  jobs:\n    probe:\n      runs-on: ubuntu-latest\n"
        "      steps:\n        - run: echo probe\n"),
    "sequence at key indent in indented root": (
        "  on:\n  - push\n  - pull_request_target\n"
        "  jobs:\n    probe:\n      runs-on: ubuntu-latest\n"
        "      steps:\n        - run: echo probe\n"),
    # Paperclip Review round 1: YAML anchors/aliases are live triggers
    # (GitHub Actions expands them), and a top-level flow mapping may
    # continue at column 0. Both must fail (fail-closed).
    "anchor alias trigger": (
        "name: &trig pull_request_target\non: *trig\n" + JOB),
    "flow mapping continuation at column 0": (
        "on: {push: {},\npull_request_target: {}}\n" + JOB),
    # Paperclip Review round 2: the bracket-depth fail-closed is defeated
    # by a multi-line quoted scalar (quote state must persist across
    # lines), a root flow document has no `on:` line (fail closed), and a
    # YAML double-quoted escape hides the token (backslash fail-closed).
    "multi-line quoted scalar hides trigger": (
        'on: {push: {}, a: "foo\n } a #b", pull_request_target: {}}\n' + JOB),
    "root flow document trigger": (
        '{"on": {"pull_request_target": {}}, '
        '"jobs": {"j": {"runs-on": "ubuntu-latest"}}}\n'),
    "escaped token trigger": (
        'on:\n  - "pull_request\\x5ftarget"\n' + JOB),
    # Paperclip Review round 3: the alias scan missed the explicit `?` key
    # form, any alias after a plain-scalar apostrophe in the block, and
    # anchor names with punctuation; all three resolve via yaml v2.
    "explicit-key alias trigger": (
        "x: &t pull_request_target\non:\n  ? *t\n  : {}\n" + JOB),
    "apostrophe hides later alias": (
        "x: &t pull_request_target\non:\n  push:\n    branches: [main]\n"
        "  workflow_dispatch:\n    inputs:\n      a:\n"
        "        description: it's\n  *t : {}\n" + JOB),
    "punctuation anchor alias trigger": (
        "x: &~t pull_request_target\non: *~t\n" + JOB),
    "plus anchor alias trigger": (
        "x: &+t pull_request_target\non: *+t\n" + JOB),
    # Paperclip Review round 3 (late-novel): an escaped or aliased top-level
    # `on:` never matches the extractor, so any other `on:`-looking line
    # (nested mapping key or a line inside a `run: |` script) would win as a
    # clean decoy. Both must fail closed.
    "escaped on key with decoy": (
        '"\\x6fn": pull_request_target\njobs:\n  j:\n'
        "    runs-on: ubuntu-latest\n    env:\n      on: x\n"
        "    steps:\n      - run: echo hi\n"),
    "aliased on key with decoy": (
        "k: &k on\n*k : pull_request_target\njobs:\n  j:\n"
        "    runs-on: ubuntu-latest\n    env:\n      on: x\n"
        "    steps:\n      - run: echo hi\n"),
    "escaped on key with run-script decoy": (
        '"\\x6fn": pull_request_target\njobs:\n  j:\n'
        "    runs-on: ubuntu-latest\n    steps:\n      - run: |\n"
        "        on: x\n"),
    "explicit top-level on key with decoy": (
        "k: &k on\n? *k\n: pull_request_target\njobs:\n  j:\n"
        "    runs-on: ubuntu-latest\n    env:\n      on: x\n"
        "    steps:\n      - run: echo hi\n"),
    # Paperclip Review round 4: a quote after an anchor/tag (`&d "…"`,
    # `!!str "…"`) or after a JSON-style colon (`"k":"v"`) opens a quoted
    # scalar; a `}` or `#` inside that string must not hide a later
    # pull_request_target on the same line. All three read clean before.
    "anchor double-quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &d "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "tag double-quoted brace hides trigger": (
        'on: {a: !!str "x } # y", pull_request_target: {}}' + "\n" + JOB),
    "json-colon quoted brace hides trigger": (
        'on: {"a":"x } # y", pull_request_target: {}}' + "\n" + JOB),
    # Paperclip Review round 4 (late-novel): in a uniformly indented file an
    # escaped or aliased `on:` never matches the extractor, so a decoy
    # `env: on: x` below it wins and the real trigger goes unscanned. Both
    # read clean before.
    "indented escaped on key with decoy": (
        '  "\\x6fn": {pull_request_target: {}}\n  jobs:\n    j:\n'
        "      runs-on: ubuntu-latest\n      env:\n        on: x\n"),
    "indented aliased on key with decoy": (
        "  k: &k on\n  *k : {pull_request_target: {}}\n  jobs:\n    j:\n"
        "      runs-on: ubuntu-latest\n      env:\n        on: x\n"),
    # Paperclip Review round 5: a quote after a punctuation anchor/tag
    # name (`&a~b`, `&a:b`, `&a+b`, `&a@b`, `!e:t`, `!t%21`,
    # `!<tag:yaml.org,2002:str>`) opens a quoted scalar; a `}` or `#`
    # inside must not hide a later pull_request_target. All read clean
    # before (old scan only admitted `[A-Za-z0-9_\\-./]` in the name).
    "tilde anchor quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &a~b "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "colon anchor quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &a:b "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "plus anchor quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &a+b "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "at anchor quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &a@b "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "colon tag quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: !e:t "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "percent tag quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: !t%21 "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "verbatim tag quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: '
        '!<tag:yaml.org,2002:str> "a }}}} # b"}}}, pull_request_target: {}}'
        + "\n" + JOB),
    # Paperclip Review round 5 (late-novel): an anchor/tag on the `on:`
    # key (`&t on:`, `!!str on:`, `&t "on":`) never matched the extractor,
    # so in a uniformly indented file the `env: on: x` decoy won and the
    # real trigger went unscanned. All read clean before.
    "indented anchored on key with decoy": (
        "  name: x\n  &t on:\n    pull_request_target: {}\n  env:\n"
        "    on: x\n  jobs:\n    b:\n      runs-on: ubuntu-latest\n"
        "      steps:\n        - run: echo hi\n"),
    "indented tagged on key with decoy": (
        "  name: x\n  !!str on:\n    pull_request_target: {}\n  env:\n"
        "    on: x\n  jobs:\n    b:\n      runs-on: ubuntu-latest\n"
        "      steps:\n        - run: echo hi\n"),
    "indented anchored quoted on key with decoy": (
        '  name: x\n  &t "on":\n    pull_request_target: {}\n  env:\n'
        "    on: x\n  jobs:\n    b:\n      runs-on: ubuntu-latest\n"
        "      steps:\n        - run: echo hi\n"),
    # Paperclip Review round 6: anchor/tag names ending in `&`/`!`
    # (`&a!`, `&a&`, `&!`), holding a quote (`&"a"`) or holding `<>`
    # (`&<>`) still open a quoted scalar; a `}` or `#` inside must not hide
    # a later pull_request_target. All read clean before (the walk-back
    # stopped at the first `&`/`!`/quote and the verbatim branch rejected a
    # `<...>` not preceded by `!`).
    "bang anchor quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &a! "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "amp anchor quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &a& "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "bang-only anchor quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &! "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "quote-bearing anchor quoted scalar hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &"a" "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "angle anchor quoted brace hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &<> "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    # Paperclip Review round 6: an anchor name may start with `*`, so a
    # value-position `**t` is a live alias, not a glob fragment. Both read
    # clean before (the `**` skip in both scanners).
    "star-star alias trigger": (
        "name: &*t pull_request_target\non: **t\n" + JOB),
    "star-star alias flow trigger": (
        "name: &*t pull_request_target\non: [**t]\n" + JOB),
    # Paperclip Review round 6 (late-novel): an indented root flow document
    # has no column-0 content, so the nested decoy `env.on` won and the
    # real mid-line `"on"` went unscanned. Reads clean before.
    "indented root flow document with decoy": (
        ' {"env": {\n    "on": x,\n    "b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}}}\n'
        '  "jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    # Paperclip Review round 7: a quote glued to an anchor/tag name is part
    # of the name, not a scalar start (`&" "a ...` names the anchor `"` and
    # the real scalar opens at the second quote). All read clean before
    # (the token test ignored the glue).
    "glued-quote anchor hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &" "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "glued-squote anchor hides trigger": (
        "on: {workflow_dispatch: {inputs: {x: {description: &' 'a }}}} # b'}}},"
        " pull_request_target: {}}" + "\n" + JOB),
    "glued-quote anchor with tab hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &"\t"a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    # Paperclip Review round 7: a `---` marker or `%YAML` directive before a
    # root flow document defeated the guard (it only inspected the first
    # non-blank line), so the nested decoy `env.on` won. All read clean
    # before.
    "doc-start marker before root flow with decoy": (
        '---\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    "doc-start marker with inline flow and decoy": (
        '--- {"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    "yaml directive plus marker before root flow": (
        '%YAML 1.2\n---\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    "comment plus marker before root flow with decoy": (
        '# a comment\n---\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    # Paperclip Review round 8: a UTF-8 BOM before a root flow document (or
    # before `---` / `%YAML`) defeats the preamble scan (`str.strip()` keeps
    # U+FEFF), so the nested decoy `env.on` wins. All read clean before
    # (gate read files as plain `utf-8`).
    "bom before root flow with decoy": (
        '\ufeff{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    "bom marker before root flow with decoy": (
        '\ufeff---\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    "bom directive plus marker before root flow": (
        '\ufeff%YAML 1.2\n---\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    # Paperclip Review round 8: an anchor/tag on its own line (or on the
    # `---` line) hides the root-flow guard (it only inspected the first
    # content line), so the nested decoy `env.on` wins. Same-line `&r {`
    # was already caught via the top-key check; the own-line forms read
    # clean before.
    "anchor-only line before root flow with decoy": (
        '&r\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    "tag-only line before root flow with decoy": (
        '!!map\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    "marker anchor before root flow with decoy": (
        '--- &r\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    "marker tag before root flow with decoy": (
        '--- !!map\n{"env": {\n"on": x,\n"b": 2}, "name": "x", '
        '"on": {"pull_request_target": {}},\n'
        '"jobs": {"a": {"runs-on": "ubuntu-latest", '
        '"steps": [{"run": "ls"}]}}}\n'),
    # Paperclip Review round 8: a verbatim tag whose URI holds a comma plus
    # `<` or a quote (`!<(<,>`, `!<?',>`, `!<a,b'c>`) broke the `!<...>`
    # back-scan (it stopped at the inner `<`/quote), so the quote after the
    # tag was missed and a `}`/`#` inside the string hid a later
    # pull_request_target. All read clean before.
    "verbatim inner-angle tag hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: !<(<,> "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "verbatim inner-quote tag hides trigger": (
        "on: {workflow_dispatch: {inputs: {x: {description: !<?',> 'a }}}} # b'}}}},"
        " pull_request_target: {}}" + "\n" + JOB),
    "verbatim comma-quote tag hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: !<a,b\'c> "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    # Paperclip Review round 9: a verbatim tag glued to a flow indicator
    # (`[`, `{` or `,` with no space) broke the whitespace-token scan (the
    # token held the bracket, and the `,`-split fallback saw only `b>`), so
    # the quote after the tag was missed and a `]`/`}`/`#` inside the string
    # hid a later pull_request_target. All read clean before.
    "verbatim tag glued to bracket hides trigger": (
        'on: {push: {}, x: [!<a,b> "a ]}" # b", pull_request_target: {}}}'
        + "\n" + JOB),
    "verbatim tag glued after comma hides trigger": (
        'on: {push: {}, x: [z,!<a,b> "a ]}" # b", pull_request_target: {}}}'
        + "\n" + JOB),
    "verbatim tag glued to brace hides trigger": (
        'on: {push: {}, x: {!<a,b> "a }}" # b", pull_request_target: {}}}'
        + "\n" + JOB),
    # Paperclip Review round 9: a quote glued to a colon inside an
    # anchor/tag name (`&a:"`, `&a:'`, `!:'`) was wrongly taken as a JSON
    # `"k":"v"` value start. The first quote opened a string the real
    # scalar's quote closed, so `}}}} # b"` read as live text and hid the
    # later pull_request_target. All read clean before.
    "colon-glued anchor dquote hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: &a:" "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "colon-glued anchor squote hides trigger": (
        "on: {workflow_dispatch: {inputs: {x: {description: &a:' 'a }}}} # b'}}}},"
        " pull_request_target: {}}" + "\n" + JOB),
    "colon-glued tag squote hides trigger": (
        "on: {workflow_dispatch: {inputs: {x: {description: !:' 'a }}}} # b'...}}},"
        " pull_request_target: {}}" + "\n" + JOB),
    # Paperclip Review round 9: str.splitlines() also splits on U+2028 /
    # U+2029 / U+0085 (and \x0b/\x0c/\x1c-\x1e), which YAML treats as
    # ordinary scalar content. A separator inside a quoted scalar forged a
    # phantom column-0 line that ended the trigger block early, so a later
    # pull_request_target fell outside the scan. Both read clean before
    # (the gate now splits on "\n" only).
    "u2028 in scalar before trigger": (
        "on:\n  workflow_dispatch:\n    inputs:\n      x:\n"
        "        description: \"foo\u2028bar\"\n  pull_request_target:\n" + JOB),
    "u0085 in scalar before trigger": (
        "on:\n  workflow_dispatch:\n    inputs:\n      x:\n"
        "        description: \"foo\u0085bar\"\n  pull_request_target:\n" + JOB),
    # Paperclip Review round 10, finding 1: a property between a JSON-style
    # colon and the quote (`"k":&a "v"`, `"k":!!str "v"`, `'k':&a 'v'`)
    # broke the token walk (it started at the key's closing quote), so the
    # string never opened and a `}`/`#` inside hid a later
    # pull_request_target. All read clean before.
    "json-colon anchor hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {"description":&a "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "json-colon tag hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {"description":!!str "a }}}}'
        ' # b"}}}, pull_request_target: {}}' + "\n" + JOB),
    "squote json-colon anchor hides trigger": (
        "on: {workflow_dispatch: {inputs: {x: {'description':&a 'a }}}} # b'}}}},"
        " pull_request_target: {}}" + "\n" + JOB),
    # Paperclip Review round 10, finding 2: a quote after a `-`/`?`
    # trailing a plain scalar (`foo- "p`, `foo? "p`), after a mid-scalar
    # `&b`/`!t`/`!`/`!<x>` (`foo &b "p`, `foo !t "p`, `foo ! "p`,
    # `foo!<x> "p`), or after a colon following a plain-scalar apostrophe
    # (`foo':"p`) spuriously opened a string that ended at the real
    # string's opening quote, flipping parity so the real `}}}} # r"`
    # read as live text and hid the later pull_request_target. All read
    # clean before.
    "dash-suffixed scalar hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: foo- "p,'
        ' type: string, default: "q }}}} # r"}}}, pull_request_target: {}}'
        + "\n" + JOB),
    "question-suffixed scalar hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: foo? "p,'
        ' type: string, default: "q }}}} # r"}}}, pull_request_target: {}}'
        + "\n" + JOB),
    "mid-scalar anchor hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: foo &b "p,'
        ' type: string, default: "q }}}} # r"}}}, pull_request_target: {}}'
        + "\n" + JOB),
    "mid-scalar tag hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: foo !t "p,'
        ' type: string, default: "q }}}} # r"}}}, pull_request_target: {}}'
        + "\n" + JOB),
    "bare-bang scalar hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: foo ! "p,'
        ' type: string, default: "q }}}} # r"}}}, pull_request_target: {}}'
        + "\n" + JOB),
    "glued verbatim tag in scalar hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: foo!<x> "p,'
        ' type: string, default: "q }}}} # r"}}}, pull_request_target: {}}'
        + "\n" + JOB),
    "dash-suffixed scalar squote hides trigger": (
        "on: {workflow_dispatch: {inputs: {x: {description: foo- 'p,"
        " type: string, default: 'q }}}} # r'}}}}, pull_request_target: {}}"
        + "\n" + JOB),
    "apostrophe-colon hides trigger": (
        'on: {workflow_dispatch: {inputs: {x: {description: foo\':"p,'
        ' type: string, default: "q }}}} # r"}}}, pull_request_target: {}}'
        + "\n" + JOB),
}


def run_gate(workflow_text):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        gate = root / "scripts" / "quality-gates" / GATE.name
        gate.parent.mkdir(parents=True)
        shutil.copy(GATE, gate)
        workflows = root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        if workflow_text is not None:
            (workflows / "ci.yml").write_text(workflow_text)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        done = subprocess.run(["bash", str(gate)], cwd=root,
                              capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr


class TestNoPullRequestTarget(unittest.TestCase):
    def test_non_trigger_mentions_pass(self):
        for label, text in PASS.items():
            with self.subTest(label):
                code, out = run_gate(text)
                self.assertEqual(code, 0, out)

    def test_live_trigger_forms_fail(self):
        for label, text in FAIL.items():
            with self.subTest(label):
                code, out = run_gate(text)
                self.assertEqual(code, 1, out)

    def test_no_workflows_passes(self):
        code, out = run_gate(None)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
