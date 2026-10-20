import test from "node:test";
import assert from "node:assert/strict";
import { recordGemmaPacking, gemmaPackingStatus } from "../src/gemma-packing.js";

const sample = (id, input = 12000, estimate = 12000, output = 0) => ({
  id, input, output, estimate, overflow: input + output > 16000,
});
function seed(count = 19, estimate = 12000) {
  let state = null;
  for (let i = 0; i < count; i++) state = recordGemmaPacking(state, sample(`ok${i}`, estimate, estimate));
  return state;
}

test("a single correlated recovery cannot move a cold target", () => {
  const state = recordGemmaPacking(null, sample("one", 10000, 8000, 31000));
  assert.equal(state.target, null);
  assert.equal(state.fresh, 1);
});

test("combined usage can lower the target after twenty resolutions, bounded to ten percent", () => {
  const state = recordGemmaPacking(seed(), sample("packet:digest", 14000, 12000, 4000));
  assert.ok(state.target <= 11400 && state.target >= 10800);
  assert.equal(state.fresh, 0);
  assert.deepEqual(recordGemmaPacking(state, sample("packet:digest", 14000, 12000, 4000)), state);
  assert.equal(gemmaPackingStatus(state).correlated_size_overflows, 1);
});

test("old backlog and fewer than twenty fresh resolutions cannot repeatedly shrink", () => {
  let state = recordGemmaPacking(seed(), sample("first", 18000));
  const target = state.target;
  for (let i = 0; i < 20; i++) state = recordGemmaPacking(state, sample(`old${i}`, 19000));
  assert.equal(state.target, target);
  assert.equal(state.fresh, 0);
  for (let i = 0; i < 19; i++) state = recordGemmaPacking(state, sample(`new${i}`, target, target));
  assert.equal(state.target, target);
  state = recordGemmaPacking(state, sample("new-fail", 17000, target));
  assert.ok(state.target < target && state.target >= Math.floor(target * 0.9));
});

test("below five percent retains the target; exactly five percent permits a cut", () => {
  const rare = recordGemmaPacking(seed(20), sample("rare", 17000));
  assert.equal(rare.target, null, "1/21 is below five percent");
  const frequent = recordGemmaPacking(rare, sample("second", 17000));
  assert.ok(frequent.target < 12000);
  const boundary = recordGemmaPacking(seed(19), sample("exact", 17000));
  assert.ok(boundary.target < 12000);
});

test("safety floor survives output-heavy replies and successes never grow it", () => {
  let state = { ...seed(19, 4000), target: 4000 };
  state = recordGemmaPacking(state, sample("huge-output", 10000, 4000, 31000));
  assert.equal(state.target, 4000);
  assert.equal(gemmaPackingStatus(state).at_safety_floor, true);
  state = recordGemmaPacking(state, sample("success", 3000, 3000));
  assert.equal(state.target, 4000);
});

test("window bounded; malformed usage ignored; no timer-driven rebound", () => {
  let state = recordGemmaPacking(seed(), sample("fail", 18000));
  const target = state.target;
  for (let i = 0; i < 200; i++) state = recordGemmaPacking(state, sample(`pass${i}`, 8000, 8000));
  assert.equal(state.samples.length, 128);
  assert.equal(state.target, target);
  assert.deepEqual(recordGemmaPacking(state, sample("bad", Infinity)), state);
  assert.deepEqual(recordGemmaPacking("bad json", sample("bad", NaN)), {
    target: null, samples: [], fresh: 0,
  });
});
