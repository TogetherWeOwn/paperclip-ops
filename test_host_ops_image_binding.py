#!/usr/bin/env python3
"""Offline tests: no host sockets, credentials, sudo execution or host writes."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("image_binding", ROOT / "host-ops/image_binding.py")
binding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(binding)
PIN = "registry.example/paperclip@sha256:" + "a" * 64
DIGEST = "sha256:" + "a" * 64
IMAGE_ID = "b" * 64
OTHER_ID = "c" * 64
OTHER_DIGEST = "sha256:" + "d" * 64
QUADLET = "[Unit]\nDescription=Paperclip\n[Container]\nImage=" + PIN + "\nReadOnly=true\n"
CONTAINER = IMAGE_ID + "|" + DIGEST + "|true\n"
IMAGE = json.dumps({"Id": "sha256:" + IMAGE_ID, "Digest": DIGEST})


class SourceTests(unittest.TestCase):
    def test_literal_container_pin(self):
        self.assertEqual(binding.quadlet_image_ref(QUADLET), PIN)

    def test_comments_and_unrelated_image_words_are_not_sources(self):
        text = "# Image=bad\n; Image=bad\n" + QUADLET + "Label=Image=not-a-source\n"
        self.assertEqual(binding.quadlet_image_ref(text), PIN)

    def test_no_fallback_to_tags_ids_templates_or_expansions(self):
        for value in ("", "paperclip", "localhost/paperclip:v1", "sha256:" + IMAGE_ID,
                      "paperclip.image", '"' + PIN + '"', "%h/paperclip", "${IMAGE}",
                      PIN + " --privileged", PIN + ", ALL=(ALL) ALL", PIN.upper()):
            with self.subTest(value=value), self.assertRaises(binding.Refusal):
                binding.quadlet_image_ref(QUADLET.replace(PIN, value))

    def test_observed_node7b1ff338_tag_form_fails_closed(self):
        # node7b1ff338's Config.Image is a tag (paperclip-local:2026.1001.0-14f66a7-candidate2).
        # The installer and broker require an immutable name@sha256 pin; the
        # observed tag must refuse at both layers rather than attest a movable
        # reference. The operator resolves the digest via RepoDigests instead.
        tag = "paperclip-local:2026.1001.0-14f66a7-candidate2"
        with self.assertRaises(binding.Refusal):
            binding.quadlet_image_ref("[Container]\nImage=" + tag + "\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(binding.main(["source-ref", "--expected-ref", tag]), 2)
        self.assertEqual(out.getvalue(), "")

    def test_ambiguous_quadlets_refuse(self):
        for text in (QUADLET + "Image=" + PIN, QUADLET + "Image=\n",
                     QUADLET.replace("[Container]", "[Service]"),
                     QUADLET.replace("Image=", "WrongKey="),
                     QUADLET + "PodmanArgs=--rootfs=/tmp/other\n",
                     QUADLET + "GlobalArgs=--root=/tmp/other\n",
                     QUADLET + "Rootfs=/tmp/other\n", QUADLET + "Environment=ONE=1\\\nTWO=2\n"):
            with self.subTest(text=text), self.assertRaises(binding.Refusal):
                binding.quadlet_image_ref(text)

    def test_source_command_uses_canonical_path_and_assertion_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            quadlet = Path(tmp) / "paperclip.container"
            quadlet.write_text(QUADLET)
            with patch.object(binding, "CANONICAL_QUADLET", quadlet):
                for args in (["source-ref"], ["source-ref", "--expected-ref", PIN]):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(binding.main(args), 0)
                    self.assertEqual(out.getvalue(), PIN + "\n")
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(binding.main(["source-ref", "--expected-ref", "localhost/other:tag"]), 2)
                self.assertEqual(out.getvalue(), "")

    def test_missing_source_has_no_default(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(binding, "CANONICAL_QUADLET", Path(tmp) / "absent"):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(binding.main(["source-ref"]), 2)


class ObservationTests(unittest.TestCase):
    def test_realistic_id_and_digest_domains_pass(self):
        binding.assert_running_image(PIN, CONTAINER, IMAGE)
        binding.assert_running_image(PIN, "sha256:" + CONTAINER, IMAGE)
        binding.assert_running_image(PIN, CONTAINER, json.dumps({"Id": IMAGE_ID, "Digest": DIGEST}))

    def test_config_sudoers_agreement_does_not_approve_wrong_running_digest(self):
        with self.assertRaises(binding.Refusal):
            binding.assert_running_image(PIN, CONTAINER.replace(DIGEST, OTHER_DIGEST), IMAGE)

    def test_local_image_id_is_not_a_manifest_digest(self):
        for container, image in (
            (CONTAINER.replace(IMAGE_ID, OTHER_ID), IMAGE),
            (CONTAINER, IMAGE.replace(DIGEST, "sha256:" + IMAGE_ID)),
            (CONTAINER.replace(DIGEST, "sha256:" + IMAGE_ID), IMAGE),
            (CONTAINER, IMAGE.replace(DIGEST, OTHER_DIGEST)),
        ):
            with self.subTest(container=container, image=image), self.assertRaises(binding.Refusal):
                binding.assert_running_image(PIN, container, image)

    def test_missing_malformed_and_stopped_observations_fail_closed(self):
        containers = ("", CONTAINER.replace("true", "false"), CONTAINER.replace("true", "1"),
                      CONTAINER.replace(DIGEST, ""), CONTAINER + CONTAINER,
                      CONTAINER.replace(IMAGE_ID, "short"), CONTAINER.replace("true", "True"))
        for container in containers:
            with self.subTest(container=container), self.assertRaises(binding.Refusal):
                binding.assert_running_image(PIN, container, IMAGE)
        for image in ("", "null", "[]", "{}", '{"Id": 42}',
                      json.dumps({"Id": IMAGE_ID}), json.dumps({"Digest": DIGEST})):
            with self.subTest(image=image), self.assertRaises(binding.Refusal):
                binding.assert_running_image(PIN, CONTAINER, image)

    def test_exact_read_only_argv_and_second_observation(self):
        with patch.object(binding, "capture", side_effect=[CONTAINER, IMAGE, CONTAINER]) as capture:
            binding.check_running(PIN)
        container = ["container", "inspect", "paperclip", "--format", binding.CONTAINER_FORMAT]
        self.assertEqual([call.args[0] for call in capture.call_args_list], [
            container, ["image", "inspect", PIN, "--format", "{{json .}}"], container,
        ])
        # Startup policy has no test-mode or environment switch that skips reads.
        self.assertEqual(len(capture.call_args_list), 3)

    def test_recreation_between_inspections_refuses(self):
        with patch.object(binding, "capture", side_effect=[CONTAINER, IMAGE, CONTAINER.replace(IMAGE_ID, OTHER_ID)]):
            with self.assertRaises(binding.Refusal):
                binding.check_running(PIN)

    def test_capture_has_fixed_sudo_target_timeout_and_sanitized_environment(self):
        with patch.object(binding.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, CONTAINER)) as run:
            self.assertEqual(binding.capture(["container", "inspect", "paperclip", "--format", binding.CONTAINER_FORMAT]), CONTAINER)
        # node7b1ff338 Docker port (case_81816394): root-bound docker, no user-runtime env.
        self.assertEqual(run.call_args.args[0][:7], [
            "/usr/bin/sudo", "-n", "-u", "root", "/usr/bin/docker", "container",
            "inspect",
        ])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_command_failures_never_echo_backend_details(self):
        canary = "synthetic-sensitive-backend-output"
        for error in (OSError(canary), subprocess.TimeoutExpired(canary, 15, output=canary),
                      subprocess.CalledProcessError(1, canary, output=canary, stderr=canary)):
            with self.subTest(error=type(error)), patch.object(binding.subprocess, "run", side_effect=error):
                with self.assertRaises(binding.Refusal) as caught:
                    binding.capture([])
                self.assertNotIn(canary, str(caught.exception))

    def test_check_cli_does_not_allow_test_mode_bypass_or_emit_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"paperclipImageRef": PIN, "testMode": True}))
            out, err = io.StringIO(), io.StringIO()
            with patch.object(binding, "capture", side_effect=[CONTAINER, IMAGE, CONTAINER]) as capture:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(binding.main(["check", "--config", str(config)]), 0)
                self.assertEqual(capture.call_count, 3)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "")
            with patch.object(binding, "capture", return_value=""), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(binding.main(["check", "--config", str(config)]), 2)


class IntegrationTests(unittest.TestCase):
    def test_upgrade_render_forces_disabled_and_preserves_other_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "old.json", Path(tmp) / "new.json"
            old = {"writesEnabled": True, "testMode": True, "paperclipImageRef": "old:tag", "pollSeconds": 12}
            source.write_text(json.dumps(old))
            subprocess.run(["bash", "-ceu", 'source "$1"; render_disabled_config "$2" "$3" "$4"',
                            "bash", str(ROOT / "host-ops/install-lib.sh"), str(source), str(destination), PIN], check=True)
            rendered = json.loads(destination.read_text())
            self.assertIs(rendered["writesEnabled"], False)
            self.assertIs(rendered["testMode"], False)
            self.assertEqual(rendered["paperclipImageRef"], PIN)
            self.assertEqual(rendered["pollSeconds"], 12)
            self.assertEqual(json.loads(source.read_text()), old)

    def test_bad_config_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "old.json", Path(tmp) / "new.json"
            for text in ("[]", "null", "invalid json"):
                source.write_text(text)
                result = subprocess.run(["bash", "-ceu", 'source "$1"; render_disabled_config "$2" "$3" "$4"',
                                         "bash", str(ROOT / "host-ops/install-lib.sh"), str(source), str(destination), PIN],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(destination.exists())

    def test_startup_binding_runs_after_mirror_cmp_without_root_prefix(self):
        unit = (ROOT / "host-ops/host-ops-broker.service").read_text()
        lines = [line for line in unit.splitlines() if line.startswith("ExecStartPre=")]
        self.assertEqual(lines, [
            "ExecStartPre=+/usr/bin/cmp --silent /etc/sudoers.d/paperclip-host-ops-broker /etc/paperclip-host-ops/sudoers.rendered",
            "ExecStartPre=/usr/bin/python3 /usr/local/libexec/paperclip-host-ops/image_binding.py check --config /etc/paperclip-host-ops/config.json",
            "ExecStartPre=/usr/local/libexec/paperclip-host-ops/broker.py acceptance-check --config /etc/paperclip-host-ops/config.json",
        ])
        # node7b1ff338 Docker port (case_81816394): the container read is exercised through
        # image_binding.py's root-bound Docker ENGINE_PREFIX, granted as the
        # PAPERCLIP_CONTAINER_READ alias. Full container JSON stays ungranted
        # (it can expose env secrets). The E3 CLIProxy read keeps its --user
        # form, so XDG_RUNTIME_DIR appears exactly once (that grant only).
        sudoers = (ROOT / "host-ops/host-ops-broker.sudoers").read_text()
        self.assertIn("/usr/bin/docker image inspect ", sudoers)
        self.assertIn(
            "/usr/bin/docker container inspect paperclip --format "
            + binding.CONTAINER_FORMAT,
            sudoers,
        )
        self.assertNotIn("container inspect paperclip --format {{json .}}", sudoers)
        self.assertNotIn("podman", sudoers)
        self.assertEqual(sudoers.count("XDG_RUNTIME_DIR"), 1)

    def test_installer_checks_observed_image_before_account_or_file_mutation(self):
        install = (ROOT / "host-ops/install.sh").read_text()
        check = 'python3 "$SOURCE_DIR/image_binding.py" check --config "$CONFIG_TMP"'
        self.assertIn('PAPERCLIP_IMAGE_REF=$(python3 "$SOURCE_DIR/image_binding.py" source-ref', install)
        transaction = 'python3 -B "$SOURCE_DIR/install_transaction.py" apply'
        self.assertLess(install.index(check), install.index(transaction))
        self.assertLess(install.index('validate_response_signing_key "$PRIVATE_KEY"'), install.index(transaction))
        self.assertIn('[[ "$identity_state" == valid ]] || refuse', install)
        self.assertNotIn('groupadd --system', install)
        self.assertNotIn('install -d -o root', install)
        self.assertIn('broker.py image_binding.py install_state.py install_transaction.py', install)


if __name__ == "__main__":
    unittest.main()
