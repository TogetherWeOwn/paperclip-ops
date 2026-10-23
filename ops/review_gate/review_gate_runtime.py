"""Neutral runtime interface for source review, not deployed configuration.

All values are placeholders. Deployment must inject the unchanged private policy,
identities and transports using its existing authorized configuration mechanism.
Importing this module performs no command, network, secret, or filesystem read.
"""
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get('REVIEW_GATE_ROOT', '/configure/review-gate'))
COMPANY_ID = os.environ.get('REVIEW_GATE_COMPANY_ID', '00000000-0000-0000-0000-000000000001')
ENDPOINT_ID = os.environ.get('REVIEW_GATE_ENDPOINT_ID', '00000000-0000-0000-0000-000000000002')
BATCH_EXECUTOR_ID = os.environ.get('REVIEW_GATE_BATCH_EXECUTOR_ID', '00000000-0000-0000-0000-000000000003')
REVIEWER_ID = os.environ.get('REVIEW_GATE_REVIEWER_ID', '00000000-0000-0000-0000-000000000004')
FOCUS_REPOS = tuple(json.loads(os.environ.get('REVIEW_GATE_FOCUS_REPOS', '["primary-service-a", "primary-service-b", "tertiary-service"]')))
REPO_RANK = tuple(tuple(pair) for pair in json.loads(os.environ.get('REVIEW_GATE_REPO_RANK', '[["primary-", 1], ["secondary", 2], ["tertiary", 3], ["last-service", 5]]')))
DEFAULT_OWNER = os.environ.get('REVIEW_GATE_DEFAULT_OWNER', 'example-org')
PROJECT_LOOKUP_ISSUE = os.environ.get('REVIEW_GATE_PROJECT_LOOKUP_ISSUE', 'EXAMPLE-1')
REVIEW_REQUEST_TEXT = os.environ.get('REVIEW_GATE_REQUEST_TEXT', '@example-reviewer review this PR')
API_BASE = os.environ.get('REVIEW_GATE_API_BASE', 'https://control-plane.invalid').rstrip('/')
API_CURL_CONFIG = Path(os.environ.get('REVIEW_GATE_API_CURL_CONFIG', str(ROOT / 'config/api.curl')))
DB_COMMAND = json.loads(os.environ.get('REVIEW_GATE_DB_COMMAND', '["false"]'))
TOKEN_COMMAND = json.loads(os.environ.get('REVIEW_GATE_TOKEN_COMMAND', '["false"]'))
GATE_PAUSE = Path(os.environ.get('REVIEW_GATE_PAUSE', str(ROOT / 'stops/review-gate.PAUSE')))
REVIEW_HOLD = Path(os.environ.get('REVIEW_GATE_HOLD', str(ROOT / 'stops/review-hold.ACTIVE')))
GATE_STATE = Path(os.environ.get('REVIEW_GATE_STATE', str(ROOT / 'state/review-gate.json')))
LOG = Path(os.environ.get('REVIEW_GATE_LOG', str(ROOT / 'logs/review-gate.log')))
FINDINGS_CONFIG = Path(os.environ.get('REVIEW_GATE_FINDINGS_CONFIG', str(ROOT / 'config/findings-contract.json')))
CONTEXT_DIR = Path(os.environ.get('REVIEW_GATE_CONTEXT_DIR', str(ROOT / 'state/review-context')))
FINDINGS_FLAGS = Path(os.environ.get('REVIEW_GATE_FINDINGS_FLAGS', str(ROOT / 'state/review-findings-flags.jsonl')))
FIRSTPASS_CONFIG = Path(os.environ.get('REVIEW_GATE_FIRSTPASS_CONFIG', str(ROOT / 'config/firstpass-review.json')))
FIRSTPASS_STATE = Path(os.environ.get('REVIEW_GATE_FIRSTPASS_STATE', str(ROOT / 'state/firstpass-review.json')))
FIRSTPASS_PAUSE = Path(os.environ.get('REVIEW_GATE_FIRSTPASS_PAUSE', str(ROOT / 'stops/firstpass-review.PAUSE')))
PROVIDER_USAGE = Path(os.environ.get('REVIEW_GATE_PROVIDER_USAGE', str(ROOT / 'metrics/provider-usage.json')))
HOST_DISK_ROOT = os.environ.get('REVIEW_GATE_HOST_DISK_ROOT', '/configure/guarded-volume')
