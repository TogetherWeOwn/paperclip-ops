#!/usr/bin/env python3
"""Acceptance contract for dispatch sweep failure visibility; tests only.

Run: python3 -B -m unittest -v test_dispatch_sweep_failure_visibility
Red-before: DISPATCH_TEST_BASELINE_REF=<commit> python3 -B -m unittest -v \
    test_dispatch_sweep_failure_visibility

The registered job callback is exercised, not a copied implementation. A Node
VM links the actual worker, policy and reporting sources; only the SDK bootstrap
and host services are in-memory doubles. The selection and summary exports are
wrapped solely to inject stage failures. No SDK installation, live host, credentials,
network, timers, wakes or filesystem writes are available inside the VM.

The optional baseline ref reads ONLY the tracked worker from git without
checking out or overwriting any file. Dependency modules are ignored toolkit
inputs: restore them with bash scripts/sync-toolkit.sh if absent. The same
restored dependencies are used for baseline and candidate. Missing sources fail,
never skip.
This file belongs to the acceptance-test author; implementation changes must not
weaken it or the existing dispatch suite.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent
DIST = "plugins/dispatch/dist"
STAGES = (
    "config-read", "issues-read", "selection", "summary", "metrics-write", "state-read",
    "activity-write", "state-write",
)
MARKER = "PRIVATE_FIXTURE_VALUE_8e53"

NODE_FIXTURE = r"""
import vm from 'node:vm';
import { inspect } from 'node:util';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
const context = vm.createContext({});
const RealmError = vm.runInContext('Error', context);
const marker = request.marker;
let codeGetterReads = 0;
const errorSpec = request.error ?? {};
const error = vm.runInContext(errorSpec.type === 'TypeError'
  ? 'new TypeError()' : 'new Error()', context);
