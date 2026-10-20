import test from "node:test";
import assert from "node:assert/strict";
import { recordGemmaPacking, gemmaPackingStatus } from "../src/gemma-packing.js";

const sample = (id, input = 12000, estimate = 10000) => ({
  id, input, output: 0, estimate, overflow: input > 16000,
});

test("correlated combined usage lowers the raw target", () => {
  const state = recordGemmaPacking(null, sample("packet:digest", 18000, 12000));
  assert.equal(state.target, Math.floor(16000 / (1.5 * 1.05)));
  assert.ok(state.target < 12000);
  const same = recordGemmaPacking(state, sample("packet:digest", 18000, 12000));
  assert.deepEqual(same, state);
  assert.equal(gemmaPackingStatus(state).correlated_size_overflows, 1);
});

test("old backlog does not repeatedly shrink; same-size new failures can", () => {
  let state = recordGemmaPacking(null, sample("one", 18000, 12000));
  const target = state.target;
  state = recordGemmaPacking(state, sample("old", 19000, 12000));
  assert.equal(state.target, target);
  state = recordGemmaPacking(state, sample("new", 17000, target));
  assert.ok(state.target < target);
  const decreased = state.target;
  state = recordGemmaPacking(state, sample("success", 9000, 8000));
  assert.equal(state.target, decreased, "success never silently grows the ceiling");
});

test("below five percent retains the target; exactly five percent triggers", () => {
  let state = null;
  for (let i = 0; i < 20; i++) state = recordGemmaPacking(state, sample(`ok${i}`));
  const rare = recordGemmaPacking(state, sample("rare", 17000));
  assert.equal(rare.target, null, "1/21 is below five percent");
  const frequent = recordGemmaPacking(rare, sample("second", 17000));
  assert.ok(frequent.target < 10000);
  let boundary = null;
  for (let i = 0; i < 19; i++) boundary = recordGemmaPacking(boundary, sample(`pass${i}`));
  boundary = recordGemmaPacking(boundary, sample("exact", 17000));
  assert.ok(boundary.target < 10000);
});

test("window bounded, malformed usage ignored, no timer-driven rebound", () => {
  let state = recordGemmaPacking(null, sample("fail", 18000));
  const target = state.target;
  for (let i = 0; i < 200; i++) state = recordGemmaPacking(state, sample(`pass${i}`, 9000, 8000));
  assert.equal(state.samples.length, 128);
  assert.equal(state.target, target);
  assert.deepEqual(recordGemmaPacking(state, sample("bad", Infinity)), state);
  assert.deepEqual(recordGemmaPacking("bad json", sample("bad", NaN)), {
    target: null, samples: [],
  });
});


test("input below 16k plus output above the combined budget is a downward signal", () => {
  const state = recordGemmaPacking(null, {
    id: "combined", input: 14000, output: 4000, estimate: 12000, overflow: true,
  });
  assert.equal(state.target, Math.floor(16000 / (1.5 * 1.05)));
  assert.equal(state.samples[0].input, 14000);
  assert.equal(state.samples[0].output, 4000);
});
