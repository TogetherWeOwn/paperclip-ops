import { describe, expect, it } from "vitest";

import {
  BRIDGE_MODEL_IDS,
  NO_ENABLED_ROWS,
  adviseBridgeModel,
} from "../src/engine/bridge-models.js";
import {
  rosterWithDisabledOnlyBridgeRows,
  rosterWithEnabledBridgeRow,
  rosterWithoutBridgeRow,
} from "./case_9a8d0afb-bridge-no-enabled-rows-fixtures.js";

/**
 * case_9a8d0afb: router advise-path machine-readable no-enabled-row reason code
 * for bridge models (fixtures only).
 *
 * Parent case_f05a9638, epic case_cc33e13f. Scope: the pure advise-path lookup
 * `adviseBridgeModel` returns `NO_ENABLED_ROWS` + the model id when zero
 * enabled rows exist for a bridge model, instead of a bare fallback the
 * caller must interpret. Advise-mode only: no enforce change, no
 * `selection.mode` change.
 *
 * WHAT THIS PROVES (unit checks over fixtures, no live state):
 * - some-rows: an enabled bridge row resolves to `ok` with exactly that row;
 * - zero-rows: an absent bridge id resolves to `NO_ENABLED_ROWS` + model id;
 * - disabled-only: present-but-all-disabled resolves to `NO_ENABLED_ROWS` +
 *   model id, with `rowsSeen` distinguishing it from absent.
 *
 * NON-GOALS (owned elsewhere, do not duplicate): enforce preflight
 * (case_d5f0e2f5/case_0a9d4e51); repin orphan-drop (case_3097ed32); advise offline smoke
 * (case_36c19c98); agreement probe harness (case_7ab3fa77).
 */

describe("case_9a8d0afb advise-path bridge reason code: enabled rows resolve", () => {
  for (const modelId of BRIDGE_MODEL_IDS) {
    it(`${modelId}: some-rows returns the enabled row`, () => {
      const advice = adviseBridgeModel(rosterWithEnabledBridgeRow(modelId), modelId);
      expect(advice.status).toBe("ok");
      expect(advice.modelId).toBe(modelId);
      if (advice.status !== "ok") return;
      expect(advice.rows).toHaveLength(1);
      expect(advice.rows[0]!.id).toBe(modelId);
      expect(advice.rows[0]!.enabled).toBe(true);
    });
  }
});

describe("case_9a8d0afb advise-path bridge reason code: zero enabled rows", () => {
  for (const modelId of BRIDGE_MODEL_IDS) {
    it(`${modelId}: zero-rows returns NO_ENABLED_ROWS + model id`, () => {
      const advice = adviseBridgeModel(rosterWithoutBridgeRow(modelId), modelId);
      expect(advice.status).toBe("no-enabled-rows");
      if (advice.status !== "no-enabled-rows") return;
      // Machine-readable: exact code, no prose parsing.
      expect(advice.reason).toBe(NO_ENABLED_ROWS);
      expect(advice.reason).toBe("NO_ENABLED_ROWS");
      expect(advice.modelId).toBe(modelId);
      expect(advice.rowsSeen).toBe(0);
    });

    it(`${modelId}: disabled-only returns NO_ENABLED_ROWS + model id`, () => {
      const advice = adviseBridgeModel(rosterWithDisabledOnlyBridgeRows(modelId), modelId);
      expect(advice.status).toBe("no-enabled-rows");
      if (advice.status !== "no-enabled-rows") return;
      expect(advice.reason).toBe(NO_ENABLED_ROWS);
      expect(advice.modelId).toBe(modelId);
      // Present but unservable: rowsSeen names the difference vs absent.
      expect(advice.rowsSeen).toBe(2);
    });
  }

  it("covers every bridge model id exactly once", () => {
    expect([...BRIDGE_MODEL_IDS]).toEqual([
      "muse-spark-1.3-contributor",
      "claude-sonnet-5-5",
      "gpt-6.1-sol",
    ]);
  });
});
