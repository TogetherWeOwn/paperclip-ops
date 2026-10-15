#!/usr/bin/env python3
"""Execute the real transport metadata queries in the guarded CI org_fixture DB.

Uses a private, randomly named schema; never resets the shared fixture tables.
Only the agent API is stubbed. No credentials or model calls are involved.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import uuid

from test_provisioned_transport import AGENT, CLI_STUB, COMPANY, CONFIG, ROOT, SECRET

RUN = "00000000-0000-4000-8000-000000000110"
ENV = {
    "ANTHROPIC_BASE_URL": CONFIG["baseUrl"],
    "ANTHROPIC_AUTH_TOKEN": {"type": "secret_ref", "secretId": SECRET, "version": "latest"},
    "PAPERCLIP_ASSIGNED_MODEL": CONFIG["assignedModel"],
    "CLAUDE_CODE_SUBAGENT_MODEL": CONFIG["assignedModel"],
    "ANTHROPIC_DEFAULT_OPUS_MODEL": CONFIG["assignedModel"],
    "ANTHROPIC_DEFAULT_SONNET_MODEL": CONFIG["assignedModel"],
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": CONFIG["smallFastModel"],
    "ANTHROPIC_SMALL_FAST_MODEL": CONFIG["smallFastModel"],
    "API_TIMEOUT_MS": CONFIG["apiTimeoutMs"],
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": CONFIG["maxContextTokens"],
}
# Use the exact marker from the checksum-pinned disposable fixture producer.
# A sanitized copy of its old literal no longer matches the restored fixture.
_fixture_reset = (ROOT / "test/fixtures/orgdb/reset.sh").read_text()
_marker = re.search(r"(?m)^SENTINEL_VALUE=([a-zA-Z0-9_:-]+)$", _fixture_reset)
if not _marker:
    raise RuntimeError("REFUSED: disposable fixture marker is absent or not a literal")
FIXTURE_SENTINEL = _marker.group(1)
GUARD = """
DO $guard$
DECLARE marker_value text;
BEGIN
  IF current_database() <> 'org_fixture' THEN
    RAISE EXCEPTION 'transport tests require org_fixture';
  END IF;
  SELECT marker INTO STRICT marker_value FROM public.org_fixture_sentinel;
  IF marker_value IS DISTINCT FROM '__FIXTURE_SENTINEL__' THEN
    RAISE EXCEPTION 'transport tests require the org_fixture sentinel';
  END IF;
END
$guard$;
""".replace("__FIXTURE_SENTINEL__", FIXTURE_SENTINEL)


class TransportDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("PGDATABASE") != "org_fixture":
            raise RuntimeError("REFUSED: set PGDATABASE=org_fixture and initialize the disposable fixture")
        cls.env = os.environ.copy()
        for key in ("DATABASE_URL", "BASH_ENV", "PGSERVICE", "PGSERVICEFILE"):
            cls.env.pop(key, None)
        cls.schema = "transport_test_" + uuid.uuid4().hex
        cls.env["PGOPTIONS"] = "-c search_path=" + cls.schema
        cls.sql("CREATE SCHEMA " + cls.schema + ";")
        cls.addClassCleanup(lambda: cls.sql("DROP SCHEMA " + cls.schema + " CASCADE;"))
        # Minimal projections of the installed schema, with the production types.
        # This tests SQL behavior, not schema-drift coverage or the live service.
        cls.sql("""
CREATE TABLE agents (id uuid, company_id uuid, status text, adapter_type text,
  adapter_config jsonb, created_at timestamptz, updated_at timestamptz);
CREATE TABLE company_secrets (id uuid, company_id uuid, key text, status text, updated_at timestamptz);
CREATE TABLE company_secret_bindings (company_id uuid, secret_id uuid, target_type text,
  target_id text, config_path text, version_selector text, required boolean,
  created_at timestamptz, updated_at timestamptz);
CREATE TABLE agent_config_revisions (company_id uuid, agent_id uuid, created_at timestamptz);
CREATE TABLE heartbeat_runs (id uuid, company_id uuid, agent_id uuid, status text,
  created_at timestamptz, started_at timestamptz, finished_at timestamptz,
  usage_json jsonb, error text, error_code text, exit_code integer);
""")

    @classmethod
    def sql(cls, body):
        # Validate the server and sentinel in the same transaction as every write.
        result = subprocess.run(["psql", "-X", "-Atq", "-v", "ON_ERROR_STOP=1"],
                                input="BEGIN;\n" + GUARD + body + "\nCOMMIT;",
                                env=cls.env, capture_output=True, text=True)
        if result.returncode:
            raise AssertionError("fixture SQL failed: " + result.stderr)
        return result.stdout

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
                                               or os.environ.get("RUNNER_TEMP"))
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        cli = self.work / "paperclipai"
        cli.write_text(CLI_STUB)
        cli.chmod(0o700)
        payload = {"adapterType": "claude_local", "adapterConfig": {"env": ENV}}
        (self.work / "payload.json").write_text(json.dumps(payload))
        self.run_env = dict(self.env, PAPERCLIP_CLI=str(cli), PAPERCLIP_SQL_BACKEND="psql",
                            COMPANY_ID=COMPANY, TEST_AGENT=AGENT,
                            PROVISIONER_CLAUDE_TRANSPORT_JSON=json.dumps(CONFIG),
                            CALL_LOG=str(self.work / "calls.jsonl"), PAYLOAD=str(self.work / "payload.json"),
                            READBACK="ok")
        self.seed()

    def seed(self):
        config = json.dumps({"env": ENV}).replace("'", "''")
        self.sql(f"""
