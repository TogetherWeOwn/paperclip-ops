#!/usr/bin/env python3
"""Print the one-click GitHub App registration URL for app-manifest.json.

The URL-parameter flow needs no hosted page and never handles a credential: the owner
opens the link, GitHub pre-fills the form from the query string, and the private key is
generated later on the App's own settings page. Stdlib only; no network.

    registration_url.py [--org TogetherWeOwn]
"""
import argparse
import json
import os
import sys
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))


def build(org, manifest):
    q = [("name", manifest["name"]), ("description", manifest["description"]),
         ("url", manifest["url"]), ("public", str(manifest["public"]).lower()),
         ("webhook_active", "false")]
    q += sorted(manifest["default_permissions"].items())
    return f"https://github.com/organizations/{org}/settings/apps/new?" + urlencode(q)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="TogetherWeOwn")
    ap.add_argument("--manifest", default=os.path.join(HERE, "app-manifest.json"))
    a = ap.parse_args()
    print(build(a.org, json.load(open(a.manifest))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