error.message = `${errorSpec.detail ?? marker}: capability denied, EIO, raw error message`;
error.stack = `${marker}: private stack`;
error.cause = { message: `${marker}: nested cause`, code: -32001 };
error.config = { privateValue: `${marker}: config` };
if (errorSpec.type === 'throwing-code') {
  Object.defineProperty(error, 'code', { get() {
    codeGetterReads += 1;
    throw new RealmError(`${marker}: code getter`);
  }});
} else if (errorSpec.type === 'object-code') {
  error.code = { value: marker, toString() { throw new RealmError(marker); } };
} else if ('code' in errorSpec) {
  error.code = errorSpec.code;
}
const thrown = errorSpec.type === 'primitive' ? `${marker}: primitive` : error;
const events = [];
const logs = [];
const metrics = [];
const activity = [];
const states = new Map();
let callback;
let currentCompany;
let releaseHealthy;
let reachedHealthy;
const healthyReached = new Promise(resolve => { reachedHealthy = resolve; });
const healthyRelease = new Promise(resolve => { releaseHealthy = resolve; });
const shouldFail = companyId => request.failCompanies?.includes(companyId);
function step(stage, companyId) {
  events.push({ stage, companyId, phase: 'start' });
  if (stage === request.failStage && shouldFail(companyId)) throw thrown;
}
const modules = new Map();
const sdk = new vm.SyntheticModule(['definePlugin', 'runWorker'], function () {
  this.setExport('definePlugin', definition => Object.freeze({ definition }));
  this.setExport('runWorker', () => {});
}, { context });
modules.set('@paperclipai/plugin-sdk', sdk);
for (const [name, source] of Object.entries(request.sources)) {
  modules.set(name, new vm.SourceTextModule(source, { context, identifier: name }));
}
// Link real selection first, then replace only its export boundary for faults.
const policy = modules.get('./selection.js');
await policy.link(() => { throw new Error('unexpected policy dependency'); });
await policy.evaluate();
const selection = new vm.SyntheticModule(Object.keys(policy.namespace), function () {
  for (const name of Object.keys(policy.namespace)) {
    this.setExport(name, name === 'selectDispatch' ? (...args) => {
      step('selection', currentCompany);
      return policy.namespace.selectDispatch(...args);
    } : policy.namespace[name]);
  }
}, { context });
modules.set('./selection.js', selection);
const reporting = modules.get('./reporting.js');
await reporting.link(specifier => {
  if (specifier !== './selection.js') throw new Error('unexpected reporting dependency');
  return selection;
});
await reporting.evaluate();
modules.set('./reporting.js', new vm.SyntheticModule(Object.keys(reporting.namespace), function () {
  for (const name of Object.keys(reporting.namespace)) {
    this.setExport(name, name === 'summariseFiring' ? (...args) => {
      step('summary', args[0]);
      return reporting.namespace.summariseFiring(...args);
    } : reporting.namespace[name]);
  }
}, { context }));
const worker = modules.get('./worker.js');
await worker.link(specifier => {
  if (!modules.has(specifier)) throw new Error(`unexpected dependency: ${specifier}`);
  return modules.get(specifier);
});
await worker.evaluate();
const ctx = {
  jobs: { register(key, handler) {
    if (key !== 'dispatch-sweep' || callback) throw new Error('unexpected registration');
    callback = handler;
  } },
  companies: { async list() {
    events.push({ stage: 'companies-read' });
    return (request.companies ?? []).map(id => ({ id }));
  } },
  config: { async get(companyId) {
    currentCompany = companyId;
    step('config-read', companyId);
    return { idleMinutes: 1, ...request.config };
  } },
  issues: {
    async list({ companyId }) {
      step('issues-read', companyId);
      return request.withIssue ? [{
        id: `issue-${companyId}`, identifier: 'EX-1', companyId,
        projectId: 'project-a', status: 'in_progress', priority: 'high',
        assigneeAgentId: 'assignee-a', unblockDescriptor: null,
        createdAt: '2026-01-01T00:00:00.000Z',
        updatedAt: '2026-01-01T00:00:00.000Z',
      }] : [];
    },
    summaries: { async getOrchestration({ companyId }) {
      events.push({ stage: 'orchestration-read', companyId });
      return { relations: {}, runs: [], invocationBlocks: [] };
    } },
    async requestWakeup(issueId, companyId) {
      events.push({ stage: 'wake', companyId, issueId });
      return { queued: true, runId: 'fixture-wake' };
    },
  },
  agents: { async list() { throw new Error('fixture has no routing gap'); } },
  metrics: { async write(name, value, tags) {
    step('metrics-write', tags.companyId);
    metrics.push({ name, value, tags });
  } },
  activity: { async log(entry) {
    step('activity-write', entry.companyId);
    activity.push(entry);
  } },
  state: {
    async get(key) {
      step('state-read', key.scopeId);
      if (request.stateFault && shouldFail(key.scopeId)) {
        // Return successfully; the real hasStateChanged must encounter the fault
        // outside atStage, not while awaiting this host read.
        if (request.stateFault === 'non-iterable') return { pickedIssueIds: 7 };
        if (request.stateFault !== 'throwing-iterator') throw new Error('unknown state fault');
        return { pickedIssueIds: { get [Symbol.iterator]() {
          events.push({ stage: 'state-compare', companyId: key.scopeId });
          throw thrown;
        } } };
      }
      return states.get(key.scopeId) ?? null;
    },
    async set(key, summary) {
      step('state-write', key.scopeId);
      if (request.deferHealthy && key.scopeId === 'healthy') {
        reachedHealthy();
        await healthyRelease;
      }
      states.set(key.scopeId, JSON.parse(JSON.stringify(summary)));
      events.push({ stage: 'state-write', companyId: key.scopeId, phase: 'finish' });
    },
  },
  logger: Object.fromEntries(['error', 'warn', 'info', 'debug'].map(level => [level,
    (message, fields) => logs.push({ level, message, fields,
      inspected: inspect([message, fields], { depth: 10, getters: false }) }),
  ])),
};
await worker.namespace.plugin.definition.setup(ctx);
if (typeof callback !== 'function') throw new Error('dispatch callback not registered');
const outcomes = [];
let pendingAtHealthy = null;
for (let run = 0; run < (request.repeats ?? 1); run++) {
  let status = 'pending';
  const outcome = callback({
    jobKey: 'dispatch-sweep', runId: `fixture-${run}`, trigger: 'schedule',
    scheduledAt: '2026-01-02T00:00:00.000Z',
  }).then(() => {
    status = 'fulfilled';
    events.push({ stage: 'settled', status });
    return { status };
  }, failure => {
    status = 'rejected';
    events.push({ stage: 'settled', status });
    return { status, isError: failure instanceof RealmError,
      message: failure?.message, code: failure?.code,
      failedCompanies: failure?.failedCompanies,
      ownProperties: Object.getOwnPropertyNames(failure).sort(),
      inspected: inspect(failure, { depth: 10, getters: false }) };
  });
  if (request.deferHealthy) {
    const first = await Promise.race([healthyReached.then(() => 'healthy'),
      outcome.then(() => 'settled')]);
    pendingAtHealthy = first === 'healthy' && status === 'pending';
    releaseHealthy();
  }
  outcomes.push(await outcome);
}
process.stdout.write(JSON.stringify({ outcomes, events, logs, metrics, activity,
  states: Object.fromEntries(states), pendingAtHealthy, codeGetterReads }));
