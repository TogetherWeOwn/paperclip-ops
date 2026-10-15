#!/usr/bin/env python3
# ===========================================================================
# test_two_bot_staging_precheck.py — hostile tests for the ingress-independent
# two-bot-next staging deploy pre-checks (case_be75d8d7, leaf of case_70b26044).
#
# These are written to FAIL a plausible-but-wrong implementation, not to
# confirm the one that exists. A suite that only feeds a healthy config
# validates the author's own dialect and nothing else — so the synthetic
# fixtures exercise healthy inputs and specific shortcuts a reimplementation
# would take. No case reads or pins a two-bot-next snapshot; this suite does
# not prove compatibility with the live staging surface.
#
# THE CASES THAT CARRY THIS SUITE:
#
#   1. The [env.staging] table is STRUCTURAL. Staging MUST declare its own
#      Container/DO wiring: a key-by-key diff of an absent staging block
#      against top-level would report nothing, because nothing is there to
#      differ. The suite pins that a config with no [env.staging] is REFUSED
#      (exit 2), never a pass.
#
#   2. The container-class gate is LOAD-BEARING. A check that names the
#      required class in a comment but never branches on it still prints the
#      right words. The suite deletes the MUTATION-ANCHOR block and pins that
#      the mutant PASSES a config whose staging containers omit
#      TwoBotContainer (exit 0) — proving the passing run measures the gate.
#
#   3. The digest-pinning gate is LOAD-BEARING. Same discipline for the
#      MUTATION-ANCHOR-DIGEST block: the mutant must PASS a floating-tag
#      Dockerfile the real tool refuses.
#
#   4. Var VALUES never enter output, on any path, in any mode. A tool that
#      echoes a vars table trains operators to paste real secrets into the
#      file it reads. The suite feeds a sentinel secret value and asserts the
#      string appears nowhere on stdout or stderr, while the finding itself
#      still fires.
#
#   5. A refused measurement is DISTINCT from a finding. Missing files,
#      unparseable TOML, absent staging and FROM-less Dockerfiles are exit 2;
#      drift is exit 1. A gate that reports "finding" when it read nothing
#      sends the operator to fix a config that was never opened.
#
# Deterministic: every case builds its fixtures in a temp dir. No network, no
# clock read, no credential, no write outside the temp dir. Exit status only
# for pass/fail; assertions match exit codes and machine-readable record
# fields, never human-readable prose.
# ===========================================================================
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "two_bot_staging_precheck.py")

BASE_VARS = {
    "REDIRECT_FALLBACK_CODE": "",
    "REDIRECT_MAPPINGS_JSON": "[]",
    "BOT_PORT": "8080",
    "KEEPALIVE_SECONDS": "60",
}

STAGING_VARS = dict(BASE_VARS)
STAGING_VARS["TWO_GUILD_NAME"] = "TWO Staging"
STAGING_VARS["INTERNAL_ACTIONS_INGRESS"] = "1"

CONTAINER = (
    '[[env.staging.containers]]\n'
    'class_name = "TwoBotContainer"\n'
    'image = "../Dockerfile"\n'
    'instance_type = "lite"\n'
    'max_instances = 1\n'
    '\n'
    '[env.staging.containers.constraints]\n'
    'regions = ["ENAM"]\n'
    'jurisdiction = "us"\n'
)

DO_WIRING = (
    '[[env.staging.durable_objects.bindings]]\n'
    'name = "TWO_BOT"\n'
    'class_name = "TwoBotContainer"\n'
    '\n'
    '[env.staging.exports.TwoBotContainer]\n'
    'type = "durable-object"\n'
    'storage = "sqlite"\n'
    '\n'
    '[env.staging.version_metadata]\n'
    'binding = "CF_VERSION_METADATA"\n'
)

OBSERVABILITY = '[env.staging.observability]\nenabled = true\nhead_sampling_rate = 1\n'

DOCKERFILE_PINNED = (
    "FROM rust:1.94-trixie@sha256:"
    "652612f07bfbbdfa3af34761c1e435094c00dde4a98036132fca28c7bb2b165c"
    " AS builder\n"
    "WORKDIR /app\n"
    "FROM gcr.io/distroless/cc-debian13:nonroot@sha256:"
    "e792ab3d241a468a4fd7519ddbbebe66b49b5f365771716ea688ad40b6c6f1c2"
    " AS runtime\n"
    "EXPOSE 8080\n"
    "ENV LISTEN_ADDR=0.0.0.0:8080\n"
)


def write_toml(top_vars=None, staging_vars=None, extra="",
               prod_vars=None, strip_staging=False):
    top = dict(BASE_VARS) if top_vars is None else top_vars
    stg = dict(STAGING_VARS) if staging_vars is None else staging_vars

    def table(mapping):
        return "\n".join(
            '%s = "%s"' % (key, value) for key, value in sorted(mapping.items())
        )

    parts = [
        'name = "two-bot-next"',
        'main = "src/index.ts"',
        'compatibility_date = "2026-09-29"',
        "",
        "[vars]",
        table(top),
        "",
    ]
    if not strip_staging:
        parts += [
            CONTAINER,
            DO_WIRING,
            "[env.staging.vars]",
            table(stg),
            "",
            OBSERVABILITY,
        ]
    if prod_vars is not None:
        parts += ["[env.production.vars]", table(prod_vars), ""]
    if extra:
        parts.append(extra)
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "wrangler.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts) + "\n")
    return path


def write_dockerfile(body=None):
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "Dockerfile")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body if body is not None else DOCKERFILE_PINNED)
    return path


def run_tool(*argv, cwd=None):
    proc = subprocess.run(
        [sys.executable, TOOL, *argv],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
    )
    return proc.returncode, proc.stdout, proc.stderr


def check(toml=None, docker=None, record=True, fmt="json"):
    directory = tempfile.mkdtemp()
    argv = [
        "check",
        "--wrangler",
        toml or write_toml(),
        "--dockerfile",
        docker or write_dockerfile(),
        "--format",
        fmt,
    ]
    record_path = None
    if record:
        record_path = os.path.join(directory, "record.json")
        argv += ["--record", record_path]
    code, out, err = run_tool(*argv)
    record_doc = None
    if record_path and os.path.exists(record_path):
        with open(record_path, encoding="utf-8") as handle:
            record_doc = json.load(handle)
    return code, out, err, record_doc


def mutated_toml(*pairs, **kwargs):
    path = write_toml(**kwargs)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    for old, new in pairs:
        if old not in text:
            raise AssertionError("fixture drift, pattern absent: %r" % old)
        text = text.replace(old, new)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def delete_anchor(tool_path, start_marker, end_marker):
    with open(tool_path, encoding="utf-8") as handle:
        src = handle.read()
    start = src.index(start_marker)
    end = src.index(end_marker) + len(end_marker)
    mutant_fd, mutant_path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(mutant_fd, "w", encoding="utf-8") as handle:
        handle.write(src[:start] + src[end:])
    return mutant_path


