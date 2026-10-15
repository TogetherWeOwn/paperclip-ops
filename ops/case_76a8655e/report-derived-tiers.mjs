#!/usr/bin/env node
/**
 * case_76a8655e tier-delta report.
 *
 * Runs the plugin's OWN `deriveModelTier` (bundled straight out of
 * `src/engine`, not a reimplementation) over the live roster and prints the
 * tier each model would be assigned.
 *
 * The posterior is computed with NO local run evidence, so `p` equals the
 * prior here. In production `refreshScores` folds in each model's recency-
 * weighted run outcomes, which can move a model either way — this report shows
 * the tiering rule's opinion on the benchmark evidence alone, which is the part
 * this card changed.
 *
 * Usage: node ops/case_76a8655e/report-derived-tiers.mjs [roster.json]
 */
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const repoRoot = resolve(new URL("../..", import.meta.url).pathname);
const pluginRoot = join(repoRoot, "plugins/model-selection");
// Defaults to the frozen copy committed beside this script. The case_b73a83f5
// original (`ops/case_b73a83f5/aa-index-refreshed-roster.json`) is untracked, so
// relying on it would make this report unreproducible from a clean checkout.
const rosterPath = process.argv[2] ?? join(repoRoot, "ops/case_76a8655e/roster-input.json");

/** Ascending capability, matching the engine's own `TIER_ORDER`. */
const TIER_RANK = { T3: 0, T2: 1, T1: 2 };
const round2 = (value) => Math.round(value * 100) / 100;

const scratch = await mkdtemp(join(tmpdir(), "case_76a8655e-report-"));
try {
  const entry = join(scratch, "entry.mjs");
  await writeFile(
    entry,
    `export { applyDerivedTiers, deriveModelTier, emptyTierScoreStats } from ${JSON.stringify(join(pluginRoot, "src/engine/scores.ts"))};
     export { FROZEN_BENCHMARK_ROWS, FROZEN_BENCHMARK_CAPTURED_AT } from ${JSON.stringify(join(pluginRoot, "src/engine/benchmark-data.ts"))};
     export { BENCHMARK_SPEC_VERSION } from ${JSON.stringify(join(pluginRoot, "src/engine/benchmark-prior.ts"))};`,
  );
  const bundle = join(scratch, "bundle.mjs");
  // esbuild's bin is a native executable, not a node shim — invoke it directly.
  const build = spawnSync(
    join(pluginRoot, "node_modules/esbuild/bin/esbuild"),
    [
      entry,
      "--bundle",
      "--format=esm",
      "--platform=node",
      `--outfile=${bundle}`,
    ],
    { cwd: pluginRoot, encoding: "utf8" },
  );
  if (build.status !== 0) {
    process.stderr.write(build.stderr);
    process.exit(1);
  }

  const engine = await import(bundle);
  const { models } = JSON.parse(await readFile(rosterPath, "utf8"));

  const rows = models.map((model) => {
    const row = engine.FROZEN_BENCHMARK_ROWS[model.id] ?? null;
    const derived = engine.deriveModelTier(model.aaIndex ?? null, row, engine.emptyTierScoreStats());
    return {
      id: model.id,
      enabled: model.enabled === true,
      configuredTier: model.tier,
      derivedTier: derived.tier,
      moved: derived.tier !== null && derived.tier !== model.tier,
      basis: derived.basis,
      belowT3Floor: derived.belowT3Floor,
      aaIndex: model.aaIndex ?? null,
      prior: derived.prior,
      p: derived.p,
    };
  });

  // `rows` is `deriveModelTier`'s opinion, one verdict per model. What the fleet
  // actually routes on is the OVERLAY, which is row-level and refuses some of
  // those verdicts — an index-only promotion, and a promotion that would vacate
  // a rung a model is separately listed at. Report what lands, not what was
  // proposed: the two differ, and the difference is the live behaviour change.
  const scoresByModelId = {};
  for (const row of rows) {
    scoresByModelId[row.id] = {
      derivedTier: row.derivedTier,
      tierSpecVersion: engine.BENCHMARK_SPEC_VERSION,
      priorBasis: row.basis,
    };
  }
  const overlaid = engine.applyDerivedTiers(models, scoresByModelId);

  /** The reviewer's rung-composition unit: 80/20 input/output blend, $/Mtok. */
  const blendedCost = (model) => round2(0.8 * model.costPerMTokIn + 0.2 * model.costPerMTokOut);
  const label = (model, i) => `${model.id}${model.laneId ? `@${model.laneId}` : ""}`;

  const applied = [];
  const refused = [];
  for (const [i, after] of overlaid.entries()) {
    const before = models[i];
    const verdict = rows[i];
    if (after.tier !== before.tier) {
      applied.push(`${label(before)}: ${before.tier} -> ${after.tier} (p=${verdict.p}, ${verdict.basis})`);
      continue;
    }
    if (verdict.derivedTier !== null && verdict.derivedTier !== before.tier) {
      const promotion = TIER_RANK[verdict.derivedTier] > TIER_RANK[before.tier];
      refused.push(
        `${label(before)}: ${before.tier} -> ${verdict.derivedTier} REFUSED ` +
          `(${!promotion ? "demotion-not-applied" : verdict.basis === "index-only" ? "index-only" : "not-the-top-enabled-rung"})`,
      );
    }
  }

  const composition = (roster) => {
    const out = { T1: [], T2: [], T3: [] };
    for (const model of roster) {
      if (model.enabled !== true) continue;
      out[model.tier].push(`${label(model)} ${blendedCost(model)}`);
    }
    for (const tier of Object.keys(out)) {
      out[tier].sort((a, b) => Number(a.split(" ").pop()) - Number(b.split(" ").pop()));
    }
    return out;
  };

  const enabled = rows.filter((r) => r.enabled);
  const summary = {
    specVersion: engine.BENCHMARK_SPEC_VERSION,
    benchmarksCapturedAt: engine.FROZEN_BENCHMARK_CAPTURED_AT,
    rosterPath,
    totals: {
      models: rows.length,
      enabled: enabled.length,
      blended: rows.filter((r) => r.basis === "blended").length,
      indexOnly: rows.filter((r) => r.basis === "index-only").length,
      unscored: rows.filter((r) => r.basis === "unscored").length,
      moved: rows.filter((r) => r.moved).length,
      movedEnabled: enabled.filter((r) => r.moved).length,
      belowT3Floor: rows.filter((r) => r.belowT3Floor).length,
      appliedRows: applied.length,
      refusedRows: refused.length,
    },
    /** What the overlay actually writes, row by row. The operative list. */
    applied,
    /** Derived verdicts the overlay declined, with the rule that declined them. */
    refused,
    /** Enabled rows per rung, blended $/Mtok, before and after the overlay. */
    rungComposition: { before: composition(models), after: composition(overlaid) },
    movedEnabled: enabled
      .filter((r) => r.moved)
      .map((r) => `${r.id}: ${r.configuredTier} -> ${r.derivedTier} (p=${r.p}, ${r.basis})`),
    unscoredRetained: rows.filter((r) => r.basis === "unscored").map((r) => `${r.id} (keeps ${r.configuredTier})`),
    rows,
  };
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
} finally {
  await rm(scratch, { recursive: true, force: true });
}
