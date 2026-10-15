#!/usr/bin/env python3
"""Fail-closed install/startup image binding; never prints engine output.

Docker inspect field contracts (node7b1ff338 is rootful Docker, case_81816394):
https://docs.docker.com/reference/cli/docker/container/inspect/
https://docs.docker.com/reference/cli/docker/image/inspect/
An image ID is not a registry manifest digest. Compare each in its own domain.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


# node7b1ff338 Docker port (case_81816394): the retired rootless-Podman host read the pin from
# the canonical Quadlet Image=. On node7b1ff338 the canonical pin is the reference the
# running Docker container actually runs (`docker inspect paperclip
# --format '{{.Config.Image}}'`), asserted by install.sh --paperclip-image-ref.
CANONICAL_QUADLET = Path("/home/ubuntu/.config/containers/systemd/paperclip.container")
PIN_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,159}@sha256:[0-9a-f]{64}")
ID_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
CONTAINER_FORMAT = "{{.Image}}|{{.ImageDigest}}|{{.State.Running}}"
ENGINE_PREFIX = [
    "/usr/bin/sudo", "-n", "-u", "root", "/usr/bin/docker",
]
# Retired Podman prefix kept as a name alias so older callers fail loudly at
# import rather than silently running the wrong engine. Remove once no
# in-tree reference remains.
PODMAN_PREFIX = ENGINE_PREFIX


class Refusal(ValueError):
    pass


def require_pin(image_ref):
    if not isinstance(image_ref, str) or not PIN_RE.fullmatch(image_ref):
        raise Refusal("immutable registry @sha256 image reference required")
    return image_ref


def quadlet_image_ref(text):
    """Accept only one literal Image= in [Container], not systemd expansions.

    This deliberately rejects continuation/escaping, indirect .image units and
    extra Podman arguments rather than pretending to implement the generator.
    Host-render verification is still required for a new carrier.
    On node7b1ff338 (case_81816394) there is no Quadlet file; install.sh passes the Docker
    pin via --expected-ref and this parser is only the assertion grammar.
    """
    section = None
    refs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "\\" in line:
            raise Refusal("Quadlet continuations and escapes require separate review")
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            raise Refusal("malformed Quadlet entry")
        key, value = (part.strip() for part in line.split("=", 1))
        if key == "Image":
            if section != "Container":
                raise Refusal("Image must be in [Container]")
            refs.append(value)
        if section == "Container" and key in {"Rootfs", "PodmanArgs", "GlobalArgs"}:
            raise Refusal("alternate runtime arguments require separate review")
    if len(refs) != 1:
        raise Refusal("canonical Quadlet must contain exactly one Image entry")
    return require_pin(refs[0])


def assert_running_image(image_ref, container_output, image_output):
    """Validate captured observations; no fixture or test-mode bypass here."""
    pin = require_pin(image_ref).split("@", 1)[1]
    fields = container_output.strip().split("|")
    if len(fields) != 3:
        raise Refusal("invalid running-container observation")
    running_id, running_digest, running = fields
    if running != "true":
        raise Refusal("Paperclip container is not running")
    try:
        image = json.loads(image_output)
    except (ValueError, TypeError):
        raise Refusal("invalid image observation") from None
    if not isinstance(image, dict):
        raise Refusal("image observation must be an object")
    configured_id = image.get("Id")
    if not isinstance(configured_id, str):
        raise Refusal("inspected image ID is missing")
    observed = ID_RE.fullmatch(running_id)
    configured = ID_RE.fullmatch(configured_id)
    if observed is None or configured is None or observed[1] != configured[1]:
        raise Refusal("running image ID differs from inspected pinned image")
    if running_digest != pin or image.get("Digest") != pin:
        raise Refusal("observed image digest differs from install-time pin")


def capture(args):
    try:
        result = subprocess.run(
            ENGINE_PREFIX + args, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=15, text=True,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # Inspection failures may contain sensitive runtime context. Never echo it.
        raise Refusal("fixed Docker inspection failed or timed out") from None
    if len(result.stdout) > 1024 * 1024:
        raise Refusal("image observation exceeds size bound")
    return result.stdout


def check_running(image_ref):
    require_pin(image_ref)
    container = capture(["container", "inspect", "paperclip", "--format", CONTAINER_FORMAT])
    image = capture(["image", "inspect", image_ref, "--format", "{{json .}}"])
    assert_running_image(image_ref, container, image)
    # Reobserve after image inspection so a recreation during the two reads does
    # not pass as the original container. This is not a lock against later rolls.
    assert_running_image(
        image_ref,
        capture(["container", "inspect", "paperclip", "--format", CONTAINER_FORMAT]),
        image,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    source = sub.add_parser("source-ref")
    source.add_argument("--expected-ref")
    check = sub.add_parser("check")
    check.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "source-ref":
            # node7b1ff338 Docker port (case_81816394): no Quadlet file exists on the host.
            # --expected-ref (install.sh --paperclip-image-ref, read by the
            # operator from `docker inspect paperclip`) is the canonical pin;
            # it must still pass the immutable @sha256 grammar. The Quadlet
            # parser above remains the assertion grammar for that value.
            if args.expected_ref is None:
                try:
                    image_ref = quadlet_image_ref(CANONICAL_QUADLET.read_text())
                except OSError:
                    raise Refusal(
                        "no canonical image source: pass --expected-ref "
                        "(node7b1ff338 has no Quadlet file; read it from "
                        "`docker inspect paperclip --format '{{.Config.Image}}'`)"
                    ) from None
            else:
                image_ref = quadlet_image_ref(
                    "[Container]\nImage=" + args.expected_ref + "\n"
                )
            print(image_ref)
        else:
            config = json.loads(args.config.read_text())
            if not isinstance(config, dict):
                raise Refusal("config must be an object")
            check_running(config.get("paperclipImageRef"))
    except (Refusal, OSError, ValueError) as exc:
        # Only locally authored Refusal messages are safe to publish.
        message = str(exc) if isinstance(exc, Refusal) else "image binding input is unreadable or invalid"
        print("REFUSED: " + message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