"""


class DispatchSweepFailureVisibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {}
        ref = os.environ.get("DISPATCH_TEST_BASELINE_REF")
        for name in ("worker.js", "selection.js", "reporting.js", "manifest.js"):
            path = f"{DIST}/{name}"
            if ref and name == "worker.js":
                result = subprocess.run(
                    ["git", "show", f"{ref}:{path}"], cwd=ROOT,
                    text=True, capture_output=True, timeout=10, check=True,
                )
                cls.sources[f"./{name}"] = result.stdout
            else:
                cls.sources[f"./{name}"] = (ROOT / path).read_text()

    def probe(self, **scenario):
        request = {"sources": self.sources, "marker": MARKER, **scenario}
        # Do not pass any credentials or shell-startup hooks to the child.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        result = subprocess.run(
            ["node", "--experimental-vm-modules", "--input-type=module", "-e", NODE_FIXTURE],
            cwd=ROOT, input=json.dumps(request), text=True, capture_output=True,
            timeout=10, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_aggregate_rejected(self, result, failed_companies=1):
        outcome = result["outcomes"][-1]
        self.assertEqual(outcome["status"], "rejected", "fatal sweep falsely fulfilled")
        self.assertTrue(outcome["isError"], "aggregate must be an Error")
        self.assertEqual(outcome["message"], "dispatch-sweep-failed")
        self.assertEqual(outcome["code"], "dispatch-sweep-failed")
        self.assertIs(type(outcome.get("failedCompanies")), int)
        self.assertEqual(outcome["failedCompanies"], failed_companies)
        self.assertEqual(outcome["ownProperties"], ["code", "failedCompanies", "message", "stack"])
        self.assertNotIn(MARKER, outcome["inspected"])
        self.assertNotIn(MARKER, json.dumps(outcome))

    def company_failures(self, result):
        return [log for log in result["logs"] if log["level"] == "error"]

    def assert_diagnostic(self, result, stage, family, error_code):
        failures = self.company_failures(result)
        self.assertEqual(len(failures), 1, failures)
        self.assertEqual(failures[0]["message"], "dispatch: company sweep failed")
        self.assertNotIn(MARKER, failures[0]["inspected"])
        fields = failures[0]["fields"]
        self.assertEqual(set(fields), {"companyId", "code", "stage", "family", "errorCode"})
        self.assertEqual(fields.get("companyId"), "bad")
        self.assertEqual(fields.get("code"), "dispatch-company-failed")
        self.assertEqual(fields.get("stage"), stage)
        self.assertEqual(fields.get("family"), family)
        self.assertIsInstance(fields.get("errorCode"), str)
        self.assertTrue(fields["errorCode"])
        self.assertEqual(fields["errorCode"], error_code)
        self.assertNotIn(MARKER, json.dumps(fields))
        self.assert_aggregate_rejected(result)
        return fields

    def test_first_fatal_company_does_not_hide_healthy_work_or_reject_early(self):
        result = self.probe(
            companies=["bad", "healthy"], failCompanies=["bad"],
            failStage="config-read", withIssue=True, deferHealthy=True,
        )
        self.assertTrue(result["pendingAtHealthy"], "callback settled before healthy company finished")
        self.assertIn("healthy", result["states"])
        self.assertNotIn("bad", result["states"])
        self.assertEqual(result["states"]["healthy"]["pickedIssueIds"], ["issue-healthy"])
        self.assertEqual([row["companyId"] for row in result["activity"]], ["healthy"])
        self.assertTrue(result["metrics"])
        self.assertEqual({row["tags"]["companyId"] for row in result["metrics"]}, {"healthy"})
        self.assertEqual(result["events"][-2], {
            "stage": "state-write", "companyId": "healthy", "phase": "finish",
        })
        self.assert_aggregate_rejected(result)

    def test_all_companies_fail_and_callback_rejects_after_attempting_every_company(self):
        result = self.probe(
            companies=["bad", "also-bad"], failCompanies=["bad", "also-bad"],
            failStage="config-read",
        )
        attempts = [row["companyId"] for row in result["events"] if row["stage"] == "config-read"]
        self.assertEqual(attempts, ["bad", "also-bad"])
        self.assertEqual(len(self.company_failures(result)), 2)
        self.assertEqual(result["metrics"], [])
        self.assertEqual(result["activity"], [])
        self.assertEqual(result["states"], {})
        self.assert_aggregate_rejected(result, failed_companies=2)

    def test_empty_company_list_fulfills_without_host_writes_or_wakes(self):
        result = self.probe(companies=[])
        self.assertEqual(result["outcomes"], [{"status": "fulfilled"}])
        self.assertEqual(result["events"], [
            {"stage": "companies-read"}, {"stage": "settled", "status": "fulfilled"},
        ])
        for field in ("metrics", "activity", "logs"):
            self.assertEqual(result[field], [])
        self.assertEqual(result["states"], {})

    def test_successful_report_only_repeat_keeps_metrics_but_adds_no_writes_or_wakes(self):
        result = self.probe(companies=["healthy"], withIssue=True, repeats=2)
        self.assertEqual(result["outcomes"], [{"status": "fulfilled"}] * 2)
        self.assertEqual(result["logs"], [])
        self.assertEqual(len(result["activity"]), 1)
        self.assertIn("report-only", result["activity"][0]["message"])
        self.assertTrue(any("would have woken" in note
                            for note in result["activity"][0]["metadata"]["notes"]))
        self.assertEqual(result["states"]["healthy"]["pickedIssueIds"], ["issue-healthy"])
        self.assertEqual(result["states"]["healthy"]["counters"]["woken"], 0)
        self.assertEqual(sum(row["stage"] == "state-write" and row["phase"] == "finish"
                             for row in result["events"]), 1)
        self.assertFalse(any(row["stage"] == "wake" for row in result["events"]))
        count = len(result["metrics"])
        self.assertGreater(count, 0)
        self.assertEqual(count % 2, 0)
        self.assertEqual(result["metrics"][:count // 2], result["metrics"][count // 2:])
        self.assertTrue(all(row["tags"]["wakeEnabled"] == "false" for row in result["metrics"]))

    def test_empty_board_is_a_successful_company_sweep(self):
        result = self.probe(companies=["healthy"])
        self.assertEqual(result["outcomes"], [{"status": "fulfilled"}])
        self.assertEqual(result["states"]["healthy"]["pickedIssueIds"], [])
        self.assertTrue(result["metrics"])
        self.assertEqual(len(result["activity"]), 1)
        self.assertFalse(any(row["stage"] == "wake" for row in result["events"]))

    def test_every_fatal_operation_has_safe_stage_diagnostics(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                result = self.probe(
                    companies=["bad", "healthy"], failCompanies=["bad"], failStage=stage,
                    withIssue=True, error={"code": "EIO"},
                )
                self.assert_diagnostic(result, stage, "environment", "EIO")
                self.assertNotIn("bad", result["states"])
                self.assertEqual(result["states"]["healthy"]["pickedIssueIds"], ["issue-healthy"])
                self.assertEqual(result["events"][-2], {
                    "stage": "state-write", "companyId": "healthy", "phase": "finish",
                })
                self.assertFalse(any(row["stage"] == "wake" for row in result["events"]))

    def test_unwrapped_state_comparison_failures_are_safe_and_isolated(self):
        for fault, family, error_code in (
            ("non-iterable", "unknown", "unknown"),
            ("throwing-iterator", "environment", "EIO"),
        ):
            with self.subTest(fault=fault):
                result = self.probe(
                    companies=["bad", "healthy"], failCompanies=["bad"],
                    stateFault=fault, withIssue=True, error={"code": "EIO"},
                )
                self.assert_diagnostic(result, "company-sweep", family, error_code)
                if fault == "throwing-iterator":
                    self.assertIn({"stage": "state-compare", "companyId": "bad"}, result["events"])
                self.assertNotIn("bad", result["states"])
                self.assertEqual(result["states"]["healthy"]["pickedIssueIds"], ["issue-healthy"])
                self.assertEqual([row["companyId"] for row in result["activity"]], ["healthy"])
                self.assertEqual(result["events"][-2], {
                    "stage": "state-write", "companyId": "healthy", "phase": "finish",
                })
                self.assertFalse(any(row["stage"] == "wake" for row in result["events"]))

    def test_known_sdk_policy_denials_are_not_reasoning_or_environment(self):
        # SDK PLUGIN_RPC_ERROR_CODES: capability, invocation scope, tenant config.
        cases = (
            (-32001, "capability-denied"),
            (-32005, "invocation-scope-denied"),
            (-32006, "cross-tenant-config"),
        )
        for code, error_code in cases:
            with self.subTest(code=code):
                result = self.probe(
                    companies=["bad"], failCompanies=["bad"], failStage="config-read",
                    error={"code": code},
                )
                self.assert_diagnostic(result, "config-read", "policy", error_code)

    def test_known_transport_and_filesystem_codes_use_allowlisted_families(self):
        cases = (
            ("transport", -32000, "worker-unavailable"),
            ("transport", -32003, "rpc-timeout"),
            *(("transport", code, code) for code in
              ("ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EPIPE", "ENOTFOUND", "EAI_AGAIN")),
            *(("environment", code, code) for code in
              ("ENOENT", "EACCES", "EPERM", "ENOSPC", "EIO", "ENOTDIR", "EISDIR")),
        )
        for family, code, error_code in cases:
            with self.subTest(family=family, code=code):
                result = self.probe(
                    companies=["bad"], failCompanies=["bad"], failStage="config-read",
                    error={"code": code},
                )
                self.assert_diagnostic(result, "config-read", family, error_code)

    def test_unrecognized_codes_names_messages_and_nested_causes_stay_unknown(self):
        cases = (
            {}, {"type": "TypeError"}, {"type": "primitive"},
            {"code": MARKER}, {"code": "CAPABILITY_DENIED"},
            {"code": "-32001"}, {"code": -32002}, {"code": -32099},
            {"code": None}, {"type": "object-code"},
            {"code": "ECONNRESET "}, {"code": "eio"},
        )
        for error in cases:
            with self.subTest(error=error):
                result = self.probe(
                    companies=["bad"], failCompanies=["bad"], failStage="config-read",
                    error=error,
                )
                self.assert_diagnostic(result, "config-read", "unknown", "unknown")

    def test_throwing_code_getter_cannot_leak_or_abort_remaining_companies(self):
        result = self.probe(
            companies=["bad", "healthy"], failCompanies=["bad"], failStage="config-read",
            error={"type": "throwing-code"}, withIssue=True,
        )
        self.assertIn("healthy", result["states"])
        self.assert_diagnostic(result, "config-read", "unknown", "unknown")

    def test_failure_logs_and_aggregate_never_expose_raw_error_material(self):
        for error in ({}, {"type": "TypeError"}, {"code": MARKER},
                      {"type": "object-code"}, {"type": "throwing-code"}):
            with self.subTest(error=error):
                result = self.probe(
                    companies=["bad"], failCompanies=["bad"], failStage="config-read",
                    config={"privateSetting": MARKER}, error=error,
                )
                failures = self.company_failures(result)
                self.assertEqual(len(failures), 1)
                self.assertNotIn(MARKER, failures[0]["inspected"])
                self.assertNotIn(MARKER, json.dumps(failures[0]["fields"]))
                self.assert_aggregate_rejected(result)

    def test_diagnostic_codes_are_stable_when_private_config_and_error_text_change(self):
        results = []
        for private_config in (MARKER, MARKER + "-changed"):
            results.append(self.probe(
                companies=["bad"], failCompanies=["bad"], failStage="state-read",
                config={"unrecognizedPrivateConfig": private_config},
                error={"code": "EACCES", "detail": private_config},
            ))
        diagnostics = [self.assert_diagnostic(row, "state-read", "environment", "EACCES")
                       for row in results]
        self.assertEqual(diagnostics[0], diagnostics[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