class PrecheckTest(unittest.TestCase):
    maxDiff = 4000

    def test_healthy_shape_passes_with_pinned_record(self):
        code, out, err, record = check()
        self.assertEqual(code, 0, "healthy shape must pass: %s%s" % (out, err))
        self.assertIsNotNone(record, "pass must write the digest record")
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["findings"], [])
        self.assertEqual(len(record["stages"]), 2)
        for stage in record["stages"]:
            self.assertRegex(stage["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(record["wrangler_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["dockerfile_sha256"], r"^[0-9a-f]{64}$")

    def test_subcommand_is_required(self):
        for argv in ((), ("--wrangler", write_toml(),
                          "--dockerfile", write_dockerfile())):
            with self.subTest(argv=argv):
                code, out, err = run_tool(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertTrue(err)
                self.assertNotIn("Traceback", err)

    def test_flags_before_subcommand_are_usage_errors(self):
        toml = write_toml()
        cwd = os.path.dirname(toml)
        with open(write_dockerfile()) as src:
            with open(os.path.join(cwd, "Dockerfile"), "w") as dst:
                dst.write(src.read())
        # Healthy cwd defaults must not hide a silently ignored explicit path.
        for command in ("check", "surface"):
            with self.subTest(command=command):
                code, out, err = run_tool(
                    "--wrangler", toml, command, cwd=cwd,
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)

    def test_check_uses_explicit_paths_not_cwd_defaults(self):
        toml, docker = write_toml(), write_dockerfile()
        with tempfile.TemporaryDirectory() as cwd:
            for name in ("wrangler.toml", "Dockerfile"):
                with open(os.path.join(cwd, name), "w") as handle:
                    handle.write("deliberately invalid cwd default\n")
            record_path = os.path.join(cwd, "record.json")
            for options in (
                ["--wrangler", toml, "--dockerfile", docker],
                ["--dockerfile", docker, "--wrangler", toml],
            ):
                with self.subTest(options=options):
                    code, out, err = run_tool(
                        "check", *options, "--format", "json",
                        "--record", record_path, cwd=cwd,
                    )
                    self.assertEqual(code, 0, out + err)
                    doc = json.loads(out)
                    with open(record_path) as handle:
                        self.assertEqual(json.load(handle), doc)
                    for field, path in (("wrangler_sha256", toml),
                                        ("dockerfile_sha256", docker)):
                        with open(path, "rb") as handle:
                            self.assertEqual(
                                doc[field], hashlib.sha256(handle.read()).hexdigest(),
                            )

    def test_surface_uses_explicit_path_not_cwd_default(self):
        with tempfile.TemporaryDirectory() as cwd:
            code, out, err = run_tool(
                "surface", "--wrangler", write_toml(), cwd=cwd,
            )
        self.assertEqual(code, 0, out + err)
        self.assertIn("BOT_PORT=8080", out)

    def test_invalid_cli_options_are_refused(self):
        for argv in (("check", "--format", "yaml"),
                     ("check", "--wrangler"),
                     ("check", "--unknown"),
                     ("surface", "--dockerfile", write_dockerfile())):
            with self.subTest(argv=argv):
                code, out, err = run_tool(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)

    def test_unexpected_exceptions_refuse_without_values_or_traceback(self):
        sentinel = "SENTINEL_EXCEPTION_VALUE_MUST_NOT_APPEAR"
        harness = (
            "import runpy, sys\n"
            "ns = runpy.run_path(sys.argv[1])\n"
            "def crash(args):\n"
            "    raise RuntimeError(sys.argv[2])\n"
            "ns['main'].__globals__['cmd_' + sys.argv[3]] = crash\n"
            "sys.exit(ns['main']([sys.argv[3]]))\n"
        )
        for command in ("check", "surface"):
            with self.subTest(command=command):
                proc = subprocess.run(
                    [sys.executable, "-c", harness, TOOL, sentinel, command],
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(proc.stderr)
                self.assertNotIn(sentinel, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_previous_stage_aliases_need_no_digest(self):
        docker = write_dockerfile(
            DOCKERFILE_PINNED + "from RuNtImE as final\nFROM FINAL AS last\n"
            "EXPOSE 8080\nENV LISTEN_ADDR=0.0.0.0:8080\n"
        )
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(record["stages"][2:], [
            {"stage": "final", "base": "RuNtImE", "digest": ""},
            {"stage": "last", "base": "FINAL", "digest": ""},
        ])
        self.assertTrue(all(item["digest"] for item in record["stages"][:2]))

    def test_scratch_needs_no_digest(self):
        docker = write_dockerfile(
            DOCKERFILE_PINNED + "FROM scratch AS out\n"
            "EXPOSE 8080\nENV LISTEN_ADDR=0.0.0.0:8080\n"
        )
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(record["stages"][-1],
                         {"stage": "out", "base": "scratch", "digest": ""})

    def test_platform_flag_records_the_external_image(self):
        docker = write_dockerfile(DOCKERFILE_PINNED.replace(
            "FROM rust:", "FROM --platform=$BUILDPLATFORM rust:", 1,
        ))
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 0, out + err)
        stage = record["stages"][0]
        self.assertEqual(stage["stage"], "builder")
        self.assertEqual(stage["base"], "rust:1.94-trixie")
        self.assertEqual(stage["digest"], "sha256:"
                         "652612f07bfbbdfa3af34761c1e435094c00dde4a98036132fca28c7bb2b165c")

    def test_platform_flag_does_not_exempt_unpinned_external_image(self):
        docker = write_dockerfile(DOCKERFILE_PINNED +
                                 "FROM --platform=linux/amd64 alpine:latest AS final\n")
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(record["stages"][-1],
                         {"stage": "final", "base": "alpine:latest", "digest": ""})

    def test_later_or_self_alias_does_not_exempt_external_image(self):
        for instruction in ("FROM runtime AS early\n", "FROM early AS early\n"):
            with self.subTest(instruction=instruction):
                code, out, err, record = check(
                    docker=write_dockerfile(instruction + DOCKERFILE_PINNED),
                )
                self.assertEqual(code, 1, out + err)
                self.assertEqual(record["verdict"], "finding")

    def test_continued_from_records_the_actual_image(self):
        for platform in ("", "--platform=$BUILDPLATFORM "):
            for escape in ("\\", "`"):
                with self.subTest(platform=platform, escape=escape):
                    text = "# syntax=docker/dockerfile:1\n# escape=%s\n" % escape + DOCKERFILE_PINNED.replace(
                        "FROM rust:", "FROM " + platform + "rust:", 1,
                    ).replace(" AS builder\n", " %s\n  AS builder\n" % escape, 1)
                    code, out, err, record = check(docker=write_dockerfile(text))
                    self.assertEqual(code, 0, out + err)
                    self.assertEqual(record["stages"][0], {
                        "stage": "builder", "base": "rust:1.94-trixie",
                        "digest": "sha256:"
                        "652612f07bfbbdfa3af34761c1e435094c00dde4a98036132fca28c7bb2b165c",
                    })

    def test_ignored_escape_directive_cannot_hide_an_external_stage(self):
        for prefix in ("# ordinary comment\n", "\n", "# unknown=value\n"):
            with self.subTest(prefix=prefix):
                text = prefix + "# escape=`\n" + DOCKERFILE_PINNED.replace(
                    "WORKDIR /app\n", "RUN echo `printf ok`\nFROM alpine AS final\n", 1,
                )
                code, out, err, record = check(docker=write_dockerfile(text))
                self.assertEqual(code, 1, out + err)
                self.assertEqual(record["verdict"], "finding")
                self.assertEqual(record["stages"][1],
                                 {"stage": "final", "base": "alpine", "digest": ""})
                self.assertEqual(len(record["stages"]), 3)

    def test_invalid_or_duplicate_escape_directive_is_refused(self):
        for prefix in ("# escape=x\n", "# escape=`\n# escape=\\\n"):
            with self.subTest(prefix=prefix):
                code, out, err, record = check(
                    docker=write_dockerfile(prefix + DOCKERFILE_PINNED),
                )
                self.assertEqual(code, 2, out + err)
                self.assertIsNone(record)

    def test_heredoc_data_cannot_create_an_alias(self):
        body = (
            "RUN <<SCRIPT\ncat > /tmp/Dockerfile <<'INNER'\n"
            "FROM scratch AS alpine\nINNER\nSCRIPT\n"
        )
        text = DOCKERFILE_PINNED.replace("WORKDIR /app\n", body, 1)
        code, out, err, record = check(
            docker=write_dockerfile(text + "FROM alpine AS final\n"),
        )
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(len(record["stages"]), 3)
        self.assertEqual(record["stages"][-1],
                         {"stage": "final", "base": "alpine", "digest": ""})

    def test_run_copy_and_add_heredoc_data_are_not_instructions(self):
        for instruction in ("RUN <<'EOF'", "COPY <<\"EOF\" /tmp/data",
                            "ADD <<'EOF' /app/data",
                            "RUN <<-EOF", "RUN cat <<ONE <<'TWO'"):
            with self.subTest(instruction=instruction):
                if "ONE" in instruction:
                    body = "FROM alpine:latest\nONE\nFROM scratch AS fake\nTWO\n"
                elif "<<-" in instruction:
                    body = "\tFROM alpine:latest\n\tEOF\n"
                else:
                    body = "FROM alpine:latest\nEOF\n"
                text = DOCKERFILE_PINNED.replace(
                    "WORKDIR /app\n", instruction + "\n" + body, 1,
                )
                code, out, err, record = check(docker=write_dockerfile(text))
                self.assertEqual(code, 0, out + err)
                self.assertEqual([item["stage"] for item in record["stages"]],
                                 ["builder", "runtime"])

    def test_quoted_redirection_is_not_a_heredoc(self):
        for instruction in ("RUN echo '<<EOF'", "RUN echo \"<< EOF\""):
            with self.subTest(instruction=instruction):
                text = DOCKERFILE_PINNED.replace(
                    "WORKDIR /app\n", instruction + "\n", 1,
                )
                code, out, err, record = check(
                    docker=write_dockerfile(text + "FROM alpine AS final\n"),
                )
                self.assertEqual(code, 1, out + err)
                self.assertEqual(len(record["stages"]), 3)

    def test_shell_comment_still_opens_a_heredoc_like_buildkit(self):
        # BuildKit splits words on whitespace and never strips shell
        # comments: `RUN echo ok # <<EOF` carries a heredoc, so a fake alias
        # hidden in its body cannot exempt a floating FROM.
        text = DOCKERFILE_PINNED.replace(
            "WORKDIR /app\n",
            "RUN echo ok # <<EOF\nFROM scratch AS alpine\nEOF\n", 1,
        )
        code, out, err, record = check(
            docker=write_dockerfile(text + "FROM alpine\n"),
        )
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(record["stages"][-1],
                         {"stage": "", "base": "alpine", "digest": ""})

    def test_onbuild_heredoc_data_cannot_create_an_alias(self):
        # BuildKit's canContainHeredoc looks through the ONBUILD prefix.
        text = DOCKERFILE_PINNED.replace(
            "WORKDIR /app\n",
            "ONBUILD RUN <<EOF\nFROM scratch AS alpine\nEOF\n", 1,
        )
        code, out, err, record = check(
            docker=write_dockerfile(text + "FROM alpine\n"),
        )
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(record["stages"][-1],
                         {"stage": "", "base": "alpine", "digest": ""})

    def test_heredoc_delimiter_is_the_whole_word_like_buildkit(self):
        # `RUN cat <<EOF;` takes `EOF;` as the whole delimiter, so a bare
        # `EOF` line does not end the body. The fake alias between `EOF`
        # and `EOF;` stays hidden and the floating `FROM alpine` still fails.
        # An implementation that splits at the operator (delimiter `EOF`)
        # would end at the bare line, register the alias, and wrongly pass.
        text = DOCKERFILE_PINNED.replace(
            "WORKDIR /app\n",
            "RUN cat <<EOF;\nEOF\nFROM scratch AS alpine\nEOF;\nFROM alpine\n", 1,
        )
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        # `FROM alpine` is the second stage; the pinned runtime follows it.
        self.assertEqual(record["stages"][1],
                         {"stage": "", "base": "alpine", "digest": ""})

    def test_bracket_shell_form_heredoc_data_cannot_create_an_alias(self):
        # BuildKit's parseMaybeJSON uses JSON form only when the args parse
        # as a JSON string array; otherwise the line is shell form and can
        # carry a heredoc. An implementation that treats any `[` prefix as
        # JSON form would miss the heredoc, register the fake alias, and
        # wrongly pass the floating `FROM alpine`.
        for instruction in (
            "RUN [ -d /tmp ] && cat <<EOF > /tmp/x",
            'RUN ["true"] <<EOF',
            "ONBUILD RUN [ -d /tmp ] && cat <<EOF",
        ):
            with self.subTest(instruction=instruction):
                text = DOCKERFILE_PINNED.replace(
                    "WORKDIR /app\n",
                    instruction + "\nFROM scratch AS alpine\nEOF\n", 1,
                )
                code, out, err, record = check(
                    docker=write_dockerfile(
                        text + "FROM alpine\nEXPOSE 8080\n"
                        "ENV LISTEN_ADDR=0.0.0.0:8080\n"
                    ),
                )
                self.assertEqual(code, 1, out + err)
                self.assertEqual(record["verdict"], "finding")
                self.assertEqual(record["stages"][-1],
                                 {"stage": "", "base": "alpine", "digest": ""})

    def test_json_form_carries_no_heredoc(self):
        # Args that parse as a JSON string array are JSON form: `<<` inside
        # the strings is data, never a heredoc.
        text = DOCKERFILE_PINNED.replace(
            "WORKDIR /app\n", 'RUN ["echo", "<<EOF"]\n', 1,
        )
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 0, out + err)
        self.assertEqual([item["stage"] for item in record["stages"]],
                         ["builder", "runtime"])

    def test_escaped_heredoc_delimiter_matches_buildkit(self):
        # BuildKit runs ProcessWords over the delimiter and removes the
        # escape, so `<<E\\OF` ends at `EOF` (and `<<\\ EOF` ends at ` EOF`).
        # The fake alias stays hidden and the floating `FROM alpine` fails.
        for instruction, body in (
            ("RUN cat <<E\\OF\n", "E\\OF\nFROM scratch AS alpine\nEOF\n"),
            ("RUN cat <<\\ EOF\n", "\\ EOF\nFROM scratch AS alpine\n EOF\n"),
        ):
            with self.subTest(instruction=instruction):
                text = DOCKERFILE_PINNED.replace(
                    "WORKDIR /app\n", instruction + body, 1,
                )
                code, out, err, record = check(
                    docker=write_dockerfile(
                        text + "FROM alpine\nEXPOSE 8080\n"
                        "ENV LISTEN_ADDR=0.0.0.0:8080\n"
                    ),
                )
                self.assertEqual(code, 1, out + err)
                self.assertEqual(record["verdict"], "finding")
                self.assertEqual(record["stages"][-1],
                                 {"stage": "", "base": "alpine", "digest": ""})

    def test_non_space_separators_still_split_heredoc_words(self):
        # BuildKit's lexer splits words on unicode.IsSpace: a heredoc opener
        # glued with NBSP, vertical tab, or form feed is still found.
        for sep, label in (("\u00a0", "nbsp"), ("\u000b", "vtab"),
                            ("\u000c", "ff")):
            with self.subTest(sep=label):
                text = DOCKERFILE_PINNED.replace(
                    "WORKDIR /app\n",
                    "RUN cat%s<<EOF\nFROM scratch AS alpine\nEOF\n" % sep, 1,
                )
                code, out, err, record = check(
                    docker=write_dockerfile(
                        text + "FROM alpine\nEXPOSE 8080\n"
                        "ENV LISTEN_ADDR=0.0.0.0:8080\n"
                    ),
                )
                self.assertEqual(code, 1, out + err)
                self.assertEqual(record["verdict"], "finding")
                self.assertEqual(record["stages"][-1],
                                 {"stage": "", "base": "alpine", "digest": ""})

    def test_spaced_redirect_still_opens_a_heredoc_like_buildkit(self):
        # BuildKit's shell lexer glues spaces/tabs after `<<` into the same
        # word, so `RUN cat << EOF` opens a heredoc with delimiter `EOF`.
        text = DOCKERFILE_PINNED.replace(
            "WORKDIR /app\n",
            "RUN cat << EOF\nFROM scratch AS alpine\nEOF\n", 1,
        )
        code, out, err, record = check(
            docker=write_dockerfile(
                text + "FROM alpine\nEXPOSE 8080\n"
                "ENV LISTEN_ADDR=0.0.0.0:8080\n"
            ),
        )
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(record["stages"][-1],
                         {"stage": "", "base": "alpine", "digest": ""})

    def test_final_stage_inherits_ports_from_its_base_stage(self):
        docker = write_dockerfile(
            DOCKERFILE_PINNED
            + "FROM runtime AS final\nUSER 1000\n"
        )
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(record["findings"], [])
        self.assertEqual(record["stages"][-1],
                         {"stage": "final", "base": "runtime", "digest": ""})

    def test_final_stage_listen_addr_override_replaces_inherited_value(self):
        # Docker ENV replaces the variable: a final stage that changes the
        # listen port ships 9090, so BOT_PORT=8080 must still fail W7 on
        # LISTEN_ADDR even though the inherited EXPOSE still agrees.
        docker = write_dockerfile(
            DOCKERFILE_PINNED
            + "FROM runtime AS final\nENV LISTEN_ADDR=0.0.0.0:9090\n"
        )
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("LISTEN_ADDR" in item for item in record["findings"]),
            record["findings"],
        )
        self.assertFalse(
            any("EXPOSE" in item for item in record["findings"]),
            record["findings"],
        )

    def test_final_stage_listen_addr_override_with_non_default_host_is_a_finding(self):
        # Only 0.0.0.0:<port> counts as listening: an override to loopback,
        # an empty host, or IPv6 any-address ships a different listen
        # endpoint, so BOT_PORT=8080 must fail W7 on LISTEN_ADDR. The legacy
        # `ENV LISTEN_ADDR <value>` form replaces the same way.
        for value in ("127.0.0.1:9090", ":9090", "[::]:9090",
                      "0.0.0.0:9090"):
            with self.subTest(value=value):
                docker = write_dockerfile(
                    DOCKERFILE_PINNED
                    + "FROM runtime AS final\nENV LISTEN_ADDR=%s\n" % value
                )
                code, out, err, record = check(docker=docker)
                self.assertEqual(code, 1, out + err)
                self.assertTrue(
                    any("LISTEN_ADDR" in item for item in record["findings"]),
                    record["findings"],
                )
                self.assertFalse(
                    any("EXPOSE" in item for item in record["findings"]),
                    record["findings"],
                )
        with self.subTest(value="legacy-space-form"):
            docker = write_dockerfile(
                DOCKERFILE_PINNED
                + "FROM runtime AS final\nENV LISTEN_ADDR 0.0.0.0:9090\n"
            )
            code, out, err, record = check(docker=docker)
            self.assertEqual(code, 1, out + err)
            self.assertTrue(
                any("LISTEN_ADDR" in item for item in record["findings"]),
                record["findings"],
            )

    def test_lone_carriage_return_does_not_split_a_line_like_buildkit(self):
        # BuildKit splits lines on LF only, so `# note\rRUN ...` is one
        # comment. Universal-newline splitting would see a RUN with a heredoc
        # (delimiter EXPOSE 8080), skip the real floating FROM alpine, and
        # wrongly pass.
        text = (
            DOCKERFILE_PINNED
            + "# note\rRUN cat <<\"EXPOSE 8080\"\n"
            + "FROM alpine\nEXPOSE 8080\nENV LISTEN_ADDR=0.0.0.0:8080\n"
        )
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(record["stages"][-1],
                         {"stage": "", "base": "alpine", "digest": ""})

    def test_control_separators_are_not_go_spaces(self):
        # U+001C-U+001F satisfy Python's str.isspace() but not Go's
        # unicode.IsSpace, so `RUN echo\x1c<<"EXPOSE 8080"` carries no heredoc
        # for BuildKit. Splitting on str.isspace() would see one, skip the
        # real floating FROM alpine, and wrongly pass.
        for sep in ("\x1c", "\x1d", "\x1e", "\x1f"):
            with self.subTest(sep=repr(sep)):
                text = (
                    DOCKERFILE_PINNED
                    + "RUN echo%s<<\"EXPOSE 8080\"\n" % sep
                    + "FROM alpine\nEXPOSE 8080\n"
                      "ENV LISTEN_ADDR=0.0.0.0:8080\n"
                )
                code, out, err, record = check(
                    docker=write_dockerfile(text))
                self.assertEqual(code, 1, out + err)
                self.assertEqual(record["verdict"], "finding")
                self.assertEqual(record["stages"][-1],
                                 {"stage": "", "base": "alpine", "digest": ""})

    def test_non_ascii_digit_does_not_open_a_heredoc(self):
        # Go's RE2 \d is ASCII-only, while Python's \d also matches U+0661.
        # `RUN echo ١<<"EXPOSE 8080"` is one non-heredoc word for BuildKit;
        # an ASCII-blind regex would see a heredoc, skip the floating FROM,
        # and wrongly pass.
        text = (
            DOCKERFILE_PINNED
            + "RUN echo ١<<\"EXPOSE 8080\"\n"
            + "FROM alpine\nEXPOSE 8080\nENV LISTEN_ADDR=0.0.0.0:8080\n"
        )
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(record["stages"][-1],
                         {"stage": "", "base": "alpine", "digest": ""})

    def test_dollar_brace_with_redirect_is_refused_fail_closed(self):
        # BuildKit re-splits ${...} expansions ignoring quotes, so
        # `RUN echo ${X:-'a <<EOF\'}` carries a heredoc the word split cannot
        # see. Refuse (exit 2) instead of risking a missed alias.
        text = DOCKERFILE_PINNED.replace(
            "WORKDIR /app\n",
            "RUN echo ${X:-'a <<EOF\\'}\nFROM scratch AS alpine\nEOF'}\n", 1,
        )
        code, out, err, record = check(
            docker=write_dockerfile(
                text + "FROM alpine\nEXPOSE 8080\n"
                "ENV LISTEN_ADDR=0.0.0.0:8080\n"
            ),
        )
        self.assertEqual(code, 2, out + err)
        self.assertIsNone(record)
        self.assertNotIn("Traceback", err)

    def test_huge_expose_range_refuses_quickly(self):
        docker = write_dockerfile(
            DOCKERFILE_PINNED.replace(
                "EXPOSE 8080\n", "EXPOSE 1-100000000\n", 1),
        )
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 2, out + err)
        self.assertIsNone(record)

    def test_out_of_range_expose_bounds_are_refused(self):
        for spec in ("EXPOSE 8082-8080\n", "EXPOSE 70000\n"):
            with self.subTest(spec=spec):
                docker = write_dockerfile(
                    DOCKERFILE_PINNED.replace("EXPOSE 8080\n", spec, 1),
                )
                code, out, err, record = check(docker=docker)
                self.assertEqual(code, 2, out + err)
                self.assertIsNone(record)

    def test_three_trailing_escapes_do_not_join_the_next_line(self):
        text = DOCKERFILE_PINNED.replace(
            "FROM gcr.io", "RUN echo a\\\\\\\nFROM alpine AS final\nFROM gcr.io", 1,
        )
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["stages"][1],
                         {"stage": "final", "base": "alpine", "digest": ""})

    def test_byte_order_mark_does_not_hide_the_first_from(self):
        text = "\ufeffFROM alpine AS final\n" + DOCKERFILE_PINNED
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["stages"][0],
                         {"stage": "final", "base": "alpine", "digest": ""})

    def test_any_exposed_port_can_agree_with_bot_port(self):
        text = DOCKERFILE_PINNED.replace("EXPOSE 8080\n", "EXPOSE 9229 8080/tcp\n", 1)
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(record["findings"], [])

    def test_unicode_line_separator_does_not_end_a_line(self):
        text = DOCKERFILE_PINNED.replace(
            "FROM gcr.io", "RUN echo a\\ \nFROM alpine AS final\nFROM gcr.io", 1,
        )
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["stages"][1],
                         {"stage": "final", "base": "alpine", "digest": ""})

    def test_port_agreement_uses_only_the_shipped_final_stage(self):
        builder, runtime = DOCKERFILE_PINNED.split("FROM gcr.io", 1)
        text = (builder + "EXPOSE 8080\nENV LISTEN_ADDR=0.0.0.0:8080\n"
                + "FROM gcr.io" + runtime.replace("8080", "9000"))
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("agrees with no Dockerfile EXPOSE port" in item
                for item in record["findings"]),
            record["findings"],
        )

    def test_exposed_port_range_can_agree_with_bot_port(self):
        text = DOCKERFILE_PINNED.replace("EXPOSE 8080\n", "EXPOSE 8078-8082\n", 1)
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(record["findings"], [])

    def test_glued_shift_operator_is_not_a_heredoc(self):
        text = DOCKERFILE_PINNED.replace("WORKDIR /app\n", "RUN echo $((1<<4))\n", 1)
        code, out, err, record = check(docker=write_dockerfile(text))
        self.assertEqual(code, 0, out + err)
        self.assertEqual([item["stage"] for item in record["stages"]],
                         ["builder", "runtime"])

    def test_unterminated_logical_instructions_are_refused(self):
        # `RUN <<'EOF` opens no heredoc: BuildKit's heredocsFromLine drops the
        # lexer error (`words, _ :=`), so the line is a complete RUN with no
        # body to terminate (see test_unbalanced_quote_opens_no_heredoc).
        for instruction in ("RUN <<EOF\nFROM scratch AS alpine\n",
                            "FROM runtime \\\n"):
            with self.subTest(instruction=instruction):
                code, out, err, record = check(
                    docker=write_dockerfile(DOCKERFILE_PINNED + instruction),
                )
                self.assertEqual(code, 2, out + err)
                self.assertIsNone(record)
                self.assertNotIn("Traceback", err)

    def test_unbalanced_quote_opens_no_heredoc_like_buildkit(self):
        # BuildKit's heredocsFromLine drops the shell-lexer error, so
        # `RUN echo <<"EXPOSE 8080" don't` (unterminated `'`) carries no
        # heredoc. The following floating `FROM alpine` must be a finding,
        # not hidden as heredoc data.
        text = DOCKERFILE_PINNED.replace(
            "WORKDIR /app\n", 'RUN echo <<"EXPOSE 8080" don\'t\n', 1,
        )
        code, out, err, record = check(
            docker=write_dockerfile(
                text + "FROM alpine\nEXPOSE 8080\n"
                "ENV LISTEN_ADDR=0.0.0.0:8080\n"
            ),
        )
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(record["stages"][-1],
                         {"stage": "", "base": "alpine", "digest": ""})

    def test_onbuild_nbsp_opens_no_heredoc_like_buildkit(self):
        # BuildKit splits the ONBUILD/RUN command word on ASCII whitespace
        # only, so `ONBUILD RUN\xa0<<...` (NBSP) is not a RUN heredoc. The
        # following floating `FROM alpine` must be a finding.
        text = DOCKERFILE_PINNED.replace(
            "WORKDIR /app\n", "ONBUILD RUN\u00a0<<\"EXPOSE 8080\"\n", 1,
        )
        code, out, err, record = check(
            docker=write_dockerfile(
                text + "FROM alpine\nEXPOSE 8080\n"
                "ENV LISTEN_ADDR=0.0.0.0:8080\n"
            ),
        )
        self.assertEqual(code, 1, out + err)
        self.assertEqual(record["verdict"], "finding")
        self.assertEqual(record["stages"][-1],
                         {"stage": "", "base": "alpine", "digest": ""})

    def test_listen_addr_assigns_only_from_env_exact_key(self):
        # Only `ENV LISTEN_ADDR=...` (exact key, last wins) replaces the
        # inherited value. References (HEALTHCHECK/CMD/RUN), longer keys
        # (OLD_LISTEN_ADDR), ARG, and LABEL never assign.
        for body, expected_code, label in (
            # References leave the inherited 0.0.0.0:8080 alone: pass.
            ("HEALTHCHECK CMD curl http://$LISTEN_ADDR/health\n", 0,
             "healthcheck-ref"),
            ("RUN echo $LISTEN_ADDR\n", 0, "run-ref"),
            # Longer key is not an assignment: pass.
            ("FROM runtime AS final\nENV OLD_LISTEN_ADDR=0.0.0.0:9090\n",
             0, "old-prefix"),
            # ARG/LABEL never assign runtime env: pass.
            ("FROM runtime AS final\nARG LISTEN_ADDR=0.0.0.0:9090\n",
             0, "arg"),
            ("FROM runtime AS final\nLABEL LISTEN_ADDR=\"0.0.0.0:9090\"\n",
             0, "label"),
            # Unrelated later ENV leaves the assignment alone: pass.
            ("FROM runtime AS final\nENV OTHER=1\n", 0, "unrelated-env"),
            # Duplicate key: last wins, so a final 9090 is a finding.
            ("FROM runtime AS final\n"
             "ENV LISTEN_ADDR=0.0.0.0:8080 LISTEN_ADDR=0.0.0.0:9090\n",
             1, "duplicate-last-wins"),
        ):
            with self.subTest(label=label):
                code, out, err, record = check(
                    docker=write_dockerfile(DOCKERFILE_PINNED + body),
                )
                self.assertEqual(code, expected_code, out + err)
                if expected_code == 0:
                    self.assertEqual(record["findings"], [])
                else:
                    self.assertTrue(
                        any("LISTEN_ADDR" in item
                            for item in record["findings"]),
                        record["findings"],
                    )

    def test_env_listen_addr_is_read_like_buildkit_words(self):
        # BuildKit splits ENV words on Go whitespace, keeps quotes raw, then
        # removes quotes and escapes from each key and value.
        for body, expected_code, label in (
            ('ENV "LISTEN_ADDR"=0.0.0.0:9090\n', 1, "quoted-key"),
            ('ENV "LISTEN_ADDR" 0.0.0.0:9090\n', 1, "quoted-key-legacy"),
            ("ENV LISTEN\\_ADDR=0.0.0.0:9090\n", 1, "escaped-key"),
            ('ENV LISTEN_ADDR="0.0.0.0:9090"\n', 1, "quoted-value"),
            ('ENV LISTEN_ADDR=0.0.0.0:9090 NOTE="default LISTEN_ADDR=0.0.0.0:8080"\n',
             1, "later-quoted-value-mentions-key"),
            ('ENV NOTE="set LISTEN_ADDR=0.0.0.0:9090 to override"\n',
             0, "quoted-value-mentions-key-assigns-nothing"),
            ("ENV A=1\u00a0LISTEN_ADDR=0.0.0.0:9090\n", 1, "nbsp-separator"),
            ("ENV A=1\u2003LISTEN_ADDR=0.0.0.0:9090\n", 1, "em-space-separator"),
            ("ENV A=<< LISTEN_ADDR=0.0.0.0:9090\n", 1, "shift-is-not-a-heredoc"),
            ("ENV FOO LISTEN_ADDR=0.0.0.0:9090\n", 0, "legacy-value-names-key"),
            ("ENV $X=0.0.0.0:9090\n", 1, "dollar-key-may-expand-to-key"),
        ):
            with self.subTest(label=label):
                code, out, err, record = check(
                    docker=write_dockerfile(DOCKERFILE_PINNED + body),
                )
                self.assertEqual(code, expected_code, out + err)
                if expected_code == 0:
                    self.assertEqual(record["findings"], [])
                else:
                    self.assertTrue(
                        any("LISTEN_ADDR" in item
                            for item in record["findings"]),
                        record["findings"],
                    )

    def test_escape_directive_changes_env_word_splitting(self):
        # Under `# escape=`` a backslash is literal, so the space after it ends
        # the word and LISTEN_ADDR is assigned. The default escape joins the
        # words and assigns nothing.
        for head, body, expected_code, label in (
            ("# escape=`\n", "ENV A=\\ LISTEN_ADDR=0.0.0.0:9090\n", 1,
             "backtick-literal-backslash-splits"),
            ("", "ENV A=\\ LISTEN_ADDR=0.0.0.0:9090\n", 0,
             "default-escape-joins"),
            ("# escape=`\n", "ENV `LISTEN_ADDR`=0.0.0.0:9090\n", 1,
             "backtick-escaped-key"),
            ("", "ENV `LISTEN_ADDR`=0.0.0.0:9090\n", 0,
             "default-escape-leaves-backticks"),
        ):
            with self.subTest(label=label):
                code, out, err, record = check(
                    docker=write_dockerfile(head + DOCKERFILE_PINNED + body),
                )
                self.assertEqual(code, expected_code, out + err)

    def test_env_flag_terminator_and_unknown_flags_follow_buildkit(self):
        # A bare `--` ends ENV's flag list, so the assignment after it counts.
        # Any other flag fails the BuildKit build (ENV defines none).
        for body, expected_code, label in (
            ("ENV -- LISTEN_ADDR=0.0.0.0:8080\n", 0, "terminator-matching-port"),
            ("ENV -- LISTEN_ADDR=0.0.0.0:9999\n", 1, "terminator-assigns"),
            ("ENV --  LISTEN_ADDR=0.0.0.0:9999\n", 1, "terminator-two-spaces"),
            ("ENV -- LISTEN_ADDR 0.0.0.0:9999\n", 1, "terminator-legacy"),
            ("ENV --foo LISTEN_ADDR=0.0.0.0:8080\n", 1, "unknown-flag"),
            ("ENV --\u2003LISTEN_ADDR=0.0.0.0:8080\n", 1, "non-ascii-space-after-dashes"),
            ("ENV \u3000-- LISTEN_ADDR=0.0.0.0:9999\n", 0,
             "non-ascii-space-hides-terminator"),
            ("ENV LISTEN_ADDR=0.0.0.0:8080\x1f\n", 1, "control-char-is-not-space"),
        ):
            with self.subTest(label=label):
                code, out, err, record = check(
                    docker=write_dockerfile(DOCKERFILE_PINNED + body),
                )
                self.assertEqual(code, expected_code, out + err)
                if expected_code == 1:
                    self.assertTrue(
                        any("LISTEN_ADDR" in item
                            for item in record["findings"]),
                        record["findings"],
                    )

    def test_heredoc_delimiter_keeps_trailing_bytes_like_buildkit(self):
        # BuildKit keeps the instruction's trailing bytes, so `\<VT>` is an
        # escaped VT and the delimiter is `EOF<VT>`. The ENV 8080 line is then
        # heredoc body, and the final stage listens on 9999.
        body = ("ENV LISTEN_ADDR=0.0.0.0:9999\nRUN cat <<EOF\\\x0b\nEOF\n"
                "ENV LISTEN_ADDR=0.0.0.0:8080\nEOF\x0b\n")
        code, out, err, record = check(docker=write_dockerfile(DOCKERFILE_PINNED + body))
        self.assertEqual(code, 1, out + err)
        self.assertTrue(any("LISTEN_ADDR" in item for item in record["findings"]),
                        record["findings"])

    def test_heredoc_operator_with_only_blanks_opens_nothing(self):
        # `<<` followed only by blanks has an empty delimiter, which BuildKit ignores.
        code, out, err, record = check(
            docker=write_dockerfile(DOCKERFILE_PINNED + "RUN cat << \t\n"),
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(record["findings"], [])

    def test_control_char_continuation_line_is_content_like_buildkit(self):
        # \x1c is not Go space: the continuation line is content, so the RUN ends
        # there and the ENV that follows is a real instruction.
        for body, expected_code, label in (
            ("RUN echo a \\\n\x1c\nENV LISTEN_ADDR=127.0.0.1:8080\n", 1,
             "control-char-line-ends-run"),
            ("RUN echo a \\\n\x1c# x\nENV LISTEN_ADDR=127.0.0.1:8080\n", 1,
             "control-char-then-comment-text"),
        ):
            with self.subTest(label=label):
                code, out, err, record = check(
                    docker=write_dockerfile(DOCKERFILE_PINNED + body),
                )
                self.assertEqual(code, expected_code, out + err)

    def test_directive_name_and_value_match_buildkit(self):
        # BuildKit's \s is ASCII, so a VT before `=` is not a directive, and a
        # directive needs a non-empty value. Either way the escape stays `\`,
        # so the RUN folds the ENV into itself and nothing listens.
        digest = ("gcr.io/distroless/cc-debian13:nonroot@sha256:"
                  "e792ab3d241a468a4fd7519ddbbebe66b49b5f365771716ea688ad40b6c6f1c2")
        stage = "FROM %s AS runtime\nEXPOSE 8080\nRUN echo hi \\\nENV LISTEN_ADDR=0.0.0.0:8080\n" % digest
        for head, expected_code, label in (
            ("# escape\x0b=`\n", 1, "vt-before-equals-is-not-a-directive"),
            ("# syntax=\n# escape=`\n", 1, "empty-value-closes-the-window"),
            ("# escape=`\n", 0, "backtick-directive-is-honored"),
        ):
            with self.subTest(label=label):
                code, out, err, record = check(
                    docker=write_dockerfile(head + stage),
                )
                self.assertEqual(code, expected_code, out + err)

    def test_directive_window_matches_buildkit_unicode_rules(self):
        # BuildKit trims only leading Go spaces, keeps a trailing NBSP in the
        # value, and folds names with ASCII case only. Each decoy is a directive
        # for one side only, so the escape line after it is honored by one side
        # and the RUN joins the final FROM on the other.
        digest = ("rust:1.94-trixie@sha256:"
                  "652612f07bfbbdfa3af34761c1e435094c00dde4a98036132fca28c7bb2b165c")
        for decoy, run_end, label in (
            ("# check=\u00a0\n", "\\", "nbsp-valued-check-is-a-directive"),
            ("# chec\u212a=skip=all\n", "`", "kelvin-sign-check-key-is-not-a-directive"),
            ("# \u017fyntax=docker/dockerfile:1\n", "`", "long-s-syntax-key-is-not-a-directive"),
        ):
            with self.subTest(label=label):
                text = (
                    decoy + "# escape=`\n"
                    + "FROM %s AS builder\nRUN echo hi # x %s\nFROM alpine AS final\n"
                    % (digest, run_end)
                )
                code, out, err, record = check(docker=write_dockerfile(text))
                self.assertEqual(code, 1, out + err)
                self.assertIn({"stage": "final", "base": "alpine", "digest": ""},
                              record["stages"], out)

    def test_onbuild_in_base_stage_is_refused_and_final_onbuild_passes(self):
        # BuildKit runs a base stage's ONBUILD in the child, which the pre-check
        # does not evaluate, so the base stage is refused. An ONBUILD in the
        # shipped final stage triggers only in images built from it.
        code, out, err, record = check(
            docker=write_dockerfile(
                DOCKERFILE_PINNED
                + "ONBUILD ENV LISTEN_ADDR=127.0.0.1:8080\n"
                + "FROM runtime AS final\nENV LISTEN_ADDR=0.0.0.0:8080\n"
            ),
        )
        self.assertEqual(code, 2, out + err)
        code, out, err, record = check(
            docker=write_dockerfile(
                DOCKERFILE_PINNED + "ONBUILD ENV LISTEN_ADDR=0.0.0.0:9999\n"
            ),
        )
        self.assertEqual(code, 0, out + err)

    def test_from_without_image_is_refused(self):
        for instruction in ("FROM", "FROM --platform=linux/amd64",
                            "FROM --platform= rust:latest"):
            with self.subTest(instruction=instruction):
                code, out, err, record = check(
                    docker=write_dockerfile(DOCKERFILE_PINNED + instruction + "\n"),
                )
                self.assertEqual(code, 2, out + err)
                self.assertIsNone(record)

    def test_missing_staging_is_refused_not_pass(self):
        code, out, err, record = check(toml=write_toml(strip_staging=True))
        self.assertEqual(code, 2, "absent staging must refuse: %s%s" % (out, err))
        self.assertIn("REFUSED", err)
        self.assertIsNone(record, "a refused run writes no record")

    def test_absent_container_class_is_a_finding(self):
        toml = write_toml()
        with open(toml, encoding="utf-8") as handle:
            text = handle.read()
        # Break ONLY the container class (first occurrence): the DO binding
        # keeps TwoBotContainer so this case isolates the container-class gate.
        text = text.replace('class_name = "TwoBotContainer"',
                            'class_name = "OtherBox"', 1)
        with open(toml, "w", encoding="utf-8") as handle:
            handle.write(text)
        code, out, err, record = check(toml=toml)
        self.assertEqual(code, 1, "missing container class: %s%s" % (out, err))
        self.assertTrue(
            any("containers" in item for item in record["findings"]),
            record["findings"],
        )
        self.assertEqual(record["verdict"], "finding")

    def test_unrepeated_var_is_a_finding(self):
        staging = dict(STAGING_VARS)
        del staging["TWO_GUILD_NAME"]
        code, out, err, record = check(toml=write_toml(staging_vars=staging))
        self.assertEqual(code, 1, "dropped TWO_GUILD_NAME: %s%s" % (out, err))
        self.assertTrue(
            any("TWO_GUILD_NAME" in item for item in record["findings"]),
            record["findings"],
        )

    def test_ingress_in_production_is_a_finding(self):
        prod = dict(BASE_VARS)
        prod["INTERNAL_ACTIONS_INGRESS"] = "1"
        code, out, err, record = check(toml=write_toml(prod_vars=prod))
        self.assertEqual(code, 1, "production ingress: %s%s" % (out, err))
        self.assertTrue(
            any("production" in item for item in record["findings"]),
            record["findings"],
        )

    def test_floating_tag_is_a_finding(self):
        docker = write_dockerfile(
            "FROM rust:1.94-trixie AS builder\n"
            "FROM gcr.io/distroless/cc-debian13:nonroot@sha256:"
            "e792ab3d241a468a4fd7519ddbbebe66b49b5f365771716ea688ad40b6c6f1c2"
            " AS runtime\nEXPOSE 8080\nENV LISTEN_ADDR=0.0.0.0:8080\n"
        )
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 1, "floating tag: %s%s" % (out, err))
        self.assertTrue(
            any("digest-pinned" in item for item in record["findings"]),
            record["findings"],
        )

    def test_integer_bot_port_agrees_with_exposed_port(self):
        code, out, err, record = check(
            toml=mutated_toml(('BOT_PORT = "8080"', "BOT_PORT = 8080")),
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(record["findings"], [])

    def test_non_integer_max_instances_is_a_drift_finding(self):
        for value in ("true", "1.0"):
            with self.subTest(value=value):
                code, out, err, record = check(
                    toml=mutated_toml(("max_instances = 1",
                                       "max_instances = " + value)),
                )
                self.assertEqual(code, 1, out + err)
                self.assertTrue(
                    any("max_instances drift" in item
                        for item in record["findings"]),
                    record["findings"],
                )

    def test_secret_shaped_value_nested_in_a_var_table_is_a_finding(self):
        toml = write_toml(
            extra='[env.staging.vars.NESTED]\ntoken_value = "ghp_12345678"\n',
        )
        code, out, err, record = check(toml=toml)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("secret-shaped value under NESTED" in item
                for item in record["findings"]),
            record["findings"],
        )
        self.assertNotIn("ghp_12345678", out + err)

    def test_secret_shaped_plain_value_is_a_finding_without_echo(self):
        top = dict(BASE_VARS, NOTE="ghp_12345678")
        staging = dict(STAGING_VARS, NOTE="ghp_12345678")
        code, out, err, record = check(
            toml=write_toml(top_vars=top, staging_vars=staging),
        )
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("[vars]: secret-shaped value under NOTE" in item
                for item in record["findings"]),
            record["findings"],
        )
        self.assertNotIn("ghp_12345678", out + err)

    def test_missing_durable_object_binding_is_a_finding(self):
        code, out, err, record = check(
            toml=mutated_toml(('name = "TWO_BOT"', 'name = "TWO_OTHER"')),
        )
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("binding TWO_BOT absent" in item for item in record["findings"]),
            record["findings"],
        )

    def test_version_metadata_drift_is_a_finding(self):
        code, out, err, record = check(
            toml=mutated_toml(('binding = "CF_VERSION_METADATA"',
                               'binding = "OTHER"')),
        )
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("version_metadata]: binding drift" in item
                for item in record["findings"]),
            record["findings"],
        )

    def test_exposed_port_must_agree_with_bot_port(self):
        docker = write_dockerfile(
            DOCKERFILE_PINNED.replace("EXPOSE 8080\n", "EXPOSE 8081\n", 1),
        )
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("agrees with no Dockerfile EXPOSE port" in item
                for item in record["findings"]),
            record["findings"],
        )

    def test_staging_ingress_must_stay_default_dark(self):
        staging = dict(STAGING_VARS, INTERNAL_ACTIONS_INGRESS="0")
        code, out, err, record = check(toml=write_toml(staging_vars=staging))
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("default-dark ingress" in item for item in record["findings"]),
            record["findings"],
        )

    def test_disabled_staging_observability_is_a_finding(self):
        code, out, err, record = check(
            toml=mutated_toml(("enabled = true", "enabled = false")),
        )
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            any("staging logs disabled" in item for item in record["findings"]),
            record["findings"],
        )

    def test_fromless_dockerfile_is_refused(self):
        docker = write_dockerfile("WORKDIR /app\nEXPOSE 8080\n")
        code, out, err, record = check(docker=docker)
        self.assertEqual(code, 2, "FROM-less file must refuse: %s%s" % (out, err))
        self.assertIn("REFUSED", err)

    def test_missing_dockerfile_is_refused(self):
        code, out, err = run_tool(
            "check",
            "--wrangler",
            write_toml(),
            "--dockerfile",
            "/nonexistent/Dockerfile",
        )
        self.assertEqual(code, 2, "missing Dockerfile: %s%s" % (out, err))

    def test_unparseable_toml_is_refused(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "wrangler.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[[[ this is not toml\n")
        code, out, err = run_tool(
            "check", "--wrangler", path, "--dockerfile", write_dockerfile()
        )
        self.assertEqual(code, 2, "unparseable TOML: %s%s" % (out, err))
        self.assertIn("REFUSED", err)

    def test_secret_value_never_enters_output(self):
        sentinel = "SENTINEL_PRECHECK_SECRET_MUST_NEVER_APPEAR_7qx2"
        staging = dict(STAGING_VARS)
        staging["BACKUP_WEBHOOK"] = "https://hooks.example/" + sentinel
        code, out, err, record = check(toml=write_toml(staging_vars=staging))
        self.assertEqual(code, 1, "secret-shaped var: %s%s" % (out, err))
        self.assertNotIn(sentinel, out + err)
        self.assertNotIn(
            sentinel, json.dumps(record), "record must not carry var values"
        )

    def test_surface_withholds_unknown_var_values(self):
        staging = dict(STAGING_VARS)
        staging["QUIET_TOKEN"] = "abc123"
        code, out, err = run_tool(
            "surface", "--wrangler", write_toml(staging_vars=staging)
        )
        self.assertEqual(code, 0, "surface: %s%s" % (out, err))
        self.assertIn("BOT_PORT=8080", out)
        self.assertNotIn("abc123", out + err)

    def test_surface_withholds_secret_shaped_values_under_safe_keys(self):
        staging = dict(STAGING_VARS)
        staging["BOT_PORT"] = "ghp_12345678"
        code, out, err = run_tool(
            "surface", "--wrangler", write_toml(staging_vars=staging)
        )
        self.assertEqual(code, 0, "surface: %s%s" % (out, err))
        self.assertIn("var: BOT_PORT (value withheld)", out)
        self.assertNotIn("ghp_12345678", out + err)

    def test_surface_withholds_non_scalar_values_under_safe_keys(self):
        staging = dict(STAGING_VARS)
        del staging["KEEPALIVE_SECONDS"]
        toml = write_toml(
            staging_vars=staging,
            extra='[env.staging.vars.KEEPALIVE_SECONDS]\n'
                  'value = "ghp_12345678"\n',
        )
        code, out, err = run_tool("surface", "--wrangler", toml)
        self.assertEqual(code, 0, "surface: %s%s" % (out, err))
        self.assertIn("var: KEEPALIVE_SECONDS (value withheld)", out)
        self.assertNotIn("ghp_12345678", out + err)

    def test_container_gate_is_load_bearing(self):
        mutant = delete_anchor(
            TOOL, "# MUTATION-ANCHOR-START", "# MUTATION-ANCHOR-END"
        )
        toml = write_toml()
        with open(toml, encoding="utf-8") as handle:
            text = handle.read()
        text = text.replace('class_name = "TwoBotContainer"',
                            'class_name = "OtherBox"', 1)
        with open(toml, "w", encoding="utf-8") as handle:
            handle.write(text)
        proc = subprocess.run(
            [sys.executable, mutant, "check", "--wrangler", toml,
             "--dockerfile", write_dockerfile()],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "mutant without the class gate must pass the broken config: %s%s"
            % (proc.stdout, proc.stderr),
        )

    def test_digest_gate_is_load_bearing(self):
        mutant = delete_anchor(
            TOOL,
            "# MUTATION-ANCHOR-DIGEST-START",
            "# MUTATION-ANCHOR-DIGEST-END",
        )
        docker = write_dockerfile(
            "FROM rust:1.94-trixie AS builder\n"
            "FROM gcr.io/distroless/cc-debian13:nonroot@sha256:"
            "e792ab3d241a468a4fd7519ddbbebe66b49b5f365771716ea688ad40b6c6f1c2"
            " AS runtime\nEXPOSE 8080\nENV LISTEN_ADDR=0.0.0.0:8080\n"
        )
        proc = subprocess.run(
            [sys.executable, mutant, "check", "--wrangler", write_toml(),
             "--dockerfile", docker],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "mutant without the digest gate must pass the floating tag: %s%s"
            % (proc.stdout, proc.stderr),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