TRUNCATE agents, company_secrets, company_secret_bindings, agent_config_revisions, heartbeat_runs;
INSERT INTO agents VALUES ('{AGENT}', '{COMPANY}', 'idle', 'claude_local', '{config}',
  now() - interval '10 minutes', now());
INSERT INTO company_secrets VALUES ('{SECRET}', '{COMPANY}', 'cliproxy_agent_api_key', 'active',
  now() - interval '10 minutes');
INSERT INTO company_secret_bindings VALUES ('{COMPANY}', '{SECRET}', 'agent', '{AGENT}',
  'env.ANTHROPIC_AUTH_TOKEN', 'latest', true, now() - interval '10 minutes', now() - interval '10 minutes');
INSERT INTO agent_config_revisions VALUES ('{COMPANY}', '{AGENT}', now() - interval '8 minutes');
INSERT INTO heartbeat_runs VALUES ('{RUN}', '{COMPANY}', '{AGENT}', 'succeeded',
  now() - interval '3 minutes', now() - interval '2 minutes', now() - interval '1 minute',
  '{{"inputTokens":12,"outputTokens":3}}', null, null, 0);
""")

    def verify(self):
        return subprocess.run(["bash", str(ROOT / "org_provisioner.sh"), "verify-transport",
                               "--target", AGENT, "--smoke-run", RUN],
                              env=self.run_env, capture_output=True, text=True)

    def assert_ready(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TRANSPORT_READY " + AGENT, result.stdout)

    def test_real_sql_positive_control_and_birth_without_revision(self):
        self.assert_ready()
        self.sql("DELETE FROM agent_config_revisions;")
        self.assert_ready()
        # Ordinary heartbeat/permission writes may touch this clock after a run.
        self.sql("UPDATE agents SET updated_at = now() + interval '1 minute';")
        self.assert_ready()

    def test_real_sql_rejects_each_bad_run_or_stale_configuration(self):
        changes = {
            "zero usage": "UPDATE heartbeat_runs SET usage_json = '{\"inputTokens\":0,\"outputTokens\":0}';",
            "missing usage": "UPDATE heartbeat_runs SET usage_json = null;",
            "string usage": "UPDATE heartbeat_runs SET usage_json = '{\"inputTokens\":\"12\",\"outputTokens\":3}';",
            "invalid numeric": "UPDATE heartbeat_runs SET usage_json = '{\"inputTokens\":\"oops\",\"outputTokens\":3}';",
            "negative usage": "UPDATE heartbeat_runs SET usage_json = '{\"inputTokens\":-1,\"outputTokens\":3}';",
            "queued": "UPDATE heartbeat_runs SET status = 'queued';",
            "failed": "UPDATE heartbeat_runs SET status = 'failed';",
            "error code": "UPDATE heartbeat_runs SET error_code = 'connection_refused';",
            "error text": "UPDATE heartbeat_runs SET error = 'connection refused';",
            "bad exit": "UPDATE heartbeat_runs SET exit_code = 1;",
            "wrong company": f"UPDATE heartbeat_runs SET company_id = '{AGENT}';",
            "wrong agent": f"UPDATE heartbeat_runs SET agent_id = '{COMPANY}';",
            "wrong run": f"UPDATE heartbeat_runs SET id = '{SECRET}';",
            "missing start": "UPDATE heartbeat_runs SET started_at = null;",
            "missing finish": "UPDATE heartbeat_runs SET finished_at = null;",
            "future finish": "UPDATE heartbeat_runs SET finished_at = now() + interval '1 minute';",
            "queued before config": "UPDATE heartbeat_runs SET created_at = now() - interval '9 minutes';",
            "started before config": "UPDATE heartbeat_runs SET started_at = now() - interval '9 minutes';",
            "new revision": "UPDATE agent_config_revisions SET created_at = now();",
            "new binding": "UPDATE company_secret_bindings SET updated_at = now();",
            "secret rotation": "UPDATE company_secrets SET updated_at = now();",
            "missing binding": "DELETE FROM company_secret_bindings;",
            "optional binding": "UPDATE company_secret_bindings SET required = false;",
            "disabled secret": "UPDATE company_secrets SET status = 'disabled';",
            "wrong binding company": f"UPDATE company_secret_bindings SET company_id = '{AGENT}';",
            "wrong binding secret": f"UPDATE company_secret_bindings SET secret_id = '{AGENT}';",
            "wrong binding path": "UPDATE company_secret_bindings SET config_path = 'env.OTHER';",
            "wrong binding version": "UPDATE company_secret_bindings SET version_selector = '1';",
            "terminated": "UPDATE agents SET status = 'terminated';",
            "stored env changed after API": "UPDATE agents SET adapter_config = '{\"env\":{}}';",
        }
        for label, sql in changes.items():
            with self.subTest(case=label):
                self.seed()
                self.assert_ready()
                self.sql(sql)
                result = self.verify()
                self.assertNotEqual(result.returncode, 0, label)
                self.assertNotIn("TRANSPORT_READY", result.stdout)
                self.assertIn("REFUSED:", result.stderr)


if __name__ == "__main__":
    unittest.main()
